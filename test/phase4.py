#!/usr/bin/env python3
"""Phase-4 bonded GATT provisioning for the Modern Keyboard, over a raw L2CAP
ATT socket (CID 4) — no bluetoothd, no controller seizure.

Run AFTER a Phase-0 pair (kernel has the LTK). Sequence per
modernkeyboard/docs/BOND-COMPLETION.md "Phase 4":

  1. connect L2CAP ATT, encrypted with the stored LTK (BT_SECURITY_HIGH)
  2. Exchange MTU; full GATT discovery (printed)
  3. subscribe CCCDs: Write Req `01 00` to the HID Report + vendor CCCDs
  4. Write Cmd  -> HID Report handle 0x0038 : `01`   (Report ID 1 Output / LED)
  5. Write Req  -> HID Report handle 0x0041 : `E2 06 <tok:4> <13x 00>` (Feature 0x24)
  6. expect HVN on 0x0024 : `e2 00 0a 00 ...` (token echoed + 0x00010000)
  7. Read handles 0x000e / 0x000b (MS accessory vendor service)
  8. hold ~7 s, disconnect
  9. reconnect on the LTK (2nd encryption = NVM flush), brief hold
 10. re-read USB F1 -> did current_addr advance to the F3 address?

Handles default to the BOND-COMPLETION.md values (the keyboard's own GATT DB,
independent of the local adapter); discovery output lets you spot a mismatch.
"""
import argparse
import os
import struct
import subprocess
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
MKBD = os.environ.get("MKBD") or os.path.join(_here, "..", "..", "modernkeyboard")
sys.path.insert(0, os.path.join(os.path.abspath(MKBD), "lib"))
import mkbd_common as m  # noqa: E402

import socket

# ---- ATT opcodes ----------------------------------------------------------
ATT_ERROR_RSP          = 0x01
ATT_EXCHANGE_MTU_REQ   = 0x02
ATT_EXCHANGE_MTU_RSP   = 0x03
ATT_FIND_INFO_REQ      = 0x04
ATT_FIND_INFO_RSP      = 0x05
ATT_READ_BY_TYPE_REQ   = 0x08
ATT_READ_BY_TYPE_RSP   = 0x09
ATT_READ_REQ           = 0x0A
ATT_READ_RSP           = 0x0B
ATT_READ_BY_GROUP_REQ  = 0x10
ATT_READ_BY_GROUP_RSP  = 0x11
ATT_WRITE_REQ          = 0x12
ATT_WRITE_RSP          = 0x13
ATT_HANDLE_VALUE_NTF   = 0x1B
ATT_WRITE_CMD          = 0x52

UUID_PRIMARY_SERVICE = 0x2800
UUID_CHARACTERISTIC  = 0x2803
UUID_CCCD            = 0x2902
UUID_REPORT          = 0x2A4D
UUID_REPORT_REF      = 0x2908

BT_SECURITY = 4          # SOL_BLUETOOTH option
BT_SECURITY_HIGH = 3

# BOND-COMPLETION.md defaults
CCCD_HANDLES = [0x0017, 0x001d, 0x0021, 0x0025, 0x0029, 0x002d, 0x0031, 0x0035, 0x000c]
H_LED      = 0x0038        # Report ID 1 / Output
H_FEATURE  = 0x0041        # Report ID 0x24 / Feature
H_NOTIFY   = 0x0024        # keyboard's reply notification
H_MSACC    = [0x000e, 0x000b]


