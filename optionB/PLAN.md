Overview
- Implement a dedicated MGMT opcode to allow userspace to add a stored LE legacy Temporary Key (TK) for LE legacy OOB pairing: MGMT_OP_ADD_REMOTE_LE_LEGACY_OOB_DATA.
- Strong semantic separation between existing add-remote-OOB command (BR/EDR & LE SC) and the LE legacy TK path.
- Allows future LE-legacy-TK-specific fields (TTL, flags, owner, persistence) to be added without impacting other MGMT semantics.

Goals
- Clear API semantics: a single opcode, explicitly typed.
- Easy to audit and discover: opcode name documents intent.
- Future-proof: fields like TTL, per-controller scope, and explicit “force legacy” flags can be added without changing an existing opcode’s semantics.
- Backwards compatible: existing implementations continue to use the existing opcode; new userspace switches to this opcode when needed.

Patch series (suggested, 1/5..5/5)
1. Bluetooth: mgmt: add MGMT_OP_ADD_REMOTE_LE_LEGACY_OOB_DATA (uapi + mgmt parsing)
2. Bluetooth: SMP: use a stored LE legacy OOB Temporary Key
3. Bluetooth: SMP: optionally force legacy pairing when an LE legacy OOB TK is set (MGMT flag)
4. Bluetooth: SMP: distribute local identity for LE legacy OOB bonds (optional/quirk)
5. Bluetooth: selftest: LE legacy OOB SMP vectors

UAPI design (sketch)
- Add new opcode constant:
  - #define MGMT_OP_ADD_REMOTE_LE_LEGACY_OOB_DATA 0x00XX  /* choose next free id */
- Define a clear typed payload struct (uapi header) for the new opcode, for example:
  - struct mgmt_add_remote_le_legacy_oob_data {
      bdaddr_t bdaddr;
      u8 addr_type;
      u8 flags; /* bit definitions below */
      u8 le_legacy_tk[16];
      /* optional: u8 persistence; u32 ttl_seconds; u32 owner_pid; */
    };
- Define flags:
  - MGMT_LE_LEGACY_OOB_FLAG_PERSISTENT (0x01) — store across controller resets (explicit)
  - MGMT_LE_LEGACY_OOB_FLAG_FORCE_LEGACY (0x02) — request controller/kernel to prefer legacy pairing for this entry (optional)
  - Future flags reserved bits.

Command semantics
- Required privileges: CAP_NET_ADMIN (or existing mgmt ACL). Document in mgmt API.
- Atomic replace semantics: adding an entry for an existing identity/address will replace the stored one.
- Persistence semantics:
  - Default: ephemeral (removed on successful pairing or controller reset). If PERSISTENT flag set, store across reboots/resets until explicitly removed.
  - If TTL support is added, TTL overrides persistence.
- Removal:
  - Provide either MGMT_OP_REMOVE_REMOTE_OOB_DATA (reuse or new opcode) with the same address/type payload to delete entries.
- Address behavior:
  - Document whether address must be identity (static/public) or whether RPA lookups are supported; recommend requiring identity address or providing an explicit identity field to avoid RPA lookup pitfalls.

Kernel changes (sketch)
- Add new mgmt opcode decoding in mgmt.c: validate payload size and auth.
- Introduce a dedicated storage structure for the new opcode or extend existing struct oob_data with a per-opcode discriminant.
- hci_add_remote_le_legacy_oob_data() or reuse hci_add_remote_oob_data() with a discriminator param.
- For security: do not log TK bytes, zero TK on removal and after successful pairing (unless flagged persistent), limit read access (no readback of TK via mgmt).
- Implement TTL/persistence/owner semantics if the flags are used; ensure multi-controller semantics are explicit (per-controller or global store).

