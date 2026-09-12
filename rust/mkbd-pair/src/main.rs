//! mkbd-pair — Rust port of this repo's Microsoft Modern Keyboard (Fingerprint
//! ID, model 1780) pairing tools: lib/mkbd_common.py, test/phase4.py,
//! test/optionA-dbus-pair.py, and the optionA/autopair udev/zenity wrapper,
//! folded into one binary. See rust/mkbd-pair/README.md for scope, status,
//! and what is NOT wired in yet.
//!
//! The raw-mgmt pairing engines (test/tk-pair.py's debugfs path,
//! test/optionA-pair.py's MGMT_OP_ADD_REMOTE_OOB_DATA path, and
//! pairmodernkeyboard.sh) were ported here initially but removed after
//! hardware testing: they require stopping/masking bluetoothd for the
//! duration of the pair, and a masked-but-not-restored bluetooth.service
//! from an earlier such run broke Bluetooth entirely until manually
//! unmasked. The D-Bus path (`dbus-pair`) never touches bluetoothd's running
//! state and is hardware-verified working, so it's the only pairing engine
//! left. See git history (this crate's earlier commits) for the removed
//! mgmt.rs/pair.rs if that path is ever needed again.

mod att;
mod autopair;
mod bdaddr;
mod bond;
mod dbus_pair;
mod hid;
mod log;
mod sock;

use att::{AdoptOpts, CCCD_HANDLES, H_FEATURE, H_LED, H_MSACC, H_NOTIFY};
use autopair::AutoOpts;
use clap::{Parser, Subcommand};

/// Format like Python's `%g` for the small set of values this tool prints
/// (hold/timeout seconds) — integral values print without a decimal point.
pub fn fmt_g(v: f64) -> String {
    if v.fract() == 0.0 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

#[derive(Parser)]
#[command(name = "mkbd-pair", about = "Pair the Microsoft Modern Keyboard (1780) natively on Linux")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Standalone bonded GATT provisioning: subscribe CCCDs and hold the link until the
    /// keyboard adopts its new address. Replaces `test/phase4.py`.
    Adopt(AdoptArgs),
    /// Pair the keyboard through bluetoothd's own D-Bus surface (AddRemoteLegacyOOB +
    /// ConnectDevice), bluetoothd never stopped. Replaces `test/optionA-dbus-pair.py`.
    DbusPair(DbusPairArgs),
    /// udev-triggered detect -> prompt -> pair -> confirm -> reconnect-watch flow.
    /// Replaces optionA/autopair/mkbd-optionA-autopair.
    Auto(AutoArgs),
}

#[derive(clap::Args)]
struct AdoptArgs {
    /// keyboard LE static-random address (the F3 addr)
    kbd: String,
    #[arg(long)]
    adapter: Option<String>,
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long, default_value_t = 1)]
    rounds: u32,
    #[arg(long, default_value_t = 8.0)]
    hold: f64,
    #[arg(long)]
    discover: bool,
    #[arg(long)]
    writes: bool,
    #[arg(long = "read-msacc")]
    read_msacc: bool,
    #[arg(long = "f3-check")]
    f3_check: bool,
    #[arg(long, value_delimiter = ',')]
    cccd: Vec<String>,
    #[arg(long, default_value = "0x0038")]
    led: String,
    #[arg(long, default_value = "0x0041")]
    feature: String,
    #[arg(long, default_value = "0x0024")]
    notify: String,
    #[arg(long = "led-writes", default_value_t = 3)]
    led_writes: u32,
    #[arg(long, value_delimiter = ',')]
    msacc: Vec<String>,
}

#[derive(clap::Args)]
struct DbusPairArgs {
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long)]
    adapter: Option<String>,
    #[arg(long = "tk-order", value_parser = ["as-is", "reversed"], default_value = "as-is")]
    tk_order: String,
    #[arg(long = "connect-timeout", default_value_t = 20.0)]
    connect_timeout: f64,
    #[arg(long = "pair-timeout", default_value_t = 30.0)]
    pair_timeout: f64,
}

#[derive(clap::Args)]
struct AutoArgs {
    #[arg(long, default_value = "hci0")]
    hci: String,
    #[arg(long = "reconnect-timeout", default_value_t = 90)]
    reconnect_timeout: u64,
}

fn parse_int(s: &str) -> Result<u16, String> {
    let s = s.trim();
    if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        u16::from_str_radix(hex, 16).map_err(|e| e.to_string())
    } else {
        s.parse().map_err(|e: std::num::ParseIntError| e.to_string())
    }
}

fn main() {
    let cli = Cli::parse();
    let code = match cli.cmd {
        Cmd::Adopt(a) => run_adopt_cmd(a),
        Cmd::DbusPair(a) => run_dbus_pair_cmd(a),
        Cmd::Auto(a) => run_auto_cmd(a),
    };
    std::process::exit(code);
}

fn run_adopt_cmd(a: AdoptArgs) -> i32 {
    let adapter = match a.adapter.or_else(|| bond::adapter_bdaddr(&a.hci).ok()) {
        Some(x) => x.to_uppercase(),
        None => {
            eprintln!("error: could not determine adapter bdaddr");
            return 1;
        }
    };
    let parse_list = |v: &[String], default: &[u16]| -> Vec<u16> {
        if v.is_empty() {
            default.to_vec()
        } else {
            v.iter().filter_map(|s| parse_int(s).ok()).collect()
        }
    };
    let opts = AdoptOpts {
        adapter,
        kbd: a.kbd.to_uppercase(),
        rounds: a.rounds,
        hold: a.hold,
        discover: a.discover,
        writes: a.writes,
        read_msacc: a.read_msacc,
        f3_check: a.f3_check,
        cccd: parse_list(&a.cccd, &CCCD_HANDLES),
        led: parse_int(&a.led).unwrap_or(H_LED),
        feature: parse_int(&a.feature).unwrap_or(H_FEATURE),
        notify: parse_int(&a.notify).unwrap_or(H_NOTIFY),
        led_writes: a.led_writes,
        msacc: parse_list(&a.msacc, &H_MSACC),
    };
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("error: run as root");
        return 1;
    }
    let result = att::run_adopt(&opts);
    println!();
    println!("{}/{} CCCDs subscribed", result.cccd_ok, result.cccd_total);
    match result.adopted {
        Some(true) => println!("address ADOPTED"),
        Some(false) => println!("address NOT adopted (retry, or --rounds 2)"),
        None => {}
    }
    0
}

fn run_dbus_pair_cmd(a: DbusPairArgs) -> i32 {
    let opts = dbus_pair::DbusPairOpts {
        hci: a.hci,
        adapter: a.adapter,
        tk_reversed: a.tk_order == "reversed",
        connect_timeout: std::time::Duration::from_secs_f64(a.connect_timeout),
        pair_timeout: std::time::Duration::from_secs_f64(a.pair_timeout),
    };
    match dbus_pair::run_dbus_pair(&opts) {
        Ok(o) => {
            println!();
            println!("Modern Keyboard  ·  {}", o.addr);
            println!(
                "paired via D-Bus (Paired={} Bonded={} Connected={}), bluetoothd never stopped",
                o.paired, o.bonded, o.connected
            );
            0
        }
        Err(e) => {
            eprintln!("error: {e}");
            1
        }
    }
}

fn run_auto_cmd(a: AutoArgs) -> i32 {
    let opts = AutoOpts {
        hci: a.hci,
        reconnect_timeout: a.reconnect_timeout,
    };
    match autopair::run_auto(&opts) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("error: {e}");
            1
        }
    }
}
