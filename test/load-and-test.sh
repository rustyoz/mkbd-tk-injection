#!/usr/bin/env bash
# Swap in the patched bluetooth.ko ON DISK, run the Phase 0 hardware test,
# then restore. Swapping on disk (rather than insmod) avoids the autoload
# race that makes `insmod` fail with "File exists".
#
#   sudo test/load-and-test.sh
#
# Requires: root; the Modern Keyboard on USB (045e:0815), switched on; the
# running kernel EXACTLY 7.1.9-arch1-2. Briefly kills all Bluetooth on the
# machine; restores the original module on exit.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
KO=${KO:-$HERE/../artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko}
HCI=${HCI:-hci0}
KREL=$(uname -r)

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -f $KO ]] || { echo "missing $KO — build it (kernel/README.md)"; exit 1; }

want=$(modinfo -F vermagic "$KO" | awk '{print $1}')
[[ $want == "$KREL" ]] || { echo "vermagic mismatch: module=$want running=$KREL"; exit 1; }

if ! lsusb 2>/dev/null | grep -qiE "045e:081[0-9a-f]"; then
    echo "WARNING: no Microsoft 045e:081x on USB — plug the keyboard in, switch it on."
    read -rp "continue anyway? [y/N] " a; [[ ${a,,} == y ]] || exit 1
fi

TARGET=$(modinfo -n bluetooth)          # the file modprobe currently resolves
[[ -f $TARGET ]] || { echo "cannot resolve on-disk bluetooth.ko"; exit 1; }
BACKUP="$TARGET.tkinj-backup.$$"
COMP=""; case "$TARGET" in *.zst) COMP=zst;; *.xz) COMP=xz;; *.gz) COMP=gz;; esac

DEPS="btusb hci_uart btrtl btmtk btintel btbcm btqca btsdio bnep rfcomm hidp cmtp bluetooth_6lowpan"

unload_stack() {
    systemctl stop bluetooth 2>/dev/null || true
    rfkill block bluetooth 2>/dev/null || true
    local m
    for m in $DEPS; do modprobe -r "$m" 2>/dev/null || true; done
    # anything still holding bluetooth?
    if [[ -d /sys/module/bluetooth ]]; then
        local h; h=$(ls /sys/module/bluetooth/holders 2>/dev/null)
        [[ -n $h ]] && { echo "still held by: $h"; for m in $h; do modprobe -r "$m" 2>/dev/null || true; done; }
        rmmod bluetooth 2>/dev/null || true
    fi
    sleep 1
    [[ -d /sys/module/bluetooth ]] && {
        echo "FAIL: bluetooth won't unload (refcnt $(cat /sys/module/bluetooth/refcnt 2>/dev/null), holders: $(ls /sys/module/bluetooth/holders 2>/dev/null)).";
        echo "Reboot into a kernel with the patch instead, or stop whatever holds it."; return 1; }
    return 0
}

reload_stack() {
    depmod -a "$KREL"
    rfkill unblock bluetooth 2>/dev/null || true
    modprobe btusb 2>/dev/null || modprobe bluetooth 2>/dev/null || true
    systemctl start bluetooth 2>/dev/null || true
    local i; for i in $(seq 1 20); do [[ -d /sys/class/bluetooth/$HCI ]] && break; sleep 0.5; done
}

restore() {
    echo "== restoring original bluetooth.ko =="
    unload_stack || true
    [[ -f $BACKUP ]] && mv -f "$BACKUP" "$TARGET"
    reload_stack
    echo "restored: $(modinfo -F vermagic bluetooth 2>/dev/null) from $(modinfo -n bluetooth)"
}
trap restore EXIT

echo "on-disk bluetooth.ko : $TARGET"
echo "  (not pacman-owned — backed up to $BACKUP, restored on exit)"

echo "== unloading Bluetooth stack =="
unload_stack || exit 1

echo "== swapping in patched module =="
cp -p "$TARGET" "$BACKUP"
case "$COMP" in
    zst) zstd -q -f -o "$TARGET" "$KO";;
    xz)  xz  -c "$KO" > "$TARGET";;
    gz)  gzip -c "$KO" > "$TARGET";;
    "")  cp -f "$KO" "$TARGET";;
esac
depmod -a "$KREL"

echo "== reloading with patched module =="
reload_stack
[[ -d /sys/class/bluetooth/$HCI ]] || { echo "$HCI did not appear"; exit 1; }

lv=$(modinfo -F vermagic bluetooth 2>/dev/null)
sv_want=$(modinfo -F srcversion "$KO"); sv_have=$(modinfo -F srcversion bluetooth 2>/dev/null)
echo "loaded bluetooth: vermagic='$lv' srcversion=$sv_have (patched build = $sv_want)"
[[ $sv_have == "$sv_want" ]] || { echo "FAIL: loaded module is not the patched build"; exit 1; }

DBG=/sys/kernel/debug/bluetooth/$HCI/le_legacy_oob_tk
[[ -e $DBG ]] || { echo "FAIL: $DBG absent — patch not active"; exit 1; }
echo "OK: $DBG present, patched module live."
dmesg | tail -20 | grep -i bluetooth || true

echo "== running hw-test.sh =="
HCI=$HCI "$HERE/hw-test.sh"
