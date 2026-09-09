#!/usr/bin/env python3
"""Pair the Microsoft Modern Keyboard (Fingerprint ID, model 1780) natively on
Linux — no Windows, no HCI_CHANNEL_USER.

  Phase 0  F1/F2/F3 over USB -> inject the F3 legacy-OOB TK into the patched
           kernel (debugfs knob) -> MGMT Pair Device -> authenticated LE bond
  Phase 4  bonded L2CAP ATT connection + CCCD subscribes, held ~7 s, so the
           keyboard adopts its generated address

Needs the patched bluetooth.ko loaded (test/install-module.sh + reboot), the
keyboard on USB, and root. Quiet by default; -v for detail, --diag for a btmon
SMP trace + dmesg.
"""
import argparse
import contextlib
import io
import os
import subprocess
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
MKBD = (os.environ.get("MKBD")
        or next((p for p in (
            os.path.join(_here, "..", "..", "modernkeyboard"),
            os.path.expanduser("~/Work/modernkeyboard"),
            os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}/Work/modernkeyboard"),
        ) if os.path.isdir(os.path.join(p, "lib"))), "/nonexistent"))
MKBD = os.path.abspath(MKBD)
sys.path.insert(0, os.path.join(MKBD, "lib"))
sys.path.insert(0, _here)
try:
    import mkbd_common as m
except ImportError as e:
    sys.exit(f"cannot import mkbd_common from {MKBD}/lib: {e}\n"
             f"  set MKBD=/path/to/modernkeyboard")
import phase4

VERBOSE = False


def step(msg):
    print(msg, flush=True)


def v(msg):
    if VERBOSE:
        print(msg, flush=True)


