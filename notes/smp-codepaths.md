# Kernel SMP code-path analysis (linux-7.2.3)

Everything the Phase 0 patch depends on, with line numbers from a pristine
`net/bluetooth/smp.c` in linux-7.2.3.

## Why the keyboard can't pair today

`tk_request()` (827) fills `smp->tk`:

- `!(auth & SMP_AUTH_MITM)` → `smp->method = JUST_CFM` (850)
- else `smp->method = get_auth_method(smp, local_io, remote_io)` (852)
- `JUST_CFM` + initiator → `JUST_WORKS` (855-857)
- `JUST_CFM` + local NoInputNoOutput → `JUST_WORKS` (860-862)
- `JUST_WORKS` → `mgmt_user_confirm_request`, TK stays `0`, return (866-875)
- passkey methods → 32-bit value into `smp->tk` (901-908)

There is **no OOB branch**. `get_auth_method()` (812) has no OOB awareness:
`gen_method[remote_io][local_io]`, and `gen_method[3][3]` (NoInputNoOutput ×
NoInputNoOutput) = `JUST_WORKS`.

`build_pairing_cmd()` (625) only sets `oob_flag = SMP_OOB_PRESENT` inside
`if (hci_dev_test_flag(hdev, HCI_SC_ENABLED) && (authreq & SMP_AUTH_SC))`
(649-673) — i.e. **Secure Connections OOB only**. Legacy pairing always sends
`oob_flag = SMP_OOB_NOT_PRESENT`. The keyboard sees that and hangs up (`0x13`).

`MGMT_OP_ADD_REMOTE_OOB_DATA` / `struct mgmt_cp_add_remote_oob_data`:
`hash192/rand192/hash256/rand256` — BR/EDR + LE SC OOB. No legacy TK field.
`struct oob_data` (hci_core.h:229): same four fields + `present`. The only
userspace legacy-TK writer anywhere is `USER_PASSKEY_REPLY` (32-bit).

## Two MITM pre-checks that reject before tk_request

Initiator — `smp_cmd_pairing_rsp()` (1897), lines 1958-1966:

```c
if (conn->hcon->pending_sec_level >= BT_SECURITY_HIGH) {
    method = get_auth_method(smp, req->io_capability, rsp->io_capability);
    if (method == JUST_WORKS || method == JUST_CFM)
        return SMP_AUTH_REQUIREMENTS;          /* -> Pairing Failed 0x03 */
}
```

Responder — `smp_cmd_pairing_req()` (1704), lines 1794-1806: same shape, plus it
force-sets the MITM bit in `auth` / `rsp.auth_req` when the check passes.

For this keyboard `pending_sec_level` reaches HIGH (keyboard demands MITM; we
want an authenticated bond), so **both roles bail with `0x03` before
`tk_request()` is ever called** unless `get_auth_method()` stops returning
`JUST_WORKS`. Hence the patch touches `get_auth_method()`, not just
`tk_request()`.

## Why REQ_OOB is safe as the legacy method value

`#define REQ_OOB 0x04` (792). Every site that inspects `smp->method == REQ_OOB`:

| line | function | reached in legacy? |
|---|---|---|
| 1439 | `sc_dhkey_check` | no — SC only |
| 2134, 2167 | `sc_dhkey_check` (responder) | no — SC only |
| 2656 | `sc_select_method` | no — SC only |
| 2793 | `smp_cmd_public_key` (SC) | no — SC only |
| 2866 | `smp_cmd_dhkey_check` | no — SC only |

The legacy confirm/random path does **not** branch on `smp->method` at all:

- `smp_confirm()` (925) → `smp_c1(smp->tk, smp->prnd, …)` then send Pairing
  Confirm.
- `smp_cmd_pairing_confirm()` (2056): for `!SC`, initiator sends Pairing Random;
  responder calls `smp_confirm()` if `SMP_FLAG_TK_VALID`.
- `smp_random()` (952) → verify `pcnf` via `smp_c1`, then `smp_s1(smp->tk, …)`
  → STK → `hci_le_start_enc()`.

So setting `smp->method = REQ_OOB` on the legacy path only needs `smp->tk`
populated and `SMP_FLAG_TK_VALID` set. Confirmed by grepping every
`smp->method` read.

## Initiator flow after tk_request returns 0

`smp_cmd_pairing_rsp()` line 1988-1994:

```c
set_bit(SMP_FLAG_CFM_PENDING, &smp->flags);
if (test_bit(SMP_FLAG_TK_VALID, &smp->flags))
    return smp_confirm(smp);        /* our patch sets TK_VALID -> sends Confirm now */
return 0;
```

Exactly the passkey-CFM shape, which is what we want.

## Authenticated-LTK chain

1. patch: `tk_request()` — `if (hcon->pending_sec_level < BT_SECURITY_HIGH)
   hcon->pending_sec_level = BT_SECURITY_HIGH;` + `set_bit(SMP_FLAG_MITM_AUTH)`.
2. `smp_random()` line 998 — responder STK stored with
   `auth = test_bit(SMP_FLAG_MITM_AUTH) ? 1 : 0`.
3. `hci_event.c:5207` (LE Encryption Change) — `conn->sec_level =
   conn->pending_sec_level;` → `BT_SECURITY_HIGH`.
4. `smp_cmd_initiator_ident()` line 2509 — received LTK stored with
   `authenticated = (hcon->sec_level == BT_SECURITY_HIGH)` → `1`.

## Where the debugfs file is created

`smp_register()` is called from `hci_sync.c:3655` on **every power-on**, so a
`debugfs_create_file` there would duplicate. `hci_debugfs_create_le()`
(`hci_debugfs.c:1195`, called once per adapter from `hci_sync.c:5109`) is the
right place — files there live and die with `hdev->debugfs`. The patch adds one
call at the end of that function; the fops + parser live in `smp.c`
(`hci_debugfs.c` already `#include "smp.h"`).

## smp_chan / flags reference

`struct smp_chan` (92): `conn`, `tk[16]`, `flags`, `method`, … `smp->conn` is
set at `smp_chan_create` and is always valid where `get_auth_method` runs.

`SMP_FLAG_*` (66-80): `TK_VALID`, `MITM_AUTH`, `INITIATOR`, `SC`, `WAIT_USER`,
`REMOTE_OOB`, `LOCAL_OOB`, …

`hcon->dst_type`: `ADDR_LE_DEV_PUBLIC` (0) / `ADDR_LE_DEV_RANDOM` (1) during
SMP, before IRK resolution swaps `hcon->dst` to the identity address. The
keyboard advertises its `F3` static-random address → `dst_type == 1`. The
debugfs knob uses the same 0/1 convention.
