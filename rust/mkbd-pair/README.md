# mkbd-pair (Rust)

A from-scratch Rust port of this repo's Microsoft Modern Keyboard (Fingerprint
ID, model 1780) pairing tools, built for eventual release packaging (see
`../../PLUGIN-PLAN.md` section 2, "Userspace → `mkbd-pair`"). One static
binary instead of a Python engine + a vendored library + two bash wrappers.

**Status: NOT wired in.** `../../pairmodernkeyboard.sh` and
`../../optionA/autopair/mkbd-optionA-autopair` are untouched and remain the
hardware-verified tools. This binary has not been run against the physical
keyboard — it compiles clean and its pure logic (bdaddr parsing) has unit
tests, but every USB/mgmt-socket/L2CAP code path is an unverified port. Do
not point udev or a systemd unit at it, and do not delete the Python tools,
until it's been exercised on real hardware and someone says to cut over.

## What this replaces

| Python / bash | Rust | Notes |
|---|---|---|
| `lib/mkbd_common.py` | `src/hid.rs`, `src/mgmt.rs`, `src/bond.rs`, `src/bdaddr.rs` | hidraw discovery/ioctls, F1/F2/F3 exchange, MGMT control socket, BlueZ bond-file writer |
| `test/tk-pair.py` | `src/pair.rs` (`Engine::DebugfsTk`) | debugfs TK-injection PoC path |
| `test/optionA-pair.py` | `src/pair.rs` (`Engine::OptionA`) | the real `MGMT_OP_ADD_REMOTE_OOB_DATA` len-88 path |
| `test/phase4.py` | `src/att.rs` (`run_adopt`) | bonded GATT provisioning / address adoption over raw L2CAP ATT |
| `pairmodernkeyboard.sh` | `mkbd-pair pair` | CLI wrapper folded into the engine itself |
| `test/optionA-dbus-pair.py` | `src/dbus_pair.rs` (`mkbd-pair dbus-pair`) | Option A over bluetoothd's D-Bus surface, bluetoothd never stopped |
| `optionA/autopair/mkbd-optionA-autopair` | `mkbd-pair auto` (`src/autopair.rs`) | udev-triggered detect/prompt/pair/confirm/reconnect-watch; tries D-Bus first, falls back to raw-mgmt |

`lib/mkbd_common.py`'s BR/EDR helpers (`write_device_info`,
`mgmt_load_link_keys`/`mgmt_load_ltks`/`mgmt_load_irks`) and the report
descriptor mini-parser were **not** ported — nothing in the pairing engine
calls them; they exist in Python for other tools outside this repo's scope.

## CLI

```
mkbd-pair pair [--option-a] [--tk-order as-is|reversed] [--addr-type 0|1]
               [--timeout N] [--diag] [--no-mask] [--no-adopt]
               [--adopt-rounds N] [--hci hci0] [--adapter ADDR]

mkbd-pair adopt <KBD_ADDR> [--adapter ADDR] [--hci hci0] [--rounds N]
                [--hold SECS] [--discover] [--writes] [--read-msacc]
                [--f3-check] [--cccd 0x17,0x1d,...] [--led H] [--feature H]
                [--notify H] [--led-writes N] [--msacc H,H]

mkbd-pair dbus-pair [--hci hci0] [--adapter ADDR] [--tk-order as-is|reversed]
                    [--connect-timeout SECS] [--pair-timeout SECS]

mkbd-pair auto [--hci hci0] [--reconnect-timeout SECS]
```

`mkbd-pair pair` defaults to the debugfs PoC path, matching
`pairmodernkeyboard.sh`'s default; pass `--option-a` for the real kernel-patch
path (what `mkbd-pair auto` always uses, matching the bash wrapper it
replaces).

## Naming: "Phase 0" / "Phase 4" → debugfs / Option A / address adoption

`PLAN.md`'s numbered phases (0, 1, 2, 4) are a project roadmap, not a naming
scheme worth carrying into a CLI a new user has to read cold. This port keeps
**Option A** (already a meaningful, established name across `optionA/`,
`BLUEZ-NOTES.md`, etc.) but renames:

