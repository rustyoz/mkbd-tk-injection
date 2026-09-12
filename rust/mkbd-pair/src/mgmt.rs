//! The Linux Bluetooth MGMT control-socket protocol: opcodes, `mgmt_cmd`,
//! `mgmt_pair_device`, key loaders, and (Option A) the extended
//! `MGMT_OP_ADD_REMOTE_OOB_DATA` LE-legacy-OOB-TK payload. Port of the MGMT
//! section of lib/mkbd_common.py plus test/optionA-pair.py's wire format.

use crate::bdaddr::{bdaddr_to_bytes, bytes_to_bdaddr};
use crate::log::info;
use crate::sock::RawSock;
use std::time::{Duration, Instant};

// mkbd_common.py also defines MGMT_OP_LOAD_LINK_KEYS / LOAD_LONG_TERM_KEYS /
// LOAD_IRKS for its key-loader helpers; those aren't used by the pairing
// engine (only by other tools out of this port's scope), so weren't carried
// over here.
pub const MGMT_OP_SET_POWERED: u16 = 0x0005;
pub const MGMT_OP_PAIR_DEVICE: u16 = 0x0019;
pub const MGMT_OP_UNPAIR_DEVICE: u16 = 0x001B;
pub const MGMT_OP_USER_CONFIRMATION_REPLY: u16 = 0x001C;
pub const MGMT_OP_USER_PASSKEY_NEG_REPLY: u16 = 0x001F;
pub const MGMT_OP_ADD_REMOTE_OOB_DATA: u16 = 0x0021;

pub const MGMT_ADDR_LE_PUBLIC: u8 = 0x01;
pub const MGMT_ADDR_LE_RANDOM: u8 = 0x02;

pub const MGMT_EV_CMD_COMPLETE: u16 = 0x0001;
pub const MGMT_EV_CMD_STATUS: u16 = 0x0002;
pub const MGMT_EV_NEW_LTK: u16 = 0x000A;
pub const MGMT_EV_DEVICE_CONNECTED: u16 = 0x000B;
pub const MGMT_EV_DEVICE_DISCONNECTED: u16 = 0x000C;
#[allow(dead_code)]
pub const MGMT_EV_CONNECT_FAILED: u16 = 0x000D;
pub const MGMT_EV_USER_CONFIRMATION_REQUEST: u16 = 0x000F;
pub const MGMT_EV_USER_PASSKEY_REQUEST: u16 = 0x0010;
pub const MGMT_EV_AUTH_FAILED: u16 = 0x0011;
#[allow(dead_code)]
pub const MGMT_EV_DEVICE_UNPAIRED: u16 = 0x0016;
pub const MGMT_EV_NEW_IRK: u16 = 0x0018;
#[allow(dead_code)]
pub const MGMT_EV_NEW_CSRK: u16 = 0x0019;

/// Option A: `optionA/0001-Bluetooth-mgmt-accept-LE-legacy-OOB-TK.patch` gives
/// MGMT_OP_ADD_REMOTE_OOB_DATA a third, 88-byte accepted payload — the
/// existing extended (P-192+P-256) layout with a flags byte + 16-byte
/// le_legacy_tk appended. Unmodified on an unpatched kernel.
pub const MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT: u8 = 0x01;
pub const MGMT_ADD_REMOTE_OOB_LE_LEGACY_DATA_SIZE: usize = 88;

fn mgmt_status_name(st: u8) -> String {
    match st {
        0x00 => "success",
        0x01 => "unknown-command",
        0x02 => "not-connected",
        0x03 => "failed",
        0x04 => "connect-failed",
        0x05 => "auth-failed",
        0x06 => "not-paired",
        0x07 => "no-resources",
        0x08 => "timeout",
        0x09 => "already-connected",
        0x0a => "busy",
        0x0b => "rejected",
        0x0c => "not-supported",
        0x0d => "invalid-params",
        0x0e => "disconnected",
        0x0f => "not-powered",
        0x10 => "cancelled",
        0x11 => "invalid-index",
        _ => return format!("0x{st:02x}"),
    }
    .to_string()
}

