# Upstreaming the SMP LE-legacy-OOB support

The `kernel/0001-*.patch` in this repo is a **debugfs proof of concept** — not
submittable (debugfs is not a stable ABI; the commit is `Not-Signed-off-by`).
This file is the plan to turn it into a real mainline patch series, plus a draft
RFC cover letter.

Target list: **`linux-bluetooth@vger.kernel.org`** (+ `netdev@vger`,
`linux-kernel@vger`). Maintainers per `scripts/get_maintainer.pl`
`net/bluetooth/smp.c`: Luiz Augusto von Dentz, Marcel Holtmann.

BlueZ side goes to the same list against `bluez.git`.

---

## Why upstream (motivations to state in the cover letter)

1. **A real, shipping device is unpairable on Linux.** The Microsoft Modern
   Keyboard with Fingerprint ID (USB `045e:0815`, BT `045e:0813`, model 1780) is
   IO-cap `NoInputNoOutput`, sets the MITM bit, sets OOB-present in its Pairing
   Response, and **terminates the link (`0x13`) on any non-OOB Pairing
   Request**. Just Works is refused. It only pairs with LE legacy OOB, and the
   TK arrives out of band over a USB vendor HID channel. Likely other
   Surface-family accessories behave the same (documented on the fprint list as
   an "in-house protocol for automatic bluetooth pairing").
2. **The spec still defines LE legacy OOB.** It was not removed. The kernel
   supports every other OOB variant (BR/EDR OOB, LE Secure Connections OOB) via
   `MGMT_OP_ADD_REMOTE_OOB_DATA` — the legacy LE TK is the one missing case.
