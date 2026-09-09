#!/usr/bin/env bash
# Phase 0 hardware test for LE legacy OOB TK injection.
#
# Prereqs:
#   - running a kernel built with kernel/0001-*.patch (check: the debugfs file
#     /sys/kernel/debug/bluetooth/hci0/le_legacy_oob_tk must exist)
#   - the Modern Keyboard connected over USB (045e:0815), switched on
#   - modernkeyboard repo checked out next to this one (../../modernkeyboard)
#   - run as root
#
# What it does:
#   1. F1/F2/F3 over USB via mkbd-provision --exchange-only  -> new addr + TK
#   2. write the TK to the debugfs knob
#   3. start btmon capture
#   4. MGMT/bluetoothctl pair to the new address
#   5. dump the resulting bond + tell you what to check
set -euo pipefail

HCI=${HCI:-hci0}
TKORDER=${TKORDER:-as-is}          # as-is | reversed
MKBD=${MKBD:-$(cd "$(dirname "$0")/../../modernkeyboard" && pwd)}
DBG=/sys/kernel/debug/bluetooth/$HCI/le_legacy_oob_tk
OUT=${OUT:-/tmp/mkbd-tkinj-$(date +%s)}

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
if [[ ! -e $DBG ]]; then
    echo "MISSING $DBG"
    echo "The running bluetooth module is not the patched build."
    echo "  sudo test/install-module.sh   then reboot   then re-run this."
    KOA=$(cd "$(dirname "$0")/.." && pwd)/artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko
    [[ -f $KOA ]] && echo "  (on disk now: $(modinfo -F srcversion bluetooth 2>/dev/null)  patched: $(modinfo -F srcversion "$KOA" 2>/dev/null))"
    exit 1
fi
[[ -x $MKBD/bin/mkbd-provision ]] || { echo "no mkbd-provision at $MKBD"; exit 1; }

strip_ansi() { sed -r 's/\x1B\[[0-9;]*[mK]//g'; }

echo "== 1. USB F1/F2/F3 =="
EX=$("$MKBD/bin/mkbd-provision" --exchange-only 2>&1 | strip_ansi | tee "$OUT.exchange")
ADDR=$(grep -oE '([0-9A-F]{2}:){5}[0-9A-F]{2}' <<<"$EX" | head -1)
TK=$(grep -iE 'OOB TK' <<<"$EX" | grep -oE '[0-9A-Fa-f]{32}' | head -1)
[[ -n $ADDR && -n $TK ]] || { echo "could not parse addr/TK from:"; echo "$EX"; exit 1; }
if [[ $TKORDER == reversed ]]; then
    TK=$(echo "$TK" | fold -w2 | tac | tr -d '\n')
fi
echo "  new address : $ADDR"
echo "  TK ($TKORDER): $TK"

echo "== 2. inject TK =="
echo "$ADDR 1 $TK" > "$DBG"
echo "  wrote: $ADDR 1 $TK  ->  $DBG"

echo "== 3. btmon -> $OUT.btsnoop =="
btmon -w "$OUT.btsnoop" >/dev/null 2>&1 &
BTMON=$!
trap 'kill $BTMON 2>/dev/null || true' EXIT
sleep 1

echo "== 4. pair =="
# directed advert after F3 -> connect by address, no scan.
bluetoothctl --timeout 20 <<EOF || true
power on
agent NoInputNoOutput
default-agent
pair $ADDR
EOF

sleep 2
kill $BTMON 2>/dev/null || true
wait $BTMON 2>/dev/null || true

echo "== 5. result =="
BONDDIR=$(ls -d /var/lib/bluetooth/*/"$ADDR" 2>/dev/null | head -1 || true)
if [[ -n $BONDDIR ]]; then
    echo "  bond dir: $BONDDIR"
    sed -n '1,80p' "$BONDDIR/info"
else
    echo "  NO bond written for $ADDR"
fi
echo
echo "check in $OUT.btsnoop (btmon -r $OUT.btsnoop):"
echo "  - our SMP Pairing Request: OOB flag = present (0x01)"
echo "  - SMP Pairing Confirm / Random both directions"
echo "  - LE Start Encryption -> Encryption Change (Status 0)"
echo "  - Encryption Information / Central Identification / Identity Information"
echo "  - bond info: LongTermKey present, Authenticated=1"
echo
echo "Pairing Failed 0x04 after our Confirm -> re-run with TKORDER=reversed"
echo "Disconnect 0x13 with no Pairing Response -> OOB flag not set: check dmesg"
echo "  for 'using LE legacy OOB TK' and that the address/type matched."
