# mkbd-pair (Rust)

A from-scratch Rust port of this repo's Microsoft Modern Keyboard (Fingerprint
ID, model 1780) pairing tools, built for eventual release packaging (see
`../../PLUGIN-PLAN.md` section 2, "Userspace → `mkbd-pair`"). One static
binary instead of a Python engine + a vendored library + two bash wrappers.

**Status: `mkbd-pair auto`'s full flow — detect, prompt, pair via D-Bus,
unplug, reconnect over Bluetooth — is hardware-verified end to end
(2026-09-12). `adopt` hasn't been exercised standalone. None of it is wired
in** — `../../pairmodernkeyboard.sh` and
`../../optionA/autopair/mkbd-optionA-autopair` are untouched and remain the
tools actually wired to udev/systemd. Do not point those at this binary, and
do not delete the Python tools, until it's proven reliable across more runs
(see the flakiness note below).

`mkbd-pair dbus-pair` ran end to end on the physical keyboard, several times:
`AddRemoteLegacyOOB` → `ConnectDevice` → `Device1.Pair()` →
`Paired=true Bonded=true Connected=true`, full GATT resolution (HID, Battery,
Device Information, the vendor service), and a live `bluez-hog-device` uhid
keyboard input device — bluetoothd never stopped. `mkbd-pair auto` then ran
the complete flow — prompt, pair, info dialog, unplug, reconnect — with the
reconnect showing up **within 0s** of unplugging.

**Not perfectly reliable yet — two transient failures seen, both recovered
by a plain retry, no code fix needed:**
- One `Adapter1.ConnectDevice()` call timed out after 20s (no error, just
  silence) on an otherwise-identical attempt; retrying immediately after
  succeeded in the usual <1s.
- One reconnect-after-unplug attempt did not see the keyboard reconnect
  within the 90s watch window, despite the bond staying intact
  (`Paired/Bonded/Trusted: yes`, `Connected: no`); a subsequent full run
  reconnected in 0s.

