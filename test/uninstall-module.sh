#!/usr/bin/env bash
# Restore the bluetooth.ko that install-module.sh backed up. Reboot after.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
KREL=$(uname -r)
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

TARGET=$(modinfo -n bluetooth)
BK="$TARGET.stock-backup"
[[ -f $BK ]] || { echo "no backup at $BK — nothing to restore"; exit 1; }

mv -f "$BK" "$TARGET"
depmod -a "$KREL"
echo "restored $TARGET from backup."
echo "reboot to load the original module."
