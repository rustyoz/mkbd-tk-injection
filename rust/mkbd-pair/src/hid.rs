//! hidraw discovery + ioctls + the COL03 F1/F2/F3 vendor pairing exchange.
//! Port of the hidraw section of lib/mkbd_common.py.

use crate::bdaddr::{bdaddr_to_bytes, bytes_to_bdaddr};
use std::fs;
use std::io;
use std::os::fd::{AsRawFd, RawFd};
use std::path::PathBuf;
use std::time::{Duration, Instant};

pub const VID: u32 = 0x045E;
pub const PID: u32 = 0x0815;

pub const VENDOR_IFACE: i32 = 0;
const COL03_FEATURE_REPORT_ID: u8 = 0x24;
const COL03_INPUT_REPORT_ID: u8 = 0x27;
const COL03_REPORT_LEN: usize = 64;

// --------------------------------------------------------------------- //
// _IOC (asm-generic, matches x86_64 / arm64) — see linux/ioctl.h
// --------------------------------------------------------------------- //
const IOC_NRBITS: u32 = 8;
const IOC_TYPEBITS: u32 = 8;
const IOC_SIZEBITS: u32 = 14;
const IOC_NRSHIFT: u32 = 0;
const IOC_TYPESHIFT: u32 = IOC_NRSHIFT + IOC_NRBITS;
const IOC_SIZESHIFT: u32 = IOC_TYPESHIFT + IOC_TYPEBITS;
const IOC_DIRSHIFT: u32 = IOC_SIZESHIFT + IOC_SIZEBITS;
const IOC_WRITE: u32 = 1;
const IOC_READ: u32 = 2;

const fn ioc(dir: u32, typ: u8, nr: u32, size: u32) -> libc::c_ulong {
    ((dir << IOC_DIRSHIFT) | ((typ as u32) << IOC_TYPESHIFT) | (nr << IOC_NRSHIFT) | (size << IOC_SIZESHIFT))
        as libc::c_ulong
}

fn hidiocsfeature(len: usize) -> libc::c_ulong {
    ioc(IOC_WRITE | IOC_READ, b'H', 0x06, len as u32)
}

// --------------------------------------------------------------------- //
// hidraw discovery
// --------------------------------------------------------------------- //
pub struct HidRawDev {
    pub node: String,
    pub iface: i64,
    #[allow(dead_code)]
    pub syspath: PathBuf,
}

pub fn find_hidraw(vid: u32, pid: u32) -> Vec<HidRawDev> {
    let mut found = Vec::new();
    let mut entries: Vec<_> = fs::read_dir("/sys/class/hidraw")
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .collect();
    entries.sort();
    for sysdir in entries {
        let node = format!(
            "/dev/{}",
            sysdir.file_name().unwrap_or_default().to_string_lossy()
        );
        let uevent = match fs::read_to_string(sysdir.join("device").join("uevent")) {
            Ok(s) => s,
            Err(_) => continue,
        };
        let Some(hid_id) = uevent
            .lines()
            .find_map(|l| l.strip_prefix("HID_ID="))
        else {
            continue;
        };
        let parts: Vec<&str> = hid_id.split(':').collect();
        if parts.len() != 3 {
            continue;
        }
        let (Ok(v), Ok(p)) = (
            u32::from_str_radix(parts[1], 16),
            u32::from_str_radix(parts[2], 16),
        ) else {
            continue;
        };
        if v != vid || p != pid {
            continue;
        }

        // walk up to the usb_interface to read bInterfaceNumber
        let mut iface: i64 = -1;
        if let Ok(mut cur) = fs::canonicalize(sysdir.join("device")) {
            for _ in 0..6 {
                cur = match cur.parent() {
                    Some(p) => p.to_path_buf(),
                    None => break,
                };
                let bif = cur.join("bInterfaceNumber");
                if let Ok(s) = fs::read_to_string(&bif) {
                    if let Ok(n) = i64::from_str_radix(s.trim(), 16) {
                        iface = n;
                    }
                    break;
                }
            }
        }
        found.push(HidRawDev {
            node,
            iface,
            syspath: sysdir,
        });
    }
    found
}

pub fn open_hidraw(iface: i64, vid: u32, pid: u32) -> io::Result<fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    let devs = find_hidraw(vid, pid);
    let dev = devs
        .iter()
        .find(|d| d.iface == iface)
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                format!(
                    "no hidraw node for {vid:04x}:{pid:04x} interface {iface}. \
                     Plug the keyboard in via USB. (root may be needed to open /dev/hidraw*)"
                ),
            )
        })?;
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(&dev.node)
}

fn poll_readable(fd: RawFd, timeout: Duration) -> io::Result<bool> {
    let mut pfd = libc::pollfd {
        fd,
        events: libc::POLLIN,
        revents: 0,
    };
    let ms = timeout.as_millis().min(i32::MAX as u128) as i32;
    let rc = unsafe { libc::poll(&mut pfd, 1, ms) };
    if rc < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(rc > 0 && (pfd.revents & libc::POLLIN) != 0)
}

