//! BlueZ device lookup/cleanup + adapter address lookup. Port of the
//! relevant parts of lib/mkbd_common.py's adapter_bdaddr(). The bond-file
//! writer (write_le_device_info) was removed along with the raw-mgmt
//! pairing engine it supported — bluetoothd manages the bond file itself on
//! the D-Bus pairing path this crate now uses exclusively; see main.rs's
//! module doc.

use std::fs;
use std::process::Command;

/// Identifying strings for "this is the Modern Keyboard" independent of its
/// current (counter-generated, ever-changing) BLE address: the resolved GAP
/// device name, its Bluetooth-side PnP ID (distinct from the USB 045e:0815
/// used for the vendor hidraw exchange), and its vendor-specific GATT
/// service UUID. Checked against `bluetoothctl info <addr>` output, which
/// includes all of these once a device has been seen/bonded before.
const KEYBOARD_MATCH_STRINGS: [&str; 3] = [
    "fingerprint id",
    "v045ep0813", // Modalias: usb:v045Ep0813d0112
    "d4e3e3eb-a4ae-4193-bbf8-c769980abfe0", // vendor-specific service UUID
];

/// Remove every BlueZ device entry (paired, trusted, or merely known) that
/// matches this keyboard by name/PnP-ID/vendor-UUID, regardless of its
/// current address. Needed because the keyboard hands out a new address
/// each time the F1/F2/F3 exchange runs (a per-attempt counter), so repeated
/// test/pairing runs leave a trail of stale device objects behind — and a
/// leftover `Paired=true` one is exactly what makes a fresh `Device1.Pair()`
/// fail with `org.bluez.Error.AlreadyExists` even though the address being
/// paired now was never seen before.
pub fn remove_matching_bluez_devices() -> Vec<String> {
    let mut removed = Vec::new();
    let Ok(out) = Command::new("bluetoothctl").arg("devices").output() else {
        return removed;
    };
    let list = String::from_utf8_lossy(&out.stdout);
    for line in list.lines() {
        let Some(rest) = line.strip_prefix("Device ") else {
            continue;
        };
        let Some(addr) = rest.split_whitespace().next() else {
            continue;
        };
        let Ok(info_out) = Command::new("bluetoothctl").args(["info", addr]).output() else {
            continue;
        };
        let info_lower = String::from_utf8_lossy(&info_out.stdout).to_lowercase();
        if KEYBOARD_MATCH_STRINGS.iter().any(|m| info_lower.contains(m)) {
            let _ = Command::new("bluetoothctl")
                .args(["remove", addr])
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status();
            removed.push(addr.to_string());
        }
    }
    removed
}

pub fn adapter_bdaddr(hci: &str) -> Result<String, String> {
    let p = format!("/sys/class/bluetooth/{hci}/address");
    if let Ok(s) = fs::read_to_string(&p) {
        return Ok(s.trim().to_uppercase());
    }
    let idx: String = hci.chars().filter(|c| c.is_ascii_digit()).collect();
    let idx = if idx.is_empty() { "0".to_string() } else { idx };

    for (cmd, args) in [
        ("btmgmt", vec!["--index", idx.as_str(), "info"]),
        ("bluetoothctl", vec!["show"]),
    ] {
        let Ok(out) = Command::new(cmd).args(&args).output() else {
            continue;
        };
        let text = String::from_utf8_lossy(&out.stdout);
        if let Some(addr) = extract_bdaddr(&text) {
            return Ok(addr.to_uppercase());
        }
    }
    Err("could not determine adapter bdaddr".to_string())
}

fn extract_bdaddr(text: &str) -> Option<String> {
    for marker in ["Controller ", "addr "] {
        if let Some(pos) = text.find(marker) {
            let rest = &text[pos + marker.len()..];
            let candidate: String = rest.chars().take(17).collect();
            if is_bdaddr(&candidate) {
                return Some(candidate);
            }
        }
    }
    None
}

fn is_bdaddr(s: &str) -> bool {
    let parts: Vec<&str> = s.split(':').collect();
    parts.len() == 6 && parts.iter().all(|p| p.len() == 2 && u8::from_str_radix(p, 16).is_ok())
}
