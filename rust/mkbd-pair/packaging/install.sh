#!/usr/bin/env bash
# Install mkbd-pair (the binary) + the udev rule/systemd service that
# auto-pair the Modern Keyboard when it's plugged in over USB. Not run
# automatically by anything else in this repo — review, then run by hand.
#
#   sudo rust/mkbd-pair/packaging/install.sh
#   sudo rust/mkbd-pair/packaging/install.sh --uninstall
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)

BIN=/usr/local/bin/mkbd-pair
RULE=/etc/udev/rules.d/71-mkbd-pair.rules
UNIT=/etc/systemd/system/mkbd-pair-autopair.service

# Superseded bash-based autopair (kept in optionA/autopair/ as a reference
# implementation) — remove it if present so udev doesn't fire both on the
# same USB event.
OLD_RULE=/etc/udev/rules.d/71-mkbd-optionA-autopair.rules
OLD_UNIT=/etc/systemd/system/mkbd-optionA-autopair.service

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
    rm -f "$BIN" "$RULE" "$UNIT"
    udevadm control --reload
    systemctl daemon-reload
    echo "removed $BIN, $RULE, and $UNIT"
    exit 0
fi

if [[ -f $OLD_RULE || -f $OLD_UNIT ]]; then
    echo "removing the superseded optionA/autopair install ($OLD_RULE, $OLD_UNIT)"
    echo "  so the old bash-based autopair and this one don't both fire on the same USB event"
    rm -f "$OLD_RULE" "$OLD_UNIT"
fi

BUILT_BIN="$REPO/rust/mkbd-pair/target/release/mkbd-pair"
if [[ ! -x $BUILT_BIN ]]; then
    echo "building mkbd-pair (cargo build --release) ..."
    (cd "$REPO/rust/mkbd-pair" && cargo build --release)
fi

install -m0755 "$BUILT_BIN" "$BIN"
install -m0644 "$HERE/mkbd-pair.rules" "$RULE"
install -m0644 "$HERE/mkbd-pair-autopair.service" "$UNIT"
udevadm control --reload
systemctl daemon-reload

echo "installed:"
echo "  $BIN"
echo "  $RULE"
echo "  $UNIT  (ExecStart -> $BIN auto)"
echo
echo "Prereqs before this does anything useful — see the repo README's"
echo "Installation section:"
echo "  1. the Option A kernel module built + installed, rebooted"
echo "  2. the patched bluetoothd built + installed, running with --experimental"
echo "  3. zenity + notify-send/libnotify installed and a notification agent"
echo "     running in the target graphical session"
echo
echo "Test without physically unplugging/replugging:"
echo "  sudo systemctl start mkbd-pair-autopair.service"
echo
echo "Uninstall: sudo $0 --uninstall"