// --------------------------------------------------------------------- //
// hidraw ioctls
// --------------------------------------------------------------------- //
fn ioctl(fd: RawFd, req: libc::c_ulong, arg: *mut libc::c_void) -> io::Result<()> {
    let rc = unsafe { libc::ioctl(fd, req, arg) };
    if rc < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn set_feature(fd: RawFd, report_id: u8, payload: &[u8]) -> io::Result<()> {
    let mut buf = Vec::with_capacity(1 + payload.len());
    buf.push(report_id);
    buf.extend_from_slice(payload);
    let len = buf.len();
    ioctl(fd, hidiocsfeature(len), buf.as_mut_ptr() as *mut libc::c_void)
}

// --------------------------------------------------------------------- //
// COL03 vendor exchange (F1/F2/F3) — the USB pairing bootstrap
// --------------------------------------------------------------------- //

/// Read HID input reports off `fd` (non-blocking) until one starts with
/// `want_id`, or `deadline` passes. Returns the raw report (id byte first)
/// or an empty vec on timeout.
fn read_report(fd: &fs::File, want_id: u8, deadline: Instant) -> io::Result<Vec<u8>> {
    loop {
        let now = Instant::now();
        if now >= deadline {
            return Ok(Vec::new());
        }
        let raw = poll_readable(fd.as_raw_fd(), deadline - now)?;
        if !raw {
            return Ok(Vec::new());
        }
        let mut buf = [0u8; COL03_REPORT_LEN];
        match std::io::Read::read(&mut &*fd, &mut buf) {
            Ok(0) => continue,
            Ok(n) => {
                if n > 0 && buf[0] == want_id {
                    return Ok(buf[..n].to_vec());
                }
            }
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => continue,
            Err(e) => return Err(e),
        }
    }
}

pub fn col03_command(
    fd: &fs::File,
    opcode: u8,
    payload: &[u8],
    timeout: Duration,
) -> Result<(u8, Vec<u8>), String> {
    let mut body = vec![opcode, payload.len() as u8];
    body.extend_from_slice(payload);
    body.resize(COL03_REPORT_LEN - 1, 0);
    set_feature(fd.as_raw_fd(), COL03_FEATURE_REPORT_ID, &body)
        .map_err(|e| format!("SET_FEATURE 0x{opcode:02X} failed: {e}"))?;

    let deadline = Instant::now() + timeout;
    loop {
        let rep = read_report(fd, COL03_INPUT_REPORT_ID, deadline)
            .map_err(|e| format!("reading COL03 reply: {e}"))?;
        if rep.is_empty() {
            return Err(format!("no 0x27 reply to opcode 0x{opcode:02X}"));
        }
        if rep.len() >= 4 && rep[1] == opcode {
            let status = rep[2];
            let length = rep[3] as usize;
            let end = (4 + length).min(rep.len());
            return Ok((status, rep[4..end].to_vec()));
        }
    }
}

pub struct VendorExchange {
    pub bond_exists: bool,
    pub current_addr: String,
    pub new_addr: String,
    pub tk: [u8; 16],
    pub name: String,
    #[allow(dead_code)]
    pub name_len: Option<u8>,
}

/// Run the full F1/F2/F3 handshake on COL03 — see mkbd_common.py's
/// `vendor_pairing_exchange` for the wire-format writeup. Callers must pair
/// against `new_addr` and use `tk` from the same call; both must come from
/// one exchange and not be cached across attempts.
pub fn vendor_pairing_exchange(fd: &fs::File, adapter_addr: &str) -> Result<VendorExchange, String> {
    let host = bdaddr_to_bytes(adapter_addr, true)?;
    let (st, d) = col03_command(fd, 0xF1, &host, Duration::from_secs(2))?;
    if st != 0 || d.len() < 7 {
        return Err(format!(
            "F1 (set host address) failed: status 0x{st:02X}, {} bytes",
            d.len()
        ));
    }
    let bond_exists = d[0] != 0;
    let current_addr = bytes_to_bdaddr(&d[1..7], true);

    let mut name_len = None;
    match col03_command(fd, 0xF2, &[], Duration::from_secs(2)) {
        Ok((0, d)) if !d.is_empty() => name_len = Some(d[0]),
        Ok(_) => {}
        Err(_) => crate::log::warn("F2 (name length) got no reply — continuing"),
    }

    let (st, d) = col03_command(fd, 0xF3, &[0xF0], Duration::from_secs(2))?;
    if st != 0 || d.len() < 22 {
        return Err(format!(
            "F3 (generate OOB record) failed: status 0x{st:02X}, {} bytes",
            d.len()
        ));
    }
    let new_addr = bytes_to_bdaddr(&d[0..6], true);
    let mut tk = [0u8; 16];
    tk.copy_from_slice(&d[6..22]);
    let name_bytes = &d[22..];
    let name_end = name_bytes.iter().position(|&b| b == 0).unwrap_or(name_bytes.len());
    let name = String::from_utf8_lossy(&name_bytes[..name_end]).into_owned();

    Ok(VendorExchange {
        bond_exists,
        current_addr,
        new_addr,
        tk,
        name,
        name_len,
    })
}
