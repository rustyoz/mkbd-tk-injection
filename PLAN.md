# Plan — add LE legacy OOB TK injection to the Linux stack (kernel SMP + BlueZ)

## Goal

Let BlueZ pair this keyboard through the **in-kernel** Security Manager, by giving
the kernel a way to accept a 128-bit LE-legacy-OOB Temporary Key from userspace.
Today the kernel SMP has no such path (`smp->tk` is only ever set to 0 for Just
Works or a 32-bit passkey), so the only working routes are the userspace-host
hacks (`mkbd-bumble-pair`, `mkbd-smp-pair`) that seize the controller over
`HCI_CHANNEL_USER`, or the `mkbd-transplant` copy-from-Windows.

This is a **necessary but not sufficient** piece for "the keyboard reconnects on
Linux": after SMP the keyboard still needs the phase-4 vendor GATT provisioning +
the hold/reconnect described in `docs/BOND-COMPLETION.md` or it leaves a
half-bond. TK injection is what lets that tail run through a normal `bluetoothd`
(GATT client, no controller seizure) instead of a bespoke host stack.

## Why it's blocked today (precise)

LE legacy pairing derives the STK from a TK (Core Spec Vol 3 Part H 2.3.5.2):

| method       | TK                                  |
|--------------|-------------------------------------|
| Just Works   | `0`                                 |
| Passkey      | 6 decimal digits, zero-padded to 128 bit |
| **OOB**      | **128-bit value transferred out of band** |

In `net/bluetooth/smp.c`:

- `smp_allocate_smp()` kzallocs `struct smp_chan`, so `smp->tk` starts zeroed.
- `tk_request()` selects the method from the two IO caps + OOB flags + MITM bit
  and fills `smp->tk`. For Just Works it leaves it zero; for Passkey it writes the
  32-bit value; **for the OOB branch on the legacy path there is no source for the
  value** — the code either falls through with `tk = 0` or (SC path) uses the
  `r`/`Cr` confirm material from `hci_remote_oob_data_lookup()`, which is a
  different thing (SC OOB, not a legacy TK).
- `MGMT_OP_ADD_REMOTE_OOB_DATA` / `struct mgmt_cp_add_remote_oob_data` carries
  `hash192/rand192/hash256/rand256` — BR/EDR OOB and **LE Secure Connections**
  OOB only. No `tk` field. Kernel doc `mgmt-api.txt` says as much: *"there is no
  support for providing the Security Manager TK Value for LE legacy pairing."*
- The only userspace writer of legacy TK material is `USER_PASSKEY_REPLY`, 32
  bits — unusable for a full 128-bit OOB TK.

This keyboard forces the issue: IO cap `NoInputNoOutput`, sets OOB-present in its
Pairing Response, sets the MITM bit (AuthReq `0x0d`), and **hangs up (`0x13`) on
any non-OOB Pairing Request** (tested — HANDOFF.md). Just Works is refused; OOB
with the real TK is mandatory.

## Reference implementation already in this repo

The crypto and PDU flow are done and proven — the kernel work is moving a
known-correct sequence to the right place, not new reverse engineering:

| Piece | Where | Status |
|---|---|---|
| AES-128, `c1`, `s1` | `lib/mkbd_crypto.py` | self-test vs FIPS-197 + Core D.1/D.2 **+ 3 real keyboard pairings** |
| legacy-OOB SMP as initiator | `bin/mkbd-smp-pair` `do_smp()` | works end to end over `HCI_CHANNEL_USER` |
| TK byte order | `mkbd_crypto._vec` | `TK = reverse(F3 bytes)` for a big-endian `c1`; verified |
| test vectors | `mkbd_crypto._vec` | `(F3 TK, Mrand, Srand, Mconfirm, Sconfirm, kbd addr, STK)` ×3 |

The kernel's own `smp_c1()` / `smp_s1()` are already correct for standard legacy
pairing; they have simply never been handed a non-zero OOB TK. So the risk is
almost entirely in the *plumbing* (get 16 bytes from userspace into `smp->tk`
before `tk_request()` runs the confirm), not the maths.

---

## Phase 0 — debugfs proof of concept (kernel only, ~1 day)

