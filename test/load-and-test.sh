#!/usr/bin/env bash
# Load the patched bluetooth.ko, run the Phase 0 hardware test, then restore.
#
#   sudo test/load-and-test.sh
#
# Requires: root; the Modern Keyboard on USB (045e:0815), switched on; the
# running kernel EXACTLY 7.1.9-arch1-2 (the built module's vermagic).
#
# This unloads and reloads the Bluetooth stack — it briefly kills all
# Bluetooth on the machine. It restores the on-disk module on exit.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
KO=${KO:-$HERE/../artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko}
HCI=${HCI:-hci0}

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -f $KO ]] || { echo "missing $KO — build it first (see kernel/README.md)"; exit 1; }

want=$(modinfo -F vermagic "$KO" | awk '{print $1}')
have=$(uname -r)
[[ $want == "$have" ]] || { echo "vermagic mismatch: module=$want running=$have"; exit 1; }

if ! lsusb 2>/dev/null | grep -qiE "045e:081[0-9a-f]"; then
    echo "WARNING: no Microsoft 045e:081x on USB — plug the keyboard in and switch it on."
    read -rp "continue anyway? [y/N] " a; [[ ${a,,} == y ]] || exit 1
fi

CURRENT=$(modinfo -F filename bluetooth)
echo "current bluetooth.ko: $CURRENT"
echo "  (note: not pacman-owned if it is under updates/ — restored as-is on exit)"

reload_stack() {
    systemctl stop bluetooth 2>/dev/null || true
    modprobe -r btusb hci_uart bnep rfcomm hidp btrtl btmtk btintel btbcm 2>/dev/null || true
    rmmod bluetooth 2>/dev/null || true
}
restore() {
    echo "== restoring stock stack =="
    modprobe -r btusb hci_uart bnep rfcomm hidp btrtl btmtk btintel btbcm 2>/dev/null || true
    rmmod bluetooth 2>/dev/null || true
    modprobe bluetooth           # picks the on-disk module again
    modprobe btusb
    systemctl start bluetooth 2>/dev/null || true
    echo "restored: $(modinfo -F vermagic bluetooth 2>/dev/null)"
}
trap restore EXIT

echo "== unloading current Bluetooth stack =="
reload_stack
lsmod | grep -q '^bluetooth' && { echo "bluetooth still loaded (in use?) — aborting"; exit 1; }

echo "== inserting patched module =="
modprobe rfkill 2>/dev/null || true
insmod "$KO" || { echo "insmod failed"; exit 1; }
modprobe btusb
systemctl start bluetooth 2>/dev/null || true

for i in $(seq 1 20); do [[ -d /sys/class/bluetooth/$HCI ]] && break; sleep 0.5; done
[[ -d /sys/class/bluetooth/$HCI ]] || { echo "$HCI did not appear"; exit 1; }

DBG=/sys/kernel/debug/bluetooth/$HCI/le_legacy_oob_tk
[[ -e $DBG ]] || { echo "FAIL: $DBG absent — patch not active in the loaded module"; exit 1; }
echo "OK: $DBG present, patched module live (vermagic $(modinfo -F vermagic "$KO"))"

echo "== running hw-test.sh =="
HCI=$HCI "$HERE/hw-test.sh"
