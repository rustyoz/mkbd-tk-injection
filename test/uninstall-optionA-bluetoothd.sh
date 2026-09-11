#!/usr/bin/env bash
# Restore the bluetoothd that install-optionA-bluetoothd.sh backed up.
# Takes effect immediately (restarts bluetooth.service), no reboot needed.
set -euo pipefail
TARGET=/usr/lib/bluetooth/bluetoothd
BACKUP="$TARGET.stock-backup"

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -f $BACKUP ]] || { echo "no backup at $BACKUP — nothing to restore"; exit 1; }

mv -f "$BACKUP" "$TARGET"
systemctl restart bluetooth
echo "restored stock bluetoothd, restarted bluetooth.service"