Both look like ordinary BLE connection flakiness (consistent with
`test/optionA-dbus-pair.py`'s own header: "don't burn retries... casually")
rather than a logic bug in this port — no fix was needed, just a retry. If
this shows up again with a pattern (e.g. reconnect *always* fails on the
first unplug after a fresh pair), the earlier hypothesis is worth
revisiting: chain `adopt`'s GATT-hold sequence into `dbus-pair`/`auto` before
telling the user to unplug, the way the (now-removed) raw-mgmt engine always
did.

## What this replaces

| Python / bash | Rust | Notes |
|---|---|---|
| `lib/mkbd_common.py` (hidraw/F1-F2-F3 parts) | `src/hid.rs`, `src/bond.rs`, `src/bdaddr.rs` | hidraw discovery/ioctls, F1/F2/F3 exchange, BlueZ device lookup/cleanup |
| `test/phase4.py` | `src/att.rs` (`mkbd-pair adopt`) | bonded GATT provisioning / address adoption over raw L2CAP ATT |
| `test/optionA-dbus-pair.py` | `src/dbus_pair.rs` (`mkbd-pair dbus-pair`) | Option A pairing over bluetoothd's D-Bus surface, bluetoothd never stopped — **the only pairing engine in this crate** |
| `optionA/autopair/mkbd-optionA-autopair` | `mkbd-pair auto` (`src/autopair.rs`) | udev-triggered detect/prompt/pair/confirm/reconnect-watch |

**Removed after hardware testing:** `test/tk-pair.py` (debugfs TK-injection
PoC), `test/optionA-pair.py` (raw `MGMT_OP_ADD_REMOTE_OOB_DATA`), and
`pairmodernkeyboard.sh` were all initially ported (as `src/pair.rs` +
`src/mgmt.rs`, an `Engine::DebugfsTk`/`Engine::OptionA` raw-mgmt pairing
path, and `auto`'s fallback to it) but then deleted. Reason: that path masks
`bluetooth.service` for the duration of every pairing attempt, and an
earlier test session's raw-mgmt run left it *masked and stopped* afterward —
which doesn't self-heal, breaks Bluetooth system-wide, and needs a manual
`systemctl unmask bluetooth && systemctl start bluetooth` to recover. Given
`dbus-pair` (bluetoothd never stopped) is hardware-proven and covers the same
ground, the raw-mgmt path wasn't worth the operational risk. See git history
on this branch for the removed `pair.rs`/`mgmt.rs` if that path is ever
needed again (e.g. to support a kernel that only has the Option A patches and
not the BlueZ D-Bus patch).

`lib/mkbd_common.py`'s BR/EDR helpers, its MGMT key-loader functions, the
bond-file writer (bluetoothd manages the bond file itself on the D-Bus path),
and the report descriptor mini-parser were **not** ported — nothing left in
this crate calls them.

## CLI

```
mkbd-pair dbus-pair [--hci hci0] [--adapter ADDR] [--tk-order as-is|reversed]
                    [--connect-timeout SECS] [--pair-timeout SECS]

mkbd-pair adopt <KBD_ADDR> [--adapter ADDR] [--hci hci0] [--rounds N]
                [--hold SECS] [--discover] [--writes] [--read-msacc]
                [--f3-check] [--cccd 0x17,0x1d,...] [--led H] [--feature H]
                [--notify H] [--led-writes N] [--msacc H,H]

mkbd-pair auto [--hci hci0] [--reconnect-timeout SECS]
```

`mkbd-pair auto` always uses `dbus-pair` — there is no fallback engine left
to try.

## Naming: "Phase 0" / "Phase 4" → Option A / address adoption

`PLAN.md`'s numbered phases (0, 1, 2, 4) are a project roadmap, not a naming
scheme worth carrying into a CLI a new user has to read cold. "Phase 0" (the
debugfs PoC) went away entirely with the raw-mgmt engine removal. "Phase 4"
is now **`adopt` / "address adoption"** — what it actually does: hold a
bonded GATT connection long enough that the keyboard adopts the new address
generated during F3. Comments that cite the Python filenames
(`test/phase4.py`, etc.) for traceability were left alone — those are real
paths, not phase numbers.

## `dbus-pair` (ported from `worktree-kernel-leak-fix`, then hardware-verified here)

Another session's branch (`worktree-kernel-leak-fix`, PR #2, commit
`af3523e`/`e3e7357`) hardware-verified the key fix this engine depends on,
ported here directly from those commits (not re-derived from a description):
**`Adapter1.ConnectDevice()` instead of `Adapter1.StartDiscovery()`**. The
original attempt used `StartDiscovery()` + polling for a `Device1` object
and failed 5/5 on hardware — BlueZ's discovery pipeline never saw the
keyboard's directed advertisement. `ConnectDevice()` (a stock,
`[experimental]`-flagged BlueZ method: "Connects to device without need of
performing General Discovery") connects directly by address instead, and was
verified end to end. Ported as `src/dbus_pair.rs` / `mkbd-pair dbus-pair`,
using `zbus`'s blocking API (async-io reactor, no tokio, no libdbus) — the
`ConnectDevice`/`Pair()` calls run on a helper thread with a channel-based
timeout, mirroring the client-side `timeout=` kwargs the Python version
passes to dbus-python.

This session then ran `mkbd-pair dbus-pair` against the physical keyboard
and reproduced that exact result on the first hardware run **after fixing
one bug**: zbus's `#[proxy]` macro derives a D-Bus method name by PascalCasing
each snake_case segment of the Rust method name and doesn't know `oob`
should stay all-caps, so `add_remote_legacy_oob` became `AddRemoteLegacyOob`
and BlueZ rejected it with `UnknownMethod`. Fixed with an explicit
`#[zbus(name = "AddRemoteLegacyOOB")]` override — worth remembering as a
general lesson for any future zbus proxy method here: check the derived name
against the real D-Bus method name whenever the interface has an acronym or
other non-standard capitalization.

A second, unrelated bug also turned up on the second hardware run:
`Device1.Pair()` failed with `org.bluez.Error.AlreadyExists`, traced to a
stale `Paired=true` Device1 object left behind under a *different*,
previously-used address (the keyboard hands out a new address every time the
F1/F2/F3 exchange runs, so repeated test/pairing runs leave a trail of stale
device entries in bluetoothd). Fixed with
`bond::remove_matching_bluez_devices()`, which enumerates `bluetoothctl
devices`, matches each against the keyboard's stable identity (resolved GAP
name, Bluetooth-side PnP ID, vendor-specific GATT service UUID — not its
ever-changing address) and removes matches via `bluetoothctl remove` before
anything else runs; called as the first action in `dbus_pair::run_dbus_pair`.

