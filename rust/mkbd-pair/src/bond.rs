//! BlueZ bond-file writer + adapter address lookup. Port of the relevant
//! parts of lib/mkbd_common.py (write_le_device_info / adapter_bdaddr).
//! write_device_info (BR/EDR link-key variant) is not ported: nothing in the
//! pairing engine uses it — this keyboard is LE-only.

use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

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

pub fn bluez_device_dir(adapter: &str, remote: &str) -> String {
    format!("/var/lib/bluetooth/{}/{}", adapter.to_uppercase(), remote.to_uppercase())
}

fn clean_hex(s: &str) -> String {
    s.replace(':', "").replace(' ', "").to_uppercase()
}

/// Write a persistent BlueZ **LE** bond (needs root). This keyboard pairs
/// over LE legacy OOB, so the bond is authenticated legacy. `addr_type` is
/// "static" for an LE static-random address (this keyboard) or "public".
pub fn write_le_device_info(
    adapter: &str,
    remote: &str,
    ltk_hex: &str,
    ediv: u16,
    rand: u64,
    irk_hex: Option<&str>,
    authenticated: u8,
    enc_size: u8,
    name: &str,
    addr_type: &str,
) -> Result<String, String> {
    let dir = bluez_device_dir(adapter, remote);
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let info_path = format!("{dir}/info");

    let ltk = clean_hex(ltk_hex);
    if ltk.len() != 32 {
        return Err(format!("LTK must be 16 bytes / 32 hex chars, got {}", ltk.len()));
    }

    let mut lines = vec![
        "[General]".to_string(),
        format!("Name={name}"),
        format!("AddressType={addr_type}"),
        "SupportedTechnologies=LE;".to_string(),
        "Trusted=true".to_string(),
        "Blocked=false".to_string(),
        "WakeAllowed=true".to_string(),
        String::new(),
        "[LongTermKey]".to_string(),
        format!("Key={ltk}"),
        format!("Authenticated={authenticated}"),
        format!("EncSize={enc_size}"),
        format!("EDiv={ediv}"),
        format!("Rand={rand}"),
    ];
    if let Some(irk_hex) = irk_hex {
        let irk = clean_hex(irk_hex);
        if irk.len() != 32 {
            return Err(format!("IRK must be 16 bytes / 32 hex chars, got {}", irk.len()));
        }
        lines.push(String::new());
        lines.push("[IdentityResolvingKey]".to_string());
        lines.push(format!("Key={irk}"));
    }

    let mut f = fs::File::create(&info_path).map_err(|e| e.to_string())?;
    f.write_all((lines.join("\n") + "\n").as_bytes())
        .map_err(|e| e.to_string())?;
    fs::set_permissions(&info_path, fs::Permissions::from_mode(0o600)).map_err(|e| e.to_string())?;
    Ok(info_path)
}
