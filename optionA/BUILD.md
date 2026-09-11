# Option A — building against the running kernel (7.1.9-arch1-2)

The canonical five patches in this directory are written and verified against
**linux-7.2.3** (see `PLAN.md`'s "as built" note and `build/optionA/linux-7.2.3`,
a git tree with one commit per patch on a `linux-7.2.3 baseline` commit).

For hardware end-to-end testing, the module has to match the *running* kernel
(`uname -r` = `7.1.9-arch1-2` on this box, same constraint Phase 0 hit — see
`../kernel/README.md`). This file is the recipe for that build; the resulting
module is `../artifacts/bluetooth-7.1.9-arch1-2-optionA.ko`.

## Recipe

```bash
cd build
mkdir optionA-719 && cd optionA-719
tar -xf ../linux-7.1.9.tar.xz
cd linux-7.1.9
git init -q && git add -A && git commit -q -m 'linux-7.1.9 baseline'
git am ../../../optionA/0001-*.patch \
       ../../../optionA/0002-*.patch \
       ../../../optionA/0003-*.patch \
       ../../../optionA/0004-*.patch
# 0005 (selftest) does NOT apply here — see "Patch 5" below.

cp /usr/lib/modules/7.1.9-arch1-2/build/Module.symvers .   # for a linkable .ko
cp ../../linux-7.1.9/localversion.* .                       # -> vermagic 7.1.9-arch1-2, not 7.1.9+
rm -rf .git                                                  # setlocalversion appends "+" for a dirty git tree
cp ../../linux-7.1.9/.config .

export PATH="$PWD/../../shim:$PATH"                          # `bc` shim: cats the installed kernel's
                                                               # timeconst.h (no `pacman -S bc` needed)
make -j"$(nproc)" olddefconfig
make -j"$(nproc)" modules_prepare
make -j"$(nproc)" M=net/bluetooth

modinfo net/bluetooth/bluetooth.ko | grep -E 'vermagic|srcversion'
# vermagic:  7.1.9-arch1-2 SMP preempt mod_unload   <- must match `uname -r`
```

Two things that are easy to get wrong and silently produce an unloadable or
falsely-successful module:

- **Missing `Module.symvers`** → `make M=net/bluetooth` compiles every `.o`
  fine but modpost fails with a wall of `undefined!` errors and never produces
  `bluetooth.ko`. This is the same "expected spew" `../kernel/README.md`
  documents for the Phase 0 build; the fix is the same — copy
  `Module.symvers` from `/usr/lib/modules/$(uname -r)/build/`.
- **`vermagic` reads `7.1.9+` instead of `7.1.9-arch1-2`** → looks like a
  successful build, but `insmod`/`install-module.sh` will refuse it (or worse,
  load it into the wrong ABI). Cause: `scripts/setlocalversion` appends `+`
  when it finds a git repository without the distro's
  `localversion.05-arch` / `localversion.10-pkgrel` files that spell out the
  `-arch1-2` suffix. Fix: copy those two files in, and build without a `.git`
  present (or `git commit` a completely clean, tag-matching tree — copying
  the files and dropping `.git` is simpler for a one-off build).

## Patch 5 (selftest) does not apply to 7.1.9 as-is

`git am` on `0005-Bluetooth-selftest-LE-legacy-OOB-vectors.patch` fails:

```
error: sha1 information is lacking or useless (net/bluetooth/smp.c).
error: could not build fake ancestor
```

`git apply --3way` gets further but still fails — the real conflict is that
`run_selftests()` in `net/bluetooth/smp.c` has a different signature between
these two versions:

| Tree | `run_selftests()` |
|---|---|
| 7.2.3 (patch written against) | `run_selftests(struct crypto_kpp *tfm_ecdh)` |
| 7.1.9 (this tree) | `run_selftests(struct crypto_shash *tfm_cmac, struct crypto_kpp *tfm_ecdh)` |

Patch 5 only touches this function to add one `test_le_legacy_oob()` call and
its call site's context lines don't match. This is a portability gap in the
selftest patch, not a functional problem — 1-4 (the actual mgmt/SMP behavior
under test) applied and built clean on both kernels. Not fixed here because
it's not needed for hardware end-to-end testing (that exercises the real SMP
path over the air, not the boot-time selftest); fix it before submitting patch
5 upstream against whatever tree that targets, by hand-adapting the
`run_selftests()` call site to match.

## Status

- [x] Patches 1-4 apply cleanly to both linux-7.2.3 (canonical) and linux-7.1.9
      (running kernel).
