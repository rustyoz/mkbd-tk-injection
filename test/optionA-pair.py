#!/usr/bin/env python3
"""Option A hardware test — LE legacy OOB pairing through MGMT_OP_ADD_REMOTE_OOB_DATA
(patched kernel, extended len-88 payload), no debugfs knob.

  1. F1/F2/F3 over USB              -> keyboard's new bond address + one-time TK
  2. MGMT Add Remote OOB Data       -> store the TK under the peer's identity
                                        (len 88, flags=LE_LEGACY_TK_PRESENT);
                                        needs optionA/0001-0004 applied + booted
  3. MGMT Pair Device               -> in-kernel SMP runs LE legacy OOB with that TK
  4. report the distributed keys / bond, optionally chain phase4.py

Unlike Phase 0 (test/tk-pair.py, debugfs knob, throwaway), this drives the real
MGMT opcode that any trusted mgmt-socket client — including a patched BlueZ, once
one exists — would use. No BlueZ patch exists yet (optionA/BLUEZ-NOTES.md is the
spec), so this script talks to mgmt directly and bypasses bluetoothd, exactly like
tk-pair.py does for Phase 0.

Uses this repo's vendored lib/mkbd_common.py (from the modernkeyboard repo)
for the USB exchange and MGMT plumbing. Run as root.
"""
import argparse
import contextlib
import io
import os
import subprocess
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "lib"))
import mkbd_common as m  # noqa: E402

# --------------------------------------------------------------------------- #
# Option A wire format — optionA/0001-Bluetooth-mgmt-accept-LE-legacy-OOB-TK.patch
# and optionA/BLUEZ-NOTES.md section 1. MGMT_OP_ADD_REMOTE_OOB_DATA (0x0021) gets
# a third, 88-byte accepted payload: the existing extended (P-192+P-256) layout
# with a flags byte + 16-byte le_legacy_tk appended. Unmodified on an unpatched
# kernel — old userspace sending len 39/71 is unaffected.
# --------------------------------------------------------------------------- #
MGMT_OP_ADD_REMOTE_OOB_DATA = 0x0021
MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT = 0x01
MGMT_ADD_REMOTE_OOB_LE_LEGACY_DATA_SIZE = 88


def mgmt_add_remote_oob_le_legacy_tk(addr: str, addr_type: int, tk: bytes,
                                     hci_index: int = 0) -> None:
    """Arm the in-kernel LE legacy OOB TK for `addr` (Option A). Dies (via
    mkbd_common.die -> SystemExit) if the running kernel does not accept the
    len-88 payload, i.e. optionA/0001-0004 are not the booted kernel."""
    if len(tk) != 16:
        m.die("LE legacy TK must be 16 bytes")
    payload = (m.bdaddr_to_bytes(addr, little_endian=True) + bytes([addr_type])
               + b"\x00" * 16    # hash192 — unused on this path
               + b"\x00" * 16    # rand192
               + b"\x00" * 16    # hash256 — all-zero disables SC OOB for this peer
               + b"\x00" * 16    # rand256
               + bytes([MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT])
               + tk)
    assert len(payload) == MGMT_ADD_REMOTE_OOB_LE_LEGACY_DATA_SIZE, len(payload)
    m._mgmt_cmd(MGMT_OP_ADD_REMOTE_OOB_DATA, payload, hci_index)


