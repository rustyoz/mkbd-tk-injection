# Option B — BlueZ changes required

There is no BlueZ source tree in this repo (`bluez/` holds only a README), so
no BlueZ patches were written. This is the spec for them.

Kernel side, as implemented in `optionB/0001..0005`:

| Name | Opcode | Payload |
|---|---|---|
| Add Remote LE Legacy OOB Data | `0x005c` | `mgmt_addr_info` (7) + `flags` (1) + `tk` (16) = 24 bytes |
| Remove Remote LE Legacy OOB Data | `0x005d` | `mgmt_addr_info` (7) = 7 bytes |

Flags: `0x01` Persistent, `0x02` Force Legacy, `0x04`..`0x80` reserved and
rejected if set. `addr.type` must be `BDADDR_LE_PUBLIC` (1) or
`BDADDR_LE_RANDOM` (2). An all-zero TK is rejected with `INVALID_PARAMS`.
Both commands need a trusted (`CAP_NET_ADMIN`) mgmt socket. The TK is
write-only: nothing reads it back.

## 1. `src/shared/mgmt.h` / `lib/mgmt.h`

Mirror the kernel header:

```c
#define MGMT_OP_ADD_REMOTE_LE_LEGACY_OOB_DATA	0x005C
struct mgmt_cp_add_remote_le_legacy_oob_data {
	struct mgmt_addr_info addr;
	uint8_t  flags;
	uint8_t  tk[16];
} __packed;

#define MGMT_LE_LEGACY_OOB_FLAG_PERSISTENT	0x01
#define MGMT_LE_LEGACY_OOB_FLAG_FORCE_LEGACY	0x02

#define MGMT_OP_REMOVE_REMOTE_LE_LEGACY_OOB_DATA	0x005D
struct mgmt_cp_remove_remote_le_legacy_oob_data {
	struct mgmt_addr_info addr;
} __packed;
```

Add both names to the opcode string table used by `btmon` (`monitor/packet.c`,
`monitor/control.c`) so captures decode. `btmon` must **not** print the `tk`
field — print it as `TK (16 bytes, hidden)`, the way link keys and LTKs are
already redacted under `--no-keys`, or unconditionally, since unlike an LTK
this value is supplied by a user-facing API and is likely to end up in bug
reports.

## 2. `doc/mgmt-api.txt`

Add both commands. The kernel patch adds
`Documentation/networking/bluetooth-le-legacy-oob.rst` with the same content in
kernel-doc form; that text can be transcribed directly. Note in particular:

- `Address` must be the address the adapter will connect to, i.e. the identity
  address for a peer that does not use an RPA. Entries are **not** matched
  against resolved identity addresses, because during pairing `hcon->dst` is
  still the advertised address.
- `TK` byte order is the same as the `Value` field of `Load Long Term Keys`
  (LSB first). Hardware testing of the PoC (see `PROGRESS.md`, 2026-09-10)
  confirmed the F3-exchange bytes go in as-is, not reversed.
- Without `Persistent`, entries are dropped when the adapter is powered down
  and when pairing with that peer completes.

## 3. D-Bus API — `org.bluez.Adapter1`

```
void AddRemoteLegacyOOB(string address, string address_type,
                        array{byte} tk, dict options)
```

- `address` — `"XX:XX:XX:XX:XX:XX"`.
- `address_type` — `"public"` or `"random"`.
- `tk` — exactly 16 bytes, else `org.bluez.Error.InvalidArguments`.
- `options`:
  - `"Persistent"` boolean → `MGMT_LE_LEGACY_OOB_FLAG_PERSISTENT`
  - `"ForceLegacy"` boolean → `MGMT_LE_LEGACY_OOB_FLAG_FORCE_LEGACY`

  An unknown key must be rejected with `InvalidArguments` rather than ignored,
  so that a future `"TTL"` option cannot be silently dropped by an old daemon.

```
void RemoveRemoteLegacyOOB(string address, string address_type)
```

Implementation notes:

- Live in `src/adapter.c` next to the existing `AddRemoteOOBData`/`Pair`
  plumbing; the mgmt call is a plain `mgmt_send()` with a completion callback
  mapping `MGMT_STATUS_*` to `org.bluez.Error.*`
  (`INVALID_PARAMS` → `InvalidArguments`, `NOT_SUPPORTED` → `NotSupported`,
  `FAILED` → `Failed`).
- Zero the caller's TK buffer (`explicit_bzero`) once it has been handed to
  the kernel, and never log it, not even at `DBG()` level.
- **Do not clear the stored entry on `Pair()` entry or on a pairing failure.**
  The kernel drops a non-persistent entry itself when pairing completes.
  `device_remove_bonding()` / `btd_adapter_remove_bonding()` must not be
  extended to call `RemoveRemoteLegacyOOB`, or a retry after a transient
  failure will have no key.
- `adapter_remove()` needs no cleanup: the kernel drops everything when the
  controller unregisters.

## 4. Polkit

Both methods need their own action, defaulting to `auth_admin_keep`, because
supplying a TK lets the caller establish an *authenticated* (MITM) bond. Add
to `src/bluetooth.conf` / the polkit policy used by the distribution:

```
org.bluez.adapter.add-remote-legacy-oob
org.bluez.adapter.remove-remote-legacy-oob
```

The existing `org.bluez.Adapter1` blanket rule is too coarse: it would let any
session that may start discovery inject key material.

## 5. `mgmt-tester`

Cases to add (`tools/mgmt-tester.c`):

| Case | Expect |
|---|---|
| valid 24-byte payload, LE random address | `MGMT_STATUS_SUCCESS` |
| payload length 23 or 25 | `MGMT_STATUS_INVALID_PARAMS` |
| `addr.type = BDADDR_BREDR` | `MGMT_STATUS_INVALID_PARAMS` |
| `flags = 0x04` (reserved bit) | `MGMT_STATUS_INVALID_PARAMS` |
| all-zero `tk` | `MGMT_STATUS_INVALID_PARAMS` |
| add twice, same address | second returns `SUCCESS` (atomic replace) |
| untrusted socket | `MGMT_STATUS_PERMISSION_DENIED` |
| remove, no entry present | `MGMT_STATUS_INVALID_PARAMS` |
| remove with `BDADDR_ANY` | `MGMT_STATUS_SUCCESS`, clears all |
| add, power off, add again | non-persistent entry gone; persistent one kept |

## 6. `client/` (bluetoothctl)

Optional but useful for manual testing:

```
[bluetooth]# legacy-oob.add C9:6C:7E:E2:6C:7E random <32 hex digits> force-legacy
[bluetooth]# legacy-oob.remove C9:6C:7E:E2:6C:7E random
```

The TK must be read from the command line only in the interactive client, and
the command must not be recorded in the readline history.