fn ev_name(ev: u16) -> String {
    match ev {
        MGMT_EV_CMD_COMPLETE => "cmd-complete",
        MGMT_EV_CMD_STATUS => "cmd-status",
        MGMT_EV_NEW_LTK => "new-ltk",
        MGMT_EV_DEVICE_CONNECTED => "connected",
        MGMT_EV_DEVICE_DISCONNECTED => "disconnected",
        MGMT_EV_CONNECT_FAILED => "connect-failed",
        MGMT_EV_USER_CONFIRMATION_REQUEST => "confirm-request",
        MGMT_EV_USER_PASSKEY_REQUEST => "passkey-request",
        MGMT_EV_AUTH_FAILED => "auth-failed",
        MGMT_EV_NEW_IRK => "new-irk",
        MGMT_EV_NEW_CSRK => "new-csrk",
        _ => return format!("ev-0x{ev:04x}"),
    }
    .to_string()
}

/// Send one MGMT command on a fresh control socket and return its Command
/// Complete return parameters. Needs root.
pub fn mgmt_cmd(opcode: u16, param: &[u8], hci_index: u16) -> Result<Vec<u8>, String> {
    let s = RawSock::mgmt_control().map_err(|e| format!("cannot bind MGMT control socket ({e}); run as root"))?;
    s.set_recv_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| e.to_string())?;

    let mut req = Vec::with_capacity(6 + param.len());
    req.extend_from_slice(&opcode.to_le_bytes());
    req.extend_from_slice(&hci_index.to_le_bytes());
    req.extend_from_slice(&(param.len() as u16).to_le_bytes());
    req.extend_from_slice(param);
    s.send(&req).map_err(|e| format!("MGMT opcode 0x{opcode:04x}: send failed ({e})"))?;

    loop {
        let mut buf = [0u8; 1024];
        let n = s
            .recv(&mut buf)
            .map_err(|e| format!("MGMT opcode 0x{opcode:04x}: no response ({e})"))?;
        if n < 6 {
            if n == 0 {
                return Err(format!("MGMT opcode 0x{opcode:04x}: no response (timeout)"));
            }
            continue;
        }
        let ev = u16::from_le_bytes([buf[0], buf[1]]);
        let plen = u16::from_le_bytes([buf[4], buf[5]]) as usize;
        let body = &buf[6..(6 + plen).min(n)];
        if ev == MGMT_EV_CMD_COMPLETE && body.len() >= 3 {
            let cc_op = u16::from_le_bytes([body[0], body[1]]);
            let status = body[2];
            if cc_op != opcode {
                continue;
            }
            if status != 0x00 {
                return Err(format!(
                    "MGMT opcode 0x{opcode:04x} failed (status 0x{status:02x})"
                ));
            }
            return Ok(body[3..].to_vec());
        }
        if ev == MGMT_EV_CMD_STATUS && body.len() >= 3 {
            let cs_op = u16::from_le_bytes([body[0], body[1]]);
            let status = body[2];
            if cs_op == opcode && status != 0x00 {
                return Err(format!(
                    "MGMT opcode 0x{opcode:04x} failed (status 0x{status:02x})"
                ));
            }
        }
    }
}

pub fn mgmt_set_powered(on: bool, hci_index: u16) {
    if let Err(e) = mgmt_cmd(MGMT_OP_SET_POWERED, &[if on { 1 } else { 0 }], hci_index) {
        crate::log::warn(format!("MGMT Set Powered failed (already in that state?) — continuing ({e})"));
    }
}

pub fn mgmt_unpair_device(addr: &str, addr_type: u8, hci_index: u16) -> Result<(), String> {
    let mut param = bdaddr_to_bytes(addr, true)?.to_vec();
    param.push(addr_type);
    param.push(1); // disconnect
    mgmt_cmd(MGMT_OP_UNPAIR_DEVICE, &param, hci_index).map(|_| ())
}

