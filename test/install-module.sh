#!/usr/bin/env bash
# Install the patched bluetooth.ko to disk (backing up the current one) so the
# NEXT BOOT loads it. No live unload — reboot after running this.
#
#   sudo test/install-module.sh      # then reboot, then sudo test/hw-test.sh
#   sudo test/uninstall-module.sh    # then reboot to go back
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
KO=${KO:-$HERE/../artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko}
KREL=$(uname -r)

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -f $KO ]] || { echo "missing $KO — build it (kernel/README.md)"; exit 1; }
want=$(modinfo -F vermagic "$KO" | awk '{print $1}')
[[ $want == "$KREL" ]] || { echo "vermagic mismatch: module=$want running=$KREL"; exit 1; }

TARGET=$(modinfo -n bluetooth)
[[ -f $TARGET ]] || { echo "cannot resolve on-disk bluetooth.ko"; exit 1; }
BK="$TARGET.stock-backup"

if [[ -f $BK ]]; then
    echo "backup already present, keeping: $BK"
else
    cp -p "$TARGET" "$BK"
    echo "backed up current bluetooth.ko -> $BK"
fi

case "$TARGET" in
    *.zst) zstd -q -f -o "$TARGET" "$KO";;
    *.xz)  xz  -c "$KO" > "$TARGET";;
    *.gz)  gzip -c "$KO" > "$TARGET";;
    *)     install -m0644 "$KO" "$TARGET";;
esac
depmod -a "$KREL"

# uhid must be loaded for the kernel to build the HID input device on reconnect;
# make it persistent so no `modprobe uhid` is needed after a reboot.
echo uhid > /etc/modules-load.d/mkbd-uhid.conf
modprobe uhid 2>/dev/null || true

echo "installed patched bluetooth.ko at: $TARGET"
sv_have=$(modinfo -F srcversion bluetooth); sv_want=$(modinfo -F srcversion "$KO")
echo "  srcversion on disk now: $sv_have   (patched build: $sv_want)"
[[ $sv_have == "$sv_want" ]] || { echo "WARNING: on-disk module does not read back as the patched build"; }

if command -v mokutil >/dev/null; then
    echo "  secure boot: $(mokutil --sb-state 2>/dev/null | tr -d '\n')"
fi
echo
echo "NEXT:"
echo "  1. reboot"
echo "  2. verify:  cat /sys/kernel/debug/bluetooth/hci0/le_legacy_oob_tk 2>&1  (should say 'Permission denied' or exist, not 'No such file')"
echo "  3. plug in the Modern Keyboard (USB, switched on)"
echo "  4. sudo test/hw-test.sh"
echo
echo "REVERT: sudo test/uninstall-module.sh  then reboot"
