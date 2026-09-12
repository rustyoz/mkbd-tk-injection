//! The pairing engine shared by test/tk-pair.py (debugfs TK injection, a
//! throwaway PoC with no BlueZ/UAPI involved) and test/optionA-pair.py
//! (Option A, the real MGMT_OP_ADD_REMOTE_OOB_DATA extended payload) — the
//! two differ only in how the one-time F3 TK gets into the kernel; everything
//! else (USB exchange, bluetoothd masking, MGMT Pair Device, bond-file
//! writing, chaining into address adoption) is identical, so this module
//! parameterizes over `Engine` instead of duplicating it.

use crate::att::{self, AdoptOpts, AdoptResult};
use crate::bond;
use crate::hid;
use crate::mgmt;
use std::fs;
use std::process::Command;
use std::time::Duration;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Engine {
    /// test/tk-pair.py — debugfs knob, throwaway, no BlueZ/UAPI involved.
    DebugfsTk,
    /// test/optionA-pair.py — the real MGMT opcode (len-88 payload).
    OptionA,
}

pub struct PairOpts {
    pub engine: Engine,
    pub hci: String,
    pub adapter: Option<String>,
    pub tk_reversed: bool,
    pub addr_type: u8, // 0 = public, 1 = LE random (default; this keyboard)
    pub timeout: u64,
    pub diag: bool,
    pub no_mask: bool,
    pub no_adopt: bool,
    pub adopt_rounds: u32,
}

impl Default for PairOpts {
    fn default() -> Self {
        Self {
            engine: Engine::DebugfsTk,
            hci: "hci0".to_string(),
            adapter: None,
            tk_reversed: false,
            addr_type: 1,
            timeout: 30,
            diag: false,
            no_mask: false,
            no_adopt: false,
            adopt_rounds: 1,
        }
    }
}

pub struct PairOutcome {
    pub addr: String,
    pub authenticated: bool,
    /// Exposed for programmatic callers; the CLI summary reports only
    /// pass/fail + address.
    #[allow(dead_code)]
    pub ltk: mgmt::Ltk,
    #[allow(dead_code)]
    pub irk: Option<mgmt::Irk>,
    pub adopt: Option<AdoptResult>,
}

fn hci_index_of(hci: &str) -> u16 {
    hci.chars()
        .filter(|c| c.is_ascii_digit())
        .collect::<String>()
        .parse()
        .unwrap_or(0)
}

fn run(cmd: &str, args: &[&str]) {
    let _ = Command::new(cmd).args(args).status();
}

fn run_capture(cmd: &str, args: &[&str]) -> String {
    Command::new(cmd)
        .args(args)
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default()
}

pub fn run_pair(opts: &PairOpts) -> Result<PairOutcome, String> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("run as root".to_string());
    }

    let hci_index = hci_index_of(&opts.hci);

    if opts.engine == Engine::DebugfsTk {
        let dbg = format!("/sys/kernel/debug/bluetooth/{}/le_legacy_oob_tk", opts.hci);
        if !std::path::Path::new(&dbg).exists() {
            return Err(format!(
                "{dbg} missing — patched bluetooth.ko not loaded.\n  \
                 sudo test/install-module.sh ; reboot ; retry\n  \
                 (or pass --option-a, if you installed the Option A module instead)"
            ));
        }
    }

    let adapter = match &opts.adapter {
        Some(a) => a.to_uppercase(),
        None => bond::adapter_bdaddr(&opts.hci)?.to_uppercase(),
    };
    println!(":: adapter       : {adapter}");

    let devs = hid::find_hidraw(hid::VID, hid::PID);
    let dev = devs
        .iter()
        .find(|d| d.iface == hid::VENDOR_IFACE as i64)
        .ok_or_else(|| {
            "no vendor hidraw for 045e:0815 interface 0 — plug in the keyboard over USB and switch it on"
                .to_string()
        })?;
    println!(":: vendor hidraw : {}", dev.node);

    println!(":: USB F1/F2/F3 ...");
    let fd = hid::open_hidraw(hid::VENDOR_IFACE as i64, hid::VID, hid::PID)
        .map_err(|e| format!("opening {}: {e}", dev.node))?;
    let res = hid::vendor_pairing_exchange(&fd, &adapter)?;
    drop(fd);

    let addr = res.new_addr.clone();
    println!("   bond already on keyboard : {}", res.bond_exists);
    println!("   keyboard addr (was)      : {}", res.current_addr);
    println!("   keyboard addr (new bond) : {addr}");
    println!("   one-time F3 TK           : {}", hex_upper(&res.tk));
    println!("   device name              : {:?}", res.name);

    let snoop = format!(
        "/tmp/mkbd-{}-{}.btsnoop",
        if opts.engine == Engine::OptionA { "optionA" } else { "tkinj" },
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
    );

    if !opts.no_mask {
        run("systemctl", &["mask", "--now", "bluetooth"]);
        run("systemctl", &["stop", "bluetooth"]);
    } else {
        println!(":: --no-mask: leaving bluetooth.service running");
    }

    let mut btmon: Option<std::process::Child> = None;
    let outcome = pair_body(opts, &adapter, &addr, &res, hci_index, &snoop, &mut btmon);

    if let Some(mut child) = btmon.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    if !opts.no_mask {
        run("systemctl", &["unmask", "bluetooth"]);
        run("systemctl", &["start", "bluetooth"]);
    }
    if opts.diag {
        dump_diag(&snoop);
        println!(":: full capture  : sudo btmon -r {snoop}");
    }

    match outcome {
        Ok(o) => {
            std::thread::sleep(Duration::from_secs(2));
            run("bluetoothctl", &["trust", &o.addr]);
            println!();
            println!("Next: unplug USB, power-cycle the keyboard —");
            println!(
                "  bluetoothctl connect {}   (or just wait; it is Trusted and reconnects on its own)",
                o.addr
            );
            Ok(o)
        }
        Err(e) => Err(e),
    }
}