/// Option A: arm the in-kernel LE legacy OOB TK for `addr`. Fails if the
/// running kernel does not accept the len-88 payload (optionA/0001-0004 not
/// applied/booted).
pub fn mgmt_add_remote_oob_le_legacy_tk(
    addr: &str,
    addr_type: u8,
    tk: &[u8; 16],
    hci_index: u16,
) -> Result<(), String> {
    let mut payload = bdaddr_to_bytes(addr, true)?.to_vec();
    payload.push(addr_type);
    payload.extend_from_slice(&[0u8; 16]); // hash192 — unused on this path
    payload.extend_from_slice(&[0u8; 16]); // rand192
    payload.extend_from_slice(&[0u8; 16]); // hash256 — all-zero disables SC OOB
    payload.extend_from_slice(&[0u8; 16]); // rand256
    payload.push(MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT);
    payload.extend_from_slice(tk);
    assert_eq!(payload.len(), MGMT_ADD_REMOTE_OOB_LE_LEGACY_DATA_SIZE);
    mgmt_cmd(MGMT_OP_ADD_REMOTE_OOB_DATA, &payload, hci_index).map(|_| ())
}

#[derive(Debug, Default, Clone)]
pub struct Ltk {
    // addr/addr_type complete the MGMT_EV_NEW_LTK record for parity with the
    // Python dict, even though today's callers only read the fields below.
    #[allow(dead_code)]
    pub addr: String,
    #[allow(dead_code)]
    pub addr_type: u8,
    pub key_type: u8,
    #[allow(dead_code)]
    pub central: u8,
    pub enc_size: u8,
    pub ediv: u16,
    pub rand: u64,
    pub val: String,
}

#[derive(Debug, Default, Clone)]
pub struct Irk {
    #[allow(dead_code)]
    pub addr: String,
    #[allow(dead_code)]
    pub addr_type: u8,
    pub val: String,
}

#[derive(Debug, Default)]
pub struct PairResult {
    pub status: Option<u8>,
    pub status_name: String,
    pub ltk: Option<Ltk>,
    pub irk: Option<Irk>,
    pub events: Vec<String>,
    pub connected: bool,
    pub disc_reason: Option<u8>,
    pub smp_seen: bool,
}

