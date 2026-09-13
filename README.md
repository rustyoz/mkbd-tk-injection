# Microsoft Keyboard Pairing on Linux

**TL;DR:** the Microsoft Modern Keyboard (Fingerprint ID) can't be paired
through a normal Linux Bluetooth stack — it demands an OOB pairing key the
kernel has no way to accept. This repo has the kernel + BlueZ patches that
fix that, and `mkbd-pair`, a Rust tool that pairs the keyboard and can
auto-pair it the moment it's plugged in over USB. See
[Installation](#installation) to set it up, or
[Supported devices](#supported-devices) to check whether your keyboard
applies.

No Windows, no seizing the Bluetooth controller into a userspace host
process — just a normal Bluetooth pairing through a normal `bluetoothd`,
same as any other Bluetooth keyboard.

## The problem

LE legacy pairing derives the session key from a 128-bit Temporary Key (TK).
For the out-of-band (OOB) method, that TK is carried outside the Bluetooth
link — here, over the keyboard's USB vendor channel (its `F3` response). The
in-kernel Security Manager (`net/bluetooth/smp.c`) has **no way to accept
it**: `tk_request()` only ever sets the TK to `0` (Just Works) or a 32-bit
passkey, and `MGMT_OP_ADD_REMOTE_OOB_DATA` has no field for a legacy TK (only
BR/EDR and LE *Secure Connections* OOB `hash`/`rand`).

This keyboard forces the issue: IO capability `NoInputNoOutput`, sets the
MITM bit, sets OOB-present in its Pairing Response, and **hangs up (`0x13`)
on any non-OOB Pairing Request**. Just Works is refused; OOB with the real TK
is mandatory. Without a kernel/BlueZ change, it's unpairable through a normal
`bluetoothd` on Linux.

## The fix — "Option A"

A small kernel patch set (`optionA/0001`-`0004`) teaches the kernel's mgmt
UAPI and SMP implementation to accept and use a real LE-legacy-OOB TK,
delivered through an extended `MGMT_OP_ADD_REMOTE_OOB_DATA` payload — the
smallest surface change that fits the existing opcode (see `UPSTREAM.md` for
the upstreaming rationale, and `optionB/` for a fallback design using a new
opcode instead, kept in case upstream maintainers prefer that shape). A
matching two-patch BlueZ change (`optionA/bluez/`) exposes the same thing as
`Adapter1.AddRemoteLegacyOOB()` over D-Bus, so `bluetoothd` can drive the
whole pairing itself instead of needing a userspace tool to talk to the raw
mgmt socket directly.

`rust/mkbd-pair` is the tool that uses this: it reads the keyboard's
one-time TK over its USB vendor channel, hands it to `bluetoothd` via that
new D-Bus method, then asks `bluetoothd` to connect and pair — all without
ever stopping the system's Bluetooth daemon.

## Status

`mkbd-pair auto`'s full flow — detect the keyboard on USB, prompt, pair via
the D-Bus method above, tell you to unplug, confirm the reconnect over
Bluetooth — is **hardware-verified end to end** (2026-09-12), including full
GATT resolution (HID, Battery, Device Information) and a live keyboard input
device. It isn't perfectly reliable on every single run yet: two transient
BLE connection timeouts have been seen in testing so far, both cleared by a
plain retry with no code change needed. See `rust/mkbd-pair/README.md` for
the details, and `optionA/BUILD.md` / `PROGRESS.md` for the full dated
history of how this was built and verified.

The kernel and BlueZ patches themselves are further along: hardware-verified
working (`optionA/BUILD.md`), and `optionA/PLAN.md`/`UPSTREAM.md` lay out
what's left before proposing them upstream. Phase 0 (a throwaway debugfs-only
kernel PoC, superseded by the real Option A patches) and the original raw
`mgmt`-socket Python tools (`test/`) are kept as reference/historical
material — useful for understanding the protocol or porting it elsewhere —
but `mkbd-pair` is the one to actually install.

## Supported devices

**Tested and working: Microsoft Modern Keyboard, Fingerprint ID, model 1780**
(USB `045e:0815`, Bluetooth PnP ID `045e:0813`) — the "BTLE Keyboard
Fingerprint ID" that ships as part of the Microsoft Modern Keyboard with
Fingerprint ID bundle. This is the only device this has been built and
hardware-verified against.

Untested, may or may not work: any other Microsoft keyboard using the same
LE-legacy-OOB-over-USB-vendor-channel pairing scheme. The `F1`/`F2`/`F3` USB
vendor exchange in `rust/mkbd-pair`'s `hid.rs`, the TK format, and the
`045e:0815` VID/PID match in the udev rule are all specific to this exact
device — a different Microsoft keyboard would need its own USB vendor
protocol reverse-engineered (or confirmed identical) before any of this
applies. The kernel/BlueZ patches themselves are protocol-generic (they just
teach the stack to accept an LE-legacy-OOB TK from *any* peer that provides
one), so a differently-shaped device that also needs this could reuse them
even if `mkbd-pair`'s USB side needs adapting.

## Installation

This builds and installs three things: a patched kernel Bluetooth module, a
patched `bluetoothd`, and the `mkbd-pair` tool. All three need root at
install time; only `mkbd-pair` itself needs root at *run* time.

Tested on Arch Linux. The kernel and BlueZ steps involve patching and
rebuilding system components — read `optionA/BUILD.md` and
`optionA/bluez/README.md` before running anything if you want the full
rationale, not just the commands.

### 1. Patched kernel module

```bash
# Find your running kernel's upstream version (drop any distro suffix,
# e.g. 7.2.3-arch1-1 -> 7.2.3), then download the matching source:
uname -r
curl -LO "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.3.tar.xz"   # adjust version + v7.x/v6.x to match

# apply patches 0001-0004 (0005 is a selftest patch tied to a specific
# kernel version — see optionA/BUILD.md, skip it) and build net/bluetooth
# against your *exact* running kernel — full recipe, including the
# vermagic-matching gotchas, is in optionA/BUILD.md
```

Follow `optionA/BUILD.md`'s recipe exactly — matching your running kernel's
build config/`Module.symvers` is the fiddly part, and that file covers the
failure modes. Once you have `bluetooth.ko` built and its `vermagic` matches
`uname -r`:

```bash
sudo test/install-optionA-module.sh
sudo reboot
```

### 2. Patched `bluetoothd`

Follow `optionA/bluez/README.md` (patches a stock **bluez-5.87** source
tree; adjust the version if your distro ships a different one — the patches
may need a rebase). Summary:

```bash
curl -LO https://mirrors.edge.kernel.org/pub/linux/bluetooth/bluez-5.87.tar.xz
tar -xf bluez-5.87.tar.xz && cd bluez-5.87
git init -q && git add -A && git commit -q -m baseline
git am ../optionA/bluez/0001-*.patch ../optionA/bluez/0002-*.patch
./configure --disable-obex --disable-cups --disable-manpages
make src/builtin.h   # must run before the next line, once
make src/bluetoothd
```

Install it in place of the stock binary (this restarts `bluetoothd`, briefly
dropping every Bluetooth connection on the machine):

```bash
sudo test/install-optionA-bluetoothd.sh
```

Then enable the `--experimental` flag `mkbd-pair` needs (for
`Adapter1.ConnectDevice()`, an experimental BlueZ method):

```bash
sudo systemctl edit bluetooth.service
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/lib/bluetooth/bluetoothd --experimental
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
```

### 3. `mkbd-pair`

```bash
cd rust/mkbd-pair
cargo build --release
```

Plug the keyboard in over USB, then test pairing manually:

```bash
sudo target/release/mkbd-pair dbus-pair
```

If that works, install it as an auto-pair-on-plug-in service (needs
`zenity` and a notification daemon running in your graphical session):

```bash
sudo rust/mkbd-pair/packaging/install.sh
```

From then on, plugging the keyboard in over USB prompts you to pair; once
paired, unplug the cable and it reconnects over Bluetooth on its own.

## Layout

```
README.md, LICENSE               this file, MIT license
PLAN.md                          the phased plan (debugfs PoC -> Option A -> upstream)
UPSTREAM.md                      turning the patches into a mainline series + RFC cover letter
PLUGIN-PLAN.md                   packaging as an AUR / Omarchy install (DKMS + hooks + udev) -- not yet built
PROGRESS.md                      detailed dated worklog -- not required reading, but has the full run history
kernel/                          Phase 0 kernel patch (debugfs PoC, superseded by optionA/) + README
optionA/                         the real fix: kernel patches 0001-0004, BlueZ patches, build docs,
                                    the bash-based reference autopair implementation
optionB/                         a fallback kernel-API design (new mgmt opcode instead of extending
                                    the existing one), kept in case upstream prefers that shape -- see UPSTREAM.md
rust/mkbd-pair/                  the tool to install -- `dbus-pair` (manual) and `auto` (udev-triggered)
  packaging/                     install.sh + udev rule + systemd service for the auto-pair flow
test/, lib/                      the original Python/raw-mgmt-socket tools mkbd-pair was ported from --
                                    reference implementation, kept for protocol study, superseded by rust/mkbd-pair
notes/                           analysis of the kernel SMP paths the patches touch
.github/workflows/               CI: verifies the kernel/BlueZ patches still apply + build, and that
                                    mkbd-pair still builds/tests/lints clean
build/                           (gitignored) kernel/BlueZ source trees, scratch
```

## License

MIT — see [`LICENSE`](LICENSE). The kernel and BlueZ patch files are diffs
against GPL-2.0/LGPL-2.1 projects and carry those projects' license terms.
