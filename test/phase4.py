#!/usr/bin/env python3
"""Phase-4 bonded GATT provisioning for the Modern Keyboard, over a raw L2CAP
ATT socket (CID 4) — no bluetoothd, no controller seizure.

Run AFTER a Phase-0 pair (the kernel holds the LTK). The minimal sequence that
makes the keyboard adopt its F3 address (established on hardware, see
PROGRESS.md):

  1. connect L2CAP ATT, encrypted with the stored LTK (BT_SECURITY_HIGH)
  2. Exchange MTU
  3. subscribe the 9 report/vendor CCCDs  (Write Req `01 00`)
  4. hold until the keyboard tears the link down (~7 s while USB is attached)

That's it. The BOND-COMPLETION.md `0x0038` / `0x0041` write sequence and the
MS-accessory reads are NOT load-bearing for adoption — opt in with --writes /
--read-msacc if you want to exercise them. --discover prints the GATT DB.
"""
import argparse
import os
import socket
import struct
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "lib"))
import mkbd_common as m  # noqa: E402

# ---- ATT ----------------------------------------------------------------
ATT_ERROR_RSP         = 0x01
ATT_EXCHANGE_MTU_REQ  = 0x02
ATT_EXCHANGE_MTU_RSP  = 0x03
ATT_FIND_INFO_REQ     = 0x04
ATT_FIND_INFO_RSP     = 0x05
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

UUID_PRIMARY_SERVICE = 0x2800
UUID_CHARACTERISTIC  = 0x2803

BT_SECURITY = 4
BT_SECURITY_HIGH = 3

# keyboard's GATT DB (adapter-independent; matches BOND-COMPLETION.md, verified
# on hardware). Battery CCCD 0x0017 + 7 HID-report CCCDs + vendor CCCD 0x000c.
CCCD_HANDLES = [0x0017, 0x001d, 0x0021, 0x0025, 0x0029, 0x002d, 0x0031, 0x0035, 0x000c]
H_LED     = 0x0038        # Report ID 1 / Output
H_FEATURE = 0x0041        # Report ID 0x24 / Feature
H_NOTIFY  = 0x0024
H_MSACC   = [0x000e, 0x000b]

DROP = (ConnectionResetError, BrokenPipeError, OSError)


class ATT:
    def __init__(self, sock):
        self.s = sock
        self.mtu = 23
        self.ntf = []

    def _txn(self, req, want_op):
        self.s.send(req)
        while True:
            pkt = self.s.recv(512)
            if not pkt:
                raise OSError("ATT link closed")
            op = pkt[0]
            if op == ATT_HANDLE_VALUE_NTF:
                self.ntf.append((struct.unpack_from("<H", pkt, 1)[0], pkt[3:]))
                continue
            if op == ATT_ERROR_RSP:
                ro, h, ec = struct.unpack_from("<BHB", pkt, 1)
                raise OSError(f"ATT error req 0x{ro:02x} handle 0x{h:04x} code 0x{ec:02x}")
            if op == want_op:
                return pkt
            print(f"   (att: unexpected op 0x{op:02x})")

    def exchange_mtu(self, mtu=517):
        rsp = self._txn(struct.pack("<BH", ATT_EXCHANGE_MTU_REQ, mtu), ATT_EXCHANGE_MTU_RSP)
        self.mtu = min(mtu, struct.unpack_from("<H", rsp, 1)[0])
        return self.mtu

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
            ln = rsp[1]
            body = rsp[2:]
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


def connect_att(adapter, kbd, timeout=15.0):
    last = None
    for st in (1, 0):                         # src type: LE public, then BR/EDR
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                          socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_BLUETOOTH, BT_SECURITY,
                     struct.pack("BB", BT_SECURITY_HIGH, 0))
        try:
            s.bind((adapter, 0, 4, st))
            s.settimeout(timeout)
            s.connect((kbd, 0, 4, 2))         # dst: LE random (static)
            s.settimeout(8.0)
            return s
        except OSError as e:
            last = e
            s.close()
    raise last


def subscribe_cccds(att, handles):
    ok = 0
    for h in handles:
        try:
            att.write_req(h, b"\x01\x00")
            ok += 1
        except DROP as e:
            print(f"   CCCD 0x{h:04x}: {e}")
            return ok
    print(f"   subscribed {ok}/{len(handles)} CCCDs")
    return ok


def do_writes(att, token, args):
    try:
        for _ in range(args.led_writes):
            att.write_cmd(args.led, b"\x01")
            time.sleep(0.03)
        print(f"   LED 0x{args.led:04x} <- 01 x{args.led_writes}")
        body = struct.pack("<BBI", 0xE2, 0x06, token) + b"\x00" * 13
        att.write_req(args.feature, body)
        print(f"   Feature 0x{args.feature:04x} <- {body.hex()}")
        end = time.monotonic() + 2
        while time.monotonic() < end and not any(h in (H_NOTIFY, args.notify)
                                                 for h, _ in att.ntf):
            try:
                att.s.settimeout(0.4)
                p = att.s.recv(512)
                if p and p[0] == ATT_HANDLE_VALUE_NTF:
                    att.ntf.append((struct.unpack_from("<H", p, 1)[0], p[3:]))
            except (socket.timeout, TimeoutError):
                pass
        for h, v in att.ntf:
            print(f"   NTF 0x{h:04x}: {v.hex()}")
    except DROP as e:
        print(f"   writes dropped: {e}")