/// Pair with the LE peer at `addr` (io_cap 0x03 = NoInputNoOutput -> Just
/// Works). Auto-accepts the confirmation. Needs root, and bluetoothd should
/// be stopped first.
pub fn mgmt_pair_device(
    addr: &str,
    addr_type: u8,
    io_cap: u8,
    hci_index: u16,
    timeout: Duration,
    trace: bool,
) -> Result<PairResult, String> {
    let s = RawSock::mgmt_control().map_err(|e| format!("cannot bind MGMT control socket ({e}); run as root"))?;
    s.set_recv_timeout(Some(Duration::from_secs(1))).map_err(|e| e.to_string())?;

    let peer = bdaddr_to_bytes(addr, true)?;
    let mut param = peer.to_vec();
    param.push(addr_type);
    param.push(io_cap);
    let mut req = Vec::with_capacity(6 + param.len());
    req.extend_from_slice(&MGMT_OP_PAIR_DEVICE.to_le_bytes());
    req.extend_from_slice(&hci_index.to_le_bytes());
    req.extend_from_slice(&(param.len() as u16).to_le_bytes());
    req.extend_from_slice(&param);
    s.send(&req).map_err(|e| e.to_string())?;

    let mut res = PairResult {
        status_name: String::new(),
        ..Default::default()
    };
    let mut pair_cmd_done = false;
    let mut end = Instant::now() + timeout;

    while Instant::now() < end {
        let mut buf = [0u8; 2048];
        let n = match s.recv(&mut buf) {
            Ok(0) => continue, // timeout tick, matches Python's `except socket.timeout: continue`
            Ok(n) => n,
            Err(e) => return Err(format!("MGMT recv failed: {e}")),
        };
        if n < 6 {
            continue;
        }
        let ev = u16::from_le_bytes([buf[0], buf[1]]);
        let plen = u16::from_le_bytes([buf[4], buf[5]]) as usize;
        let body = &buf[6..(6 + plen).min(n)];
        res.events.push(ev_name(ev));
        if trace {
            info(format!("  mgmt: {}", ev_name(ev)));
        }

        if (ev == MGMT_EV_CMD_COMPLETE || ev == MGMT_EV_CMD_STATUS) && body.len() >= 3 {
            let op = u16::from_le_bytes([body[0], body[1]]);
            let st = body[2];
            if op == MGMT_OP_PAIR_DEVICE {
                if ev == MGMT_EV_CMD_STATUS && st == 0 {
                    continue; // accepted, pairing in progress
                }
                res.status = Some(st);
                res.status_name = mgmt_status_name(st);
                if st != 0 || res.smp_seen {
                    break;
                }
                // Command Complete with status 0, but no SMP activity
                // observed yet. Seen on real hardware with the Option A
                // LE-legacy-OOB kernel patches: this can arrive *before*
                // MGMT_EV_NEW_LTK. Don't treat "success" as done until the
                // LTK actually shows up (or the timeout elapses).
                pair_cmd_done = true;
                continue;
            }
        } else if ev == MGMT_EV_DEVICE_CONNECTED {
            res.connected = true;
        } else if ev == MGMT_EV_DEVICE_DISCONNECTED && body.len() >= 8 {
            res.disc_reason = Some(body[7]);
        } else if ev == MGMT_EV_USER_CONFIRMATION_REQUEST && body.len() >= 7 {
            res.smp_seen = true;
            let rep = &body[0..7];
            let mut out = Vec::with_capacity(6 + rep.len());
            out.extend_from_slice(&MGMT_OP_USER_CONFIRMATION_REPLY.to_le_bytes());
            out.extend_from_slice(&hci_index.to_le_bytes());
            out.extend_from_slice(&(rep.len() as u16).to_le_bytes());
            out.extend_from_slice(rep);
            let _ = s.send(&out);
            if trace {
                info("  mgmt: -> user confirmation reply (Just Works accept)");
            }
        } else if ev == MGMT_EV_USER_PASSKEY_REQUEST && body.len() >= 7 {
            let rep = &body[0..7];
            let mut out = Vec::with_capacity(6 + rep.len());
            out.extend_from_slice(&MGMT_OP_USER_PASSKEY_NEG_REPLY.to_le_bytes());
            out.extend_from_slice(&hci_index.to_le_bytes());
            out.extend_from_slice(&(rep.len() as u16).to_le_bytes());
            out.extend_from_slice(rep);
            let _ = s.send(&out);
            crate::log::warn("  mgmt: keyboard asked for a passkey — we cannot supply one");
        } else if ev == MGMT_EV_NEW_LTK && body.len() >= 37 {
            res.smp_seen = true;
            let k = &body[1..];
            res.ltk = Some(Ltk {
                addr: bytes_to_bdaddr(&k[0..6], true),
                addr_type: k[6],
                key_type: k[7],
                central: k[8],
                enc_size: k[9],
                ediv: u16::from_le_bytes([k[10], k[11]]),
                rand: u64::from_le_bytes(k[12..20].try_into().unwrap()),
                val: k[20..36].iter().map(|b| format!("{b:02X}")).collect(),
            });
            if pair_cmd_done {
                // Pair Device already reported success; give a short grace
                // period for a possible IRK (may follow the LTK) instead of
                // either breaking immediately or spinning to full timeout.
                end = end.min(Instant::now() + Duration::from_millis(1500));
            }
        } else if ev == MGMT_EV_NEW_IRK && body.len() >= 30 {
            let k = &body[7..];
            res.irk = Some(Irk {
                addr: bytes_to_bdaddr(&k[0..6], true),
                addr_type: k[6],
                val: k[7..23].iter().map(|b| format!("{b:02X}")).collect(),
            });
            if pair_cmd_done && res.ltk.is_some() {
                break;
            }
        } else if ev == MGMT_EV_AUTH_FAILED && body.len() >= 8 {
            res.status = Some(body[7]);
            res.status_name = format!("auth-failed:{}", mgmt_status_name(body[7]));
            break;
        }
    }

    if res.status.is_none() {
        res.status = Some(0xFF);
        res.status_name = "timeout".to_string();
    }
    Ok(res)
}
