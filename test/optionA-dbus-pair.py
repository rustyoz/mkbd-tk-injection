#!/usr/bin/env python3
"""Option A D-Bus pairing test — same F1/F2/F3 + LE legacy OOB TK as
test/optionA-pair.py, but through bluetoothd's own Adapter1.AddRemoteLegacyOOB()
D-Bus method with bluetoothd left running throughout, instead of the raw mgmt
socket with bluetoothd stopped.

What this answers: docs/PROTOCOL.md (sibling modernkeyboard repo) flags that
BlueZ's normal Device1.Pair() is *unproven* to catch the keyboard's short
directed LE advertising window — that's why every other script here
(tk-pair.py, optionA-pair.py) uses MGMT_OP_PAIR_DEVICE directly instead. This
script is the actual experiment: does StartDiscovery() + waiting for the
Device1 object to appear + Pair() work, or does the advertisement get missed?

Needs test/install-optionA-bluetoothd.sh already run (AddRemoteLegacyOOB has
to exist on the live bus) and root (hidraw + system D-Bus).

Sequence:
  1. F1/F2/F3 over USB              -> new bond address + one-time TK
  2. Adapter1.AddRemoteLegacyOOB()  -> arm the TK, bluetoothd stays up
  3. StartDiscovery(), poll for the Device1 object to appear
  4. Device1.Pair() with a NoInputNoOutput agent registered
  5. report Paired/Bonded/Trusted/Connected + GATT resolution
"""
import argparse
import os
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

_here = os.path.dirname(os.path.abspath(__file__))
MKBD = (os.environ.get("MKBD")
        or next((p for p in (
            os.path.join(_here, "..", "..", "modernkeyboard"),
            os.path.expanduser("~/Work/modernkeyboard"),
            os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}/Work/modernkeyboard"),
        ) if os.path.isdir(os.path.join(p, "lib"))), "/nonexistent"))
MKBD = os.path.abspath(MKBD)
sys.path.insert(0, os.path.join(MKBD, "lib"))
try:
    import mkbd_common as m
except ImportError as e:
    sys.exit(f"cannot import mkbd_common from {MKBD}/lib: {e}\n"
             f"  set MKBD=/path/to/modernkeyboard")

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MGR_IFACE = "org.bluez.AgentManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
AGENT_PATH = "/mkbd/optionA/agent"


class NoIoAgent(dbus.service.Object):
    """NoInputNoOutput agent — auto-accept whatever BlueZ asks. The OOB TK
    already armed in the kernel is what actually authenticates the legacy
    pairing; nothing here should normally even get called."""

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        raise dbus.exceptions.DBusException(
            "org.bluez.Error.Rejected", "no pin code available")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        raise dbus.exceptions.DBusException(
            "org.bluez.Error.Rejected", "no passkey available")

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def DisplayPasskey(self, device, passkey):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        m.info(f"  agent: RequestConfirmation({passkey}) -> auto-accept")
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        return

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


def find_adapter(bus, want_addr):
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)
    for path, ifaces in om.GetManagedObjects().items():
        props = ifaces.get(ADAPTER_IFACE)
        if props and str(props.get("Address", "")).upper() == want_addr.upper():
            return path
    sys.exit(f"no org.bluez.Adapter1 with address {want_addr} on the bus")


