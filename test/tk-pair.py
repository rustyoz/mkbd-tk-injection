#!/usr/bin/env python3
"""Phase 0 hardware test — LE legacy OOB pairing through the patched kernel.

  1. F1/F2/F3 over USB           -> keyboard's new bond address + one-time TK
  2. write TK to the debugfs knob  /sys/kernel/debug/bluetooth/<hci>/le_legacy_oob_tk
  3. MGMT Pair Device            -> in-kernel SMP runs LE legacy OOB with that TK
  4. report the distributed keys / bond

Reuses the modernkeyboard repo's mkbd_common for the USB exchange and the MGMT
plumbing (same primitives mkbd-provision --commit uses), with the debugfs
injection inserted between the exchange and the pair. Run as root.
"""
import argparse
import os
import subprocess
import sys
import time

MKBD = os.environ.get("MKBD") or os.path.expanduser("~/Work/modernkeyboard")
sys.path.insert(0, os.path.join(MKBD, "lib"))
try:
    import mkbd_common as m
except ImportError as e:
    sys.exit(f"cannot import mkbd_common from {MKBD}/lib: {e}")


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
    ap.add_argument("--keep-btmon", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root")

    dbg = f"/sys/kernel/debug/bluetooth/{args.hci}/le_legacy_oob_tk"
    if not os.path.exists(dbg):
        sys.exit(f"{dbg} missing — patched bluetooth.ko not loaded.\n"
                 f"  sudo test/install-module.sh ; reboot ; retry")

    adapter = (args.adapter or m.adapter_bdaddr(args.hci)).upper()
    print(f":: adapter       : {adapter}")

    devs = [d for d in m.find_hidraw() if d.iface == m.VENDOR_IFACE]
    if not devs:
        sys.exit("no vendor hidraw for 045e:0815 interface 0 — plug in the "
                 "keyboard over USB and switch it on")
    node = devs[0].node
    print(f":: vendor hidraw : {node}")

    snoop = f"/tmp/mkbd-tkinj-{int(time.time())}.btsnoop"
    btmon = None
    keys = None
    res = {}
    # bluetoothd's adapter-init storm tears down SMP mid-flight; mask it like
    # mkbd-provision --commit does, restore in finally.
    subprocess.run(["systemctl", "mask", "--now", "bluetooth"], check=False)
    subprocess.run(["systemctl", "stop", "bluetooth"], check=False)
    try:
        m.mgmt_set_powered(True)
        btmon = subprocess.Popen(["btmon", "-w", snoop],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)

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

        tkb = tk[::-1] if args.tk_order == "reversed" else tk
        line = f"{addr} {args.addr_type} {tkb.hex()}"
        with open(dbg, "w") as f:
            f.write(line)
        print(f":: injected TK   : {line}\n              -> {dbg}  ({args.tk_order})")

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
                  f"via in-kernel legacy OOB SMP  "
                  f"{'*** PHASE 0 WORKS ***' if auth else '(key_type not 1/3 — check)'}")
            keys = ltk
        else:
            print()
            print("FAIL: no LTK distributed.")
            print("  status 0x04 (after our Confirm)  -> wrong TK byte order: "
                  "re-run with --tk-order reversed")
            print("  hung up, no Pairing Response      -> our Pairing Request had "
                  "OOB flag = 0.  check: dmesg | grep -i 'legacy OOB TK'")
            print("  status 0x03 (Auth Requirements)   -> get_auth_method did not "
                  "return REQ_OOB (address/type mismatch with the debugfs entry?)")
    finally:
        if btmon:
            btmon.terminate()
            try:
                btmon.wait(3)
            except Exception:
                btmon.kill()
        subprocess.run(["systemctl", "unmask", "bluetooth"], check=False)
        subprocess.run(["systemctl", "start", "bluetooth"], check=False)
        print()
        print(f":: btmon capture : {snoop}")
        print(f"   sudo btmon -r {snoop} | less   # look for: our Pairing Request "
              "OOB=present, Confirm/Random x2, Encryption Change status 0")

    if keys:
        time.sleep(2)
        subprocess.run(["bluetoothctl", "trust", res["new_addr"]], check=False)
        print()
        print("Bond installed. The keyboard will not hold a BT link while USB is")
        print("connected — unplug the cable, toggle its power switch, then:")
        print(f"  bluetoothctl connect {res['new_addr']}")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