SMP changes
- In tk_request(): when (!SMP_FLAG_SC && hcon->type == LE_LINK && hci_remote_le_legacy_oob_lookup() returns an entry) then:
  - Copy the 16-byte TK into smp->tk, set SMP_FLAG_TK_VALID | SMP_FLAG_MITM_AUTH, raise pending_sec_level.
  - Advertise SMP_OOB_PRESENT in build_pairing_cmd() as appropriate.
- For FORCE_LEGACY flag handling: if a stored entry has FORCE_LEGACY, clear SMP_AUTH_SC in pairing commands for that link (unless the device explicitly supports SC and the MGMT flag says not to).
- Post-pairing: zero TK memory and, if entry is ephemeral, remove it on success. If persistent, keep unless removal requested.
- Always ensure TK is only used on legacy path — add explicit comments and checks to prevent SC flows from using it.

BlueZ integration
- New BlueZ mgmt wrapper for the new opcode in src/shared/mgmt.h.
- BlueZ Adapter1 D-Bus API:
  - Add Adapter1.AddRemoteLegacyOOB(address, addressType, array{byte} tk, dict options)
  - options may include { "Persistent": boolean, "ForceLegacy": boolean, "TTL": uint32 } — optional.
- Polkit: restrict API to root / polkit-authorized callers.
- Ensure Pair() does not clear the stored LE legacy OOB data prematurely; BlueZ must wait until pairing completes or failure before removing unless explicitly requested.

Testing and mgmt-tester
- mgmt-tester: add cases for new opcode
  - Success: valid payload -> OK
  - Missing/incorrect TK length -> MGMT status INVALID_PARAMS
  - Insufficient permission -> MGMT status NOT_ALLOWED
  - TTL and persistence testcases
- smp selftest: same vectors as Option A: positive vectors (correct TK -> success) and negative vector (wrong TK -> fail).
- RPA identity tests: test with identity address vs RPA for lookups; document required behavior.

Security & operational notes
- Treat TK as secret: do not emit in logs, do not return via mgmt query. Access limited to privileged callers.
- Consider adding an audit event that notes “TK supplied for <bdaddr>” without content.
- Overwrite semantics: atomic replace; retain owner metadata (UID/PID) optionally.
- Consider hard limits: max number of stored entries and storage quota to prevent DoS.

Lifecycle & rollback
- When adding entry with PERSISTENT, document what happens on controller reinitialization, kernel update, or system upgrades.
- Provide removal opcode or reuse existing removal semantics.
- Consider migration guidance for distributions: how BlueZ will adopt the new opcode, and how mkbd-pair and other tools will detect and use it.

Documentation & cover letter
- Document new opcode in Documentation/networking/bluetooth/mgmt-api.rst (or mgmt-api.txt).
- Cover letter should explain why a dedicated opcode was chosen: clarity, discoverability, and future extension convenience. Provide test results and security rationale.
- Provide example usage in cover letter and an example BlueZ D-Bus snippet.

Patch filenames (suggested)
- 0001-Bluetooth-mgmt-add-MGMT_OP_ADD_REMOTE_LE_LEGACY_OOB_DATA.patch
- 0002-Bluetooth-SMP-use-stored-LE-legacy-OOB-TK.patch
- 0003-Bluetooth-SMP-force-legacy-when-LE-legacy-OOB.patch
- 0004-Bluetooth-SMP-distribute-local-identity-for-LE-legacy-OOB.patch
- 0005-Bluetooth-selftest-LE-legacy-OOB-vectors.patch

Checklist (Option B)
- [ ] uapi header: new opcode + struct defined.
- [ ] mgmt parser: accept new opcode and validate params.
- [ ] kernel storage: add/replace semantics with persistence option documented.
- [ ] SMP: use stored TK as specified and zero after use.
- [ ] BlueZ: expose Adapter1.AddRemoteLegacyOOB and ensure polkit/permission needs are clear.
- [ ] mgmt-tester & selftests added and green under CONFIG_BT_SELFTEST_SMP=y.
- [ ] cover letter, docs, and changelog for each patch.