class ATT:
    def __init__(self, sock):
        self.s = sock
        self.mtu = 23
        self.ntf = []            # collected (handle, bytes)

    def _txn(self, req: bytes, want_op: int) -> bytes:
        self.s.send(req)
        while True:
            pkt = self.s.recv(512)
            if not pkt:
                raise IOError("ATT link closed")
            op = pkt[0]
            if op == ATT_HANDLE_VALUE_NTF:
                h = struct.unpack_from("<H", pkt, 1)[0]
                self.ntf.append((h, pkt[3:]))
                continue
            if op == ATT_ERROR_RSP:
                req_op, h, ec = struct.unpack_from("<BHB", pkt, 1)
                raise IOError(f"ATT error: req 0x{req_op:02x} handle 0x{h:04x} "
                              f"code 0x{ec:02x}")
            if op == want_op:
                return pkt
            # unexpected but non-fatal: keep reading
            print(f"   (att: unexpected op 0x{op:02x})")

    def exchange_mtu(self, mtu=517):
        rsp = self._txn(struct.pack("<BH", ATT_EXCHANGE_MTU_REQ, mtu),
                        ATT_EXCHANGE_MTU_RSP)
        self.mtu = min(mtu, struct.unpack_from("<H", rsp, 1)[0])
        return self.mtu

    def read(self, handle: int) -> bytes:
        rsp = self._txn(struct.pack("<BH", ATT_READ_REQ, handle), ATT_READ_RSP)
        return rsp[1:]

    def write_req(self, handle: int, val: bytes):
        self._txn(struct.pack("<BH", ATT_WRITE_REQ, handle) + val, ATT_WRITE_RSP)

    def write_cmd(self, handle: int, val: bytes):
        self.s.send(struct.pack("<BH", ATT_WRITE_CMD, handle) + val)

    def discover(self):
        """Return (services, chars, descriptors) as printable dicts."""
        services, chars, descs = [], [], []
        # primary services
        start = 0x0001
        while start <= 0xFFFF:
            try:
                rsp = self._txn(struct.pack("<BHHH", ATT_READ_BY_GROUP_REQ,
                                            start, 0xFFFF, UUID_PRIMARY_SERVICE),
                                ATT_READ_BY_GROUP_RSP)
            except IOError:
                break
            ln = rsp[1]
            body = rsp[2:]
            for i in range(0, len(body), ln):
                e = body[i:i+ln]
                h, end = struct.unpack_from("<HH", e, 0)
                uuid = e[4:]
                services.append((h, end, uuid))
                start = end + 1
            if ln == 0 or start == 0 or not body:
                break
        # characteristics (sweep 0x0001..0xffff)
        start = 0x0001
        while start <= 0xFFFF:
            try:
                rsp = self._txn(struct.pack("<BHHH", ATT_READ_BY_TYPE_REQ,
                                            start, 0xFFFF, UUID_CHARACTERISTIC),
                                ATT_READ_BY_TYPE_RSP)
            except IOError:
                break
            ln = rsp[1]
            body = rsp[2:]
            last = start
            for i in range(0, len(body), ln):
                e = body[i:i+ln]
                decl_h = struct.unpack_from("<H", e, 0)[0]
                props = e[2]
                val_h = struct.unpack_from("<H", e, 3)[0]
                uuid = e[5:]
                chars.append((decl_h, props, val_h, uuid))
                last = decl_h
            start = last + 1
            if not body:
                break
        # descriptors
        start = 0x0001
        while start <= 0xFFFF:
            try:
                rsp = self._txn(struct.pack("<BHH", ATT_FIND_INFO_REQ,
                                            start, 0xFFFF),
                                ATT_FIND_INFO_RSP)
            except IOError:
                break
            fmt = rsp[1]
            body = rsp[2:]
            esz = 4 if fmt == 1 else 18
            last = start
            for i in range(0, len(body), esz):
                e = body[i:i+esz]
                if len(e) < esz:
                    break
                h = struct.unpack_from("<H", e, 0)[0]
                uuid = e[2:]
                descs.append((h, uuid))
                last = h
            start = last + 1
            if not body:
                break
        return services, chars, descs


def _u(uuid: bytes) -> str:
    if len(uuid) == 2:
        return f"{struct.unpack('<H', uuid)[0]:04x}"
    return uuid[::-1].hex()