- "Phase 0" → **`Engine::DebugfsTk`** / "the debugfs TK-injection knob" — it's
  the throwaway PoC path that pokes a debugfs file, as opposed to Option A's
  real MGMT opcode.
- "Phase 4" → **`adopt` / "address adoption"** — what it actually does: hold a
  bonded GATT connection long enough that the keyboard adopts the new address
  generated during F3. `--p4-rounds` → `--adopt-rounds`, `--no-phase4` →
  `--no-adopt`.

Comments that cite the Python filenames (`test/phase4.py`, etc.) for
traceability were left alone — those are real paths, not phase numbers.

## `dbus-pair` and the `auto` fallback (ported from `worktree-kernel-leak-fix`)

Another session's branch (`worktree-kernel-leak-fix`, PR #2, commits
`331e28e`/`af3523e`/`e3e7357`) hardware-verified two fixes on top of what this
crate started from; both are ported here directly from those commits (not
re-derived from a description):

1. **The D-Bus pairing path works with `Adapter1.ConnectDevice()`.** The
   original attempt used `Adapter1.StartDiscovery()` + polling for a
   `Device1` object and failed 5/5 on hardware — BlueZ's discovery pipeline
   never saw the keyboard's directed advertisement. `ConnectDevice()` (a
   stock, `[experimental]`-flagged BlueZ method: "Connects to device without
   need of performing General Discovery") connects directly by address
   instead, the same mechanism the raw-mgmt path already uses, and was
   verified end to end: `AddRemoteLegacyOOB` → `ConnectDevice` (0.1s) →
   `Device1.Pair()` → `Paired=true Bonded=true Connected=true`, full GATT
   resolution, live `uhid` input device, bluetoothd never stopped. Ported as
   `src/dbus_pair.rs` / `mkbd-pair dbus-pair`, using `zbus`'s blocking API
   (async-io reactor, no tokio, no libdbus) — the `ConnectDevice`/`Pair()`
   calls run on a helper thread with a channel-based timeout, mirroring the
   client-side `timeout=` kwargs the Python version passes to dbus-python.
   `mkbd-pair auto` now tries this path first and falls back to
   `pair::Engine::OptionA` (raw mgmt, bluetoothd stopped) on failure — the
   fallback exists because this keyboard abandons its currently active bond
   as soon as a new F1/F2/F3 exchange starts regardless of outcome, so a
   failed D-Bus attempt has already cost the bond either way, and falling
   straight back to the proven path re-establishes it in the same run.
2. **`mgmt_pair_device` now sends `MGMT_OP_CANCEL_PAIR_DEVICE` +
   `MGMT_OP_DISCONNECT`** for the peer whenever it's about to return without
   a confirmed bond (no LTK). Root cause: both the in-flight bonding request
   and the underlying LE connection are kernel state tracked per-adapter, not
   per-socket, so closing the raw HCI socket on a failed/timed-out attempt
   left them running for the next attempt to collide with — the suspected
   cause of the "repeated attempts degrade, only a reboot fixes it" symptom
   ("ACL packet for unknown connection handle" in dmesg). This one **has not
   been hardware-stress-tested on either branch** as of this port — it's a
   strong hypothesis with a concrete, low-risk fix (send two more mgmt
   commands on an already-failing path), not a confirmed fix.

The D-Bus path is new surface with its own unverified-in-this-port status
(see below) on top of being unverified-by-this-session in general — treat it
as two layers of "needs a real hardware run before trusting it."

## Differences from the Python tools (deliberate)

- **`auto` calls the engine in-process** instead of shelling out to
  `pairmodernkeyboard.sh` and grepping its stdout for a summary line. The
  address and pass/fail come directly off the returned `PairOutcome` struct.
