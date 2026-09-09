#!/usr/bin/env bash
# Pair the Microsoft Modern Keyboard (Fingerprint ID, model 1780) natively on
# Linux — Phase 0 (kernel legacy-OOB TK injection) + Phase 4 (bonded GATT), no
# Windows, no HCI_CHANNEL_USER.
#
#   sudo ./pairmodernkeyboard.sh              # pair
#   sudo ./pairmodernkeyboard.sh --diag       # + btmon SMP trace + dmesg
#   sudo ./pairmodernkeyboard.sh --tk-order reversed
#   sudo ./pairmodernkeyboard.sh --no-phase4  # stop after the Phase-0 pair
#
# Prereqs: root; patched bluetooth.ko loaded (test/install-module.sh + reboot);
# the keyboard on USB (045e:0815), switched on; the modernkeyboard repo beside
# this one (or MKBD=/path).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
HCI=${HCI:-hci0}

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 $*"; exit 1; }

DBG=/sys/kernel/debug/bluetooth/$HCI/le_legacy_oob_tk
if [[ ! -e $DBG ]]; then
    echo "patched bluetooth.ko is not loaded ($DBG absent)."
    echo "  sudo $HERE/test/install-module.sh   then reboot"
    exit 1
fi
if ! lsusb 2>/dev/null | grep -qiE '045e:081[0-9a-f]'; then
    echo "keyboard not on USB — plug it in (045e:0815) and switch it on."
    exit 1
fi

exec python3 "$HERE/test/tk-pair.py" --hci "$HCI" "$@"
