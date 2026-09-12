//! Address adoption: bonded GATT provisioning over a raw L2CAP ATT socket
//! (CID 4) — no bluetoothd, no controller seizure. Port of test/phase4.py.
//!
//! Run AFTER a pair (the kernel holds the LTK). The minimal sequence that
//! makes the keyboard adopt its F3 address:
//!   1. connect L2CAP ATT, encrypted with the stored LTK (BT_SECURITY_HIGH)
//!   2. Exchange MTU
//!   3. subscribe the 9 report/vendor CCCDs (Write Req 01 00)
//!   4. hold until the keyboard tears the link down (~7s while USB attached)

use crate::bdaddr::bdaddr_to_bytes;
use crate::hid;
use crate::sock::{RawSock, BDADDR_BREDR, BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM};
use std::time::{Duration, Instant};

const ATT_ERROR_RSP: u8 = 0x01;
const ATT_EXCHANGE_MTU_REQ: u8 = 0x02;
const ATT_EXCHANGE_MTU_RSP: u8 = 0x03;
const ATT_READ_BY_TYPE_REQ: u8 = 0x08;
const ATT_READ_BY_TYPE_RSP: u8 = 0x09;
const ATT_READ_REQ: u8 = 0x0A;
const ATT_READ_RSP: u8 = 0x0B;
const ATT_READ_BY_GROUP_REQ: u8 = 0x10;
const ATT_READ_BY_GROUP_RSP: u8 = 0x11;
const ATT_WRITE_REQ: u8 = 0x12;
const ATT_WRITE_RSP: u8 = 0x13;
const ATT_HANDLE_VALUE_NTF: u8 = 0x1B;
const ATT_WRITE_CMD: u8 = 0x52;

const UUID_PRIMARY_SERVICE: u16 = 0x2800;
const UUID_CHARACTERISTIC: u16 = 0x2803;

/// Keyboard's GATT DB (adapter-independent; matches BOND-COMPLETION.md,
/// verified on hardware). Battery CCCD 0x0017 + 7 HID-report CCCDs + vendor
/// CCCD 0x000c.
pub const CCCD_HANDLES: [u16; 9] = [
    0x0017, 0x001d, 0x0021, 0x0025, 0x0029, 0x002d, 0x0031, 0x0035, 0x000c,
];
pub const H_LED: u16 = 0x0038;
pub const H_FEATURE: u16 = 0x0041;
pub const H_NOTIFY: u16 = 0x0024;
pub const H_MSACC: [u16; 2] = [0x000e, 0x000b];

pub struct Att {
    sock: RawSock,
    pub mtu: u16,
    pub ntf: Vec<(u16, Vec<u8>)>,
}

#[derive(Debug)]
pub struct AttError(pub String);
impl std::fmt::Display for AttError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl From<std::io::Error> for AttError {
    fn from(e: std::io::Error) -> Self {
        AttError(e.to_string())
    }
}

type AResult<T> = Result<T, AttError>;

impl Att {
    pub fn new(sock: RawSock) -> Self {
        Self {
            sock,
            mtu: 23,
            ntf: Vec::new(),
        }
    }

    /// Set the recv timeout used by the next blocking read(s) — mirrors
    /// Python's `socket.settimeout()` calls scattered through phase4.py.
    pub fn set_timeout(&self, secs: f64) {
        let _ = self.sock.set_recv_timeout(Some(Duration::from_secs_f64(secs)));
    }

    fn recv_pkt(&self) -> AResult<Vec<u8>> {
        let mut buf = [0u8; 512];
        let n = self.sock.recv(&mut buf)?;
        if n == 0 {
            return Err(AttError("ATT link closed".to_string()));
        }
        Ok(buf[..n].to_vec())
    }

    fn txn(&mut self, req: &[u8], want_op: u8) -> AResult<Vec<u8>> {
        self.sock.send(req)?;
        loop {
            let pkt = self.recv_pkt()?;
            let op = pkt[0];
            if op == ATT_HANDLE_VALUE_NTF {
                let h = u16::from_le_bytes([pkt[1], pkt[2]]);
                self.ntf.push((h, pkt[3..].to_vec()));
                continue;
            }
            if op == ATT_ERROR_RSP && pkt.len() >= 5 {
                let ro = pkt[1];
                let h = u16::from_le_bytes([pkt[2], pkt[3]]);
                let ec = pkt[4];
                return Err(AttError(format!(
                    "ATT error req 0x{ro:02x} handle 0x{h:04x} code 0x{ec:02x}"
                )));
            }
            if op == want_op {
                return Ok(pkt);
            }
            println!("   (att: unexpected op 0x{op:02x})");
        }
    }

