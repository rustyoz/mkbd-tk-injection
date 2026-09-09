#!/usr/bin/env bash
# Pair the Microsoft Modern Keyboard (Fingerprint ID, model 1780) natively on
# Linux — Phase 0 (kernel legacy-OOB TK injection) + Phase 4 (bonded GATT), no
# Windows, no HCI_CHANNEL_USER.
#
#   sudo ./pairmodernkeyboard.sh              # pair (3-line summary)
#   sudo ./pairmodernkeyboard.sh -v           # full engine output
#   sudo ./pairmodernkeyboard.sh --diag       # full output + btmon SMP trace + dmesg
#   sudo ./pairmodernkeyboard.sh --tk-order reversed
#   sudo ./pairmodernkeyboard.sh --no-phase4  # stop after the Phase-0 pair
#
# Prereqs: root; patched bluetooth.ko loaded (test/install-module.sh + reboot);
# the keyboard on USB (045e:0815), switched on; the modernkeyboard repo beside
# this one (or MKBD=/path).
set -uo pipefail
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

PYARGS=(); FULL=0
for a in "$@"; do
    case $a in
        -v|--verbose) FULL=1 ;;                 # wrapper-only, not forwarded
        --diag)       FULL=1; PYARGS+=("$a") ;;
        *)            PYARGS+=("$a") ;;
    esac
done
ENGINE=(python3 "$HERE/test/tk-pair.py" --hci "$HCI" ${PYARGS[@]+"${PYARGS[@]}"})

if [[ $FULL -eq 1 ]]; then
    exec "${ENGINE[@]}"
fi

LOG=$(mktemp); trap 'rm -f "$LOG"' EXIT
"${ENGINE[@]}" >"$LOG" 2>&1
rc=$?

if [[ $rc -ne 0 ]]; then
    echo "pairing FAILED (exit $rc):"
    sed 's/^/  /' "$LOG"
    exit "$rc"
fi

addr=$(sed -n 's/.*(new bond)[[:space:]]*:[[:space:]]*//p' "$LOG" | tr -dc '0-9A-Fa-f:')
grep -q 'AUTHENTICATED LE bond' "$LOG" && auth=authenticated || auth=UNAUTHENTICATED
cccd=$(grep -oE '[0-9]+/[0-9]+ CCCDs' "$LOG" | tail -1)
if [[ " $* " == *" --no-phase4 "* ]]; then
    line2="paired ($auth) · phase 4 skipped"
elif grep -q '==> ADOPTED' "$LOG"; then
    line2="paired ($auth) · ${cccd:-CCCDs?} · address ADOPTED"
else
    line2="paired ($auth) · ${cccd:-CCCDs?} · address NOT adopted (retry, or --p4-rounds 2)"
fi

echo "Modern Keyboard  ·  ${addr:-?}"
echo "$line2"
echo "unplug USB & power-cycle the keyboard — it reconnects on its own"