- **`mkbd-pair pair` always prints full detail**, then a compact 3-line
  summary at the end (address / authenticated+CCCD-count+adopted /
  reminder), rather than `pairmodernkeyboard.sh`'s default of burying full
  output in a temp log unless `-v`/`--diag` is passed. There's no
  process boundary to redirect across anymore, so there was no cheap way to
  keep the "quiet by default" behavior — decided full-output-always was the
  more useful default for a systems tool. Flag if you'd rather have quiet
  default back.

## Known-unverified / lower-confidence spots

Everything here is unverified on hardware, but these are the parts most
likely to need a fix on first real run:

- **`sock.rs`**: hand-written `sockaddr_hci` / `sockaddr_l2cap` structs and
  raw `libc::socket`/`bind`/`connect`/`setsockopt` calls, since Rust's
  `std::net` has no `AF_BLUETOOTH` support. Field layout and constants
  (`AF_BLUETOOTH=31`, `BTPROTO_L2CAP=0`, `BTPROTO_HCI=1`, `SOL_BLUETOOTH=274`,
  `BT_SECURITY=4`) were cross-checked against `<bluetooth/*.h>` and against
  what `lib/mkbd_common.py` / `test/phase4.py` pass through Python's
  `socket` module, but never round-tripped against a real kernel.
- **L2CAP connect with a timeout**: implemented as
  non-blocking-connect + `poll(POLLOUT)` + `SO_ERROR` check, since
  `SO_SNDTIMEO` doesn't reliably bound `connect()` on Linux. Python relied on
  `socket.settimeout()` before `connect()`, which does the equivalent under
  the hood — the Rust version should behave the same but wasn't compared
  side by side.
- **hidraw ioctl request codes** (`hid.rs`'s `ioc()`): reimplements the
  `_IOC` macro from `<linux/ioctl.h>` bit-for-bit against Python's version;
  only `HIDIOCSFEATURE` is exercised by the pairing engine (matches
  `mkbd_common.py`, which only calls `set_feature` from this path too).
- **`mgmt_pair_device`'s event loop timing** (`mgmt.rs`): ported the
  grace-period logic around `MGMT_EV_NEW_LTK` arriving before or after the
  Pair Device Command Complete verbatim from the Python comments describing
  real hardware behavior — but the *comments* are the only evidence this
  matters, so if pairing seems to cut off the LTK/IRK race early, look here
  first.
- **`btmon`/`dmesg` capture for `--diag`**: reimplemented but not exercised;
  a wrong parse of `btmon -r`/`dmesg` output only degrades diagnostics, not
  the pairing result.
- **`src/dbus_pair.rs`'s zbus usage**: the D-Bus method signatures
  (`Adapter1.AddRemoteLegacyOOB(s,s,ay)`, `Adapter1.ConnectDevice(a{sv})->o`,
  `AgentManager1`/`Agent1`) were cross-checked against the actual BlueZ patch
  (`optionA/bluez/0001-*.patch`) and the verified Python script, and it
  compiles/links against zbus 5.19's blocking API — but this exact Rust
  translation (proxy macros, the exported `Agent1` object, the
  thread+channel timeout wrapper around `ConnectDevice`/`Pair()`) has not
  been run against a live system bus or bluetoothd at all. If pairing hangs
  rather than failing cleanly, or the agent never gets a callback it should,
  start here.

## Building

```
cd rust/mkbd-pair
cargo build --release
# binary at target/release/mkbd-pair
```

No system dependencies beyond a Rust toolchain — `libc` and `clap` are the
only crates, both resolved from crates.io at build time (not a runtime
dependency of the resulting binary).

## Trying it (once you're ready to test against hardware)

Everything needs root, same as the Python tools. Point it at the same
prerequisites (`test/install-module.sh` or `test/install-optionA-module.sh` +
reboot, keyboard on USB):

```
sudo target/release/mkbd-pair pair --option-a --diag
```

If something doesn't match `test/optionA-pair.py`'s behavior byte-for-byte,
that's the port to fix — the Python tools remain the reference implementation
until this one is proven out.