    pub fn exchange_mtu(&mut self, mtu: u16) -> AResult<u16> {
        let mut req = vec![ATT_EXCHANGE_MTU_REQ];
        req.extend_from_slice(&mtu.to_le_bytes());
        let rsp = self.txn(&req, ATT_EXCHANGE_MTU_RSP)?;
        let peer_mtu = u16::from_le_bytes([rsp[1], rsp[2]]);
        self.mtu = mtu.min(peer_mtu);
        Ok(self.mtu)
    }

    pub fn read(&mut self, h: u16) -> AResult<Vec<u8>> {
        let mut req = vec![ATT_READ_REQ];
        req.extend_from_slice(&h.to_le_bytes());
        Ok(self.txn(&req, ATT_READ_RSP)?[1..].to_vec())
    }

    pub fn write_req(&mut self, h: u16, v: &[u8]) -> AResult<()> {
        let mut req = vec![ATT_WRITE_REQ];
        req.extend_from_slice(&h.to_le_bytes());
        req.extend_from_slice(v);
        self.txn(&req, ATT_WRITE_RSP)?;
        Ok(())
    }

    pub fn write_cmd(&self, h: u16, v: &[u8]) -> AResult<()> {
        let mut req = vec![ATT_WRITE_CMD];
        req.extend_from_slice(&h.to_le_bytes());
        req.extend_from_slice(v);
        self.sock.send(&req)?;
        Ok(())
    }

    fn sweep(&mut self, op_req: u8, op_rsp: u8, extra: &[u8]) -> Vec<Vec<u8>> {
        let mut start: u32 = 0x0001;
        let mut out = Vec::new();
        while start <= 0xFFFF {
            let mut req = vec![op_req];
            req.extend_from_slice(&(start as u16).to_le_bytes());
            req.extend_from_slice(&0xFFFFu16.to_le_bytes());
            req.extend_from_slice(extra);
            let rsp = match self.txn(&req, op_rsp) {
                Ok(r) => r,
                Err(_) => break,
            };
            if rsp.len() < 2 {
                break;
            }
            let ln = rsp[1] as usize;
            let body = &rsp[2..];
            if body.is_empty() || ln == 0 {
                break;
            }
            let mut last = start as u16;
            for chunk in body.chunks(ln) {
                if chunk.len() < ln {
                    break;
                }
                out.push(chunk.to_vec());
                last = u16::from_le_bytes([chunk[0], chunk[1]]);
            }
            start = last as u32 + 1;
        }
        out
    }

    pub fn discover(&mut self) -> (Vec<(u16, u16, Vec<u8>)>, Vec<(u16, u8, u16, Vec<u8>)>) {
        let svc_entries = self.sweep(
            ATT_READ_BY_GROUP_REQ,
            ATT_READ_BY_GROUP_RSP,
            &UUID_PRIMARY_SERVICE.to_le_bytes(),
        );
        let svcs = svc_entries
            .into_iter()
            .map(|e| {
                (
                    u16::from_le_bytes([e[0], e[1]]),
                    u16::from_le_bytes([e[2], e[3]]),
                    e[4..].to_vec(),
                )
            })
            .collect();

        let char_entries = self.sweep(
            ATT_READ_BY_TYPE_REQ,
            ATT_READ_BY_TYPE_RSP,
            &UUID_CHARACTERISTIC.to_le_bytes(),
        );
        let chars = char_entries
            .into_iter()
            .map(|e| {
                (
                    u16::from_le_bytes([e[0], e[1]]),
                    e[2],
                    u16::from_le_bytes([e[3], e[4]]),
                    e[5..].to_vec(),
                )
            })
            .collect();
        (svcs, chars)
    }
}

pub fn uuid_str(u: &[u8]) -> String {
    if u.len() == 2 {
        format!("{:04x}", u16::from_le_bytes([u[0], u[1]]))
    } else {
        u.iter().rev().map(|b| format!("{b:02x}")).collect()
    }
}