Fastest way to confirm the injection point is right, with no UAPI and no BlueZ
changes. Throwaway.

1. **`net/bluetooth/hci_core.h`** — add `u8 tk[16]; bool tk_present;` to
   `struct oob_data`.
2. **`net/bluetooth/smp.c`**:
   - New debugfs file per hdev (near the existing `smp` debugfs entries in
     `smp_register()`): `le_legacy_oob_tk`, write-only, format
     `AA:BB:CC:DD:EE:FF <type 0|1> <32 hex>`. Stash into a small list keyed by
     bdaddr+type (or reuse `hdev->remote_oob_data` via
     `hci_add_remote_oob_data()` with a new setter).
   - In `tk_request()`, in the branch taken when both sides set OOB-present and
     `!test_bit(SMP_FLAG_SC, &smp->flags)`: look up the stored entry for
     `hcon->dst`/`hcon->dst_type`; if found, `memcpy(smp->tk, entry->tk, 16)`,
     `set_bit(SMP_FLAG_TK_VALID, &smp->flags)`, set the method to the OOB/confirm
     method (`JUST_CFM` equivalent that still runs `c1`), and return `0` instead
     of falling to Just Works.
   - In `build_pairing_cmd()`: set `oob_flag = SMP_OOB_PRESENT` in our
     Pairing Request/Response when a legacy TK is stored for the peer (mirror the
     existing SC-OOB `if (hci_dev_test_flag(hdev, HCI_SC)...)` logic on the
     legacy side).
   - Mark the resulting key authenticated: ensure `smp->method` for this branch
     makes `smp_distribute_keys()` / key storage set `authenticated = 1` and
     `hcon->sec_level = BT_SECURITY_HIGH` (OOB legacy is an MITM method).
3. Build just the bluetooth modules
   (`make M=net/bluetooth`), unload/reload `bluetooth hci_uart btusb …` or
   reboot into the patched kernel.
4. Test:
   ```
   sudo ./bin/mkbd-provision --exchange-only          # F1/F2/F3 → prints new addr + TK
   echo "<new_addr> 1 <TK hex>" | sudo tee /sys/kernel/debug/bluetooth/hci0/le_legacy_oob_tk
   sudo btmon -w /tmp/tk.btsnoop &
   bluetoothctl
     agent NoInputNoOutput
     default-agent
     pair <new_addr>
   ```
   Expect in `btmon`: our Pairing Request with **OOB = present**, Pairing
   Confirm/Random both ways, `LE Start Encryption`, **Encryption Change**, key
   distribution (LTK/EDIV/Rand/IRK), bond written under
   `/var/lib/bluetooth/<adapter>/<addr>/`.
5. Cross-check the SMP PDUs against a `mkbd-smp-pair` btmon capture — they should
   be byte-identical.
6. Add a kernel selftest vector: extend `net/bluetooth/selftest.c` (or the
   `smp-tester` tool below) with `modern keyboard2` from `mkbd_crypto._vec`
   (`TK`, `Mrand`, `Srand` → expected `Mconfirm`/`Sconfirm`/`STK`).

**Exit criterion:** the kernel completes SMP and stores an authenticated LTK,
driven only by `bluetoothctl pair`. If Phase 0 fails, the injection point or the
OOB-flag advertisement is wrong — fix here before adding UAPI.

---

## Phase 1 — proper MGMT command + minimal BlueZ plumbing (~3–5 days)

### Kernel

1. **`include/net/bluetooth/mgmt.h`** — extend remote OOB data with the legacy TK.
   Preferred: a new accepted length for the existing opcode.
   ```c
   struct mgmt_cp_add_remote_oob_ext_data {
       struct mgmt_addr_info addr;
       __u8 hash192[16];
       __u8 rand192[16];
       __u8 hash256[16];
       __u8 rand256[16];
       __u8 le_legacy_tk[16];     /* NEW — all-zero = not present */
   } __packed;
   #define MGMT_ADD_REMOTE_OOB_EXT_DATA_SIZE 87   /* 23 + 64 + 16 ... verify */
   ```
   `add_remote_oob_data()` in `mgmt.c` already `switch`es on `len` (BR/EDR-only
   legacy size vs the SC size) — add the new size and copy `le_legacy_tk` through.
   Alternative if the maintainers object to a third length: a distinct opcode
   `MGMT_OP_ADD_REMOTE_LE_LEGACY_OOB_DATA`.