3. **The current workaround is bad.** Without a kernel interface, pairing this
   device means one of: a full userspace host stack over `HCI_CHANNEL_USER`
   (seizes the controller), or an out-of-tree `bluetooth.ko` patch. Both mean
   **`bluetoothd` cannot pair the device itself** — userspace has to take SMP
   away from it. With a proper MGMT field, `bluetoothd` pairs it natively:
   userspace just supplies the TK it obtained out of band, exactly like it
   already supplies `hash192`/`rand192` for BR/EDR OOB. **This also removes the
   need to stop/mask `bluetoothd` during pairing** (see "The bluetoothd-masking
   problem" below).

---

## Interface — decide first (this is the RFC question)

`MGMT_OP_ADD_REMOTE_OOB_DATA` (0x0021) already accepts two payload lengths:

| size | fields |
|---|---|
| `MGMT_ADD_REMOTE_OOB_DATA_SIZE` | `addr` + `hash192` + `rand192` (BR/EDR legacy OOB) |
| (larger) | `+ hash256 + rand256` (adds LE / BR-EDR Secure Connections OOB) |

**Option A — extend the existing opcode** with a third accepted length adding
`__u8 le_legacy_tk[16]`. Smallest surface, matches precedent, one doc paragraph.

**Option B — new opcode** `MGMT_OP_ADD_REMOTE_LE_LEGACY_OOB_DATA`. Cleaner
separation, but a whole new command + event + doc + `mgmt-tester` case.

Recommend proposing **A**, letting the maintainers redirect to B if they prefer.
Either way `struct oob_data` (`include/net/bluetooth/hci_core.h`) grows
`u8 tk[16]; bool tk_present;`, set via `hci_add_remote_oob_data()`.

An all-zero `le_legacy_tk` = "not present" (so old-size callers are unchanged).

---

## Patch series (against current mainline, ~5 patches)

1. **`Bluetooth: mgmt: accept an LE legacy OOB Temporary Key`**
   `mgmt.h` (UAPI: new size/field), `mgmt.c` `add_remote_oob_data()` parse,
   `hci_core.c` `hci_add_remote_oob_data()` stores it,
   `Documentation/.../mgmt-api.rst` (or `.txt`) update.
2. **`Bluetooth: SMP: use a stored LE legacy OOB Temporary Key`**
   `tk_request()` — when `!SMP_FLAG_SC`, `hcon->type == LE_LINK`, and
   `hci_remote_oob_data_lookup()` returns an entry with `tk_present`: `memcpy`
   the TK into `smp->tk`, set `SMP_FLAG_TK_VALID | SMP_FLAG_MITM_AUTH`, raise
   `pending_sec_level` to `BT_SECURITY_HIGH`, no user interaction.
   `build_pairing_cmd()` — advertise `SMP_OOB_PRESENT` when such an entry
   exists. `get_auth_method()` — return an OOB method so the "can MITM be
   achieved?" checks in the pairing handlers don't reject a NoInputNoOutput
   pair. Keep `REQ_OOB` semantics legacy-clean (don't overload the SC path).
3. **`Bluetooth: SMP: force legacy pairing when an LE legacy OOB TK is set`**
   `build_pairing_cmd()` — `authreq &= ~SMP_AUTH_SC` when a legacy TK is stored.
   Rationale patch: userspace supplying a *legacy* TK is explicitly opting into
   legacy. Reviewers may prefer this driven by a flag in the MGMT command
   instead — be ready to move it.
4. **`Bluetooth: SMP: distribute local identity for LE legacy OOB bonds`**
   `build_pairing_cmd()` — `local_dist |= SMP_DIST_ID_KEY` for these bonds, so
   `smp_distribute_keys()` sends Identity Information + Identity Address.
   Justify: this class of device only persists the bond / adopts its generated
   address after the initiator sends those PDUs (Windows always does; the one
   captured failed pairing is the one that skipped it). This is the weakest
   patch — expect "that's a device quirk". Fallbacks: gate on the OOB-TK-present
   condition (bounded), a `hci_dev` quirk, or drive it from the MGMT `auth_req`.
   Ship it separable so 1–3 can land without it.
5. **`Bluetooth: selftest: LE legacy OOB SMP vectors`**
   Add `net/bluetooth/selftest.c` (or `tools/smp-tester`) cases using the real
   captured vectors from this repo's `lib/mkbd_crypto.py` `_vec`
   (`modern keyboard2`, `modern keyboard3 #2`, `mkbd-pairing-194414 #2`):
   TK, Mrand, Srand → expected Mconfirm / Sconfirm / STK. Real device, CI
   coverage — strengthens the whole series.

Each: `checkpatch.pl --strict` clean, `Signed-off-by`, per-patch changelog under
`---`, `git send-email` threaded under the cover letter.

## BlueZ series (bluez.git, same list, can lag)

1. `src/shared/mgmt.h` — new opcode/size/struct constants; `monitor/packet.c`
   decode for `btmon`.
2. `src/adapter.c` — carry the 16-byte legacy TK through
   `btd_adapter_add_remote_oob_data()` and emit the MGMT command.
3. D-Bus surface — how the TK enters from a helper like `mkbd-pair`. Minimal:
   `org.bluez.Adapter1.AddRemoteLegacyOOB(string address, byte type,
   array{byte} tk)` forwarding straight to MGMT. (`doc/adapter-api.txt`.) Fuller:
   an `org.bluez.Agent1` callback the daemon invokes mid-pairing.
4. `src/device.c` — ensure `MGMT_OP_PAIR_DEVICE` doesn't clear stored remote OOB
   data first; resulting bond stored `Authenticated=1`.

Then the userspace flow is just:
`F1/F2/F3 over USB → Adapter1.AddRemoteLegacyOOB(addr, static, tk) → Device1.Pair()`
— **bluetoothd runs the pairing**, no controller seizure, no masking.

---

## The bluetoothd-masking problem

Today `pairmodernkeyboard.sh` / `tk-pair.py` does
`systemctl mask --now bluetooth` for the duration of Phase 0 and restores it in
a `finally`. This exists because:

- Phase 0 runs SMP via **MGMT Pair Device directly on the controller**, i.e.
  *outside* `bluetoothd`.
- `bluetoothd` is D-Bus-activated — `systemctl stop` isn't enough, any D-Bus
  call respawns it within seconds.
- On (re)start it runs an adapter-init burst (Set Local Name, Write Scan Enable,
  Add UUID, advertising params) on the same controller, which tears down the
  keyboard's directed-advertising LE link **mid-SMP**.

Downsides: all Bluetooth on the machine drops for ~15–20 s during pairing (any
BT mouse/headset with it); and a `SIGKILL` between mask and unmask leaves
`bluetooth.service` masked until the user runs `systemctl unmask` by hand.

**This is a symptom of pairing outside `bluetoothd`. Phase 1 removes it entirely**
— once `bluetoothd` can be handed the TK, it does the pairing on its own D-Bus
API and never needs to be stopped. That's the cleanest fix and another reason to
upstream.

### Near-term hardening (before Phase 1) — all in this repo, no kernel change

| change | effect | risk |
|---|---|---|
| `mask --runtime --now` (symlink in `/run`, tmpfs), but **plain** `unmask` (clears both `/run` and `/etc`) | a killed run's leaked mask clears on reboot | low; test in isolation (a prior bundled attempt regressed and was reverted — the mask change was never proven to be the cause, but retest it *alone*) |
| trap `SIGTERM`/`SIGINT` in `tk-pair.py` and always unmask | covers `systemctl kill`, session teardown | low; `finally` already covers exceptions + Ctrl-C |
| **self-heal at startup**: if `systemctl is-enabled bluetooth` == `masked` and no other `mkbd-pair` is running, auto-`unmask` + warn | recovers a previously-leaked mask automatically | very low; startup-only check, can't touch the pairing path |
| unmask + restart `bluetoothd` right after Phase 0 (bond written), run Phase 4 with it up | halves the outage — Phase 4's raw L2CAP ATT socket uses the kernel LTK and doesn't need `bluetoothd` down | medium; `bluetoothd` may race Phase 4 by auto-connecting the now-Trusted device — needs a hardware test |

For the **plugin** (udev-triggered `mkbd-pair@.service`): the auto-pair unit must
no-op when a working bond already exists, so the outage happens exactly once
(first cable attach), not on every replug.

---

## Draft RFC cover letter

> Subject: `[RFC PATCH 0/5] Bluetooth: SMP support for LE legacy OOB pairing`
>
> The in-kernel Security Manager cannot pair a device that requires LE legacy
> OOB: `tk_request()` only ever sets `smp->tk` to 0 (Just Works) or a 32-bit
> passkey, and `MGMT_OP_ADD_REMOTE_OOB_DATA` has fields for BR/EDR OOB and LE
> Secure Connections OOB but not for the 128-bit legacy Temporary Key.
>
> The Microsoft Modern Keyboard with Fingerprint ID (model 1780) is such a
> device: IO capability NoInputNoOutput, MITM required, OOB-present set in its
> Pairing Response, and it terminates the connection (reason 0x13) on any
> Pairing Request without the OOB flag. Just Works is refused. The 16-byte TK
> is delivered out of band over a USB vendor HID channel. It cannot be paired
> on Linux today without taking the controller over HCI_CHANNEL_USER and
> running SMP in userspace.
>
> This series lets user space supply that TK the same way it already supplies
> hash192/rand192 for BR/EDR OOB, so bluetoothd can pair the device normally:
>
>   1/5 mgmt: accept an LE legacy OOB Temporary Key
>   2/5 SMP: use a stored LE legacy OOB Temporary Key
>   3/5 SMP: force legacy pairing when an LE legacy OOB TK is set
>   4/5 SMP: distribute local identity for LE legacy OOB bonds
>   5/5 selftest: LE legacy OOB SMP vectors
>
> Open question: extend MGMT_OP_ADD_REMOTE_OOB_DATA with a third payload size
> carrying le_legacy_tk[16] (this series), or add a dedicated opcode? Patch 4
> addresses a device behaviour (the keyboard only persists the bond after the
> initiator distributes its identity in phase 3) and may be better as a quirk —
> feedback welcome; 1-3 stand alone without it.
>
> Tested on a Microsoft Modern Keyboard 1780, Intel AX200, kernel <ver>.
> Crypto vectors in 5/5 are captured from real pairings.

## Checklist

- [ ] RFC cover letter + the 5 patches, generated `git format-patch -v1 --cover-letter`
      against `net-next` (or `bluetooth-next`)
- [ ] `checkpatch.pl --strict` clean on every patch
- [ ] `mgmt-api` doc hunk in 1/5
- [ ] `CONFIG_BT_SELFTEST_SMP=y` build + boot, selftest passes
- [ ] `smp-tester` / `mgmt-tester` (bluez `tools/`) pass, new case added
- [ ] regression: a normal Just Works LE device and a passkey LE device still pair
- [ ] `git send-email --to=linux-bluetooth@vger.kernel.org --cc=<maintainers,netdev>`
- [ ] BlueZ series posted (can follow the kernel merge)
- [ ] on merge: note the kernel version in this repo's README; DKMS drops to
      "kernels < X only"
