# Option A — Extend MGMT_OP_ADD_REMOTE_OOB_DATA (third payload length)

This folder documents the plan to implement "Option A": extend the existing
MGMT_OP_ADD_REMOTE_OOB_DATA command so that a third accepted payload length
carries an LE legacy 128-bit Temporary Key (TK) plus an explicit presence
flag. The change is additive and backward-compatible: callers that send the
previous sizes remain unaffected.

Goals
- Minimal kernel surface and reviewers' burden.
- Backwards compatible: old userspace keeps working.
- Explicit presence bit to avoid ambiguity with an all-zero TK.
- Secure handling: TK treated as secret, not logged, zeroed after use.

Patch series (suggested, 1/5..5/5)
1. Bluetooth: mgmt: accept an LE legacy OOB Temporary Key (UAPI + mgmt parse)
2. Bluetooth: SMP: use a stored LE legacy OOB Temporary Key (tk_request changes)
3. Bluetooth: SMP: force legacy pairing when an LE legacy OOB TK is set (opt-in behavior)
4. Bluetooth: SMP: distribute local identity for LE legacy OOB bonds (optional quirk)
5. Bluetooth: selftest: LE legacy OOB SMP vectors

UAPI design (sketch)
- Keep existing constants.
- Add a new size constant for the extended payload.
- Add a 1-byte flags field and 16-byte `le_legacy_tk` appended to the payload.

Example header diff (uapi/include/uapi/linux/mgmt.h) — sketch
```c
-#define MGMT_ADD_REMOTE_OOB_DATA_SIZE  (sizeof(bdaddr_t) + ...)
+#define MGMT_ADD_REMOTE_OOB_DATA_SIZE  (...)
+#define MGMT_ADD_REMOTE_OOB_DATA_SIZE_LE_LEGACY  (MGMT_ADD_REMOTE_OOB_DATA_SIZE + 1 + 16)
+
+/* flags for extended size */
+#define MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT 0x01
```

Payload layout for new size
- addr (bdaddr)
- addr_type (u8)
- hash192 (24) + rand192 (16) (same offsets as existing fields)
- flags (u8) — bit 0: LE_LEGACY_TK_PRESENT
- le_legacy_tk[16]

Kernel side: data structures and storage
- include/net/bluetooth/hci_core.h: extend struct oob_data:
```c
struct oob_data {
    bdaddr_t addr;
    u8 addr_type;
    u8 hash192[24];
    u8 rand192[16];
    u8 hash256[32];
    u8 rand256[16];
    u8 le_legacy_tk[16];
    bool le_legacy_tk_present;
    /* other fields */
};
```
- hci_add_remote_oob_data() signature updated internally to accept flags or a presence boolean.
- Storage semantics: per-controller mapping by identity address + type. Atomic replace on add.

MGMT parsing (sketch)
- In mgmt.c "add_remote_oob_data()" accept the new length; parse flags and copy le_legacy_tk if present.
- Validate array length strictly; if flags indicate presence but TK length not present -> return MGMT_STATUS_INVALID_PARAMS.
- Do not log the TK in trace logs.

SMP changes
- In tk_request(): when (!SMP_FLAG_SC && hcon->type == LE_LINK && lookup returns entry with le_legacy_tk_present) then:
  - memcpy(smp->tk, oob->le_legacy_tk, 16);
  - set SMP_FLAG_TK_VALID | SMP_FLAG_MITM_AUTH;
  - set pending_sec_level = BT_SECURITY_HIGH;
  - advertise SMP_OOB_PRESENT in build_pairing_cmd().
- Zero the stored TK in memory on successful pairing and optionally on failure after a short TTL.
- Force-legacy behavior: either automatic (clear SMP_AUTH_SC bit) or controllable by a MGMT flag. Recommend opt-in via MGMT flag or default automatic but document clearly and make it reversible in userspace.

BlueZ changes
- src/shared/mgmt.h: accept the new size constants and flags.
- src/adapter.c: extend btd_adapter_add_remote_oob_data() to forward the 16-byte TK and flags to MGMT.
- D-Bus API: Adapter1.AddRemoteLegacyOOB(address, type, array{byte} tk)
  - Minimal: a convenience wrapper that builds MGMT payload with flag set.
  - BlueZ should not expose the raw TK to unprivileged callers; only root or polkit-authorized processes may call this method.
- Ensure MGMT_OP_PAIR_DEVICE does not clear stored OOB data prematurely.

mgmt-tester and tests
- Add mgmt-tester case: send the extended size with flags=1 + 16-byte TK -> expect success.
- Negative cases: wrong length, flags=1 but no TK bytes, insufficient permission (EPERM) if called from non-privileged process.

Selftests
- Add net/bluetooth/selftest.c vectors for LE legacy OOB using the vectors captured in this repo's lib/mkbd_crypto.py.
- Add negative vectors: wrong TK -> pairing fails.
- Add RPA/identity test: device uses RPA; ensure lookup uses identity address if provided.

Security & permissions
- Require CAP_NET_ADMIN (or equivalent mgmt ACL) to add remote OOB TK.
- Do not include TK bytes in audit logs.
- Zero TK memory after use and on explicit removal.
- Define overwrite semantics as atomic replace; document behavior.

Rollout & compatibility
- Option A allows staged rollout: kernel change landed, BlueZ updated later to call new size; userspace helpers (mkbd-pair) updated afterwards.
- If maintainers ask for a new opcode, be prepared to rework to Option B — design choices meant to be small and easily rebaseable.

Patch filenames (suggested)
- 0001-Bluetooth-mgmt-accept-LE-legacy-OOB-TK.patch
- 0002-Bluetooth-SMP-use-stored-LE-legacy-OOB-TK.patch
- 0003-Bluetooth-SMP-force-legacy-when-LE-legacy-OOB.patch
- 0004-Bluetooth-SMP-distribute-local-identity-for-LE-legacy-OOB.patch
- 0005-Bluetooth-selftest-LE-legacy-OOB-vectors.patch

Checklist (Option A)
- [ ] UAPI header added, mgmt parse updated, compile tested.
- [ ] SMP logic uses stored TK correctly and zeroes it after use.
- [ ] BlueZ Adapter API implemented and polkit rule considered.
- [ ] mgmt-tester & selftests added and passing under CONFIG_BT_SELFTEST_SMP=y.
- [ ] Cover letter states the additive nature, permission model, and security handling.
