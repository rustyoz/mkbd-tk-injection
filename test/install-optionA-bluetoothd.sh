#!/usr/bin/env bash
# Install the Option A patched bluetoothd (adds Adapter1.AddRemoteLegacyOOB)
# in place of the stock binary, backing up the original. Unlike the kernel
# module, this takes effect immediately via `systemctl restart bluetooth` —
# no reboot needed. Every other Bluetooth device on this adapter gets
# disconnected for the few seconds the daemon takes to restart.
#
#   sudo test/install-optionA-bluetoothd.sh
#   sudo test/uninstall-optionA-bluetoothd.sh   # revert
#
# `bluetoothd` itself doesn't build in OBEX/CUPS/Mesh (those are the separate
# obexd/mesh daemons and a CUPS backend) so the --disable-obex/--disable-cups/
# --disable-manpages configure flags used to build this artifact don't drop
# any bluetoothd functionality — confirmed by comparing the stock binary's
# compiled-in profile strings against a plain build script would produce.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=${BIN:-$HERE/../artifacts/bluetoothd-optionA}
TARGET=/usr/lib/bluetooth/bluetoothd
BACKUP="$TARGET.stock-backup"

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
[[ -x $BIN ]] || { echo "missing $BIN — build it first (optionA/bluez/README.md)"; exit 1; }
"$BIN" --version | grep -q '^5\.87$' || { echo "unexpected version from $BIN"; exit 1; }

if [[ -f $BACKUP ]]; then
    echo "backup already present, keeping: $BACKUP"
else
    cp -p "$TARGET" "$BACKUP"
    echo "backed up stock bluetoothd -> $BACKUP"
fi

install -m0755 -o root -g root "$BIN" "$TARGET"
echo "installed patched bluetoothd -> $TARGET"

systemctl restart bluetooth
sleep 1
if systemctl is-active --quiet bluetooth; then
    echo "bluetooth.service restarted OK"
else
    echo "bluetooth.service FAILED to come back up — rolling back"
    cp -p "$BACKUP" "$TARGET"
    systemctl restart bluetooth
    exit 1
fi

# Retry a few times: right after `install` + `systemctl restart`, a `strings`
# read has occasionally raced something (file-cache/ETXTBSY handling) and come
# back empty on the first try even though the file on disk is already correct
# — seen once during development. Non-fatal either way; the authoritative
# check is the live D-Bus introspection below.
ok=0
for _ in 1 2 3; do
    strings "$TARGET" | grep -q AddRemoteLegacyOOB && { ok=1; break; }
    sleep 1
done
if [[ $ok -eq 1 ]]; then
    echo "confirmed: AddRemoteLegacyOOB present in the installed binary"
else
    echo "note: AddRemoteLegacyOOB not found by 'strings' just now — check with the"
    echo "  gdbus introspect command below before assuming the install is wrong"
    echo "  ($(sha256sum "$TARGET" | cut -d' ' -f1) is the installed file's sha256 —"
    echo "  compare against: sha256sum $BIN)"
fi

echo
echo "Authoritative check — is the running daemon actually serving it:"
echo "  gdbus introspect --system --dest org.bluez --object-path /org/bluez/hci0 \\"
echo "    | grep -A3 AddRemoteLegacyOOB"
echo
echo "Try calling it (expect InvalidArguments for a 1-byte tk — proves it's live):"
echo "  gdbus call --system --dest org.bluez --object-path /org/bluez/hci0 \\"
echo "    --method org.bluez.Adapter1.AddRemoteLegacyOOB '<addr>' random '[byte 0x00]'"
echo
echo "REVERT: sudo test/uninstall-optionA-bluetoothd.sh"
