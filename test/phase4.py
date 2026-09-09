#!/usr/bin/env python3
"""Phase-4 bonded GATT provisioning for the Modern Keyboard, over a raw L2CAP
ATT socket (CID 4) — no bluetoothd, no controller seizure.

Run AFTER a Phase-0 pair (the kernel holds the LTK). Minimal sequence that makes
the keyboard adopt its F3 address (verified on hardware — see PROGRESS.md):

  connect L2CAP ATT (encrypted with the stored LTK) -> Exchange MTU
  -> subscribe the 9 report/vendor CCCDs -> hold until the keyboard drops the
     link (~7 s while USB is attached)

The BOND-COMPLETION.md 0x0038 / 0x0041 writes and MS-accessory reads are NOT
load-bearing for adoption; --writes / --read-msacc / --discover re-enable them.
Importable: phase4.run(adapter, kbd, ...) -> dict.
"""
import argparse
import os
import socket
import struct
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
MKBD = os.environ.get("MKBD") or os.path.join(_here, "..", "..", "modernkeyboard")
sys.path.insert(0, os.path.join(os.path.abspath(MKBD), "lib"))
import mkbd_common as m  # noqa: E402

ATT_ERROR_RSP         = 0x01
ATT_EXCHANGE_MTU_REQ  = 0x02
ATT_EXCHANGE_MTU_RSP  = 0x03
ATT_READ_BY_TYPE_REQ  = 0x08
ATT_READ_BY_TYPE_RSP  = 0x09
ATT_READ_REQ          = 0x0A
ATT_READ_RSP          = 0x0B
ATT_READ_BY_GROUP_REQ = 0x10
ATT_READ_BY_GROUP_RSP = 0x11
ATT_WRITE_REQ         = 0x12
ATT_WRITE_RSP         = 0x13
ATT_HANDLE_VALUE_NTF  = 0x1B
ATT_WRITE_CMD         = 0x52
UUID_PRIMARY_SERVICE  = 0x2800
UUID_CHARACTERISTIC   = 0x2803
BT_SECURITY = 4
BT_SECURITY_HIGH = 3

# keyboard's own GATT DB (adapter-independent; matches BOND-COMPLETION.md)
CCCD_HANDLES = [0x0017, 0x001d, 0x0021, 0x0025, 0x0029, 0x002d, 0x0031, 0x0035, 0x000c]
H_LED, H_FEATURE, H_NOTIFY = 0x0038, 0x0041, 0x0024
H_MSACC = [0x000e, 0x000b]
DROP = (ConnectionResetError, BrokenPipeError, OSError)


class ATT:
    def __init__(self, sock):
        self.s = sock
        self.ntf = []

    def _txn(self, req, want):
        self.s.send(req)
        while True:
            pkt = self.s.recv(512)
            if not pkt:
                raise OSError("ATT link closed")
            if pkt[0] == ATT_HANDLE_VALUE_NTF:
                self.ntf.append((struct.unpack_from("<H", pkt, 1)[0], pkt[3:]))
                continue
            if pkt[0] == ATT_ERROR_RSP:
                ro, h, ec = struct.unpack_from("<BHB", pkt, 1)
                raise OSError(f"ATT err req 0x{ro:02x} h 0x{h:04x} code 0x{ec:02x}")
            if pkt[0] == want:
                return pkt

    def exchange_mtu(self, mtu=517):
        self._txn(struct.pack("<BH", ATT_EXCHANGE_MTU_REQ, mtu), ATT_EXCHANGE_MTU_RSP)

    def read(self, h):
        return self._txn(struct.pack("<BH", ATT_READ_REQ, h), ATT_READ_RSP)[1:]

    def write_req(self, h, v):
        self._txn(struct.pack("<BH", ATT_WRITE_REQ, h) + v, ATT_WRITE_RSP)

    def write_cmd(self, h, v):
        self.s.send(struct.pack("<BH", ATT_WRITE_CMD, h) + v)

    def _sweep(self, op_req, op_rsp, extra=b""):
        start, out = 0x0001, []
        while start <= 0xFFFF:
            try:
                rsp = self._txn(struct.pack("<BHH", op_req, start, 0xFFFF) + extra, op_rsp)
            except OSError:
                break
            ln, body = rsp[1], rsp[2:]
            if not body:
                break
            last = start
            for i in range(0, len(body), ln):
                e = body[i:i + ln]
                if len(e) < ln:
                    break
                out.append(e)
                last = struct.unpack_from("<H", e, 0)[0]
            start = last + 1
        return out

    def discover(self):
        svcs = [struct.unpack_from("<HH", e, 0) + (e[4:],)
                for e in self._sweep(ATT_READ_BY_GROUP_REQ, ATT_READ_BY_GROUP_RSP,
                                     struct.pack("<H", UUID_PRIMARY_SERVICE))]
        chars = [(struct.unpack_from("<H", e, 0)[0], e[2],
                  struct.unpack_from("<H", e, 3)[0], e[5:])
                 for e in self._sweep(ATT_READ_BY_TYPE_REQ, ATT_READ_BY_TYPE_RSP,
                                      struct.pack("<H", UUID_CHARACTERISTIC))]
        return svcs, chars


def _u(u):
    return f"{struct.unpack('<H', u)[0]:04x}" if len(u) == 2 else u[::-1].hex()


def _connect(adapter, kbd, timeout=15.0):
    last = None
    for st in (1, 0):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                          socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_BLUETOOTH, BT_SECURITY,
                     struct.pack("BB", BT_SECURITY_HIGH, 0))
        try:
            s.bind((adapter, 0, 4, st))
            s.settimeout(timeout)
            s.connect((kbd, 0, 4, 2))
            s.settimeout(8.0)
            return s
        except OSError as e:
            last = e
            s.close()
    raise last


