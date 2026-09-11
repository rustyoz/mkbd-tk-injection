# Option A — BlueZ side

> **Patches exist now (2026-09-11):** `bluez/0001-Bluetooth-adapter-add-AddRemoteLegacyOOB.patch`
> and `bluez/0002-doc-org.bluez.Adapter-document-AddRemoteLegacyOOB.patch`,
> against upstream **bluez-5.87** (matches the installed `bluez 5.87-2`
> package) — `git am`-able, and `bluetoothd` was built with them applied
> (`make src/bluetoothd`, links clean, `strings src/bluetoothd | grep
> AddRemoteLegacyOOB` confirms the method is in the binary). There is still no
> BlueZ *source tree* checked into this repo (only the patches — see
> `bluez/README.md`), so this file stays as the design rationale; sections
> below are updated to match what was actually verified against real BlueZ
> source, correcting a few guesses the original draft made blind (the header
> path, the doc file, and — importantly — the polkit section, which assumed
> an access-control mechanism BlueZ doesn't actually have).
>
> Not implemented: the `monitor/packet.c` btmon decode (2.2) and the
> `src/device.c` premature-removal check (2.6) below are still spec-only.

This file is the concrete spec/rationale for the BlueZ work against the
kernel series in this directory.

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

### 2.1 `lib/bluetooth/mgmt.h`

Done in `bluez/0001-*.patch`. This is the actual path in bluez-5.87 (not
`src/shared/mgmt.h` — that header is BlueZ-internal request/response
plumbing built on top of the structs `lib/bluetooth/mgmt.h` mirrors from the
kernel uapi; `src/adapter.c` already builds `struct mgmt_cp_add_remote_oob_data`
straight from this header, so the new struct lives next to it). Added the
struct, the size constant and the flag exactly as the kernel patch defines
them. `MGMT_ADD_REMOTE_OOB_DATA_SIZE` / `MGMT_ADD_REMOTE_OOB_EXT_DATA_SIZE`
are untouched — this bluez-5.87 tree doesn't even define a `_SIZE` constant
for the base struct; `btd_adapter_add_remote_oob_data()` just uses `sizeof(cp)`.

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

Confirmed against real source (the guess below was right): `btd_adapter_add_remote_oob_data(adapter, bdaddr, hash, randomizer)`
is BR/EDR-only — it's called from exactly one place, the `neard` NFC plugin.
No existing LE remote-OOB path exists in BlueZ at all to extend, so `bluez/0001-*.patch`
adds the sibling function verbatim as speced:

```c
int btd_adapter_add_remote_le_legacy_oob(struct btd_adapter *adapter,
					 const bdaddr_t *bdaddr,
					 uint8_t bdaddr_type,
					 const uint8_t tk[16]);
```

Implementation matches the plan: zero-fill `struct
mgmt_cp_add_remote_oob_le_legacy_data`, set `addr`, `flags =
MGMT_OOB_FLAG_LE_LEGACY_TK_PRESENT`, `memcpy` the TK, `mgmt_send()` with
`sizeof(cp)` (88). One deliberate deviation from the original sketch: it does
**not** wipe its local copy — `tk` is the caller's buffer (the D-Bus method's
stack copy), and `mgmt_send()` copies it into its own request buffer
synchronously before returning, so there is nothing left in
`btd_adapter_add_remote_le_legacy_oob()`'s own stack to wipe once it returns.
The D-Bus handler (2.4) doesn't `explicit_bzero` its copy either, for the same
reason — the whole 16 bytes live for one function-call depth as a `uint8_t *`
straight out of the D-Bus message body, which glib/dbus owns and frees when
the message is unref'd. Never `DBG()` the TK itself (the patch's `DBG()` call
prints only the address and "tk=<16 bytes>").

### 2.4 D-Bus surface — `org.bluez.Adapter1`