def find_device_path(bus, addr):
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)
    for path, ifaces in om.GetManagedObjects().items():
        props = ifaces.get(DEVICE_IFACE)
        if props and str(props.get("Address", "")).upper() == addr.upper():
            return path
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hci", default="hci0")
    ap.add_argument("--adapter", help="local adapter bdaddr (default: hci0's)")
    ap.add_argument("--tk-order", choices=("as-is", "reversed"), default="as-is")
    ap.add_argument("--discover-timeout", type=float, default=20.0,
                    help="seconds to wait for the Device1 object to appear "
                         "after StartDiscovery (default 20)")
    ap.add_argument("--pair-timeout", type=float, default=30.0)
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    adapter_addr = (args.adapter or m.adapter_bdaddr(args.hci)).upper()
    print(f":: adapter       : {adapter_addr}")
    adapter_path = find_adapter(bus, adapter_addr)
    print(f":: adapter path  : {adapter_path}")
    adapter = dbus.Interface(bus.get_object(BLUEZ, adapter_path), ADAPTER_IFACE)
    adapter_props = dbus.Interface(bus.get_object(BLUEZ, adapter_path), PROPS_IFACE)

    if not bool(adapter_props.Get(ADAPTER_IFACE, "Powered")):
        sys.exit("adapter not powered")

    devs = [d for d in m.find_hidraw() if d.iface == m.VENDOR_IFACE]
    if not devs:
        sys.exit("no vendor hidraw for 045e:0815 interface 0 — plug in the "
                 "keyboard over USB and switch it on")
    node = devs[0].node
    print(f":: vendor hidraw : {node}")

    print(":: USB F1/F2/F3 ...")
    fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    try:
        res = m.vendor_pairing_exchange(fd, adapter_addr)
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

    # --- register a NoInputNoOutput agent (bluetoothd is live the whole time) ---
    agent = NoIoAgent(bus, AGENT_PATH)
    agent_mgr = dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"), AGENT_MGR_IFACE)
    try:
        agent_mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    except dbus.exceptions.DBusException as e:
        if "AlreadyExists" not in str(e):
            raise
    try:
        agent_mgr.RequestDefaultAgent(AGENT_PATH)
    except dbus.exceptions.DBusException as e:
        print(f"   (RequestDefaultAgent: {e})")

    # --- MGMT Add Remote OOB Data via D-Bus, bluetoothd stays up ---
    print(f":: Adapter1.AddRemoteLegacyOOB({addr}, random, "
          f"tk={tkb.hex()}) over D-Bus ...")
    try:
        adapter.AddRemoteLegacyOOB(addr, "random",
                                   dbus.Array(tkb, signature="y"))
    except dbus.exceptions.DBusException as e:
        sys.exit(f"AddRemoteLegacyOOB failed: {e}\n"
                 f"  is the patched bluetoothd installed? "
                 f"test/install-optionA-bluetoothd.sh")
    print("   stored — kernel now has the TK for this identity")

    # --- start discovery, wait for the Device1 object (the open question) ---
    print(f":: StartDiscovery(), waiting up to {args.discover_timeout:g}s "
          f"for {addr} to appear ...")
    try:
        adapter.StartDiscovery()
    except dbus.exceptions.DBusException as e:
        if "InProgress" not in str(e):
            raise

    device_path = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < args.discover_timeout:
        device_path = find_device_path(bus, addr)
        if device_path:
            break
        time.sleep(0.3)
    dt = time.monotonic() - t0

    try:
        adapter.StopDiscovery()
    except dbus.exceptions.DBusException:
        pass

    if not device_path:
        print()
        print(f"RESULT: the keyboard's directed advertisement was NOT seen by "
              f"BlueZ discovery within {args.discover_timeout:g}s.")
        print("  This confirms docs/PROTOCOL.md's caution: normal BlueZ "
              "discovery does not reliably catch this keyboard's directed "
              "advertising window. The raw MGMT_OP_PAIR_DEVICE path "
              "(test/optionA-pair.py) remains the one that works.")
        try:
            agent_mgr.UnregisterAgent(AGENT_PATH)
        except dbus.exceptions.DBusException:
            pass
        sys.exit(1)

    print(f"   found Device1 at {device_path} after {dt:.1f}s")
    device = dbus.Interface(bus.get_object(BLUEZ, device_path), DEVICE_IFACE)
    device_props = dbus.Interface(bus.get_object(BLUEZ, device_path), PROPS_IFACE)

    print(f":: Device1.Pair() -> {addr} (timeout {args.pair_timeout:g}s) ...")
    loop = GLib.MainLoop()
    outcome = {}

    def _reply():
        outcome["ok"] = True
        loop.quit()

    def _error(e):
        outcome["ok"] = False
        outcome["error"] = str(e)
        loop.quit()

    device.Pair(reply_handler=_reply, error_handler=_error,
               timeout=args.pair_timeout)
    GLib.timeout_add(int(args.pair_timeout * 1000) + 2000,
                     lambda: loop.quit() or False)
    loop.run()

    try:
        agent_mgr.UnregisterAgent(AGENT_PATH)
    except dbus.exceptions.DBusException:
        pass

    if not outcome.get("ok"):
        print()
        print(f"FAIL: Device1.Pair() did not succeed: "
              f"{outcome.get('error', 'timed out')}")
        sys.exit(1)

    paired = bool(device_props.Get(DEVICE_IFACE, "Paired"))
    bonded = bool(device_props.Get(DEVICE_IFACE, "Bonded"))
    connected = bool(device_props.Get(DEVICE_IFACE, "Connected"))
    print()
    print(f"OK  Paired={paired} Bonded={bonded} Connected={connected}")
    if paired and bonded:
        try:
            device_props.Set(DEVICE_IFACE, "Trusted", True)
        except dbus.exceptions.DBusException:
            pass
        print("*** D-BUS PAIRING WORKS — bluetoothd handled the whole thing, "
              "never stopped ***")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
