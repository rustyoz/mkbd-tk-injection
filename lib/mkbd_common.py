"""
mkbd_common — shared plumbing for the Microsoft Modern Keyboard w/ Fingerprint ID
(USB 045e:0815, model 1780) Bluetooth-over-USB provisioning tools.

Vendored from the sibling `modernkeyboard` repo
(lib/mkbd_common.py @ fc2b8d98d0d6eb1fd9bc04795ba973975b6e723f, 2026-09-11) so
this repo's test/ scripts are self-contained and don't depend on a sibling
checkout. That repo is still the canonical source — port fixes there back
here by hand (there is no submodule/symlink; this is a plain copy).

Stdlib only (ctypes / socket / struct / os) so every script runs on a stock
Omarchy install with no pip/pacman step. Optional external tools (tshark, hivex)
are only needed by decode-capture / transplant and are checked there.

This keyboard bonds over Bluetooth **LE** (HID-over-GATT), not BR/EDR — see
docs/PROTOCOL.md. The stored secret is an LTK (+ IRK), not a Classic link key,
and the USB bootstrap is a three-step TLV exchange (F1/F2/F3) on interface 0's
vendor collection COL03 (feature report 0x24 / interrupt-IN report 0x27).

Contents:
  * hidraw discovery + report-descriptor / feature-report ioctls
  * the COL03 F1/F2/F3 vendor exchange that points the keyboard at our adapter
  * BlueZ helpers: adapter address, persistent LE/BR-EDR info-file writers, live
    MGMT key loads (Load Long Term Keys / Load IRKs / Load Link Keys)
  * bdaddr string<->bytes helpers
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import os
import re
import select
import struct
import subprocess
import sys
import time

VID = 0x045E
PID = 0x0815

# The vendor pairing channel lives on USB interface 0 (interface 1 is the
# Synaptics fingerprint sensor). COL03 there is a command/response buffer:
# SET_FEATURE report 0x24 carries <opcode><len><payload>; the reply arrives on
# the interrupt-IN endpoint as report 0x27 <opcode><status><len><payload>.
VENDOR_IFACE = 0
COL03_FEATURE_REPORT_ID = 0x24
COL03_INPUT_REPORT_ID = 0x27
COL03_REPORT_LEN = 64  # report id + 63 payload bytes, fixed

# --------------------------------------------------------------------------- #
# tiny logging
# --------------------------------------------------------------------------- #
def _c(code: str, s: str) -> str:
    return s if not sys.stderr.isatty() else f"\033[{code}m{s}\033[0m"

def info(msg: str) -> None: print(_c("36", "::"), msg, file=sys.stderr)
def ok(msg: str) -> None:   print(_c("32", "ok:"), msg, file=sys.stderr)
def warn(msg: str) -> None: print(_c("33", "warn:"), msg, file=sys.stderr)
def die(msg: str, code: int = 1):
    print(_c("31", "error:"), msg, file=sys.stderr)
    raise SystemExit(code)

def hexdump(data: bytes, indent: str = "    ") -> str:
    out = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        h = " ".join(f"{b:02x}" for b in chunk)
        a = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{indent}{off:04x}  {h:<47}  {a}")
    return "\n".join(out)

# --------------------------------------------------------------------------- #
# _IOC (asm-generic, matches x86_64 / arm64)
# --------------------------------------------------------------------------- #
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2

def _IOC(d: int, t: str, nr: int, size: int) -> int:
    return ((d << _IOC_DIRSHIFT) | (ord(t) << _IOC_TYPESHIFT) |
            (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))

HIDIOCGRDESCSIZE = _IOC(_IOC_READ, "H", 0x01, ctypes.sizeof(ctypes.c_int))
HIDIOCGRDESC     = _IOC(_IOC_READ, "H", 0x02, 4 + 4096)  # struct hidraw_report_descriptor
def HIDIOCGFEATURE(length: int) -> int: return _IOC(_IOC_WRITE | _IOC_READ, "H", 0x07, length)
def HIDIOCSFEATURE(length: int) -> int: return _IOC(_IOC_WRITE | _IOC_READ, "H", 0x06, length)
HIDIOCGRAWINFO   = _IOC(_IOC_READ, "H", 0x03, 4 + 2 + 2)  # struct hidraw_devinfo {__u32 bustype; __s16 vendor; __s16 product;}

# --------------------------------------------------------------------------- #
# hidraw discovery
# --------------------------------------------------------------------------- #
class HidRawDev:
    def __init__(self, node: str, iface: int, path: str):
        self.node = node          # /dev/hidrawN
        self.iface = iface        # USB bInterfaceNumber
        self.syspath = path       # /sys/class/hidraw/hidrawN

def find_hidraw(vid: int = VID, pid: int = PID) -> list[HidRawDev]:
    """Return every /dev/hidraw* that belongs to vid:pid, tagged with its USB
    interface number (parsed from the sysfs HID phys / parent usb interface)."""
    found: list[HidRawDev] = []
    for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        node = "/dev/" + os.path.basename(sysdir)
        try:
            uevent = open(os.path.join(sysdir, "device", "uevent")).read()
        except OSError:
            continue
        m = re.search(r"HID_ID=[0-9a-fA-F]+:0*([0-9a-fA-F]+):0*([0-9a-fA-F]+)", uevent)
        if not m or int(m.group(1), 16) != vid or int(m.group(2), 16) != pid:
            continue
        # walk up to the usb_interface to read bInterfaceNumber
        iface = -1
        real = os.path.realpath(os.path.join(sysdir, "device"))
        cur = real
        for _ in range(6):
            cur = os.path.dirname(cur)
            bif = os.path.join(cur, "bInterfaceNumber")
            if os.path.exists(bif):
                iface = int(open(bif).read().strip(), 16)
                break
        found.append(HidRawDev(node, iface, sysdir))
    return found

def open_hidraw(iface: int, vid: int = VID, pid: int = PID) -> int:
    devs = [d for d in find_hidraw(vid, pid) if d.iface == iface]
    if not devs:
        die(f"no hidraw node for {vid:04x}:{pid:04x} interface {iface}. "
            f"Plug the keyboard in via USB. (root may be needed to open /dev/hidraw*)")
    try:
        return os.open(devs[0].node, os.O_RDWR)
    except PermissionError:
        die(f"permission denied on {devs[0].node} — run this as root (sudo).")

# --------------------------------------------------------------------------- #
# hidraw ioctls
# --------------------------------------------------------------------------- #
def get_report_descriptor(fd: int) -> bytes:
    size = ctypes.c_int()
    fcntl.ioctl(fd, HIDIOCGRDESCSIZE, size, True)
    buf = bytearray(4 + 4096)
    struct.pack_into("I", buf, 0, size.value)
    fcntl.ioctl(fd, HIDIOCGRDESC, buf, True)
    return bytes(buf[4:4 + size.value])

def get_feature(fd: int, report_id: int, length: int) -> bytes:
    buf = bytearray(length + 1)
    buf[0] = report_id
    fcntl.ioctl(fd, HIDIOCGFEATURE(len(buf)), buf, True)
    return bytes(buf)

def set_feature(fd: int, report_id: int, payload: bytes) -> int:
    buf = bytearray([report_id]) + bytearray(payload)
    return fcntl.ioctl(fd, HIDIOCSFEATURE(len(buf)), bytes(buf))

# --------------------------------------------------------------------------- #
# COL03 vendor exchange (F1/F2/F3) — the USB pairing bootstrap
# --------------------------------------------------------------------------- #
def _read_report(fd: int, want_id: int, deadline: float) -> bytes:
    """Read HID input reports off `fd` until one starts with `want_id`, or the
    monotonic `deadline` passes. Returns the raw report (id byte first) or b""."""
    while True:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            return b""
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return b""
        try:
            data = os.read(fd, COL03_REPORT_LEN)
        except BlockingIOError:
            continue
        if data and data[0] == want_id:
            return data


def col03_command(fd: int, opcode: int, payload: bytes = b"",
                  timeout: float = 2.0) -> tuple[int, bytes]:
    """Send one COL03 command (SET_FEATURE 0x24 <opcode><len><payload>) and
    return (status, response_payload) from the matching 0x27 interrupt report.
    Raises TimeoutError if no reply with the same opcode arrives in `timeout` s."""
    body = bytes([opcode, len(payload)]) + payload
    body = body.ljust(COL03_REPORT_LEN - 1, b"\x00")
    set_feature(fd, COL03_FEATURE_REPORT_ID, body)
    deadline = time.monotonic() + timeout
    while True:
        rep = _read_report(fd, COL03_INPUT_REPORT_ID, deadline)
        if not rep:
            raise TimeoutError(f"no 0x27 reply to opcode 0x{opcode:02X}")
        if len(rep) >= 4 and rep[1] == opcode:
            status, length = rep[2], rep[3]
            return status, rep[4:4 + length]


def vendor_pairing_exchange(fd: int, adapter_addr: str) -> dict:
    """Run the full F1/F2/F3 handshake on COL03.

    F1  writes our adapter's BD_ADDR so the keyboard directed-advertises to us,
        and returns its *current* address + a bond-exists flag.
    F2  returns the device-name length.
    F3/F0 makes the keyboard generate a fresh bond: it returns the *new*
        address it will advertise with (current_addr's counter byte + 1; it is
        NOT random and only advances once an air-side bond actually completes,
        so re-running after a failure returns the same address), a one-time
        16-byte LE legacy OOB TK, and the complete local name. This call also
        starts the pairing advertising.

    Callers must pair against new_addr and use the tk from the same call — both
    come from this one exchange and must not be cached across attempts. See
    docs/PROTOCOL.md "Generated address: a per-bond counter".

    Returns {bond_exists, current_addr, new_addr, tk (bytes), name, name_len}.
    """
    st, d = col03_command(fd, 0xF1, bdaddr_to_bytes(adapter_addr, little_endian=True))
    if st != 0 or len(d) < 7:
        die(f"F1 (set host address) failed: status 0x{st:02X}, {len(d)} bytes")
    bond_exists = bool(d[0])
    current_addr = bytes_to_bdaddr(d[1:7], little_endian=True)

    name_len = None
    try:
        st, d = col03_command(fd, 0xF2)
        if st == 0 and d:
            name_len = d[0]
    except TimeoutError:
        warn("F2 (name length) got no reply — continuing")

    st, d = col03_command(fd, 0xF3, bytes([0xF0]))
    if st != 0 or len(d) < 22:
        die(f"F3 (generate OOB record) failed: status 0x{st:02X}, {len(d)} bytes")
    new_addr = bytes_to_bdaddr(d[0:6], little_endian=True)
    tk = d[6:22]
    name = d[22:].split(b"\x00", 1)[0].decode("utf-8", "replace")
    return {"bond_exists": bond_exists, "current_addr": current_addr,
            "new_addr": new_addr, "tk": tk, "name": name, "name_len": name_len}

# --------------------------------------------------------------------------- #
# HID report-descriptor mini-parser (enough to list report IDs + kinds)
# --------------------------------------------------------------------------- #
def parse_report_descriptor(desc: bytes) -> dict:
    """Very small parser: tracks Report ID and emits Input/Output/Feature main
    items with the report id they belong to. Returns
    {'feature': set(ids), 'output': set(ids), 'input': set(ids), 'usages': [...]}."""
    i = 0
    rid = 0
    res = {"feature": set(), "output": set(), "input": set(), "items": []}
    usage_page = None
    while i < len(desc):
        b = desc[i]; i += 1
        if b == 0xFE:  # long item
            size = desc[i]; i += 2 + size
            continue
        size = {0: 0, 1: 1, 2: 2, 3: 4}[b & 0x03]
        typ = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        data = desc[i:i + size]; i += size
        val = int.from_bytes(data, "little") if data else 0
        if typ == 1 and tag == 0x0:      # Global: Usage Page
            usage_page = val
        elif typ == 1 and tag == 0x8:    # Global: Report ID
            rid = val
        elif typ == 0:                   # Main
            if tag == 0x8:  res["input"].add(rid)
            if tag == 0x9:  res["output"].add(rid)
            if tag == 0xB:  res["feature"].add(rid)
        elif typ == 2 and tag == 0x0:    # Local: Usage
            res["items"].append((usage_page, val, rid))
    return res

# --------------------------------------------------------------------------- #
# bdaddr helpers
# --------------------------------------------------------------------------- #
def bdaddr_to_bytes(s: str, little_endian: bool = True) -> bytes:
    parts = re.split(r"[:\-]", s.strip())
    if len(parts) != 6:
        raise ValueError(f"bad bdaddr: {s!r}")
    b = bytes(int(p, 16) for p in parts)
    return b[::-1] if little_endian else b

def bytes_to_bdaddr(b: bytes, little_endian: bool = True) -> str:
    if little_endian:
        b = b[::-1]
    return ":".join(f"{x:02X}" for x in b)

# --------------------------------------------------------------------------- #
# BlueZ
# --------------------------------------------------------------------------- #
def adapter_bdaddr(hci: str = "hci0", required: bool = True) -> str | None:
    p = f"/sys/class/bluetooth/{hci}/address"
    if os.path.exists(p):
        return open(p).read().strip().upper()
    # some kernels don't expose that sysfs attr; try mgmt then bluetoothctl
    idx = "".join(ch for ch in hci if ch.isdigit()) or "0"
    for cmd in (["btmgmt", "--index", idx, "info"], ["bluetoothctl", "show"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(?:Controller |addr )([0-9A-Fa-f:]{17})", out)
        if m:
            return m.group(1).upper()
    if required:
        die("could not determine adapter bdaddr")
    return None

def bluez_device_dir(adapter: str, remote: str) -> str:
    return f"/var/lib/bluetooth/{adapter.upper()}/{remote.upper()}"

def write_le_device_info(adapter: str, remote: str, ltk_hex: str, *,
                         ediv: int, rand: int, irk_hex: str | None = None,
                         authenticated: int = 1, enc_size: int = 16,
                         name: str = "Modern Keyboard",
                         addr_type: str = "static") -> str:
    """Write a persistent BlueZ **LE** bond (needs root). Returns the info path;
    caller must restart bluetooth (or MGMT-load the keys) afterwards.

    This keyboard pairs over LE legacy OOB, so the bond is authenticated legacy:
    Authenticated=1. EDiv/Rand are stored as decimal. `addr_type` is "static"
    for an LE static-random address (this keyboard) or "public".
    See docs/PROTOCOL.md / CLAUDE-output.md.
    """
    d = bluez_device_dir(adapter, remote)
    os.makedirs(d, exist_ok=True)
    info_path = os.path.join(d, "info")
    ltk = ltk_hex.replace(":", "").replace(" ", "").upper()
    if len(ltk) != 32:
        die(f"LTK must be 16 bytes / 32 hex chars, got {len(ltk)}")
    lines = [
        "[General]",
        f"Name={name}",
        f"AddressType={addr_type}",
        "SupportedTechnologies=LE;",
        "Trusted=true",
        "Blocked=false",
        "WakeAllowed=true",
        "",
        "[LongTermKey]",
        f"Key={ltk}",
        f"Authenticated={authenticated}",
        f"EncSize={enc_size}",
        f"EDiv={ediv}",
        f"Rand={rand}",
    ]
    if irk_hex:
        irk = irk_hex.replace(":", "").replace(" ", "").upper()
        if len(irk) != 32:
            die(f"IRK must be 16 bytes / 32 hex chars, got {len(irk)}")
        lines += ["", "[IdentityResolvingKey]", f"Key={irk}"]
    with open(info_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(info_path, 0o600)
    return info_path


def write_device_info(adapter: str, remote: str, link_key_hex: str,
                      name: str = "Modern Keyboard",
                      cls: str = "0x000540", key_type: int = 5,
                      pin_length: int = 0) -> str:
    """Write a persistent BlueZ bond for a BR/EDR device. Needs root.
    key_type 5 = Authenticated Combination key (P-192); 4 = Unauthenticated.
    Returns the info path. Caller must restart bluetooth afterwards.

    NOTE: the Modern Keyboard 1780 is an **LE** device — use
    write_le_device_info() for it. This is kept only for generic BR/EDR use."""
    d = bluez_device_dir(adapter, remote)
    os.makedirs(d, exist_ok=True)
    info_path = os.path.join(d, "info")
    lk = link_key_hex.replace(":", "").replace(" ", "").upper()
    if len(lk) != 32:
        die(f"link key must be 16 bytes / 32 hex chars, got {len(lk)}")
    content = (
        "[General]\n"
        f"Name={name}\n"
        f"Class={cls}\n"
        "SupportedTechnologies=BR/EDR;\n"
        "Trusted=true\n"
        "Blocked=false\n"
        "WakeAllowed=true\n"
        "\n"
        "[LinkKey]\n"
        f"Key={lk}\n"
        f"Type={key_type}\n"
        f"PINLength={pin_length}\n"
    )
    with open(info_path, "w") as f:
        f.write(content)
    os.chmod(info_path, 0o600)
    return info_path

# ---- live MGMT control socket ------------------------------------------- #
AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_CONTROL = 3
HCI_DEV_NONE = 0xFFFF

MGMT_OP_SET_POWERED = 0x0005
MGMT_OP_DISCONNECT = 0x0014
MGMT_OP_LOAD_LINK_KEYS = 0x0012        # BR/EDR (legacy; kept for reference)
MGMT_OP_LOAD_LONG_TERM_KEYS = 0x0013   # LE
MGMT_OP_PAIR_DEVICE = 0x0019
MGMT_OP_CANCEL_PAIR_DEVICE = 0x001A
MGMT_OP_UNPAIR_DEVICE = 0x001B
MGMT_OP_USER_CONFIRMATION_REPLY = 0x001C
MGMT_OP_USER_CONFIRMATION_NEG_REPLY = 0x001D
MGMT_OP_USER_PASSKEY_NEG_REPLY = 0x001F
MGMT_OP_LOAD_IRKS = 0x0030            # LE

MGMT_ADDR_BREDR = 0x00
MGMT_ADDR_LE_PUBLIC = 0x01
MGMT_ADDR_LE_RANDOM = 0x02

# LTK Key_Type for Load Long Term Keys:
MGMT_LTK_UNAUTHENTICATED = 0x00        # legacy, Just Works
MGMT_LTK_AUTHENTICATED = 0x01          # legacy, MITM (OOB / passkey) — this kbd
MGMT_LTK_P256_UNAUTHENTICATED = 0x02   # Secure Connections
MGMT_LTK_P256_AUTHENTICATED = 0x03

MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
MGMT_EV_NEW_LTK = 0x000A
MGMT_EV_DEVICE_CONNECTED = 0x000B
MGMT_EV_DEVICE_DISCONNECTED = 0x000C
MGMT_EV_CONNECT_FAILED = 0x000D
MGMT_EV_USER_CONFIRMATION_REQUEST = 0x000F
MGMT_EV_USER_PASSKEY_REQUEST = 0x0010
MGMT_EV_AUTH_FAILED = 0x0011
MGMT_EV_DEVICE_UNPAIRED = 0x0016
MGMT_EV_NEW_IRK = 0x0018
MGMT_EV_NEW_CSRK = 0x0019


def _mgmt_cmd(opcode: int, param: bytes = b"", hci_index: int = 0) -> bytes:
    """Send one MGMT command on the control socket and return its Command
    Complete return parameters. Dies on a non-zero status. Needs root."""
    import socket
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    try:
        try:
            s.bind((HCI_DEV_NONE, HCI_CHANNEL_CONTROL))
        except OSError as e:
            die(f"cannot bind MGMT control socket ({e}); run as root")
        s.send(struct.pack("<HHH", opcode, hci_index, len(param)) + param)
        s.settimeout(2.0)
        while True:
            try:
                resp = s.recv(1024)
            except OSError as e:
                die(f"MGMT opcode 0x{opcode:04x}: no response ({e})")
            ev, _idx, plen = struct.unpack_from("<HHH", resp, 0)
            body = resp[6:6 + plen]
            if ev == MGMT_EV_CMD_COMPLETE and len(body) >= 3:
                cc_op, status = struct.unpack_from("<HB", body, 0)
                if cc_op != opcode:
                    continue
                if status != 0x00:
                    die(f"MGMT opcode 0x{opcode:04x} failed (status 0x{status:02x})")
                return body[3:]
            if ev == MGMT_EV_CMD_STATUS and len(body) >= 3:
                cs_op, status = struct.unpack_from("<HB", body, 0)
                if cs_op == opcode and status != 0x00:
                    die(f"MGMT opcode 0x{opcode:04x} failed (status 0x{status:02x})")
    finally:
        s.close()


def mgmt_load_link_keys(entries: list[tuple[str, str, int, int]],
                        hci_index: int = 0, debug_keys: bool = False) -> None:
    """entries: list of (bdaddr_str, link_key_hex, key_type, pin_length).
    BR/EDR only — the Modern Keyboard 1780 needs mgmt_load_ltks()/mgmt_load_irks()."""
    blob = b""
    for addr, key_hex, ktype, pinlen in entries:
        kb = bytes.fromhex(key_hex.replace(":", "").replace(" ", ""))
        if len(kb) != 16:
            die("link key must be 16 bytes")
        blob += (bdaddr_to_bytes(addr, little_endian=True) +
                 bytes([MGMT_ADDR_BREDR]) + kb + bytes([ktype, pinlen]))
    param = struct.pack("<BH", 1 if debug_keys else 0, len(entries)) + blob
    _mgmt_cmd(MGMT_OP_LOAD_LINK_KEYS, param, hci_index)
    ok(f"MGMT: loaded {len(entries)} link key(s) into hci{hci_index}")


def mgmt_load_ltks(entries: list[dict], hci_index: int = 0) -> None:
    """Push LE Long Term Keys into the running bluetoothd (no restart). Needs root.

    Each entry: {addr, key (32 hex), ediv (int), rand (int),
                 authenticated (0..3, default 1), enc_size (default 16),
                 central (0/1, default 0), addr_type (default LE random)}.
    """
    blob = b""
    for e in entries:
        kb = bytes.fromhex(e["key"].replace(":", "").replace(" ", ""))
        if len(kb) != 16:
            die("LTK must be 16 bytes / 32 hex chars")
        blob += (bdaddr_to_bytes(e["addr"], little_endian=True)
                 + bytes([e.get("addr_type", MGMT_ADDR_LE_RANDOM),
                          e.get("authenticated", MGMT_LTK_AUTHENTICATED),
                          1 if e.get("central") else 0,
                          e.get("enc_size", 16)])
                 + struct.pack("<H", e.get("ediv", 0) & 0xFFFF)
                 + struct.pack("<Q", e.get("rand", 0) & (2**64 - 1))
                 + kb)
    param = struct.pack("<H", len(entries)) + blob
    _mgmt_cmd(MGMT_OP_LOAD_LONG_TERM_KEYS, param, hci_index)
    ok(f"MGMT: loaded {len(entries)} LTK(s) into hci{hci_index}")


def mgmt_load_irks(entries: list[tuple], hci_index: int = 0) -> None:
    """Push LE Identity Resolving Keys. Each entry: (addr, irk_hex[, addr_type]).
    Needs root."""
    blob = b""
    for addr, irk_hex, *rest in entries:
        kb = bytes.fromhex(irk_hex.replace(":", "").replace(" ", ""))
        if len(kb) != 16:
            die("IRK must be 16 bytes / 32 hex chars")
        at = rest[0] if rest else MGMT_ADDR_LE_RANDOM
        blob += bdaddr_to_bytes(addr, little_endian=True) + bytes([at]) + kb
    param = struct.pack("<H", len(entries)) + blob
    _mgmt_cmd(MGMT_OP_LOAD_IRKS, param, hci_index)
    ok(f"MGMT: loaded {len(entries)} IRK(s) into hci{hci_index}")


def mgmt_set_powered(on: bool = True, hci_index: int = 0) -> None:
    """Power the controller on/off via MGMT (used when bluetoothd is stopped)."""
    try:
        _mgmt_cmd(MGMT_OP_SET_POWERED, bytes([1 if on else 0]), hci_index)
    except SystemExit:
        warn("MGMT Set Powered failed (already in that state?) — continuing")


# --------------------------------------------------------------------------- #
# MGMT Pair Device — connect to an advertising LE peer by address and run SMP
# ourselves. Used with bluetoothd STOPPED so the two don't race on the socket;
# the New LTK / New IRK events carry everything needed to write the bond file.
# --------------------------------------------------------------------------- #
def _ev_name(ev: int) -> str:
    return {
        MGMT_EV_CMD_COMPLETE: "cmd-complete", MGMT_EV_CMD_STATUS: "cmd-status",
        MGMT_EV_NEW_LTK: "new-ltk", MGMT_EV_DEVICE_CONNECTED: "connected",
        MGMT_EV_DEVICE_DISCONNECTED: "disconnected",
        MGMT_EV_CONNECT_FAILED: "connect-failed",
        MGMT_EV_USER_CONFIRMATION_REQUEST: "confirm-request",
        MGMT_EV_USER_PASSKEY_REQUEST: "passkey-request",
        MGMT_EV_AUTH_FAILED: "auth-failed", MGMT_EV_NEW_IRK: "new-irk",
        MGMT_EV_NEW_CSRK: "new-csrk",
    }.get(ev, f"ev-0x{ev:04x}")


def mgmt_pair_device(addr: str, addr_type: int = MGMT_ADDR_LE_RANDOM,
                     io_cap: int = 0x03, hci_index: int = 0,
                     timeout: float = 30.0, trace: bool = True) -> dict:
    """Pair with the LE peer at `addr` (io_cap 0x03 = NoInputNoOutput -> Just
    Works). Auto-accepts the confirmation. Returns
    {status, status_name, ltk, irk, events} where ltk/irk are dicts (or None).

    Needs root, and bluetoothd should be stopped first.
    """
    import socket
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    try:
        try:
            s.bind((HCI_DEV_NONE, HCI_CHANNEL_CONTROL))
        except OSError as e:
            die(f"cannot bind MGMT control socket ({e}); run as root")
        s.settimeout(1.0)

        peer = bdaddr_to_bytes(addr, little_endian=True)
        param = peer + bytes([addr_type, io_cap])
        s.send(struct.pack("<HHH", MGMT_OP_PAIR_DEVICE, hci_index, len(param)) + param)

        res: dict = {"status": None, "status_name": None, "ltk": None,
                     "irk": None, "events": [], "connected": False,
                     "disc_reason": None, "smp_seen": False}
        pair_cmd_done = False
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                pkt = s.recv(2048)
            except socket.timeout:
                continue
            if len(pkt) < 6:
                continue
            ev, _idx, plen = struct.unpack_from("<HHH", pkt, 0)
            body = pkt[6:6 + plen]
            res["events"].append(_ev_name(ev))
            if trace:
                info(f"  mgmt: {_ev_name(ev)}")

            if ev in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS) and len(body) >= 3:
                op, st = struct.unpack_from("<HB", body, 0)
                if op == MGMT_OP_PAIR_DEVICE:
                    if ev == MGMT_EV_CMD_STATUS and st == 0:
                        continue  # accepted, pairing in progress
                    res["status"] = st
                    res["status_name"] = _MGMT_STATUS.get(st, f"0x{st:02x}")
                    if st != 0 or res["smp_seen"]:
                        break
                    # Command Complete with status 0 (success), but no SMP
                    # activity observed yet. Seen on real hardware with the
                    # Option A LE-legacy-OOB kernel patches: this can arrive
                    # *before* MGMT_EV_NEW_LTK -- the actual SMP exchange and
                    # Encryption Change complete a moment later. Don't treat
                    # "success" as done until the LTK actually shows up (or
                    # the timeout elapses), or a real, working pairing gets
                    # reported as a false failure. pair_cmd_done + the
                    # MGMT_EV_NEW_LTK branch below break out as soon as both
                    # signals are in, instead of always spinning to timeout.
                    pair_cmd_done = True
                    continue

            elif ev == MGMT_EV_DEVICE_CONNECTED:
                res["connected"] = True

            elif ev == MGMT_EV_DEVICE_DISCONNECTED and len(body) >= 8:
                res["disc_reason"] = body[7]

            elif ev == MGMT_EV_USER_CONFIRMATION_REQUEST and len(body) >= 7:
                res["smp_seen"] = True
                rep = body[0:7]  # address(6) + address_type(1)
                s.send(struct.pack("<HHH", MGMT_OP_USER_CONFIRMATION_REPLY,
                                   hci_index, len(rep)) + rep)
                if trace:
                    info("  mgmt: -> user confirmation reply (Just Works accept)")

            elif ev == MGMT_EV_USER_PASSKEY_REQUEST and len(body) >= 7:
                rep = body[0:7]
                s.send(struct.pack("<HHH", MGMT_OP_USER_PASSKEY_NEG_REPLY,
                                   hci_index, len(rep)) + rep)
                warn("  mgmt: keyboard asked for a passkey — we cannot supply one")

            elif ev == MGMT_EV_NEW_LTK and len(body) >= 37:
                res["smp_seen"] = True
                k = body[1:]
                res["ltk"] = {
                    "addr": bytes_to_bdaddr(k[0:6], little_endian=True),
                    "addr_type": k[6], "key_type": k[7], "central": k[8],
                    "enc_size": k[9],
                    "ediv": struct.unpack_from("<H", k, 10)[0],
                    "rand": struct.unpack_from("<Q", k, 12)[0],
                    "val": k[20:36].hex().upper(),
                }
                if pair_cmd_done:
                    # Pair Device already reported success; give a short
                    # grace period for a possible IRK (sent separately, may
                    # follow the LTK) instead of either breaking immediately
                    # (could miss it) or spinning to the full timeout.
                    end = min(end, time.monotonic() + 1.5)

            elif ev == MGMT_EV_NEW_IRK and len(body) >= 30:
                k = body[7:]
                res["irk"] = {
                    "addr": bytes_to_bdaddr(k[0:6], little_endian=True),
                    "addr_type": k[6], "val": k[7:23].hex().upper(),
                }
                if pair_cmd_done and res["ltk"]:
                    break

            elif ev == MGMT_EV_AUTH_FAILED and len(body) >= 8:
                res["status"] = body[7]
                res["status_name"] = "auth-failed:" + _MGMT_STATUS.get(
                    body[7], f"0x{body[7]:02x}")
                break

        if res["status"] is None:
            res["status"] = 0xFF
            res["status_name"] = "timeout"

        # Anything short of a real bond (an LTK actually landed) means the
        # kernel's own MGMT_OP_PAIR_DEVICE bonding request and/or the
        # underlying LE connection may still be alive and unattended --
        # closing this raw HCI socket does NOT cancel either; they are kernel
        # state, not socket state. Left alone, a subsequent attempt against
        # the same peer starts while that state is still there, which is
        # what produced the same-boot degradation (leaked connection/SMP
        # state, "ACL packet for unknown connection handle" in dmesg) that a
        # reboot was previously the only known fix for. Explicitly cancel the
        # pairing request and force a disconnect before giving up, mirroring
        # what a clean boot's absence of prior state achieves.
        if not res["ltk"]:
            addr_info = peer + bytes([addr_type])
            for op in (MGMT_OP_CANCEL_PAIR_DEVICE, MGMT_OP_DISCONNECT):
                try:
                    s.send(struct.pack("<HHH", op, hci_index, len(addr_info))
                           + addr_info)
                except OSError:
                    break
            if trace:
                info("  mgmt: pairing not confirmed bonded -> "
                     "cancel + disconnect (avoid leaking kernel conn state)")
            drain_end = time.monotonic() + 1.0
            while time.monotonic() < drain_end:
                try:
                    s.recv(2048)
                except socket.timeout:
                    break
                except OSError:
                    break

        return res
    finally:
        s.close()


_MGMT_STATUS = {
    0x00: "success", 0x01: "unknown-command", 0x02: "not-connected",
    0x03: "failed", 0x04: "connect-failed", 0x05: "auth-failed",
    0x06: "not-paired", 0x07: "no-resources", 0x08: "timeout",
    0x09: "already-connected", 0x0a: "busy", 0x0b: "rejected",
    0x0c: "not-supported", 0x0d: "invalid-params", 0x0e: "disconnected",
    0x0f: "not-powered", 0x10: "cancelled", 0x11: "invalid-index",
}