def _dump_diag(snoop):
    print("\n=== btmon (filtered) " + "=" * 45)
    try:
        out = subprocess.run(["btmon", "-r", snoop], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception as e:
        out = ""
        print(f"  (btmon decode failed: {e})")
    keep = ("LE Create Connection", "LE Connection Complete", "Advertising Report",
            "Disconnect", "Reason:", "SMP:", "Pairing", "Identity",
            "Encryption Information", "Central Identification", "Long Term Key",
            "Encryption Change", "OOB", "AuthReq", "Authentication Req",
            "IO Capability", "Key Distribution")
    ctx = 0
    for ln in out.splitlines():
        if any(k in ln for k in keep):
            print("  " + ln.rstrip())
            ctx = 2
        elif ctx and ln.strip().startswith(("Handle:", "Status:", "Address:",
                                            "Reason:", "Method:", "Key size:")):
            print("  " + ln.rstrip())
            ctx -= 1
        else:
            ctx = 0
    print("=== dmesg (bluetooth/smp) " + "=" * 40)
    try:
        dm = subprocess.run(["dmesg"], capture_output=True, text=True,
                            timeout=10).stdout.splitlines()
        for l in [x for x in dm if any(k in x.lower() for k in
                  ("bluetooth", "legacy oob tk", "hci0", "l2cap"))][-20:]:
            print("  " + l)
    except Exception as e:
        print(f"  (dmesg failed: {e})")
    print("=" * 66)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hci", default="hci0")
    ap.add_argument("--adapter", help="local adapter bdaddr (default: hci0's)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--diag", action="store_true",
                    help="capture btmon + dump the filtered SMP trace and dmesg")
    ap.add_argument("--tk-order", choices=("as-is", "reversed"), default="as-is")
    ap.add_argument("--addr-type", type=int, choices=(0, 1), default=1)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--no-phase4", action="store_true",
                    help="stop after the Phase-0 pair")
    ap.add_argument("--p4-rounds", type=int, default=1)
    ap.add_argument("--p4-hold", type=float, default=8.0)
    args = ap.parse_args()
    VERBOSE = args.verbose

    if os.geteuid() != 0:
        sys.exit("run as root")
    hci_index = int("".join(c for c in args.hci if c.isdigit()) or "0")
    dbg = f"/sys/kernel/debug/bluetooth/{args.hci}/le_legacy_oob_tk"
    if not os.path.exists(dbg):
        sys.exit(f"{dbg} missing — patched bluetooth.ko not loaded "
                 f"(sudo test/install-module.sh ; reboot)")

    adapter = (args.adapter or m.adapter_bdaddr(args.hci)).upper()
    devs = [d for d in m.find_hidraw() if d.iface == m.VENDOR_IFACE]
    if not devs:
        sys.exit("keyboard not on USB (no vendor hidraw for 045e:0815 iface 0) "
                 "— plug it in and switch it on")
    node = devs[0].node
    step(f"Modern Keyboard  ·  adapter {adapter}")

    # --- Phase 0: USB exchange (no bluetoothd) ---
    fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    try:
        res = m.vendor_pairing_exchange(fd, adapter)
    finally:
        os.close(fd)
    addr, tk = res["new_addr"], res["tk"]
    step(f"  USB handshake     {addr}")
    v(f"    was {res['current_addr']}, bond_exists={res['bond_exists']}, "
      f"name {res['name']!r}, TK {tk.hex().upper()}")

    snoop = f"/tmp/mkbd-pair-{int(time.time())}.btsnoop"
    btmon = None
    ok = False
    # bluetoothd is D-Bus-activated: `stop` alone isn't enough, it respawns
    # mid-SMP and its adapter-init storm (Set Local Name / Write Scan Enable /
    # Add UUID) tears the directed-advert link down. Mask it for the pairing.
    # --runtime keeps the mask symlink in /run (tmpfs) so it can't outlast a
    # reboot even if this script is killed; stderr is hushed (the "Created
    # symlink … → /dev/null" line). Reverted in `finally`.
    QUIET = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["systemctl", "mask", "--runtime", "--now", "bluetooth"],
                   check=False, **QUIET)
    subprocess.run(["systemctl", "stop", "bluetooth"], check=False, **QUIET)
    try:
        m.mgmt_set_powered(True)

        # stale bond -> MGMT Pair Device returns 0x13 Already Paired; clear it
        bond_dir = f"/var/lib/bluetooth/{adapter}/{addr}"
        if os.path.isdir(bond_dir):
            subprocess.run(["rm", "-rf", bond_dir], check=False)
            v(f"    removed stale bond {bond_dir}")
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                m._mgmt_cmd(m.MGMT_OP_UNPAIR_DEVICE,
                            m.bdaddr_to_bytes(addr, little_endian=True)
                            + bytes([m.MGMT_ADDR_LE_RANDOM, 1]), hci_index)
            v("    MGMT Unpair Device — cleared kernel bond")
        except SystemExit:
            pass

        if args.diag:
            btmon = subprocess.Popen(["btmon", "-w", snoop],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            time.sleep(0.5)

        tkb = tk[::-1] if args.tk_order == "reversed" else tk
        with open(dbg, "w") as f:
            f.write(f"{addr} {args.addr_type} {tkb.hex()}")
        v(f"    injected TK ({args.tk_order}) -> {dbg}")

        pr = m.mgmt_pair_device(addr, timeout=float(args.timeout))
        v(f"    MGMT Pair Device: {pr['status_name']} events={pr.get('events')} "
          f"connected={pr.get('connected')} disc={hex(pr.get('disc_reason') or 0)}")

        if pr["status"] == 0 and pr.get("ltk"):
            ltk, irk = pr["ltk"], pr.get("irk")
            auth = ltk["key_type"] in (1, 3)
            m.write_le_device_info(
                adapter, addr, ltk["val"], ediv=ltk["ediv"], rand=ltk["rand"],
                irk_hex=(irk["val"] if irk else None),
                authenticated=(1 if auth else 0), enc_size=ltk["enc_size"] or 16,
                name=(res["name"] or "Modern Keyboard"), addr_type="static")
            step(f"  pair              {'authenticated' if auth else 'UNAUTHENTICATED'}")
            v(f"    LTK={ltk['val']} EDIV={ltk['ediv']} Rand={ltk['rand']} "
              f"key_type={ltk['key_type']}" + (f" IRK={irk['val']}" if irk else ""))
            ok = True

            if not args.no_phase4:
                r = phase4.run(adapter, addr, hci=args.hci,
                               rounds=args.p4_rounds, hold=args.p4_hold,
                               verbose=VERBOSE, f3_check=True, log=v)
                step(f"  provision         {r['cccd_ok']}/9 CCCDs, "
                     f"link held {r['held']:.1f}s")
                verdict = {True: "yes", False: "NO — retry with --p4-rounds 2",
                           None: "unknown"}[r["adopted"]]
                step(f"  address adopted   {verdict}")
        else:
            step(f"  pair              FAILED ({pr['status_name']})")
            step("    0x04 after Confirm -> --tk-order reversed")
            step("    no Pairing Response -> OOB flag 0; dmesg | grep 'legacy OOB TK'")
            step("    0x03 -> get_auth_method didn't return REQ_OOB")
    finally:
        if btmon:
            btmon.terminate()
            with contextlib.suppress(Exception):
                btmon.wait(3)
        subprocess.run(["systemctl", "unmask", "--runtime", "bluetooth"],
                       check=False, **QUIET)
        subprocess.run(["systemctl", "start", "bluetooth"], check=False, **QUIET)
        if args.diag:
            _dump_diag(snoop)
            print(f":: full capture: sudo btmon -r {snoop}")

    if ok:
        time.sleep(2)
        subprocess.run(["bluetoothctl", "trust", addr],
                       stdout=subprocess.DEVNULL, check=False)
        step(f"\nPaired {addr}. Unplug USB, power-cycle the keyboard — "
             f"it reconnects on its own.")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