The peer branch's other finding — `mgmt_pair_device` sending
`MGMT_OP_CANCEL_PAIR_DEVICE`/`MGMT_OP_DISCONNECT` to avoid leaking kernel
connection state across failed raw-mgmt attempts — no longer applies: the
raw-mgmt engine it patched was removed (see above).

## Differences from the Python tools (deliberate)

- **`auto` calls the engine in-process** instead of shelling out to a wrapper
  script and grepping its stdout for a summary line. The address and
  pass/fail come directly off the returned `DbusPairOutcome` struct.
- **Stale-device cleanup runs automatically before every pairing attempt**
  (`bond::remove_matching_bluez_devices()`), which none of the Python tools
  do — added after hitting the `AlreadyExists` failure above during hardware
  testing of this port, not present in the Python originals.

## Known-unverified / lower-confidence spots

- **Reconnect-after-unplug in `auto`**: hardware-tested, succeeded (0s) on
  most runs, failed to reconnect within 90s on one — see the status note at
  the top of this file. Not enough runs yet to know if that's rare flakiness
  (most likely) or a real intermittent gap.
- **`adopt` (`src/att.rs`)**: not yet run standalone against hardware — the
  reconnect-watch working without it on most runs makes it look unnecessary
  for this keyboard's D-Bus path, but that's not the same as verified.
- **`sock.rs`**: hand-written `sockaddr_l2cap` struct and raw
  `libc::socket`/`bind`/`connect`/`setsockopt` calls for the L2CAP ATT
  channel `adopt` uses, since Rust's `std::net` has no `AF_BLUETOOTH`
  support. Cross-checked against `<bluetooth/*.h>` and against what
  `test/phase4.py` passes through Python's `socket` module, but not yet
  round-tripped against a real kernel.
- **L2CAP connect with a timeout**: implemented as non-blocking-connect +
  `poll(POLLOUT)` + `SO_ERROR` check, since `SO_SNDTIMEO` doesn't reliably
  bound `connect()` on Linux. Python relied on `socket.settimeout()` before
  `connect()`, which does the equivalent under the hood — the Rust version
  should behave the same but wasn't compared side by side.
- **hidraw ioctl request codes** (`hid.rs`'s `ioc()`): reimplements the
  `_IOC` macro from `<linux/ioctl.h>` bit-for-bit against Python's version;
  only `HIDIOCSFEATURE` is exercised (matches `mkbd_common.py`, which only
  calls `set_feature` from this path too) — this part IS hardware-proven,
  since both `dbus-pair` runs used it for the F1/F2/F3 exchange.
- **`btmon`/`dmesg` capture**: no longer wired to anything (it was
  `pair`'s `--diag` flag, removed with the raw-mgmt engine); the code for it
  went with `pair.rs`.

## Building

```
cd rust/mkbd-pair
cargo build --release
# binary at target/release/mkbd-pair
```

No system dependencies beyond a Rust toolchain — `libc`, `clap`, and `zbus`
are resolved from crates.io at build time (not a runtime dependency of the
resulting binary).

## Trying it (once you're ready to test against hardware)

Everything needs root. Needs bluetoothd running with `--experimental` and
the `AddRemoteLegacyOOB` D-Bus method (`optionA/bluez/0001-*.patch` /
`test/install-optionA-bluetoothd.sh`), and the keyboard on USB (045e:0815):

```
sudo target/release/mkbd-pair dbus-pair
```

If something doesn't match `test/optionA-dbus-pair.py`'s behavior, that's the
port to fix — the Python tools remain the reference implementation until this
one is fully proven out.
