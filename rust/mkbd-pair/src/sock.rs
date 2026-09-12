//! Raw AF_BLUETOOTH socket plumbing: the HCI MGMT control channel
//! (lib/mkbd_common.py's `_mgmt_cmd` / `mgmt_pair_device`) and the L2CAP ATT
//! fixed channel (test/phase4.py's `connect_att`). Linux only.
//!
//! Rust's std::net has no AF_BLUETOOTH support, so this goes through raw
//! libc socket()/bind()/connect()/setsockopt() calls with hand-written
//! sockaddr structs matching <bluetooth/bluetooth.h>, <bluetooth/hci.h> and
//! <bluetooth/l2cap.h>.

use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::time::Duration;

pub const AF_BLUETOOTH: i32 = 31;
pub const BTPROTO_L2CAP: i32 = 0;
pub const BTPROTO_HCI: i32 = 1;
pub const SOL_BLUETOOTH: i32 = 274;
pub const BT_SECURITY: i32 = 4;
pub const BT_SECURITY_HIGH: u8 = 3;

pub const HCI_CHANNEL_CONTROL: u16 = 3;
pub const HCI_DEV_NONE: u16 = 0xFFFF;

/// bdaddr_type values used by the kernel's L2CAP socket address (distinct
/// from the MGMT address-type constants in mgmt.rs).
pub const BDADDR_BREDR: u8 = 0;
pub const BDADDR_LE_PUBLIC: u8 = 1;
pub const BDADDR_LE_RANDOM: u8 = 2;

