# mkbd-tk-injection

Patches and progress for adding **LE legacy OOB Temporary Key injection** to the
Linux Bluetooth stack, so the Microsoft Modern Keyboard (Fingerprint ID, model
1780) can be paired through a normal `bluetoothd` instead of the
`HCI_CHANNEL_USER` userspace-host workarounds in the
[`modernkeyboard`](../modernkeyboard) repo (`mkbd-bumble-pair`,
`mkbd-smp-pair`).

## The problem

LE legacy pairing derives the STK from a 128-bit Temporary Key. For the OOB
method that TK is carried out of band — here over the keyboard's USB vendor
channel (the `F3` response). The in-kernel Security Manager (`net/bluetooth/smp.c`)
has **no way to accept it**: `tk_request()` only ever sets `smp->tk` to `0`
(Just Works) or a 32-bit passkey, and `MGMT_OP_ADD_REMOTE_OOB_DATA` has no field
for a legacy TK (only BR/EDR and LE *Secure Connections* OOB `hash`/`rand`).

This keyboard forces the issue: IO capability `NoInputNoOutput`, sets the MITM
bit, sets OOB-present in its Pairing Response, and **hangs up (`0x13`) on any
non-OOB Pairing Request**. Just Works is refused; OOB with the real TK is
mandatory. Result today: unpairable on Linux without seizing the controller.

## Plan

Full phased plan in [`PLAN.md`](PLAN.md) (copied from
`modernkeyboard/docs/TK-INJECTION-PLAN.md`):

| Phase | Scope | State |
|---|---|---|
| **0** | kernel debugfs TK injection | ✅ works |
| **4** | bonded GATT (CCCD subs + ~7s hold) → address adoption + reconnect | ✅ works end to end (2026-09-10) |
| 1 | `MGMT_OP_ADD_REMOTE_OOB_DATA` `le_legacy_tk` field + BlueZ D-Bus method + `mkbd-provision` rewrite | not started |
| 2 | kernel + BlueZ upstream submission | not started |

## Layout

```
PLAN.md                         the 3-phase plan (debugfs PoC -> MGMT field -> upstream)
UPSTREAM.md                     turning the patch into a mainline series + RFC cover letter
PLUGIN-PLAN.md                  packaging as an AUR / Omarchy install (DKMS + hooks + udev)
PROGRESS.md                     dated worklog + current state + next actions
kernel/                         kernel patches (against linux-7.2.3)
  0001-Bluetooth-SMP-inject-LE-legacy-OOB-Temporary-Key-via-.patch   Phase 0
  README.md                     how to apply / build / test
bluez/                          BlueZ patches (Phase 1, empty for now)
pairmodernkeyboard.sh           ← the command: Phase-0 pair + Phase-4 GATT + adoption check
test/
  install-module.sh / uninstall-module.sh   swap the patched bluetooth.ko on disk (+ persist uhid); reboot
  tk-pair.py                    Phase-0 engine (F1/F2/F3 -> inject TK -> MGMT Pair Device -> bond -> phase4)
  phase4.py                    Phase-4: bonded GATT provisioning over raw L2CAP ATT (also importable)
  hw-test.sh                    back-compat stub -> ../pairmodernkeyboard.sh
notes/
  smp-codepaths.md              analysis of the kernel SMP paths the patch touches
build/                          (gitignored) kernel source tree, scratch
```

## Status — 2026-09-10: END TO END ON HARDWARE 🎉

Native Linux pairing **and reconnect** for the MS Modern Keyboard, no Windows,
no `HCI_CHANNEL_USER`:

1. **Phase 0** — patched `bluetooth.ko` (`kernel/0001-*.patch`): kernel SMP runs
   LE legacy OOB with a debugfs-injected F3 TK; `build_pairing_cmd()` clears SC,
   sets OOB-present, and offers `SMP_DIST_ID_KEY` so we distribute host identity
   in phase 3. MGMT Pair Device → authenticated legacy LTK.
2. **Phase 4** (`test/phase4.py`) — a bonded L2CAP ATT connection (encrypted
   with the kernel LTK): subscribe the 9 report/vendor CCCDs, hold until the
   keyboard drops it ~7 s later. That's the whole minimal sequence — the
   keyboard then **adopts its generated address** (USB F1 confirms
   `current_addr` advanced). The `BOND-COMPLETION.md` GATT discovery and the
   LED / Feature-`0x24` writes turned out **not** to be needed (`--discover`
   / `--writes` re-enable them).
3. `bluetoothctl connect <addr>` then works through normal `bluetoothd` —
   `Connected: yes, Bonded: yes`, `input-keyboard`, battery %, kernel HID input
   device created (`uhid` is auto-loaded by `install-module.sh`).

`sudo ./pairmodernkeyboard.sh` runs Phase 0 → Phase 4 → adoption check (~15 s).
`--diag` adds the btmon SMP trace + dmesg; `--no-phase4` stops after the pair.
(It is a preflight wrapper around `test/tk-pair.py` + `test/phase4.py`.)

`PROGRESS.md` has the full run logs, the minimal-phase-4 breakdown, and the
GATT DB map. Next: **Phase 1** — swap the debugfs knob for a real
`MGMT_OP_ADD_REMOTE_OOB_DATA` field + BlueZ D-Bus method (`PLAN.md`).

---

## Earlier — 2026-09-10: Phase 0 validated on hardware

Patch `kernel/0001-*.patch` (vs linux-7.2.3; applies clean to linux-7.1.9).
Built `artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko` for the running kernel,
installed via `test/install-module.sh` + reboot.

`sudo test/hw-test.sh` → **`pair status: success`**, authenticated legacy LTK
(`key_type=1`), bond written to `/var/lib/bluetooth/<adapter>/<addr>/info`. The
in-kernel Security Manager ran LE legacy OOB SMP with the debugfs-injected F3
TK, driven by MGMT Pair Device — no controller seizure. TK byte order: **as-is**.
Full run + btmon breakdown in `PROGRESS.md`.

Open: whether the keyboard reconnects/types on this phase-3-only bond (needs
phase-4 GATT + hold per `modernkeyboard/docs/BOND-COMPLETION.md`) — orthogonal
to TK injection.

Next: **Phase 1** — move the TK from the debugfs knob to a real
`MGMT_OP_ADD_REMOTE_OOB_DATA` field + a BlueZ D-Bus method (see `PLAN.md`).