- [x] `net/bluetooth/bluetooth.ko` links with the correct vermagic for
      `7.1.9-arch1-2`, staged at `../artifacts/bluetooth-7.1.9-arch1-2-optionA.ko`.
- [x] Installed and booted: `/sys/module/bluetooth/srcversion` on the running
      system reads `25B2F2F6E13453DFB2E7358`, matching this build exactly.
- [x] **Hardware-verified end to end (2026-09-11).** `pairmodernkeyboard.sh
      --option-a` completed a real pairing: `bluetoothctl info` on the
      keyboard (`C9:6C:7E:F4:6C:7E`) shows `Paired: yes`, `Bonded: yes`,
      `Trusted: yes`, `Connected: yes`, full GATT service resolution (HID,
      Battery at 86%, the vendor service), and the kernel created live `uhid`
      input devices (`/proc/bus/input/devices` — `BTLE Keyboard Fingerprint ID`,
      full keymap) that a normal LE HID-over-GATT bond produces. This is the
      MGMT_OP_ADD_REMOTE_OOB_DATA (len-88) path in patches 1-4 actually
      authenticating and completing SMP against the real keyboard, not just
      compiling.
- [ ] The patched BlueZ (`../bluez/`) is still not installed — this pairing
      went through the raw `mgmt` socket path
      (`test/optionA-pair.py`/`pairmodernkeyboard.sh --option-a`), with
      `bluetoothd` stopped for the duration, same as Phase 0. BlueZ now owns
      the reconnect (it's a normal Trusted bond), but `Adapter1.AddRemoteLegacyOOB()`
      itself has not been exercised live.
- [x] **Reconfirmed after a clean reboot (2026-09-11, later same evening),
      via `autopair/` + a manual "Connect" click in the GUI + a keyboard
      power-cycle**: `C9:6C:7E:F7:6C:7E` shows `Paired`/`Bonded`/`Trusted`/
      `Connected` all `yes`. Auto-reconnect via `Trusted` alone was not quite
      enough this time — a manual connect kick plus power-cycling the
      keyboard was needed on top of the autopair-driven pair. Worth keeping
      in mind for the UX: "unplug and it just reconnects" is not fully
      reliable yet.

### Known issue: repeated same-boot attempts degrade and can misfire

Across ~8-10 pairing attempts in one evening (both the D-Bus experiment and
the raw-mgmt path, mostly against the same peer identity), behavior drifted
from a clean full SMP exchange + `Encryption Change: Success`, to the
identical code sending only one `Pairing Random` before the **host** issued
an `HCI Disconnect` (reason `0x15`, before any SMP failure PDU) with no code
changes in between. `dmesg` showed the tell: `ACL packet for unknown
connection handle 3585/3586` recurring across nearly an hour of uptime —
i.e. a leaked/uncleaned connection or SMP state object, most likely from
patch 2 or 3 not tearing down cleanly on an aborted OOB-legacy pairing.

**Workaround that reliably resolves it: reboot.** A clean boot cleared
whatever state had accumulated, and the very next attempt paired correctly.
**Not yet root-caused or fixed in the kernel patches** — if this recurs,
suspect the same mechanism before assuming a new bug, and don't burn many
retries against the same peer address in one boot while investigating (each
attempt also risks the keyboard's own bond state — see
`autopair/README.md` "The D-Bus pairing experiment").

### Userspace bug found and fixed

`mkbd_common.mgmt_pair_device()` had a race: it treated
`MGMT_OP_PAIR_DEVICE`'s Command Complete as terminal the instant it arrived,
but on this kernel it can arrive *before* `MGMT_EV_NEW_LTK` — so a pairing
that was actually completing correctly (full SMP exchange, real
`Encryption Change: Success` in a `btmon` capture) got reported as
`FAIL: no LTK distributed`. Fixed 2026-09-11 to keep listening for the LTK
(with a short grace period for a following IRK) instead of trusting the
premature success. This was likely responsible for at least one of the
"failed" attempts in the same-boot-degradation story above actually having
been a real pairing, misreported and then retried unnecessarily.

Found and fixed in the sibling `modernkeyboard` repo (committed there,
`lib/mkbd_common.py` @ `fc2b8d98d0d6eb1fd9bc04795ba973975b6e723f`, separate
git history from this one) and then vendored into `../lib/mkbd_common.py` in
this repo so `test/*.py` no longer depends on a sibling checkout — see that
file's header. Port future fixes from the canonical copy by hand.
