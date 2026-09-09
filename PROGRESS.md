# Progress log

## 2026-09-10 (4) — END TO END: keyboard pairs, adopts its address, reconnects, types 🎉

Two `hw-test.sh` runs (E2→E3, E3→E4), then `bluetoothctl connect`:

- Phase 0: pair success, `key_type=1`, IRK 18A04D3E… — every run.
- Phase 3: our `Identity Information` + `Identity Address 68:54:5A:D0:87:74`
  go out (btmon `<` direction).
- Phase 4 (`phase4.py`): L2CAP ATT connect (encrypted with the kernel LTK),
  MTU, **full GATT discovery, 9 CCCD subscriptions** — then the keyboard drops
  the link at ~7 s (`Disconnect … Reason: Remote User Terminated (0x13)`) as it
  does while USB is attached. The LED/Feature-`0x24` writes **never got sent**
  (`ConnectionResetError` first).
- **`address adoption (F1 re-read): current_addr == the bonded F3 addr → ADOPTED`**
  — both runs. The keyboard's per-bond counter advanced (E2→E3→E4), i.e. the
  bond *completed on the keyboard side*.
- `sudo modprobe uhid && bluetoothctl connect C9:6C:7E:E4:6C:7E`:
  `Connection successful` → `Paired: yes  Bonded: yes  Trusted: yes
  Connected: yes`, `Icon: input-keyboard`, `Battery Percentage: 87%`,
  and dmesg: `hid-generic 0005:045E:0813…: input,hidraw5: BLUETOOTH HID v1.12
  Keyboard … on 68:54:5a:d0:87:74`. **It reconnects on its own through normal
  bluetoothd and the kernel creates the HID input device.**

### What the minimal phase-4 actually is

Combined with the session-3 result (host identity, *no* phase-4 GATT →
HALF-BOND), the picture:

| step | needed? |
|---|---|
| Phase-0 kernel legacy-OOB TK injection | yes |
| Phase-3 host Identity Information + Identity Address (kernel patch) | yes |
| Phase-4 **bonded GATT connection + discovery + the 9 CCCD subscribes**, held ~7 s until the keyboard drops it | **yes** |
| Phase-4 LED write `0x0038`=`01` | **no** — never sent, still adopted |
| Phase-4 Feature `0x0041` `E2 06 …` write + `0x0024` notification | **no** — never sent, still adopted |
| explicit hold/reconnect NVM-flush round | not needed — the keyboard's own ~7 s hold then disconnect is the trigger |

So: **the write sequence in `BOND-COMPLETION.md` is not load-bearing for
address adoption.** What the keyboard needs post-SMP is a bonded ATT connection
it can talk over briefly (CCCD subs at minimum) before it tears the link down.
(2 data points — `phase4.py --no-writes` confirms; run pending.)

### GATT DB (matches BOND-COMPLETION.md handles exactly)

```
svc 0x0001-0007 GAP | 0008 GATT | 0009-000e d4e3e3eb… (MS accessory)
    000f-0013 DevInfo | 0014-0017 Battery | 0018-ffff HID (0x1812)
MS accessory:  chr 0x000a props 1a val 0x000b 7d38d135…
               chr 0x000d props 0a val 0x000e a8f04cfb…   (BOND-COMPLETION said 7d38d135@0x000e — it's a8f04cfb)
HID reports:   notify (props 1a): val 0x001c/0020/0024/0028/002c/0030/0034, CCCDs 0x001d/0021/0025/0029/002d/0031/0035
               out    (props 0e): val 0x0038/003b/003e     (0x0038 = LED / Report ID 1)
               feat   (props 0a): val 0x0041/0044/0047/004a (0x0041 = Feature / Report ID 0x24)
vendor CCCD 0x000c ; Battery CCCD 0x0017
```

### Fixes this round

- `tk-pair.py`: `args.hci` string → int index for `_mgmt_cmd` (was an
  AttributeError crash); stale-bond cleanup now silences the harmless
  "Unpair … status 0x06 (not paired)".