2. **`net/bluetooth/hci_core.c`** — `hci_add_remote_oob_data()` (or a new
   `hci_add_remote_oob_ext_data()`) stores `tk`/`tk_present` on the
   `struct oob_data` entry. `hci_remote_oob_data_lookup()` is reused unchanged.
3. **`net/bluetooth/smp.c`** — same `tk_request()` / `build_pairing_cmd()` change
   as Phase 0 but sourced from `hci_remote_oob_data_lookup()` instead of the
   debugfs list. Drop the debugfs knob (or keep it under `CONFIG_BT_DEBUGFS` for
   testing).
4. **`Documentation` / `mgmt-api.txt`** — document the new field/size and that a
   non-zero `le_legacy_tk` makes the next legacy pairing with that peer use OOB.
5. **`smp-tester`** (`tools/smp-tester.c` in the kernel tree's bluez, or bluez
   `tools/`) — add a legacy-OOB test case with the repo's real vector.

### BlueZ 5.87

1. **`src/shared/mgmt.h`** — add the opcode/struct/size constants.
2. **`monitor/packet.c`**, **`src/shared/mgmt.c`** — decode/encode the new field
   (nice-to-have for `btmon`).
3. **`src/adapter.c`** — extend `btd_adapter_add_remote_oob_data()` and its
   callers to carry the 16-byte legacy TK and emit the new MGMT command.
4. **D-Bus surface — how the TK gets in from `mkbd-provision`.** Recommended
   minimal option: a new method on `org.bluez.Adapter1`
   ```
   AddRemoteLegacyOOB(string address, byte address_type, array{byte} tk[16])
   ```
   that forwards straight to the new MGMT command and returns. Symmetric with how
   BR/EDR OOB data used to be supplied; no agent-protocol change; caller stays in
   control of ordering. (Fuller option for later: extend `org.bluez.Agent1` with
   `RequestLegacyOOBKey(object device) -> array{byte}` so the daemon pulls the TK
   mid-pairing — cleaner UX, but an agent-capability negotiation change.)
5. **`src/device.c`** — verify the `MGMT_OP_PAIR_DEVICE` path does **not** clear
   stored remote OOB data before SMP; ensure the resulting bond is stored with
   `Authenticated=1` (it will be if the kernel sets `BT_SECURITY_HIGH`).
6. **`doc/adapter-api.txt`** — document the new method.

### Tool integration — `bin/mkbd-provision`

Replace the controller-seizure path with normal BlueZ, keeping `bluetoothd` up:

```
F1/F2/F3  (mkbd_common.vendor_pairing_exchange)
  -> Adapter1.AddRemoteLegacyOOB(new_addr, 0x01 static, TK)     # new D-Bus call
  -> Device1.Pair()   (or MGMT Pair Device by addr — directed advert)
  -> kernel SMP runs legacy OOB with the TK, distributes keys, BlueZ stores bond
  -> phase-4 GATT provisioning as a GATT client on the now-bonded link
     (subscribe report CCCDs + vendor CCCD 0x000c; Write 0x0041 host-name record;
      Write 0x0038 = 01; read MS accessory service d4e3e3eb-…)   # see BOND-COMPLETION.md
  -> hold ~7 s, disconnect, let the keyboard re-advertise at the F3 addr and
     reconnect on the bonded LTK  (NVM flush / address adoption)
  -> verify: re-issue F1, require current addr == F3 addr and bond byte set
```

`mkbd-smp-pair` / `mkbd-bumble-pair` stay as the no-patch fallback and as the
byte-level reference for comparing `btmon` output.

### Validation

1. `smp-tester` legacy-OOB case passes (CI-friendly, no hardware).
2. Hardware: `mkbd-provision` on the patched kernel + patched bluez completes
   SMP, `btmon` shows OOB-present Pairing Request → Encryption Change → key
   distribution, bond appears in `/var/lib/bluetooth`.
