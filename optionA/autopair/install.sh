#!/usr/bin/env bash
# Install the udev rule + systemd service that auto-detect the Modern Keyboard
# on USB and offer to pair it via Option A. Not run automatically by anything
# else in this repo — review, then run by hand.
#
#   sudo optionA/autopair/install.sh
#   sudo optionA/autopair/install.sh --uninstall
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)

RULE=/etc/udev/rules.d/71-mkbd-optionA-autopair.rules
UNIT=/etc/systemd/system/mkbd-optionA-autopair.service

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
    rm -f "$RULE" "$UNIT"
    udevadm control --reload
    systemctl daemon-reload
    echo "removed $RULE and $UNIT"
    exit 0
fi

install -m0644 "$HERE/71-mkbd-optionA-autopair.rules" "$RULE"
install -m0644 "$HERE/mkbd-optionA-autopair.service" "$UNIT"
udevadm control --reload
systemctl daemon-reload

echo "installed:"
echo "  $RULE"
echo "  $UNIT  (ExecStart -> $HERE/mkbd-optionA-autopair)"
echo
echo "Prereqs before this does anything useful:"
echo "  1. sudo test/install-optionA-module.sh   (from the repo root)"
echo "  2. reboot"
echo "  3. sudo test/install-optionA-bluetoothd.sh   (patched bluetoothd, no reboot needed)"
echo "  4. zenity + notify-send/libnotify installed and a notification agent running"
echo "     in the target graphical session"
echo
echo "Test without physically unplugging/replugging:"
echo "  sudo udevadm test-builtin ... (or just) sudo systemctl start mkbd-optionA-autopair.service"
echo
echo "Uninstall: sudo $0 --uninstall"