#[repr(C)]
struct SockaddrHci {
    hci_family: libc::sa_family_t,
    hci_dev: u16,
    hci_channel: u16,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct BdaddrT {
    b: [u8; 6],
}

#[repr(C)]
struct SockaddrL2cap {
    l2_family: libc::sa_family_t,
    l2_psm: u16,
    l2_bdaddr: BdaddrT,
    l2_cid: u16,
    l2_bdaddr_type: u8,
}

pub struct RawSock {
    fd: OwnedFd,
}

impl RawSock {
    fn from_raw_checked(fd: RawFd) -> io::Result<Self> {
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            fd: unsafe { OwnedFd::from_raw_fd(fd) },
        })
    }

    /// AF_BLUETOOTH / BTPROTO_HCI raw socket, bound to the MGMT control
    /// channel (index HCI_DEV_NONE) — the "mgmt socket" throughout mkbd_common.py.
    pub fn mgmt_control() -> io::Result<Self> {
        let s = Self::from_raw_checked(unsafe {
            libc::socket(AF_BLUETOOTH, libc::SOCK_RAW, BTPROTO_HCI)
        })?;
        let addr = SockaddrHci {
            hci_family: AF_BLUETOOTH as libc::sa_family_t,
            hci_dev: HCI_DEV_NONE,
            hci_channel: HCI_CHANNEL_CONTROL,
        };
        let rc = unsafe {
            libc::bind(
                s.fd.as_raw_fd(),
                &addr as *const SockaddrHci as *const libc::sockaddr,
                std::mem::size_of::<SockaddrHci>() as u32,
            )
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(s)
    }

    /// AF_BLUETOOTH / BTPROTO_L2CAP SEQPACKET socket for the fixed ATT
    /// channel (CID 4), bound to `local` with the given local address type
    /// and connected to `remote` with the given remote address type.
    pub fn l2cap_att_connect(
        local: [u8; 6],
        local_type: u8,
        remote: [u8; 6],
        remote_type: u8,
        connect_timeout: Duration,
    ) -> io::Result<Self> {
        let s = Self::from_raw_checked(unsafe {
            libc::socket(AF_BLUETOOTH, libc::SOCK_SEQPACKET, BTPROTO_L2CAP)
        })?;
        s.set_security_high()?;

        let src = SockaddrL2cap {
            l2_family: AF_BLUETOOTH as libc::sa_family_t,
            l2_psm: 0,
            l2_bdaddr: BdaddrT { b: local },
            l2_cid: 4,
            l2_bdaddr_type: local_type,
        };
        let rc = unsafe {
            libc::bind(
                s.fd.as_raw_fd(),
                &src as *const SockaddrL2cap as *const libc::sockaddr,
                std::mem::size_of::<SockaddrL2cap>() as u32,
            )
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }

        let dst = SockaddrL2cap {
            l2_family: AF_BLUETOOTH as libc::sa_family_t,
            l2_psm: 0,
            l2_bdaddr: BdaddrT { b: remote },
            l2_cid: 4,
            l2_bdaddr_type: remote_type,
        };
        s.set_nonblocking(true)?;
        let rc = unsafe {
            libc::connect(
                s.fd.as_raw_fd(),
                &dst as *const SockaddrL2cap as *const libc::sockaddr,
                std::mem::size_of::<SockaddrL2cap>() as u32,
            )
        };
        if rc != 0 {
            let err = io::Error::last_os_error();
            if err.raw_os_error() != Some(libc::EINPROGRESS) {
                return Err(err);
            }
            if !s.poll_writable(connect_timeout)? {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "l2cap connect timed out"));
            }
            let e = s.take_error()?;
            if let Some(e) = e {
                return Err(e);
            }
        }
        s.set_nonblocking(false)?;
        Ok(s)
    }

    fn set_security_high(&self) -> io::Result<()> {
        let level: [u8; 2] = [BT_SECURITY_HIGH, 0];
        self.setsockopt(SOL_BLUETOOTH, BT_SECURITY, &level)
    }

    pub fn setsockopt(&self, level: i32, name: i32, val: &[u8]) -> io::Result<()> {
        let rc = unsafe {
            libc::setsockopt(
                self.fd.as_raw_fd(),
                level,
                name,
                val.as_ptr() as *const libc::c_void,
                val.len() as u32,
            )
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    /// Set (or clear, with `None`) SO_RCVTIMEO, mirroring Python's
    /// `socket.settimeout()` before a blocking recv.
    pub fn set_recv_timeout(&self, timeout: Option<Duration>) -> io::Result<()> {
        let tv = duration_to_timeval(timeout.unwrap_or_default());
        self.setsockopt(libc::SOL_SOCKET, libc::SO_RCVTIMEO, &tv)
    }

    fn set_nonblocking(&self, on: bool) -> io::Result<()> {
        let fd = self.fd.as_raw_fd();
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
        if flags < 0 {
            return Err(io::Error::last_os_error());
        }
        let flags = if on {
            flags | libc::O_NONBLOCK
        } else {
            flags & !libc::O_NONBLOCK
        };
        if unsafe { libc::fcntl(fd, libc::F_SETFL, flags) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    fn poll_writable(&self, timeout: Duration) -> io::Result<bool> {
        self.poll(libc::POLLOUT, timeout)
    }

    fn poll(&self, events: i16, timeout: Duration) -> io::Result<bool> {
        let mut pfd = libc::pollfd {
            fd: self.fd.as_raw_fd(),
            events,
            revents: 0,
        };
        let ms = timeout.as_millis().min(i32::MAX as u128) as i32;
        let rc = unsafe { libc::poll(&mut pfd, 1, ms) };
        if rc < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(rc > 0 && (pfd.revents & events) != 0)
    }

    fn take_error(&self) -> io::Result<Option<io::Error>> {
        let mut err: i32 = 0;
        let mut len = std::mem::size_of::<i32>() as u32;
        let rc = unsafe {
            libc::getsockopt(
                self.fd.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_ERROR,
                &mut err as *mut i32 as *mut libc::c_void,
                &mut len,
            )
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(if err == 0 {
            None
        } else {
            Some(io::Error::from_raw_os_error(err))
        })
    }

    pub fn send(&self, buf: &[u8]) -> io::Result<usize> {
        let rc = unsafe {
            libc::send(
                self.fd.as_raw_fd(),
                buf.as_ptr() as *const libc::c_void,
                buf.len(),
                0,
            )
        };
        if rc < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(rc as usize)
    }

    /// Blocking recv (honors whatever SO_RCVTIMEO is currently set). Returns
    /// `Ok(0)` on EAGAIN/EWOULDBLOCK-as-timeout, matching call sites that
    /// treat a timed-out recv as "no data yet" rather than an error.
    pub fn recv(&self, buf: &mut [u8]) -> io::Result<usize> {
        let rc = unsafe {
            libc::recv(
                self.fd.as_raw_fd(),
                buf.as_mut_ptr() as *mut libc::c_void,
                buf.len(),
                0,
            )
        };
        if rc < 0 {
            let e = io::Error::last_os_error();
            // EAGAIN == EWOULDBLOCK on Linux; matched once to avoid an
            // unreachable-pattern warning on platforms where libc aliases them.
            if e.raw_os_error() == Some(libc::EAGAIN) {
                return Ok(0);
            }
            return Err(e);
        }
        Ok(rc as usize)
    }
}

impl AsRawFd for RawSock {
    fn as_raw_fd(&self) -> RawFd {
        self.fd.as_raw_fd()
    }
}

fn duration_to_timeval(d: Duration) -> [u8; 16] {
    // struct timeval { time_t tv_sec; suseconds_t tv_usec; } — both `long`
    // (8 bytes) on x86_64/aarch64 Linux.
    let sec: i64 = d.as_secs() as i64;
    let usec: i64 = d.subsec_micros() as i64;
    let mut out = [0u8; 16];
    out[0..8].copy_from_slice(&sec.to_ne_bytes());
    out[8..16].copy_from_slice(&usec.to_ne_bytes());
    out
}