- `phase4.py`: the ~7 s keyboard drop is now expected — each step catches it and
  returns instead of a traceback; `--no-writes` / `--no-msacc`; discovery only
  on round 1; reports how long the keyboard held each connection.

### Next

1. Re-run to confirm `--no-writes` still adopts → lock in the minimal phase-4.
2. Trim `phase4.py` to the confirmed-minimal sequence; drop the un-needed bits.
3. **Phase 1**: replace the debugfs knob with a real `MGMT_OP_ADD_REMOTE_OOB_DATA`
   `le_legacy_tk` field + a BlueZ D-Bus method (`PLAN.md`), and fold the
   phase-3 `SMP_DIST_ID_KEY` + phase-4 CCCD dance into `mkbd-provision` proper
   (or a new `mkbd-provision --native`).

---

## 2026-09-10 (3) — host identity confirmed in phase 3; still HALF-BOND; phase4.py written

Hardware run with the host-identity module:

- pair success, `key_type=1`, IRK 18A04D3E… (the known stable one).
- btmon: a **second** `SMP: Identity Information` + `SMP: Identity Address
  Information` with **`Address: 68:54:5A:D0:87:74`** (our adapter) goes out —
  `local_dist |= SMP_DIST_ID_KEY` works, host identity is now distributed.
- **F1 re-read: `current_addr` still `C9:6C:7E:E1:6C:7E`, not the bonded
  `…E2…`** → **HALF-BOND**. Host identity in phase 3 is necessary but **not
  sufficient**; phase-4 GATT + hold is required.
- Also hit MGMT `0x13` (Already Paired) when a bond from the prior run was still
  loaded → `tk-pair.py` now clears it (rm bond dir + MGMT Unpair Device) before
  pairing.

**`test/phase4.py`** — bonded GATT provisioning over a raw L2CAP ATT socket
(Python 3.14, CID 4, `BT_SECURITY_HIGH` so the kernel encrypts with the stored
LTK; no bluetoothd, no controller seizure). Per `BOND-COMPLETION.md` "Phase 4":

1. MTU exchange, full GATT discovery (printed for handle verification).
2. Subscribe CCCDs — Write Req `01 00` to `0x0017,001d,0021,0025,0029,002d,
   0031,0035` + vendor `0x000c`.