def connect_att(adapter: str, kbd: str, timeout=15.0, src_type=1, dst_type=2):
    # (bdaddr, psm, cid, bdaddr_type): src_type 1 = LE public (adapter),
    # dst_type 2 = LE random (keyboard static-random).
    last = None
    for st in (src_type, 0):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                          socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_BLUETOOTH, BT_SECURITY,
                     struct.pack("BB", BT_SECURITY_HIGH, 0))
        try:
            s.bind((adapter, 0, 4, st))
            s.settimeout(timeout)
            s.connect((kbd, 0, 4, dst_type))
            s.settimeout(8.0)
            return s
        except OSError as e:
            last = e
            s.close()
    raise last


DROP = (ConnectionResetError, BrokenPipeError, OSError, IOError)


def provision(att: ATT, token: int, args):
    """Returns True if all steps ran, False if the keyboard dropped the link
    partway (which is expected ~7 s in while USB is attached)."""
    print(":: subscribe CCCDs")
    for h in (args.cccd or CCCD_HANDLES):
        try:
            att.write_req(h, b"\x01\x00")
            print(f"   CCCD 0x{h:04x} <- 01 00  ok")
        except DROP as e:
            print(f"   CCCD 0x{h:04x} : {e}")
            return False

    if args.msacc is not None or not args.no_msacc:
        print(":: read MS accessory vendor chars")
        for h in (args.msacc or H_MSACC):
            try:
                print(f"   0x{h:04x} -> {att.read(h).hex()}")
            except DROP as e:
                print(f"   0x{h:04x} : {e}")
                return False

    if not args.no_writes:
        print(":: LED / Output write (handle 0x%04x)" % args.led)
        try:
            for _ in range(args.led_writes):
                att.write_cmd(args.led, b"\x01")
                time.sleep(0.05)
            print(f"   wrote 01 x{args.led_writes}")
        except DROP as e:
            print(f"   dropped: {e}")
            return False

        body = struct.pack("<BBI", 0xE2, 0x06, token) + b"\x00" * 13
        assert len(body) == 19
        print(f":: Feature 0x24 write (handle 0x{args.feature:04x}) : {body.hex()}")
        try:
            att.write_req(args.feature, body)
            print("   write ok")
        except DROP as e:
            print(f"   write dropped: {e}")
            return False

        # give the keyboard a moment to answer with its notification
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(
                h in (H_NOTIFY, args.notify) for h, _ in att.ntf):
            try:
                att.s.settimeout(0.5)
                pkt = att.s.recv(512)
                if pkt and pkt[0] == ATT_HANDLE_VALUE_NTF:
                    h = struct.unpack_from("<H", pkt, 1)[0]
                    att.ntf.append((h, pkt[3:]))
            except (socket.timeout, TimeoutError):
                pass
            except OSError:
                break
        for h, v in att.ntf:
            print(f"   NTF 0x{h:04x}: {v.hex()}")
            if h in (H_NOTIFY, args.notify) and v[:1] == b"\xe2":
                echoed = struct.unpack_from("<I", v, 4)[0] if len(v) >= 8 else None
                print(f"       (token echo {echoed:#010x}, "
                      f"sent {token + 0x00010000:#010x}, "
                      f"match={echoed == (token + 0x00010000)})")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kbd", help="keyboard LE static-random address (the F3 addr)")
    ap.add_argument("--adapter", help="local adapter bdaddr (default hci0's)")
    ap.add_argument("--hci", default="hci0")
    ap.add_argument("--f3-check", action="store_true",
                    help="after the dance, re-read USB F1 and report address adoption")
    ap.add_argument("--hold", type=float, default=7.0)
    ap.add_argument("--led", type=lambda x: int(x, 0), default=H_LED)
    ap.add_argument("--feature", type=lambda x: int(x, 0), default=H_FEATURE)
    ap.add_argument("--notify", type=lambda x: int(x, 0), default=H_NOTIFY)
    ap.add_argument("--led-writes", type=int, default=3)
    ap.add_argument("--cccd", type=lambda s: [int(x, 0) for x in s.split(",")])
    ap.add_argument("--msacc", type=lambda s: [int(x, 0) for x in s.split(",")])
    ap.add_argument("--no-writes", action="store_true",
                    help="skip the LED / Feature-0x24 writes (CCCD subs only)")
    ap.add_argument("--no-msacc", action="store_true",
                    help="skip the MS-accessory reads")
    ap.add_argument("--rounds", type=int, default=2,
                    help="connect/provision/hold/disconnect rounds (default 2)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root")
    adapter = (args.adapter or m.adapter_bdaddr(args.hci)).upper()
    kbd = args.kbd.upper()
    print(f":: adapter {adapter}   keyboard {kbd}")

    token = 0x00000101
    for rnd in range(1, args.rounds + 1):
        print(f"\n===== round {rnd}/{args.rounds} =====")
        try:
            s = connect_att(adapter, kbd)
        except OSError as e:
            print(f"   L2CAP connect failed: {e}")
            time.sleep(3)
            continue
        att = ATT(s)
        t_conn = time.monotonic()
        try:
            print(f":: MTU {att.exchange_mtu()}")
            if rnd == 1:
                svcs, chars, descs = att.discover()
                print(f":: discovered {len(svcs)} services, {len(chars)} chars, "
                      f"{len(descs)} descriptors")
                for h, end, u in svcs:
                    print(f"   svc 0x{h:04x}-0x{end:04x}  {_u(u)}")
                for dh, pr, vh, u in chars:
                    print(f"   chr decl 0x{dh:04x} props 0x{pr:02x} "
                          f"val 0x{vh:04x}  {_u(u)}")
                for h, u in descs:
                    if _u(u) in ("2902", "2908"):
                        print(f"   dsc 0x{h:04x}  {_u(u)}")
            done = provision(att, token, args)
            token += 0x00010000
            if done:
                print(f":: provisioning done; holding up to {args.hold:.0f}s "
                      f"for the keyboard to drop the link")
                t0 = time.monotonic()
                while time.monotonic() - t0 < args.hold:
                    try:
                        s.settimeout(1.0)
                        pkt = s.recv(512)
                        if pkt and pkt[0] == ATT_HANDLE_VALUE_NTF:
                            h = struct.unpack_from("<H", pkt, 1)[0]
                            print(f"   NTF 0x{h:04x}: {pkt[3:].hex()}")
                    except (socket.timeout, TimeoutError):
                        pass
                    except OSError:
                        break
        except DROP as e:
            print(f"   round {rnd}: link dropped ({e})")
        finally:
            held = time.monotonic() - t_conn
            print(f":: round {rnd}: keyboard held the link {held:.1f}s")
            s.close()
        time.sleep(3)   # let the keyboard re-advertise before the next round

    if args.f3_check:
        print("\n=== address adoption (USB F1 re-read) ===")
        try:
            devs = [d for d in m.find_hidraw() if d.iface == m.VENDOR_IFACE]
            fd = os.open(devs[0].node, os.O_RDWR | os.O_NONBLOCK)
            try:
                st, d = m.col03_command(
                    fd, 0xF1, m.bdaddr_to_bytes(adapter, little_endian=True))
            finally:
                os.close(fd)
            if st == 0 and len(d) >= 7:
                cur = m.bytes_to_bdaddr(d[1:7], little_endian=True)
                print(f"   bond_exists={bool(d[0])}  current_addr={cur}")
                print("   ==> ADOPTED" if cur.upper() == kbd else
                      f"   ==> still not adopted (want {kbd})")
            else:
                print(f"   F1 status 0x{st:02x}")
        except Exception as e:
            print(f"   F1 re-read error: {e}")


if __name__ == "__main__":
    main()