pub fn connect_att(adapter: &str, kbd: &str, timeout: Duration) -> Result<Att, String> {
    let local = bdaddr_to_bytes(adapter, true)?;
    let remote = bdaddr_to_bytes(kbd, true)?;
    let mut last_err = String::from("no address type worked");
    for local_type in [BDADDR_LE_PUBLIC, BDADDR_BREDR] {
        match RawSock::l2cap_att_connect(local, local_type, remote, BDADDR_LE_RANDOM, timeout) {
            Ok(s) => {
                let att = Att::new(s);
                att.set_timeout(8.0);
                return Ok(att);
            }
            Err(e) => last_err = e.to_string(),
        }
    }
    Err(last_err)
}

pub fn subscribe_cccds(att: &mut Att, handles: &[u16]) -> usize {
    let mut ok = 0;
    for &h in handles {
        match att.write_req(h, &[0x01, 0x00]) {
            Ok(()) => ok += 1,
            Err(e) => {
                println!("   CCCD 0x{h:04x}: {e}");
                return ok;
            }
        }
    }
    println!("   subscribed {ok}/{}  CCCDs", handles.len());
    ok
}

pub struct WriteOpts {
    pub led: u16,
    pub feature: u16,
    pub notify: u16,
    pub led_writes: u32,
}

pub fn do_writes(att: &mut Att, token: u32, opts: &WriteOpts) {
    for _ in 0..opts.led_writes {
        let _ = att.write_cmd(opts.led, &[0x01]);
        std::thread::sleep(Duration::from_millis(30));
    }
    println!("   LED 0x{:04x} <- 01 x{}", opts.led, opts.led_writes);

    let mut body = vec![0xE2u8, 0x06];
    body.extend_from_slice(&token.to_le_bytes());
    body.extend_from_slice(&[0u8; 13]);
    if let Err(e) = att.write_req(opts.feature, &body) {
        println!("   writes dropped: {e}");
        return;
    }
    println!(
        "   Feature 0x{:04x} <- {}",
        opts.feature,
        body.iter().map(|b| format!("{b:02x}")).collect::<String>()
    );

    let end = Instant::now() + Duration::from_secs(2);
    while Instant::now() < end
        && !att.ntf.iter().any(|(h, _)| *h == H_NOTIFY || *h == opts.notify)
    {
        att.set_timeout(0.4);
        match att.recv_pkt() {
            Ok(p) if !p.is_empty() && p[0] == ATT_HANDLE_VALUE_NTF => {
                let h = u16::from_le_bytes([p[1], p[2]]);
                att.ntf.push((h, p[3..].to_vec()));
            }
            _ => {}
        }
    }
    for (h, v) in &att.ntf {
        println!("   NTF 0x{h:04x}: {}", v.iter().map(|b| format!("{b:02x}")).collect::<String>());
    }
}

pub fn f1_adoption(adapter: &str, kbd: &str) -> Option<bool> {
    println!("\n=== address adoption (USB F1 re-read) ===");
    let fd = match hid::open_hidraw(hid::VENDOR_IFACE as i64, hid::VID, hid::PID) {
        Ok(f) => f,
        Err(e) => {
            println!("   F1 re-read error: {e}");
            return None;
        }
    };
    let host = match bdaddr_to_bytes(adapter, true) {
        Ok(h) => h,
        Err(e) => {
            println!("   F1 re-read error: {e}");
            return None;
        }
    };
    match hid::col03_command(&fd, 0xF1, &host, Duration::from_secs(2)) {
        Ok((0, d)) if d.len() >= 7 => {
            let cur = crate::bdaddr::bytes_to_bdaddr(&d[1..7], true);
            println!("   bond_exists={}  current_addr={cur}", d[0] != 0);
            let adopted = cur.eq_ignore_ascii_case(kbd);
            if adopted {
                println!("   ==> ADOPTED");
            } else {
                println!("   ==> NOT adopted (want {kbd})");
            }
            Some(adopted)
        }
        Ok((st, _)) => {
            println!("   F1 status 0x{st:02x}");
            None
        }
        Err(e) => {
            println!("   F1 re-read error: {e}");
            None
        }
    }
}

pub struct AdoptOpts {
    pub adapter: String,
    pub kbd: String,
    pub rounds: u32,
    pub hold: f64,
    pub discover: bool,
    pub writes: bool,
    pub read_msacc: bool,
    pub f3_check: bool,
    pub cccd: Vec<u16>,
    pub led: u16,
    pub feature: u16,
    pub notify: u16,
    pub led_writes: u32,
    pub msacc: Vec<u16>,
}

