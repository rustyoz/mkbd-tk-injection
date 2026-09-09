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
| **0** | debugfs knob in `smp.c`, prove the injection point on hardware | **patch written; `bluetooth.ko` built for the running kernel (7.1.9-arch1-2); hardware test blocked on root + keyboard** |
| 1 | `MGMT_OP_ADD_REMOTE_OOB_DATA` `le_legacy_tk` field + BlueZ D-Bus method + `mkbd-provision` rewrite | not started |
| 2 | kernel + BlueZ upstream submission | not started |

## Layout

```
PLAN.md                         the 3-phase plan
PROGRESS.md                     dated worklog + current state + next actions
kernel/                         kernel patches (against linux-7.2.3)
  0001-Bluetooth-SMP-inject-LE-legacy-OOB-Temporary-Key-via-.patch   Phase 0
  README.md                     how to apply / build / test
bluez/                          BlueZ patches (Phase 1, empty for now)
test/
  hw-test.sh                    Phase 0 hardware test procedure
notes/
  smp-codepaths.md              analysis of the kernel SMP paths the patch touches
build/                          (gitignored) kernel source tree, scratch
```

## Status — 2026-09-09

Phase 0 patch (`kernel/0001-*.patch`) written against linux-7.2.3, also applies
clean to **linux-7.1.9** (the running kernel). Compiles clean (`W=1`).

A patched module for the **running** kernel is built:
`artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko` (gitignored), vermagic
`7.1.9-arch1-2`, loads without a reboot.

**Hardware test not yet run** — blocked on: (1) root (no passwordless sudo),
(2) the Modern Keyboard is not on the USB bus, (3) the running `bluetooth.ko` is
an unidentified override under `/lib/modules/.../updates/` that my build would
replace. To run once unblocked: `sudo test/load-and-test.sh`. Details in
`PROGRESS.md`.