def _dump_diag(snoop):
    """Print the SMP/connection-relevant lines from the btmon capture + dmesg."""
    print()
    print("=== btmon (filtered) " + "=" * 45)
    try:
        out = subprocess.run(["btmon", "-r", snoop], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception as e:
        print(f"  (btmon decode failed: {e})")
        out = ""
    keep = ("LE Create Connection", "LE Enhanced Create Connection",
            "LE Connection Complete", "LE Extended Advertising Report",
            "Advertising Report", "Connect Complete", "Disconnect",
            "Reason:", "SMP:", "Pairing Request", "Pairing Response",
            "Pairing Confirm", "Pairing Random", "Pairing Failed",
            "Identity", "Encryption Information", "Central Identification",
            "Long Term Key", "Encryption Change", "Encrypt Change",
            "OOB", "AuthReq", "Authentication Req", "IO Capability",
            "Key Distribution", "Bonding", "Add Remote OOB Data")
    ctx = 0
    for ln in out.splitlines():
        s = ln.strip()
        if any(k in ln for k in keep):
            print("  " + ln.rstrip())
            ctx = 2
        elif ctx and (s.startswith(("Handle:", "Status:", "Address:", "Reason:",
                                    "Method:", "Key size:", "Random:", "Confirm:",
                                    "Flags:"))):
            print("  " + ln.rstrip())
            ctx -= 1
        else:
            ctx = 0
    if not out:
        print("  (empty capture)")
    print("=== dmesg (bluetooth/smp) " + "=" * 40)
    try:
        dm = subprocess.run(["dmesg"], capture_output=True, text=True,
                            timeout=10).stdout.splitlines()
        tail = [l for l in dm if any(k in l.lower() for k in
                ("bluetooth", "smp", "legacy oob", "hci0", "l2cap"))][-25:]
        for l in tail:
            print("  " + l)
        if not tail:
            print("  (nothing)")
    except Exception as e:
        print(f"  (dmesg failed: {e})")
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hci", default="hci0")
    ap.add_argument("--adapter", help="local adapter bdaddr (default: hci0's)")
    ap.add_argument("--tk-order", choices=("as-is", "reversed"), default="as-is",
                    help="byte order to feed the F3 TK into the kernel "
                         "(default as-is, matching mkbd-bumble-pair / the "
                         "little-endian kernel smp_c1)")
    ap.add_argument("--addr-type", type=int, choices=(0, 1), default=1,
                    help="1 = LE random (static) [keyboard], 0 = public")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--diag", action="store_true",
                    help="capture btmon + dump the filtered SMP trace and dmesg")
    ap.add_argument("--no-mask", action="store_true",
                    help="do NOT mask/stop bluetooth.service during the pair")
    ap.add_argument("--no-phase4", action="store_true",
                    help="stop after the MGMT pair; skip GATT provisioning")
    ap.add_argument("--p4-rounds", type=int, default=1,
                    help="phase-4 connect/subscribe/hold rounds (default 1)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root")

    hci_index = int("".join(c for c in args.hci if c.isdigit()) or "0")
    addr_type_mgmt = m.MGMT_ADDR_LE_RANDOM if args.addr_type == 1 else m.MGMT_ADDR_LE_PUBLIC

    adapter = (args.adapter or m.adapter_bdaddr(args.hci)).upper()
    print(f":: adapter       : {adapter}")

    devs = [d for d in m.find_hidraw() if d.iface == m.VENDOR_IFACE]
    if not devs:
        sys.exit("no vendor hidraw for 045e:0815 interface 0 — plug in the "
                 "keyboard over USB and switch it on")
    node = devs[0].node
    print(f":: vendor hidraw : {node}")

    # --- USB F1/F2/F3 first (pure USB, no bluetoothd involvement) ---
    print(":: USB F1/F2/F3 ...")
    fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    try:
        res = m.vendor_pairing_exchange(fd, adapter)
    finally:
        os.close(fd)
    addr = res["new_addr"]
    tk = res["tk"]
    print(f"   bond already on keyboard : {res['bond_exists']}")
    print(f"   keyboard addr (was)      : {res['current_addr']}")
    print(f"   keyboard addr (new bond) : {addr}")
    print(f"   one-time F3 TK           : {tk.hex().upper()}")
    print(f"   device name              : {res['name']!r}")

    snoop = f"/tmp/mkbd-optionA-{int(time.time())}.btsnoop"
    btmon = None
    keys = None
    if not args.no_mask:
        subprocess.run(["systemctl", "mask", "--now", "bluetooth"], check=False)
        subprocess.run(["systemctl", "stop", "bluetooth"], check=False)
    else:
        print(":: --no-mask: leaving bluetooth.service running")
    try:
        m.mgmt_set_powered(True, hci_index)

        # Clear any stale bond, same reasoning as tk-pair.py: MGMT Pair Device
        # returns 0x13 (Already Paired) if the kernel already has an LTK for
        # this identity, and never re-runs SMP.
        if args.no_mask:
            subprocess.run(["bluetoothctl", "remove", addr],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        bond_dir = f"/var/lib/bluetooth/{adapter}/{addr}"
        if os.path.isdir(bond_dir):
            subprocess.run(["rm", "-rf", bond_dir], check=False)
            print(f":: removed stale bond dir {bond_dir}")
        try:
            addr_le = m.bdaddr_to_bytes(addr, little_endian=True)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
                m._mgmt_cmd(m.MGMT_OP_UNPAIR_DEVICE,
                            addr_le + bytes([m.MGMT_ADDR_LE_RANDOM, 1]), hci_index)
            print(":: MGMT Unpair Device — cleared kernel bond")
        except SystemExit:
            print(":: MGMT Unpair Device — no existing kernel bond (fine)")

        if args.diag:
            btmon = subprocess.Popen(["btmon", "-w", snoop],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            time.sleep(0.5)

        tkb = tk[::-1] if args.tk_order == "reversed" else tk
        print(f":: MGMT Add Remote OOB Data -> {addr} (len 88, "
              f"flags=LE_LEGACY_TK_PRESENT, tk={tkb.hex()}) ...")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                mgmt_add_remote_oob_le_legacy_tk(addr, addr_type_mgmt, tkb, hci_index)
        except SystemExit:
            sys.exit(
                "MGMT Add Remote OOB Data (len 88) was rejected — the running "
                "kernel does not have optionA/0001-0004 applied.\n"
                "  sudo test/install-optionA-module.sh   then reboot\n"
                f"  ({buf.getvalue().strip()})")
        print("   stored — kernel now has the TK for this identity")

        print(f":: MGMT Pair Device -> {addr} (LE random, NoInputNoOutput) ...")
        pr = m.mgmt_pair_device(addr, timeout=float(args.timeout))
        print(f"   pair status : {pr['status_name']}   events: {pr.get('events')}")
        print(f"   connected={pr.get('connected')} smp_seen={pr.get('smp_seen')} "
              f"disc_reason={hex(pr.get('disc_reason') or 0)}")

        if pr["status"] == 0 and pr.get("ltk"):
            ltk, irk = pr["ltk"], pr.get("irk")
            auth = ltk["key_type"] in (1, 3)
            path = m.write_le_device_info(
                adapter, addr, ltk["val"], ediv=ltk["ediv"], rand=ltk["rand"],
                irk_hex=(irk["val"] if irk else None),
                authenticated=(1 if auth else 0),
                enc_size=ltk["enc_size"] or 16,
                name=(res["name"] or "Modern Keyboard"), addr_type="static")
            print()
            print(f"OK  bond written: {path}")
            print(f"    LTK={ltk['val']} EDIV={ltk['ediv']} Rand={ltk['rand']} "
                  f"key_type={ltk['key_type']} enc_size={ltk['enc_size']}"
                  + (f" IRK={irk['val']}" if irk else " (no IRK)"))
            print(f"    ==> {'AUTHENTICATED' if auth else 'UNAUTHENTICATED'} LE bond "
                  f"via in-kernel legacy OOB SMP (MGMT_OP_ADD_REMOTE_OOB_DATA)  "
                  f"{'*** OPTION A WORKS ***' if auth else '(key_type not 1/3 — check)'}")
            keys = ltk

            if not args.no_phase4:
                print()
                print("=== phase 4: bonded GATT provisioning " + "=" * 28)
                subprocess.run(
                    [sys.executable, os.path.join(_here, "phase4.py"), addr,
                     "--adapter", adapter, "--hci", args.hci, "--f3-check",
                     "--rounds", str(args.p4_rounds)],
                    check=False)
        else:
            print()
            print("FAIL: no LTK distributed.")
            print("  status 0x04 (after our Confirm)  -> wrong TK byte order: "
                  "re-run with --tk-order reversed")
            print("  hung up, no Pairing Response      -> our Pairing Request had "
                  "OOB flag = 0.  check: dmesg | grep -i 'legacy oob'")
            print("  status 0x03 (Auth Requirements)   -> get_auth_method did not "
                  "return REQ_OOB (address/type mismatch with the stored entry?)")
    finally:
        if btmon:
            btmon.terminate()
            try:
                btmon.wait(3)
            except Exception:
                btmon.kill()
        if not args.no_mask:
            subprocess.run(["systemctl", "unmask", "bluetooth"], check=False)
            subprocess.run(["systemctl", "start", "bluetooth"], check=False)
        if args.diag:
            _dump_diag(snoop)
            print(f":: full capture  : sudo btmon -r {snoop}")

    if keys:
        time.sleep(2)
        subprocess.run(["bluetoothctl", "trust", res["new_addr"]], check=False)
        print()
        print("Next: unplug USB, power-cycle the keyboard —")
        print(f"  bluetoothctl connect {res['new_addr']}   "
              f"(or just wait; it is Trusted and reconnects on its own)")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