impl Default for AdoptOpts {
    fn default() -> Self {
        Self {
            adapter: String::new(),
            kbd: String::new(),
            rounds: 1,
            hold: 8.0,
            discover: false,
            writes: false,
            read_msacc: false,
            f3_check: false,
            cccd: CCCD_HANDLES.to_vec(),
            led: H_LED,
            feature: H_FEATURE,
            notify: H_NOTIFY,
            led_writes: 3,
            msacc: H_MSACC.to_vec(),
        }
    }
}

#[derive(Debug, Default)]
pub struct AdoptResult {
    pub cccd_ok: usize,
    pub cccd_total: usize,
    pub adopted: Option<bool>,
}

/// The phase4.py `main()` body, minus argparse: connect/subscribe/hold for
/// `opts.rounds` rounds, then optionally re-check address adoption via F1.
pub fn run_adopt(opts: &AdoptOpts) -> AdoptResult {
    let mut result = AdoptResult {
        cccd_total: opts.cccd.len(),
        ..Default::default()
    };
    let kbd = opts.kbd.to_uppercase();
    println!(
        ":: adopt  adapter {}  keyboard {kbd}  rounds={} hold={}s",
        opts.adapter,
        opts.rounds,
        crate::fmt_g(opts.hold)
    );
    let mut token: u32 = 0x0000_0101;
    for rnd in 1..=opts.rounds {
        let tag = if opts.rounds > 1 {
            format!("round {rnd}/{}", opts.rounds)
        } else {
            "connect".to_string()
        };
        let mut att = match connect_att(&opts.adapter, &kbd, Duration::from_secs(15)) {
            Ok(a) => a,
            Err(e) => {
                println!(":: {tag}: L2CAP connect failed: {e}");
                std::thread::sleep(Duration::from_secs(3));
                continue;
            }
        };
        let t0 = Instant::now();
        let hold_result = (|| -> AResult<()> {
            att.exchange_mtu(517)?;
            if opts.discover && rnd == 1 {
                let (svcs, chars) = att.discover();
                println!(":: GATT DB — {} services, {} chars", svcs.len(), chars.len());
                for (h, e, u) in &svcs {
                    println!("   svc 0x{h:04x}-0x{e:04x}  {}", uuid_str(u));
                }
                for (dh, pr, vh, u) in &chars {
                    println!("   chr 0x{dh:04x} props 0x{pr:02x} val 0x{vh:04x}  {}", uuid_str(u));
                }
            }
            result.cccd_ok = subscribe_cccds(&mut att, &opts.cccd);
            if opts.writes {
                let wo = WriteOpts {
                    led: opts.led,
                    feature: opts.feature,
                    notify: opts.notify,
                    led_writes: opts.led_writes,
                };
                do_writes(&mut att, token, &wo);
                token = token.wrapping_add(0x0001_0000);
            }
            if opts.read_msacc {
                for &h in &opts.msacc {
                    match att.read(h) {
                        Ok(v) => println!("   msacc 0x{h:04x} -> {}", v.iter().map(|b| format!("{b:02x}")).collect::<String>()),
                        Err(e) => println!("   msacc 0x{h:04x}: {e}"),
                    }
                }
            }
            // hold until the keyboard drops the link (its own ~7s teardown
            // is what commits the bond)
            while t0.elapsed().as_secs_f64() < opts.hold {
                att.set_timeout(1.0);
                match att.recv_pkt() {
                    Ok(p) if !p.is_empty() && p[0] == ATT_HANDLE_VALUE_NTF => {
                        let h = u16::from_le_bytes([p[1], p[2]]);
                        println!(
                            "   NTF 0x{h:04x}: {}",
                            p[3..].iter().map(|b| format!("{b:02x}")).collect::<String>()
                        );
                    }
                    Ok(_) => {}
                    Err(_) => break,
                }
            }
            Ok(())
        })();
        if let Err(e) = hold_result {
            println!(":: {tag}: link dropped ({e})");
        }
        println!(":: {tag}: keyboard held the link {:.1}s", t0.elapsed().as_secs_f64());
        if rnd < opts.rounds {
            std::thread::sleep(Duration::from_secs(3));
        }
    }

    if opts.f3_check {
        result.adopted = f1_adoption(&opts.adapter, &kbd);
    }
    result
}