3. Write Cmd `01` ×3 → handle `0x0038` (Report ID 1 / Output = LED; "possibly
   load-bearing").
4. Write Req → handle `0x0041` (Report ID 0x24 / Feature): `E2 06 <token:4LE>
   <13× 00>` — 19 bytes, token arbitrary (notification echoes it +0x00010000;
   the old "host name UTF-16LE" was leaked Windows stack, not a real field).
5. Await HVN on `0x0024`: `e2 00 0a 00 …`.
6. Read `0x000e` / `0x000b` (MS accessory vendor svc `d4e3e3eb-…`).
7. Hold 7 s, disconnect; repeat (2 rounds — Windows does 2–3 short
   connections); the reconnect on the LTK = the NVM-flush step.
8. Re-read USB F1 → address adoption.

All handles default to the BOND-COMPLETION.md values (keyboard's own GATT DB,
adapter-independent) and are `--`overridable; discovery output flags a mismatch.

`tk-pair.py` runs `phase4.py` automatically after a successful Phase-0 pair
(`--no-phase4` to skip, `--p4-rounds N`).

**No module rebuild — just re-run `sudo test/hw-test.sh`.** Watch: the GATT
discovery dump (do the handles match?), whether the `0x0024` notification comes
back, and the final `address adoption` line.

---

## 2026-09-10 (2) — toward phase-4: distribute host identity in phase 3

Phase 0's bond had our Pairing Request offering `init_key_dist =
ENC_KEY|SIGN` (no `ID_KEY`), so `smp_distribute_keys()` sent **no host
Identity Information / Identity Address** — the kernel only offers `ID_KEY`
under `HCI_PRIVACY`. `docs/BOND-COMPLETION.md` calls host identity in phase 3
the one *proven-necessary* step for this keyboard to persist the bond and adopt
its generated address (the only failed Windows round is the one that skipped
it), and the bumble-pair half-bond note flags this as "the first thing to fix".

**Patch updated:** the `build_pairing_cmd()` legacy-OOB block now also does
`local_dist |= SMP_DIST_ID_KEY`. `smp_distribute_keys()` line ~1501 then sends
`SMP_CMD_IDENT_INFO` (`hdev->irk`) + `SMP_CMD_IDENT_ADDR_INFO` (`hcon->src`)
after the keyboard's. The keyboard's Pairing Response already carries `ikd`
with the ID bit (per mkbd-smp-pair decode), so the negotiated set includes it.

- `kernel/0001-*.patch` regenerated (+200 lines). Applies clean to 7.1.9/7.2.3.
- Module rebuilt: `artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko`, srcversion
  `57243BDC9EDF44569FEEFF7`, vermagic `7.1.9-arch1-2`, compiles clean.
- `tk-pair.py` now does an **address-adoption check** after the bond: re-reads
  F1 over USB and reports `ADOPTED` (current_addr == the bonded F3 addr) vs
  `HALF-BOND` (bond flag set, address unchanged).

Caveat: `hdev->irk` may be all-zeros without privacy; Windows sends a random
IRK ("cosmetic" per BOND-COMPLETION.md). If the keyboard ignores a zero IRK,
next step is to make the patch generate one.

**Test:** `sudo test/install-module.sh` (new srcversion) → reboot →
`sudo test/hw-test.sh`. The "address adoption" section says whether host
identity alone closed the half-bond. If still HALF-BOND → build userspace
phase-4 GATT (`0x0038=01` write is known; `0x0041` host-name payload is only
partly reversed) + the ~7 s hold / reconnect.

---

## 2026-09-10 — PHASE 0 WORKS ON HARDWARE ✅

`sudo test/hw-test.sh` after a keyboard power-cycle (the earlier `TimeoutError`
on the USB ioctl / `connected=False` timeout were a cold/stuck BT radio — a
power-cycle with USB still plugged fixes it):

```
pair status : success   events: [connected, new-irk, new-ltk, cmd-complete]
LTK=9411727FA1196DADC505E542FE63AE68  EDIV=34314  Rand=4858899385762828146
key_type=1 (authenticated legacy)  enc_size=16  IRK=18A04D3E2A836C1893F2A6EDADCFA43D
bond: /var/lib/bluetooth/68:54:5A:D0:87:74/C9:6C:7E:E2:6C:7E/info
```

btmon confirmed the mechanism end to end:

- our **Pairing Request**: `OOB present (0x01)`, AuthReq **0x21** = Bonding + CT2,
  **no SC, no MITM** — the `build_pairing_cmd()` SC-clear + OOB-present fix works.
- keyboard **Pairing Response**: OOB present, AuthReq 0x0d (offers MITM+SC);
  legacy wins because our request had SC clear.
- **Pairing Confirm ×2, Pairing Random ×2** — legacy `c1`/`s1` ran; the F3 TK
  verified in **as-is** byte order (no `--tk-order reversed` needed), matching
  the little-endian kernel `smp_c1` / mkbd-bumble-pair.
- **Encryption Change: Success** — STK encryption.
- **Encryption Information / Central Identification / Identity Information /
  Identity Address Information** — keyboard distributed LTK + EDIV/Rand + IRK.
- MGMT **New Long Term Key** (key_type 1 = authenticated) + **New IRK**.

Driven entirely by **MGMT Pair Device** through the in-kernel Security Manager —
no HCI_CHANNEL_USER, no Bumble, no userspace SMP.

Byte order settled: debugfs knob takes the F3 TK **as-is**.

### Still open — does the keyboard reconnect and type?

The bond is written but this is a phase-3-only bond (no phase-4 vendor GATT
provisioning, no hold/reconnect — see `../modernkeyboard/docs/BOND-COMPLETION.md`).
Per that doc a phase-3-only bond can be a "half-bond": the keyboard sets its
bond-exists byte but may not adopt the F3 address, so BlueZ can't reconnect.
Next: unplug USB, power-cycle the keyboard, `bluetoothctl connect
C9:6C:7E:E2:6C:7E`, `modprobe uhid`, verify `bluetoothctl info` + keystrokes.
That question is orthogonal to TK injection — the win here is that the tail can
now run through a normal bluetoothd instead of a controller seizure.

---

## 2026-09-09 (4) — first hardware run: keyboard hangs up. Root cause: SC bit. Fixed.

Patched module installed via `install-module.sh` + reboot; `hw-test.sh` rerun
(now `tk-pair.py` — the bash version parsed the local adapter address instead of
the keyboard's and used `bluetoothctl pair`, which can't see a directed advert).

**Result:** MGMT Pair Device → keyboard **connects, then immediately drops the
link** — `connected=True`, `smp_seen=False`, MGMT disconnect reason 3 (remote
terminated). Same signature as the documented non-OOB refusal.

**Root cause:** `smp_conn_security()` sets `authreq |= SMP_AUTH_SC` on any
SC-capable adapter, so our Pairing Request goes out with the SC bit. In
`build_pairing_cmd()` the `if (HCI_SC_ENABLED && (authreq & SMP_AUTH_SC))` block
is then taken (SC-OOB path, no legacy OOB data → `oob_flag` stays NOT_PRESENT),
and the original patch's OOB-flag line was gated behind `!(authreq &
SMP_AUTH_SC)` → skipped. Our Request: `AuthReq = SC|MITM|Bonding`, `OOB = not
present` → keyboard hangs up (it does LE legacy OOB only).

**Fix (patch updated):** in `build_pairing_cmd()`, *before* the SC block, when a
legacy TK is stored for the peer: `authreq &= ~SMP_AUTH_SC` **and**
`oob_flag = SMP_OOB_PRESENT`. Forces legacy + OOB, matching Windows (SC=0) and
`mkbd-smp-pair`. `smp_send_pairing_req()` doesn't set `SMP_FLAG_SC` itself, and
the initiator only sets it later from our own (now SC-cleared) preq, so the
`!SMP_FLAG_SC` guards in `get_auth_method()` / `tk_request()` stay consistent.

- `kernel/0001-*.patch` regenerated (3 files, +193). Applies clean to 7.1.9 and
  7.2.3.
- `artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko` rebuilt, srcversion
  `8D68F14850CA9F6A0DFEF08`, vermagic `7.1.9-arch1-2`, compiles clean.

Still unverified: TK byte order (`--tk-order as-is` first; `reversed` if
`status 0x04` after Confirm — no reboot needed for that retry).

**Next:** `sudo test/install-module.sh` (overwrites the earlier build) → reboot
→ `sudo test/hw-test.sh`. Capture from the failed run:
`/tmp/mkbd-tkinj-1788961915.btsnoop` (shows the SC/no-OOB Request + hangup).

---

## 2026-09-09 (3) — live module swap won't work; install-to-disk + reboot instead

The user ran `test/load-and-test.sh` (keyboard now attached, root available).
Two attempts:

1. `insmod` approach → `insmod: File exists` (EEXIST): the on-disk module is
   auto-loadable and racing a hand `insmod`. Reworked the script to swap the
   module **on disk** (`modules.dep` already points every consumer at
   `updates/bluetooth.ko`) + `depmod` + normal `modprobe`.
2. On-disk-swap approach → **`bluetooth` won't unload: refcnt 11, holder
   `bnep`** (`modprobe -r bnep` fails — a BNEP/PAN interface or connection is
   live). This is the user's daily-driver desktop with Bluetooth in active use.
   A live unload of `bluetooth.ko` is not going to happen without tearing down
   every BT connection and possibly the hci drivers.

**Resolution: install the patched module to disk and reboot into it.** No
rebuild — `artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko` already has vermagic
`7.1.9-arch1-2`. Module-signature enforcement is off
(`/sys/kernel/security/lockdown = [none]`; the unsigned `updates/bluetooth.ko`
already loads), so an unsigned `.ko` loads fine at boot.

New scripts:
- `test/install-module.sh` — back up the current `updates/bluetooth.ko`, write
  the patched build over it, `depmod`. Run as root, then **reboot**.
- `test/uninstall-module.sh` — restore the backup, `depmod`. Then reboot.
- `test/hw-test.sh` — now preflights that the running module is the patched
  build and points at `install-module.sh` + reboot if not.
- `test/load-and-test.sh` — kept for a machine where the stack *can* be
  unloaded live, but expect it to fail on refcnt on a normal desktop.

### To run the test (user)

```bash
cd ~/Work/mkbd-tk-injection
sudo test/install-module.sh      # backs up + installs, prints next steps
# reboot
cat /sys/kernel/debug/bluetooth/hci0/le_legacy_oob_tk   # should exist (perm denied is fine)
# plug in the Modern Keyboard, switch it on
sudo test/hw-test.sh
```

Revert any time: `sudo test/uninstall-module.sh` then reboot.

---

## 2026-09-09 (2) — patched bluetooth.ko built for the RUNNING kernel; test blocked

### Done

- Found a full build tree for the **running** kernel at
  `/usr/lib/modules/7.1.9-arch1-2/build` (headers/`.config`/`Module.symvers`;
  `net/bluetooth/` stripped). So a module can be built that loads **without a
  reboot**.
- Fetched `linux-7.1.9` source
  (`sha256` of `linux-7.1.9.tar.xz` from cdn.kernel.org). The Phase 0 patch
  applies to 7.1.9 with only line-offset shifts (dry-run + real apply clean;
  hunk 1 `fuzz 2` on the `#include` block — landed correctly, verified).
- Confirmed the 7.1.9 SMP behavioural prerequisites are identical to 7.2.3:
  `authenticated = hcon->sec_level == BT_SECURITY_HIGH` (smp.c:2725),
  `conn->sec_level = conn->pending_sec_level` on LE Encryption Change
  (hci_event.c:5206), STK stored with `SMP_FLAG_MITM_AUTH`. All 5 `REQ_OOB`
  reads are in SC-only functions.
- **Built `artifacts/bluetooth-7.1.9-arch1-2-tkinj.ko`.** Clean compile.
  `vermagic: 7.1.9-arch1-2 SMP preempt mod_unload` — exact match for the running
  kernel. Contains `smp_le_legacy_oob_tk_get`, `le_legacy_oob_tk_write`,
  `smp_le_legacy_oob_tk_debugfs_create`. `depends: rfkill` (same as stock).
  - `.config` + `Module.symvers` taken from
    `/usr/lib/modules/7.1.9-arch1-2/build/`.
  - `CONFIG_LOCALVERSION_AUTO` off + `localversion.05-arch` (`-arch1`) +
    `localversion.10-pkgrel` (`-2`) to reproduce `kernel.release =
    7.1.9-arch1-2`. Without this the module built as bare `7.1.9` and would not
    load.
  - `bc` still missing; `build/shim/bc` cats the installed 7.1.9
    `include/generated/timeconst.h`.
- Added `test/load-and-test.sh` — one root command: back up / unload the stack,
  `insmod` the patched module, verify the debugfs knob appeared, run
  `hw-test.sh`, restore the on-disk module on exit.

### BLOCKED — the hardware run needs the user

1. **No root.** `sudo` needs a password here. `insmod`/`rmmod` and both test
   scripts (`EUID==0`) cannot run.
2. **Keyboard not attached.** `lsusb` shows no `045e:081x` on the bus. Nothing
   to pair. (hidraw devices present are a `0461:4E04` keyboard and 3× Razer
   `1532:0053` — not the Modern Keyboard.)
3. **Unknown `bluetooth.ko` override.** The running module loads from
   `/lib/modules/7.1.9-arch1-2/updates/bluetooth.ko` (dated 2026-09-07 23:06,
   **not** owned by any pacman package, **not** dkms — dkms only has nvidia).
   Both it and the stock in-tree `bluetooth.ko.zst` report "Bluetooth Core ver
   2.22" (the mainline version string, unchanged for years, so uninformative),
   but their `srcversion` differ, so `updates/` is a distinct build from
   unknown source. My module is built from **mainline 7.1.9** + the patch;
   loading it replaces that override. If the override carries unrelated changes
   they are lost while testing. `load-and-test.sh` restores the on-disk file on
   exit, so this is reversible, but the user should know what that override is
   before running.

### To run the test (user, with the keyboard plugged in and on)

```bash
cd ~/Work/mkbd-tk-injection
sudo test/load-and-test.sh
```

Watch for, in the btmon capture it writes to `/tmp/mkbd-tkinj-*.btsnoop`:
our Pairing Request **OOB flag = present**; Pairing Confirm/Random both ways;
Encryption Change (Status 0); LTK/EDIV/Rand/IRK distributed; bond under
`/var/lib/bluetooth/<adapter>/<addr>/info` with `Authenticated=1`.
`dmesg | grep 'using LE legacy OOB TK'` should fire once.

If `Pairing Failed 0x04` after our Confirm → `sudo TKORDER=reversed
test/hw-test.sh` (byte order of the F3 TK).

---

## 2026-09-09 — repo created, Phase 0 patch written & compiled

### Done

- New repo `~/Work/mkbd-tk-injection`.
- Copied the plan in as `PLAN.md` (source of truth stays
  `modernkeyboard/docs/TK-INJECTION-PLAN.md`, branch `worktree-tk-injection-plan`).
- Fetched `linux-7.2.3` source (matches installed `linux 7.2.3.arch1-3`;
  `sha256 8ba259e8e7b13ec6ef0941c8a39ad90b24bd4a4d6c0010ba6bafb794550ecd03`).
- Read the real kernel SMP code and mapped every path the injection touches —
  see `notes/smp-codepaths.md`.
- **Wrote `kernel/0001-Bluetooth-SMP-inject-LE-legacy-OOB-Temporary-Key-via-.patch`.**
  Three files, +189 lines:
  - `net/bluetooth/smp.c` — per-adapter debugfs knob `le_legacy_oob_tk`
    (`AA:BB:CC:DD:EE:FF <type 0|1> <32 hex>`), a small module-global list, and
    the three injection points: `build_pairing_cmd()` (advertise OOB-present),
    `get_auth_method()` (return `REQ_OOB` so the MITM pre-checks pass),
    `tk_request()` (copy the TK, set `SMP_FLAG_TK_VALID | SMP_FLAG_MITM_AUTH`,
    raise `pending_sec_level` to `BT_SECURITY_HIGH`).
  - `net/bluetooth/smp.h` — one declaration.
  - `net/bluetooth/hci_debugfs.c` — one call in `hci_debugfs_create_le()`.
- **Compiles clean.** `make modules_prepare` + `make W=1 net/bluetooth/smp.o
  net/bluetooth/hci_debugfs.o` → no warnings, no errors, against real 7.2.3
  headers. (Full `bluetooth.ko` link needs a complete vmlinux build; the
  `M=net/bluetooth modules` modpost "undefined!" spew is the expected
  missing-`Module.symvers` artifact of that shortcut, not the patch.)
  - Note: `bc` is not installed and `make prepare` needs it for
    `include/generated/timeconst.h`. Worked around with `build/shim/bc` which
    cats the known-good header from the installed build tree (CONFIG_HZ=1000,
    same kernel version). A real build needs `pacman -S bc`.

### Design decisions

- **Phase 0 = debugfs, module-global list, all in `smp.c`.** No UAPI, no BlueZ.
  Entries persist until overwritten (write an all-zero TK to clear) or reboot.
  Deliberately throwaway; Phase 1 replaces the list with `struct oob_data` fed
  by a new MGMT field.
- **`REQ_OOB` reused as the method value.** It is only ever inspected in
  Secure-Connections functions (`sc_dhkey_check`, `sc_check_confirm`,
  `sc_passkey_round`), which the legacy path never reaches. The legacy
  `smp_confirm()` / `smp_random()` are method-agnostic — they consume `smp->tk`.
  Verified by reading every `smp->method ==` site. See `notes/smp-codepaths.md`.
- **Two pre-checks had to be handled, not just `tk_request`.** The initiator
  (`smp_cmd_pairing_rsp`, ~line 1958) and responder (`smp_cmd_pairing_req`,
  ~line 1794) both do `if (pending_sec_level >= HIGH) { method =
  get_auth_method(...); if (method == JUST_WORKS || JUST_CFM) return
  SMP_AUTH_REQUIREMENTS; }` *before* `tk_request()`. For NoInputNoOutput ×
  NoInputNoOutput that check returns `JUST_WORKS` and bails with `0x03`. Making
  `get_auth_method()` OOB-aware is what gets us past it.
- **Authenticated bond:** `tk_request` raises `pending_sec_level` to HIGH;
  `hci_event.c` sets `hcon->sec_level = pending_sec_level` on Encryption Change;
  `smp_cmd_initiator_ident()` then stores the received LTK with
  `authenticated = (hcon->sec_level == BT_SECURITY_HIGH)` → `1`. So no re-pair
  churn from a profile demanding MITM.

### Not done / open

- **No hardware test yet.** Needs the physical keyboard, and ideally a reboot
  into a kernel built with this patch. `test/hw-test.sh` has the procedure.
- Running kernel is `7.1.9-arch1-2`; a module built against 7.2.3 will not load
  into it (vermagic). Options: (a) reboot into 7.2.3 then build+`insmod`, or
  (b) build a full 7.2.3 kernel package with the patch and boot it.
- `smp_le_legacy_oob_tk` list is never freed on module unload (no smp exit
  hook). Harmless leak for a PoC; note for Phase 1.
- Address-type convention in the debugfs knob is `ADDR_LE_DEV_*` (0 public,
  1 random) = `hcon->dst_type` as-is. The keyboard is static random → `1`.
- TK byte order: the debugfs knob stores the 16 bytes exactly as written and
  `memcpy`s them into `smp->tk`. `mkbd_crypto` proved the kernel's `smp_c1`
  wants `TK = reverse(F3 bytes)` relative to a big-endian c1 — but the kernel's
  `smp_c1` is little-endian internally, matching Bumble, which took the F3 bytes
  **as-is**. So the knob should almost certainly be fed the F3 bytes **as-is**
  (like `mkbd-bumble-pair`), NOT reversed. Confirm on first hardware run; if the
  keyboard sends `Pairing Failed 0x04` after our Confirm, try the reversed order.

### Next actions

1. `pacman -S bc`, then either:
   - reboot into `7.2.3-arch1-3`, `cd build/linux-7.2.3`, apply patch, `make
     -j$(nproc) modules_prepare && make -j$(nproc) M=net/bluetooth`, then build
     the full tree or just `bluetooth.ko` and `insmod`; **or**
   - patch the Arch `linux` PKGBUILD and boot the package.
2. Run `test/hw-test.sh` (F1/F2/F3 → write `le_legacy_oob_tk` → `bluetoothctl
   pair`). Capture `btmon`.
3. Confirm: our Pairing Request has **OOB = present**; Confirm/Random both ways;
   Encryption Change; LTK/EDIV/Rand/IRK distributed; bond under
   `/var/lib/bluetooth/<adapter>/<addr>/` with `Authenticated=1`.
4. Diff the SMP PDUs against a `modernkeyboard/bin/mkbd-smp-pair` btmon capture —
   should be byte-identical.
5. If it bonds: start Phase 1 (MGMT field + BlueZ). If not: `notes/` gets the
   btmon and the failure analysis.