3. SMP PDUs byte-identical to a `mkbd-smp-pair` capture and to the Windows
   captures in `capture/mkbd-pairing-*`.
4. Full path (SMP + phase 4 + hold) closes the half-bond: after a USB unplug +
   power-cycle, `bluetoothctl connect <addr>` works and keystrokes arrive as
   `Handle Value Notification` on handle `0x0020`.
5. Regression: a normal Just Works LE device and a passkey LE device still pair
   (the OOB branch must only trigger when a TK is stored for that exact peer).

---

## Phase 2 — upstreaming (~weeks, async)

> Full breakdown — patch series, interface decision, expected maintainer
> pushback, the bluetoothd-masking angle, and a draft RFC cover letter:
> **[`UPSTREAM.md`](UPSTREAM.md)**.

1. **Kernel** → `linux-bluetooth@vger`. Patch set: `mgmt.h` UAPI + `mgmt.c` +
   `smp.c` + `hci_core.c` + `mgmt-api.txt` + selftest. Precedent: SC OOB
   (`MGMT_OP_ADD_REMOTE_OOB_DATA` 192/256 fields) landed the same shape.
   Expected pushback: *"legacy pairing is deprecated."* Counter: legacy OOB is
   still in the spec, and `NoInputNoOutput` devices that **mandate** it exist and
   ship in volume (MS Modern Keyboard, model 1780) — currently unpairable on
   Linux without seizing the controller.
2. **BlueZ** → same list. `src/shared/mgmt.h` + `src/adapter.c` +
   `src/device.c` + the D-Bus method + `doc/`.
3. **Interim distribution:** keep the patches in-repo under `kernel/*.patch` and
   `bluez/*.patch` with a build note (Arch: patched `linux` + `bluez` PKGBUILDs;
   the bluetooth modules can be built out-of-tree against
   `/lib/modules/$(uname -r)/build` for iteration).

---

## Risks / open questions

- **`tk_request()` exact structure** varies across kernel versions — on this box
  the tree is `7.2.3-arch1`. Confirm against the target: does the legacy path
  currently reach a `REQ_OOB`/`JUST_CFM` method, or does it collapse to
  `JUST_WORKS` when `!SMP_FLAG_SC`? The injection is a few lines either way but
  the branch differs.
- **OOB flag advertisement.** If our Pairing Request goes out with
  `oob_flag = 0`, the keyboard hangs up before we ever run `c1`. `build_pairing_cmd()`
  must set OOB-present whenever a legacy TK is stored — get this right first
  (it's the single tested failure mode).
- **SC vs legacy selection.** Keyboard sets the SC bit in AuthReq `0x0d`; our
  initiator must **not** set SC (host SC=0 → legacy wins). Confirm
  `hci_dev_test_flag(hdev, HCI_SC)` is off, or the kernel negotiates SC and the
  TK path is never taken.
- **Authenticated LTK.** If the kernel stores the OOB legacy LTK as
  unauthenticated, BlueZ may force a re-pair when a profile asks for MITM
  security. The resulting key must be `authenticated = 1` /
  `BT_SECURITY_HIGH`.
- **TK byte order at the MGMT boundary.** `mkbd_crypto` proves
  `TK = reverse(F3 bytes)` for a big-endian `c1`; the kernel `smp_c1()` works on
  little-endian wire order internally. Decide once whether `mkbd-provision` sends
  the F3 bytes as-is or reversed over D-Bus, and document it next to the call.
- **Address adoption still needs phase 4 + hold** (`docs/BOND-COMPLETION.md`).
  TK injection alone reproduces what `mkbd-smp-pair` already does — a bond the
  keyboard doesn't fully adopt. The win is that the tail can now run through a
  normal `bluetoothd`.

## Effort summary

| Phase | Scope | Est. |
|---|---|---|
| 0 | debugfs knob in `smp.c`, prove the injection point on hardware | ~1 day |
| 1 | MGMT op + `smp.c`/`mgmt.c`/`hci_core.c`, BlueZ D-Bus method, `mkbd-provision` rewrite, hardware validation | ~3–5 days |
| 2 | kernel + BlueZ upstream submission, in-repo patch set + build notes | weeks, async |
