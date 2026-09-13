#!/usr/bin/env python3
"""Phase 0 hardware test — LE legacy OOB pairing through the patched kernel.

  1. F1/F2/F3 over USB           -> keyboard's new bond address + one-time TK
  2. write TK to the debugfs knob  /sys/kernel/debug/bluetooth/<hci>/le_legacy_oob_tk
  3. MGMT Pair Device            -> in-kernel SMP runs LE legacy OOB with that TK
  4. report the distributed keys / bond

Uses this repo's vendored lib/mkbd_common.py (from the modernkeyboard repo)
for the USB exchange and the MGMT plumbing (same primitives mkbd-provision
--commit uses), with the debugfs injection inserted between the exchange and
the pair. Run as root.
"""
import argparse
import os
import subprocess
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "lib"))
import mkbd_common as m  # noqa: E402


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
            "Key Distribution", "Bonding")
    ctx = 0
    for ln in out.splitlines():
        s = ln.strip()
        if any(k in ln for k in keep):
            print("  " + ln.rstrip())
            ctx = 2
        elif ctx and (s.startswith(("Handle:", "Status:", "Address:", "Reason:",
                                    "Method:", "Key size:", "Random:", "Confirm:"))):
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
                ("bluetooth", "smp", "legacy oob tk", "hci0", "l2cap"))][-25:]
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
                    help="do NOT mask/stop bluetooth.service during the pair "
                         "(experiment: with the kernel patch a settled "
                         "bluetoothd may not interfere)")
    ap.add_argument("--no-phase4", action="store_true",
                    help="stop after the Phase-0 pair; skip GATT provisioning")
    ap.add_argument("--p4-rounds", type=int, default=1,
                    help="phase-4 connect/subscribe/hold rounds (default 1)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root")

    hci_index = int("".join(c for c in args.hci if c.isdigit()) or "0")
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

    snoop = f"/tmp/mkbd-tkinj-{int(time.time())}.btsnoop"
    btmon = None
    keys = None
    # bluetoothd's adapter-init storm tears down SMP mid-flight; mask it like
    # mkbd-provision --commit does, restore in finally. --no-mask skips this to
    # test whether a settled bluetoothd interferes once the kernel is patched.
    if not args.no_mask:
        subprocess.run(["systemctl", "mask", "--now", "bluetooth"], check=False)
        subprocess.run(["systemctl", "stop", "bluetooth"], check=False)
    else:
        print(":: --no-mask: leaving bluetooth.service running")
    try:
        m.mgmt_set_powered(True)

        # Clear any stale bond for this address: bluetoothd loads stored LTKs
        # into the kernel on boot, so MGMT Pair Device would return 0x13
        # (Already Paired) and never re-run SMP. With bluetoothd running
        # (--no-mask) also ask it to forget the device so it doesn't rewrite the
        # dir or keep the LTK loaded.
        if args.no_mask:
            subprocess.run(["bluetoothctl", "remove", addr],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        bond_dir = f"/var/lib/bluetooth/{adapter}/{addr}"
        if os.path.isdir(bond_dir):
            subprocess.run(["rm", "-rf", bond_dir], check=False)
            print(f":: removed stale bond dir {bond_dir}")
        try:
            import contextlib
            import io
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

            if not args.no_phase4:
                # bluetoothd still masked here — phase4 uses a raw L2CAP ATT
                # socket with the LTK the kernel just stored.
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