Implemented in `bluez/0001-*.patch`, one difference from the original sketch:
`address_type` is a **string** (`"public"`/`"random"`), not a raw mgmt byte —
matching how every other `Adapter1`/`Device1` method on this D-Bus API already
spells address type (see `ConnectDevice`'s `AddressType` property in
`doc/org.bluez.Adapter.rst`), rather than leaking the mgmt wire encoding onto
the D-Bus surface where the raw `BDADDR_LE_PUBLIC`/`BDADDR_LE_RANDOM` bytes
would be an inconsistent one-off:

```
void AddRemoteLegacyOOB(string address, string address_type, array{byte} tk)
```

- `address` — peer address string, `"C9:6C:7E:E2:6C:7E"`.
- `address_type` — `"public"` or `"random"`. Anything else, including a
  BR/EDR-shaped address, is `InvalidArguments`.
- `tk` — exactly 16 bytes, Security Manager wire order (see above). Any other
  length → `org.bluez.Error.InvalidArguments`.
- Errors: `InvalidArguments` (bad address / type / TK length), `NotReady`
  (adapter down), `Failed` (the mgmt command could not be queued).
- Returns void. Synchronous like `RemoveDevice`'s sibling
  `btd_adapter_add_remote_oob_data()` call — it returns once the mgmt command
  is *queued*, not once mgmt's Command Complete comes back. Matches the
  existing (BR/EDR) function's behavior exactly; a stricter version that waits
  for Command Complete and maps `MGMT_STATUS_INVALID_PARAMS` etc. to distinct
  D-Bus errors is possible (`mgmt_send()` takes a callback) but is more code
  than the existing sibling function bothers with, so left as a follow-up if
  upstream review asks for it.

Rationale for a plain adapter method rather than an `org.bluez.Agent1`
extension: the caller (`mkbd-provision`) already has the TK in hand from the
USB F1/F2/F3 vendor exchange *before* pairing starts, so there is nothing to
ask an agent for, and an adapter method needs no agent-capability negotiation.
An `Agent1.RequestLegacyOOBKey(object device) -> array{byte}` pull model is the
nicer long-term UX and can be added later without changing the kernel ABI.

### 2.5 Access control

**Correction to the original draft, which assumed polkit:** BlueZ does not use
polkit for D-Bus method authorization. Checked directly —
`src/bluetooth.conf` is a plain D-Bus system-bus policy file, and its
`context="default"` block already reads `<allow
send_destination="org.bluez"/>` with no per-method restriction. That means
**every** `Adapter1` method — `Pair()`, `RemoveDevice()`, and now
`AddRemoteLegacyOOB()` — is callable by any local process today; there is no
polkit action, no `auth_admin_keep`, nothing to add one to. `bluez/0001-*.patch`
therefore adds no access-control changes, because there is no existing
precedent on this D-Bus API to extend and inventing one (a bespoke polkit
integration BlueZ has never had) is a separate, much larger design than this
patch series. `AddRemoteLegacyOOB` carries exactly the same trust assumption
`Pair()` already does today: whatever can reach the `org.bluez` system-bus
name can authenticate a device as the user. Restricting *that*, if wanted, is
a BlueZ-wide D-Bus policy hardening question, not something specific to this
feature.

### 2.6 `src/device.c`

Verify that the `MGMT_OP_PAIR_DEVICE` path does **not** remove stored remote OOB
data before SMP runs (`device_remove_bonding()` / `bonding_request_free()` are
the places to check). If it does, the TK is gone before `tk_request()` looks for
it and the pairing silently falls back to Just Works.

Also confirm the resulting bond is written with `Authenticated=1` — it will be,
because the kernel raises `pending_sec_level` to `BT_SECURITY_HIGH` and reports
key type 1 (authenticated legacy).

### 2.7 `doc/org.bluez.Adapter.rst`

Done in `bluez/0002-*.patch` (the doc lives here, not `doc/adapter-api.txt` —
bluez-5.87 documents each D-Bus interface as `doc/org.bluez.<Interface>.rst`,
rendered to the `org.bluez.Adapter(5)` man page). Documents `AddRemoteLegacyOOB`,
the byte order, the single-use-on-success/kept-on-failure lifetime, and that
the key must be supplied again before a second successful pairing with the
same peer.

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