def f1_adoption(adapter, kbd):
    print("\n=== address adoption (USB F1 re-read) ===")
    try:
        devs = [d for d in m.find_hidraw() if d.iface == m.VENDOR_IFACE]
        fd = os.open(devs[0].node, os.O_RDWR | os.O_NONBLOCK)
        try:
            st, d = m.col03_command(fd, 0xF1,
                                    m.bdaddr_to_bytes(adapter, little_endian=True))
        finally:
            os.close(fd)
        if st == 0 and len(d) >= 7:
            cur = m.bytes_to_bdaddr(d[1:7], little_endian=True)
            print(f"   bond_exists={bool(d[0])}  current_addr={cur}")
            print("   ==> ADOPTED" if cur.upper() == kbd.upper()
                  else f"   ==> NOT adopted (want {kbd})")
        else:
            print(f"   F1 status 0x{st:02x}")
    except Exception as e:
        print(f"   F1 re-read error: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kbd", help="keyboard LE static-random address (the F3 addr)")
    ap.add_argument("--adapter", help="local adapter bdaddr (default hci0's)")
    ap.add_argument("--hci", default="hci0")
    ap.add_argument("--rounds", type=int, default=1,
                    help="connect/subscribe/hold rounds (default 1 — enough)")
    ap.add_argument("--hold", type=float, default=8.0,
                    help="max seconds to wait for the keyboard to drop the link")
    ap.add_argument("--discover", action="store_true", help="print the GATT DB")
    ap.add_argument("--writes", action="store_true",
                    help="also do the LED / Feature-0x24 writes (not needed)")
    ap.add_argument("--read-msacc", action="store_true",
                    help="also read the MS-accessory chars (not needed)")
    ap.add_argument("--f3-check", action="store_true",
                    help="re-read USB F1 afterwards and report address adoption")
    ap.add_argument("--cccd", type=lambda s: [int(x, 0) for x in s.split(",")],
                    help="override the CCCD handle list (comma-separated)")
    ap.add_argument("--led", type=lambda x: int(x, 0), default=H_LED)
    ap.add_argument("--feature", type=lambda x: int(x, 0), default=H_FEATURE)
    ap.add_argument("--notify", type=lambda x: int(x, 0), default=H_NOTIFY)
    ap.add_argument("--led-writes", type=int, default=3)
    ap.add_argument("--msacc", type=lambda s: [int(x, 0) for x in s.split(",")])
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root")
    adapter = (args.adapter or m.adapter_bdaddr(args.hci)).upper()
    kbd = args.kbd.upper()
    cccds = args.cccd or CCCD_HANDLES
    print(f":: phase4  adapter {adapter}  keyboard {kbd}  "
          f"rounds={args.rounds} hold={args.hold:g}s")

    token = 0x00000101
    for rnd in range(1, args.rounds + 1):
        tag = f"round {rnd}/{args.rounds}" if args.rounds > 1 else "connect"
        try:
            s = connect_att(adapter, kbd)
        except OSError as e:
            print(f":: {tag}: L2CAP connect failed: {e}")
            time.sleep(3)
            continue
        att = ATT(s)
        t0 = time.monotonic()
        try:
            att.exchange_mtu()
            if args.discover and rnd == 1:
                svcs, chars = att.discover()
                print(f":: GATT DB — {len(svcs)} services, {len(chars)} chars")
                for h, e, u in svcs:
                    print(f"   svc 0x{h:04x}-0x{e:04x}  {_u(u)}")
                for dh, pr, vh, u in chars:
                    print(f"   chr 0x{dh:04x} props 0x{pr:02x} val 0x{vh:04x}  {_u(u)}")
            subscribe_cccds(att, cccds)
            if args.writes:
                do_writes(att, token, args)
                token += 0x00010000
            if args.read_msacc:
                for h in (args.msacc or H_MSACC):
                    try:
                        print(f"   msacc 0x{h:04x} -> {att.read(h).hex()}")
                    except DROP as e:
                        print(f"   msacc 0x{h:04x}: {e}")
            # hold until the keyboard drops the link (its own ~7s teardown is
            # what commits the bond)
            while time.monotonic() - t0 < args.hold:
                try:
                    s.settimeout(1.0)
                    p = s.recv(512)
                    if p and p[0] == ATT_HANDLE_VALUE_NTF:
                        print(f"   NTF 0x{struct.unpack_from('<H', p, 1)[0]:04x}: "
                              f"{p[3:].hex()}")
                except (socket.timeout, TimeoutError):
                    pass
                except OSError:
                    break
        except DROP as e:
            print(f":: {tag}: link dropped ({e})")
        finally:
            print(f":: {tag}: keyboard held the link "
                  f"{time.monotonic() - t0:.1f}s")
            s.close()
        if rnd < args.rounds:
            time.sleep(3)

    if args.f3_check:
        f1_adoption(adapter, kbd)


if __name__ == "__main__":
    main()
