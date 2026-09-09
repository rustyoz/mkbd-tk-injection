# Progress log

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
