# Option A — BlueZ side (spec only, no patches written)

There is no BlueZ source tree in this repo (`bluez/` holds a README and nothing
else), so no BlueZ patches were written. This file is the concrete spec the
BlueZ work has to implement against the kernel series in this directory, so it
can be turned into patches once a tree is available.

The kernel series is the source of truth for everything below; the constants
here are copied from `optionA/0001-Bluetooth-mgmt-accept-LE-legacy-OOB-TK.patch`.

---

## 1. Kernel ABI BlueZ has to match

Header touched in the kernel: `include/net/bluetooth/mgmt.h` (this tree has **no**
`include/uapi/linux/mgmt.h` — the mgmt ABI lives in `include/net/bluetooth/mgmt.h`
and is mirrored by hand into BlueZ's `src/shared/mgmt.h` / `lib/mgmt.h`).

Opcode is unchanged: `MGMT_OP_ADD_REMOTE_OOB_DATA` = `0x0021`.

A **third** accepted payload length is added. The first two are untouched:

| name | size | meaning |
| --- | --- | --- |
| `MGMT_ADD_REMOTE_OOB_DATA_SIZE` | `MGMT_ADDR_INFO_SIZE + 32` = 39 | BR/EDR only, existing |
| `MGMT_ADD_REMOTE_OOB_EXT_DATA_SIZE` | `MGMT_ADDR_INFO_SIZE + 64` = 71 | P-192 + P-256, existing |
| `MGMT_ADD_REMOTE_OOB_LE_LEGACY_DATA_SIZE` | `MGMT_ADDR_INFO_SIZE + 81` = **88** | **new** |

```c
struct mgmt_cp_add_remote_oob_le_legacy_data {
	struct mgmt_addr_info addr;	/* 6 + 1 */
	__u8	hash192[16];
	__u8	rand192[16];
	__u8	hash256[16];
	__u8	rand256[16];
	__u8	flags;
	__u8	le_legacy_tk[16];
} __packed;
#define MGMT_ADD_REMOTE_OOB_LE_LEGACY_DATA_SIZE (MGMT_ADDR_INFO_SIZE + 81)

/* Flags valid for the MGMT_ADD_REMOTE_OOB_LE_LEGACY_DATA_SIZE payload */
#define MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT	0x01
```

Command Complete is unchanged: status + `struct mgmt_addr_info`.

### Validation the kernel performs (all → `MGMT_STATUS_INVALID_PARAMS`)

- `addr.type` is not `BDADDR_LE_PUBLIC` / `BDADDR_LE_RANDOM`.
- `hash192` or `rand192` is non-zero (same rule the extended layout already
  enforces for LE).
- any bit set in `flags` other than `MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT`.
- a length that is not one of the three sizes above.

`P-256` handling is identical to the extended layout: an all-zero `hash256` or
`rand256` disables SC OOB for that peer, otherwise both are stored.

### Byte order

`le_legacy_tk` is in **on-air little-endian order** — the same order
`smp_c1()`/`smp_s1()` consume, i.e. the Microsoft Modern Keyboard's HID feature
report 3 payload byte for byte, no reversal anywhere. This is what the selftest
vectors in patch 5/5 pin down. BlueZ must pass the 16 bytes through untouched.

### Semantics

- Storage is per-controller, keyed on identity address + address type, atomic
  replace (the existing `hci_add_remote_oob_data()` behaviour). Re-adding wipes
  the old TK with `memzero_explicit()` before writing the new one.
- The presence flag, not the value, is what arms the key, so an all-zero TK
  stays distinguishable from "no key supplied".
- The TK is **single use**: `smp_chan_destroy()` wipes it once the pairing it
  authenticated completed. It is deliberately kept across a *failed* pairing so
  a retry does not need userspace to re-add it. Userspace must re-add before a
  second successful pairing with the same peer.
- `MGMT_OP_REMOVE_REMOTE_OOB_DATA` removes the whole entry including the TK.
- The whole `struct oob_data` is now freed with `kfree_sensitive()`.

### Permissions

`MGMT_OP_ADD_REMOTE_OOB_DATA` is not in the untrusted opcode set, so the
management socket already has to be trusted (`CAP_NET_ADMIN` at bind time).
No new kernel-side permission check was added.

---

## 2. BlueZ changes required

### 2.1 `src/shared/mgmt.h` (and `lib/mgmt.h` if the packet decoder uses it)

Add the struct, the size constant and the flag exactly as above. Do **not**
change `MGMT_ADD_REMOTE_OOB_DATA_SIZE` or `MGMT_ADD_REMOTE_OOB_EXT_DATA_SIZE`.

### 2.2 `monitor/packet.c` — btmon decode

Extend the `Add Remote OOB Data` command decoder to recognise length 88 and
print `Flags` plus a **redacted** TK. Print the presence bit and the length,
never the 16 bytes — btmon logs get pasted into bug reports. Suggested:

```
Add Remote OOB Data (0x0021) plen 88
  LE Address: C9:6C:7E:E2:6C:7E (Static)
  Hash C192: 000000...
  Flags: 0x01 (LE legacy TK present)
  LE legacy TK: <16 bytes, not shown>
```

### 2.3 `src/adapter.c` — `btd_adapter_add_remote_oob_data()`

`btd_adapter_add_remote_oob_data()` is BR/EDR-shaped (adapter, bdaddr, hash,
randomizer) — **check the exact prototype against the tree**, it is quoted here
from memory and no BlueZ source was available to verify it.

Extend, rather than overload, so existing BR/EDR callers are untouched. Add a
sibling:

```c
int btd_adapter_add_remote_le_legacy_oob(struct btd_adapter *adapter,
					 const bdaddr_t *bdaddr,
					 uint8_t bdaddr_type,
					 const uint8_t tk[16]);
```

Implementation: zero-fill a `struct mgmt_cp_add_remote_oob_le_legacy_data`, set
`addr`, set `flags = MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT`, `memcpy` the TK, send
`MGMT_OP_ADD_REMOTE_OOB_DATA` with `plen = 88`. Wipe the local copy
(`explicit_bzero`) after the send. Never `DBG()` the TK.

### 2.4 D-Bus surface — `org.bluez.Adapter1`

```
void AddRemoteLegacyOOB(string address, byte address_type, array{byte} tk)
```

- `address` — peer address string, `"C9:6C:7E:E2:6C:7E"`.
- `address_type` — `0x01` public / `0x02` random, matching the mgmt
  `BDADDR_LE_*` encoding used elsewhere on the D-Bus API. Reject `BDADDR_BREDR`.
- `tk` — exactly 16 bytes, on-air order (see above). Any other length →
  `org.bluez.Error.InvalidArguments`.
- Errors: `InvalidArguments` (bad address / type / TK length),
  `NotReady` (adapter down), `Failed` (mgmt command returned non-zero status).
- Returns void; the call completes when mgmt Command Complete arrives.

Rationale for a plain adapter method rather than an `org.bluez.Agent1`
extension: the caller (`mkbd-provision`) already has the TK in hand from the
USB F1/F2/F3 vendor exchange *before* pairing starts, so there is nothing to
ask an agent for, and an adapter method needs no agent-capability negotiation.
An `Agent1.RequestLegacyOOBKey(object device) -> array{byte}` pull model is the
nicer long-term UX and can be added later without changing the kernel ABI.

### 2.5 polkit / access control

The TK is a pairing secret; anything that can call `AddRemoteLegacyOOB` can
authenticate a device as the user. Gate it at least as tightly as
`Adapter1.SetDiscoveryFilter`/`Pair`:

- Add an action `org.bluez.adapter.add-remote-legacy-oob` to
  `src/bluetooth.conf` / the shipped polkit policy, `auth_admin_keep` for
  non-root, `yes` for `root` and for the `lp`/`bluetooth` system group that
  already owns the provisioning helper.
- If the deployment does not use polkit, fall back to the existing
  `bluez.conf` D-Bus `<policy user="root">` send_destination rule and document
  that `mkbd-provision` must run as root.

### 2.6 `src/device.c`

Verify that the `MGMT_OP_PAIR_DEVICE` path does **not** remove stored remote OOB
data before SMP runs (`device_remove_bonding()` / `bonding_request_free()` are
the places to check). If it does, the TK is gone before `tk_request()` looks for
it and the pairing silently falls back to Just Works.

Also confirm the resulting bond is written with `Authenticated=1` — it will be,
because the kernel raises `pending_sec_level` to `BT_SECURITY_HIGH` and reports
key type 1 (authenticated legacy).

### 2.7 `doc/adapter-api.txt`

Document `AddRemoteLegacyOOB`, the byte order, the single-use lifetime, and that
the key must be re-added before a second successful pairing with the same peer.

---

## 3. mgmt-tester cases

In `tools/mgmt-tester.c`:

| case | payload | expect |
| --- | --- | --- |
| `Add Remote OOB Data - LE legacy TK - Success` | len 88, LE random addr, flags 0x01, 16-byte TK, 192 values zero | `MGMT_STATUS_SUCCESS` |
| `... - flags 0` | len 88, flags 0x00 | `MGMT_STATUS_SUCCESS`, behaves as the extended layout (no TK armed) |
| `... - Invalid Params (BR/EDR addr)` | len 88, `addr.type = BDADDR_BREDR` | `MGMT_STATUS_INVALID_PARAMS` |
| `... - Invalid Params (non-zero 192)` | len 88, `hash192` non-zero | `MGMT_STATUS_INVALID_PARAMS` |
| `... - Invalid Params (unknown flag)` | len 88, `flags = 0x02` | `MGMT_STATUS_INVALID_PARAMS` |
| `... - Invalid Params (short)` | len 87 or 72 | `MGMT_STATUS_INVALID_PARAMS` |
| `... - Invalid Index` | no adapter | `MGMT_STATUS_INVALID_INDEX` |

The existing len-39 and len-71 cases must keep passing unchanged — that is the
backward-compatibility proof.

A permission case (unprivileged caller → `EPERM`) is not a new test: the opcode
is already trusted-only, so the existing untrusted-socket coverage applies.

An end-to-end `smp-tester` legacy-OOB case (emulated controller, TK armed, expect
Pairing Request with OOB present + SC clear, then confirm/random) is the useful
addition, but it needs `src/shared/tester`-side SMP support for OOB that does
not exist yet — flagged as follow-up work, not specified here.