def _f1_current_addr(adapter):
    devs = [d for d in m.find_hidraw() if d.iface == m.VENDOR_IFACE]
    fd = os.open(devs[0].node, os.O_RDWR | os.O_NONBLOCK)
    try:
        st, d = m.col03_command(fd, 0xF1, m.bdaddr_to_bytes(adapter, little_endian=True))
    finally:
        os.close(fd)
    if st == 0 and len(d) >= 7:
        return bool(d[0]), m.bytes_to_bdaddr(d[1:7], little_endian=True)
    return None, None


def run(adapter, kbd, *, hci="hci0", rounds=1, hold=8.0, verbose=False,
        discover=False, writes=False, read_msacc=False, cccd=None,
        f3_check=True, led=H_LED, feature=H_FEATURE, notify=H_NOTIFY,
        led_writes=3, msacc=None, log=print):
    """Returns {"cccd_ok": int, "held": float, "adopted": bool|None}."""
    adapter, kbd = adapter.upper(), kbd.upper()
    cccds = cccd or CCCD_HANDLES

    def v(msg):
        if verbose:
            log(msg)

    token = 0x00000101
    cccd_ok, held = 0, 0.0
    for rnd in range(1, rounds + 1):
        try:
            s = _connect(adapter, kbd)
        except OSError as e:
            v(f"   L2CAP connect failed: {e}")
            time.sleep(3)
            continue
        att = ATT(s)
        t0 = time.monotonic()
        try:
            att.exchange_mtu()
            if discover and rnd == 1:
                svcs, chars = att.discover()
                log(f":: GATT DB — {len(svcs)} services, {len(chars)} chars")
                for h, e, u in svcs:
                    log(f"   svc 0x{h:04x}-0x{e:04x}  {_u(u)}")
                for dh, pr, vh, u in chars:
                    log(f"   chr 0x{dh:04x} props 0x{pr:02x} val 0x{vh:04x}  {_u(u)}")
            n = 0
            for h in cccds:
                try:
                    att.write_req(h, b"\x01\x00")
                    n += 1
                except DROP as e:
                    v(f"   CCCD 0x{h:04x}: {e}")
                    break
            cccd_ok = max(cccd_ok, n)
            v(f"   subscribed {n}/{len(cccds)} CCCDs")
            if writes:
                try:
                    for _ in range(led_writes):
                        att.write_cmd(led, b"\x01")
                        time.sleep(0.03)
                    body = struct.pack("<BBI", 0xE2, 0x06, token) + b"\x00" * 13
                    att.write_req(feature, body)
                    v(f"   LED 0x{led:04x} <- 01 x{led_writes}; "
                      f"Feature 0x{feature:04x} <- {body.hex()}")
                    end = time.monotonic() + 2
                    while time.monotonic() < end and not any(
                            h in (H_NOTIFY, notify) for h, _ in att.ntf):
                        try:
                            att.s.settimeout(0.4)
                            p = att.s.recv(512)
                            if p and p[0] == ATT_HANDLE_VALUE_NTF:
                                att.ntf.append((struct.unpack_from("<H", p, 1)[0], p[3:]))
                        except (socket.timeout, TimeoutError):
                            pass
                    for h, val in att.ntf:
                        v(f"   NTF 0x{h:04x}: {val.hex()}")
                    token += 0x00010000
                except DROP as e:
                    v(f"   writes dropped: {e}")
            if read_msacc:
                for h in (msacc or H_MSACC):
                    try:
                        v(f"   msacc 0x{h:04x} -> {att.read(h).hex()}")
                    except DROP as e:
                        v(f"   msacc 0x{h:04x}: {e}")
            while time.monotonic() - t0 < hold:
                try:
                    s.settimeout(1.0)
                    p = s.recv(512)
                    if p and p[0] == ATT_HANDLE_VALUE_NTF:
                        v(f"   NTF 0x{struct.unpack_from('<H', p, 1)[0]:04x}: {p[3:].hex()}")
                except (socket.timeout, TimeoutError):
                    pass
                except OSError:
                    break
        except DROP as e:
            v(f"   round {rnd}: dropped ({e})")
        finally:
            held = time.monotonic() - t0
            s.close()
        if rnd < rounds:
            time.sleep(3)

    adopted = None
    if f3_check:
        try:
            be, cur = _f1_current_addr(adapter)
            adopted = bool(be) and (cur or "").upper() == kbd
            v(f"   F1: bond_exists={be} current_addr={cur}")
        except Exception as e:
            v(f"   F1 re-read error: {e}")
    return {"cccd_ok": cccd_ok, "held": held, "adopted": adopted}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kbd", help="keyboard LE static-random address (the F3 addr)")
    ap.add_argument("--adapter")
    ap.add_argument("--hci", default="hci0")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--hold", type=float, default=8.0)
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--writes", action="store_true")
    ap.add_argument("--read-msacc", action="store_true")
    ap.add_argument("--f3-check", action="store_true")
    ap.add_argument("--cccd", type=lambda s: [int(x, 0) for x in s.split(",")])
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root")
    adapter = (args.adapter or m.adapter_bdaddr(args.hci)).upper()
    r = run(adapter, args.kbd, hci=args.hci, rounds=args.rounds, hold=args.hold,
            verbose=True, discover=args.discover, writes=args.writes,
            read_msacc=args.read_msacc, cccd=args.cccd, f3_check=args.f3_check)
    print(f":: {r['cccd_ok']} CCCDs, link held {r['held']:.1f}s, "
          f"adopted={r['adopted']}")


if __name__ == "__main__":
    main()