fn pair_body(
    opts: &PairOpts,
    adapter: &str,
    addr: &str,
    res: &hid::VendorExchange,
    hci_index: u16,
    snoop: &str,
    btmon: &mut Option<std::process::Child>,
) -> Result<PairOutcome, String> {
    mgmt::mgmt_set_powered(true, hci_index);

    // Clear any stale bond: MGMT Pair Device returns 0x13 (Already Paired)
    // if the kernel already has an LTK for this identity, and never re-runs
    // SMP.
    if opts.no_mask {
        let _ = Command::new("bluetoothctl")
            .args(["remove", addr])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
    let bond_dir = format!("/var/lib/bluetooth/{adapter}/{addr}");
    if std::path::Path::new(&bond_dir).is_dir() {
        let _ = fs::remove_dir_all(&bond_dir);
        println!(":: removed stale bond dir {bond_dir}");
    }
    match mgmt::mgmt_unpair_device(addr, mgmt::MGMT_ADDR_LE_RANDOM, hci_index) {
        Ok(()) => println!(":: MGMT Unpair Device — cleared kernel bond"),
        Err(_) => println!(":: MGMT Unpair Device — no existing kernel bond (fine)"),
    }

    if opts.diag {
        let child = Command::new("btmon")
            .args(["-w", snoop])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
        if let Ok(c) = child {
            *btmon = Some(c);
        }
        std::thread::sleep(Duration::from_millis(500));
    }

    let mut tk = res.tk;
    if opts.tk_reversed {
        tk.reverse();
    }

    match opts.engine {
        Engine::DebugfsTk => {
            let dbg = format!("/sys/kernel/debug/bluetooth/{}/le_legacy_oob_tk", opts.hci);
            let line = format!("{addr} {} {}", opts.addr_type, hex_lower(&tk));
            fs::write(&dbg, &line).map_err(|e| format!("writing {dbg}: {e}"))?;
            println!(
                ":: injected TK   : {line}\n              -> {dbg}  ({})",
                if opts.tk_reversed { "reversed" } else { "as-is" }
            );
        }
        Engine::OptionA => {
            let addr_type_mgmt = if opts.addr_type == 1 {
                mgmt::MGMT_ADDR_LE_RANDOM
            } else {
                mgmt::MGMT_ADDR_LE_PUBLIC
            };
            println!(
                ":: MGMT Add Remote OOB Data -> {addr} (len 88, flags=LE_LEGACY_TK_PRESENT, tk={}) ...",
                hex_lower(&tk)
            );
            mgmt::mgmt_add_remote_oob_le_legacy_tk(addr, addr_type_mgmt, &tk, hci_index).map_err(|e| {
                format!(
                    "MGMT Add Remote OOB Data (len 88) was rejected — the running kernel does not \
                     have optionA/0001-0004 applied.\n  sudo test/install-optionA-module.sh   then reboot\n  ({e})"
                )
            })?;
            println!("   stored — kernel now has the TK for this identity");
        }
    }

    println!(":: MGMT Pair Device -> {addr} (LE random, NoInputNoOutput) ...");
    let addr_type_mgmt = if opts.addr_type == 1 {
        mgmt::MGMT_ADDR_LE_RANDOM
    } else {
        mgmt::MGMT_ADDR_LE_PUBLIC
    };
    let pr = mgmt::mgmt_pair_device(
        addr,
        addr_type_mgmt,
        0x03,
        hci_index,
        Duration::from_secs(opts.timeout),
        true,
    )?;
    println!("   pair status : {}   events: {:?}", pr.status_name, pr.events);
    println!(
        "   connected={} smp_seen={} disc_reason={:#x}",
        pr.connected,
        pr.smp_seen,
        pr.disc_reason.unwrap_or(0)
    );

    if pr.status == Some(0) {
        if let Some(ltk) = pr.ltk.clone() {
            let auth = matches!(ltk.key_type, 1 | 3);
            let name = if res.name.is_empty() { "Modern Keyboard" } else { &res.name };
            let path = bond::write_le_device_info(
                adapter,
                addr,
                &ltk.val,
                ltk.ediv,
                ltk.rand,
                pr.irk.as_ref().map(|i| i.val.as_str()),
                if auth { 1 } else { 0 },
                if ltk.enc_size == 0 { 16 } else { ltk.enc_size },
                name,
                "static",
            )?;
            println!();
            println!("OK  bond written: {path}");
            println!(
                "    LTK={} EDIV={} Rand={} key_type={} enc_size={}{}",
                ltk.val,
                ltk.ediv,
                ltk.rand,
                ltk.key_type,
                ltk.enc_size,
                pr.irk.as_ref().map(|i| format!(" IRK={}", i.val)).unwrap_or(" (no IRK)".to_string())
            );
            let engine_label = if opts.engine == Engine::OptionA {
                "MGMT_OP_ADD_REMOTE_OOB_DATA"
            } else {
                "debugfs knob"
            };
            println!(
                "    ==> {} LE bond via in-kernel legacy OOB SMP ({engine_label})  {}",
                if auth { "AUTHENTICATED" } else { "UNAUTHENTICATED" },
                if auth {
                    "*** WORKS ***".to_string()
                } else {
                    "(key_type not 1/3 — check)".to_string()
                }
            );

            let adopt = if !opts.no_adopt {
                println!();
                println!("=== address adoption: bonded GATT provisioning {}", "=".repeat(28));
                let adopt_opts = AdoptOpts {
                    adapter: adapter.to_string(),
                    kbd: addr.to_string(),
                    rounds: opts.adopt_rounds,
                    f3_check: true,
                    ..Default::default()
                };
                Some(att::run_adopt(&adopt_opts))
            } else {
                None
            };

            return Ok(PairOutcome {
                addr: addr.to_string(),
                authenticated: auth,
                ltk,
                irk: pr.irk,
                adopt,
            });
        }
    }

    println!();
    println!("FAIL: no LTK distributed.");
    println!("  status 0x04 (after our Confirm)  -> wrong TK byte order: re-run with --tk-order reversed");
    println!("  hung up, no Pairing Response      -> our Pairing Request had OOB flag = 0.  check: dmesg | grep -i 'legacy oob'");
    println!("  status 0x03 (Auth Requirements)   -> get_auth_method did not return REQ_OOB (address/type mismatch with the stored entry?)");
    Err(format!("pairing failed: {}", pr.status_name))
}

fn dump_diag(snoop: &str) {
    println!();
    println!("=== btmon (filtered) {}", "=".repeat(45));
    let out = run_capture("btmon", &["-r", snoop]);
    let keep = [
        "LE Create Connection", "LE Enhanced Create Connection", "LE Connection Complete",
        "LE Extended Advertising Report", "Advertising Report", "Connect Complete", "Disconnect",
        "Reason:", "SMP:", "Pairing Request", "Pairing Response", "Pairing Confirm",
        "Pairing Random", "Pairing Failed", "Identity", "Encryption Information",
        "Central Identification", "Long Term Key", "Encryption Change", "Encrypt Change", "OOB",
        "AuthReq", "Authentication Req", "IO Capability", "Key Distribution", "Bonding",
        "Add Remote OOB Data",
    ];
    let mut ctx = 0;
    for ln in out.lines() {
        let s = ln.trim();
        if keep.iter().any(|k| ln.contains(k)) {
            println!("  {}", ln.trim_end());
            ctx = 2;
        } else if ctx > 0
            && (s.starts_with("Handle:") || s.starts_with("Status:") || s.starts_with("Address:")
                || s.starts_with("Reason:") || s.starts_with("Method:") || s.starts_with("Key size:")
                || s.starts_with("Random:") || s.starts_with("Confirm:") || s.starts_with("Flags:"))
        {
            println!("  {}", ln.trim_end());
            ctx -= 1;
        } else {
            ctx = 0;
        }
    }
    if out.is_empty() {
        println!("  (empty capture)");
    }
    println!("=== dmesg (bluetooth/smp) {}", "=".repeat(40));
    let dm = run_capture("dmesg", &[]);
    let tail: Vec<&str> = dm
        .lines()
        .filter(|l| {
            let ll = l.to_lowercase();
            ["bluetooth", "smp", "legacy oob", "hci0", "l2cap"]
                .iter()
                .any(|k| ll.contains(k))
        })
        .collect();
    let tail = &tail[tail.len().saturating_sub(25)..];
    if tail.is_empty() {
        println!("  (nothing)");
    } else {
        for l in tail {
            println!("  {l}");
        }
    }
    println!("{}", "=".repeat(66));
}

fn hex_upper(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02X}")).collect()
}
fn hex_lower(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}
