# Plan — package this as a distributable Omarchy "plugin"

Goal: an Omarchy user installs one thing, plugs the keyboard's USB cable in
once, and from then on the Microsoft Modern Keyboard (1780) pairs and reconnects
over Bluetooth by itself — including surviving kernel updates.

## What "an Omarchy plugin" is here

Omarchy has **no** third-party system-plugin mechanism. `omarchy plugin …` is
only for Quickshell bar widgets; `omarchy install` / `omarchy pkg` install Arch
packages; `omarchy hook install` wires scripts to system events. So this ships
as:

- an **Arch package** (PKGBUILD), with the kernel change as a **DKMS module** so
  it rebuilds on every `linux` upgrade;
- **omarchy hooks** the package drops in (`post-update.d`, `post-boot.d`);
- a **udev rule + systemd unit** that auto-pairs when the USB cable is attached;
- published to the **AUR** so install is `omarchy pkg aur add mkbd-modern-keyboard`
  (or `yay -S`), plus a `git clone && makepkg -si` path and an optional
  `curl | bash` bootstrap.

Package name: **`mkbd-modern-keyboard`** (meta) → depends on
`mkbd-bluetooth-dkms` + `mkbd-pair`.

## Components

### 1. Kernel patch → `mkbd-bluetooth-dkms`

`kernel/0001-*.patch` currently needs the whole `net/bluetooth/` tree to build
`bluetooth.ko`. DKMS layout:

```
/usr/src/mkbd-bluetooth-<pkgver>/
  dkms.conf
  0001-smp-le-legacy-oob-tk.patch     (rebased to apply under net/bluetooth/)
  build.sh                            (PRE_BUILD: fetch+patch+stage the source)
```

**Build strategy (the hard part) — three options:**

| | approach | trade-off |
|---|---|---|
| **A (recommended)** | `build.sh` downloads `linux-<SUBLEVEL>.tar.xz` from cdn.kernel.org keyed to `uname -r`, extracts `net/bluetooth/` + needed headers, applies the patch, builds `bluetooth.ko` (+ `bnep hidp rfcomm bluetooth_6lowpan` — they co-link) against `/lib/modules/<kver>/build` with that tree's `Module.symvers`, installs to `updates/`, `depmod`. | ~160 MB download + ~1 GB extract at build time; cache the tarball under `/var/cache/mkbd-bluetooth/`. Works because Arch does not patch `net/bluetooth` (verified). |
| B | vendor `net/bluetooth/` per supported kernel series in the package | brittle, huge, needs constant updates |
| C | pacman hook that re-patches + rebuilds from the Arch `linux` PKGBUILD, or a full `linux-mkbd` | heaviest; conflicts with `linux` |

**Why DKMS wins over the current `install-module.sh` + reboot:** the standard
`dkms` pacman hook rebuilds the module automatically on `pacman -S linux`, so a
kernel upgrade doesn't silently drop the patch. If a rebuild *fails* on a new
kernel, modprobe falls back to the stock in-tree `bluetooth.ko.zst` — Bluetooth
keeps working, pairing this keyboard just stops until fixed. Good failure mode;
document it and have `post-update.d` warn.

**Secure Boot:** if enforced, DKMS signs the module with the machine MOK (same
as the nvidia DKMS module already on this box). Detect `mokutil --sb-state`,
document `mokutil --import`. This box is `lockdown=none` → unsigned loads fine.

**Risk:** DKMS building a *core in-tree* module is unusual. Verify the `dkms`
pacman hook fires for this conf on a `linux` upgrade, and that `updates/`
precedence over `kernel/…/bluetooth.ko.zst` holds after `depmod`.

### 2. Userspace → `mkbd-pair`

The engine (`test/tk-pair.py`, `test/phase4.py`) imports `mkbd_common` /
`mkbd_crypto` from the **separate `modernkeyboard` repo**. A package can't
depend on a second checkout, so:

- **vendor** a trimmed `mkbdlib` — the USB F1/F2/F3 exchange
  (`vendor_pairing_exchange`), the MGMT helpers (`mgmt_pair_device`,
  `mgmt_set_powered`, `_mgmt_cmd`, `MGMT_OP_UNPAIR_DEVICE`), the bond writer
  (`write_le_device_info`), address helpers — into `/usr/lib/mkbd/`.
- `tk-pair.py` + `phase4.py` → `/usr/lib/mkbd/` internals.
- `/usr/bin/mkbd-pair` = the current 3-line wrapper (`pairmodernkeyboard.sh`),
  with `MKBD` pointing at the vendored lib.
- deps: `python` (stdlib only — good), `bluez`, `bluez-utils`.
- keep `modernkeyboard` as the upstream of the vendored files; a `make vendor`
  target in this repo copies + trims them so they don't drift silently.

### 3. Auto-pair on USB attach

- `udev/90-mkbd.rules`: on `add`, `SUBSYSTEM=="hidraw"`, `045e:0815`
  interface 0 → `SYSTEMD_WANTS=mkbd-pair@.service` (instance = the USB path).
- `mkbd-pair@.service` (oneshot, `After=bluetooth.service`): runs
  `mkbd-pair --auto` →
  - read F1: if a working BlueZ bond already exists for the keyboard's *current*
    address, exit 0 (nothing to do);
  - else run the full Phase-0 + Phase-4 flow, quiet, log to the journal;
  - per-USB-device `flock` so replug storms don't double-run.
- Net effect: "plug the cable in once" replaces "run `sudo mkbd-pair`".

### 4. Auto-reconnect

- `modules-load.d/mkbd.conf` → `uhid` (ship it; today `install-module.sh` writes
  `/etc/modules-load.d/mkbd-uhid.conf` by hand).
- The keyboard already reconnects through `bluetoothd` once Trusted. Optional
  `mkbd-autoconnect.service` (port `modernkeyboard/bin/mkbd-autoconnect`):
  watch `udev` for the keyboard's USB `remove`, then nudge `bluetoothctl
  connect` with a bounded retry.

### 5. Omarchy hooks the package installs

- `~/.config/omarchy/hooks/post-update.d/mkbd-dkms-check` — after
  `omarchy update`: confirm `dkms status mkbd-bluetooth` shows `installed` for
  the running (or newly installed) kernel; if not, `notify-send` + an
  `omarchy reminder` with the one-line fix (`sudo dkms autoinstall`).
- `~/.config/omarchy/hooks/post-boot.d/mkbd` — ensure `uhid` is loaded and
  `mkbd-autoconnect` is up.
- These are dropped by the package into the user's hook dirs on install (or the
  package README tells the user to `omarchy hook install …`).

### 6. Uninstall

`pacman -R mkbd-modern-keyboard` →
`dkms remove mkbd-bluetooth --all`, pacman removes the `updates/bluetooth.ko`,
`depmod` restores stock, remove udev rule / units / modules-load / hooks.
`post_remove` in `.install`.

## Repo layout for the package

Add a `packaging/` dir to this repo (or a dedicated `mkbd-modern-keyboard` repo):

```
packaging/
  PKGBUILD                     mkbd-modern-keyboard (+ split: -dkms, -pair)
  mkbd-bluetooth.install       dkms add/build/install on post_install/upgrade
  dkms/
    dkms.conf
    build.sh                   fetch matching kernel src, patch, build, stage
    0001-smp-le-legacy-oob-tk.patch   -> generated from ../kernel/0001-*.patch
  usr-lib-mkbd/                tk-pair.py phase4.py mkbdlib/*.py  (vendored)
  bin/mkbd-pair
  udev/90-mkbd.rules
  systemd/mkbd-pair@.service   mkbd-autoconnect.service
  modules-load.d/mkbd.conf
  hooks/post-update.d/mkbd-dkms-check   hooks/post-boot.d/mkbd
  mkbd-pair.1                  man page
  install.sh                   makepkg -si + reboot prompt  (bootstrap)
```

## Distribution steps

1. **Local**: `cd packaging && makepkg -si` on this box — validates the DKMS
   build + install end to end.
2. **GitHub**: push `packaging/` (or split repo); README with `makepkg -si`.
3. **AUR**: publish `mkbd-modern-keyboard` (pulls `-dkms` + `-pair`). Then
   `omarchy pkg aur add mkbd-modern-keyboard` / `yay -S mkbd-modern-keyboard`.
4. **Bootstrap** (optional): `curl -fsSL <raw>/install.sh | bash`.
5. **CI**: weekly job applies `kernel/0001-*.patch` against the last N stable
   kernels + Arch's current `linux`; fail → patch drift alert. Keep
   version-conditional patch variants under `dkms/patches/` if `smp.c` diverges.

## The real end state (get off DKMS)

DKMS is the bridge. The durable answer is **Phase 1 of `PLAN.md`**: land the
`MGMT_OP_ADD_REMOTE_OOB_DATA` `le_legacy_tk` field upstream + a BlueZ release
with the D-Bus method. Then mainline kernels ≥ X + a stock BlueZ need **no
out-of-tree module** — the "plugin" collapses to the userspace `mkbd-pair` tool
+ udev/systemd glue + config, and the AUR package drops the `-dkms` split. State
this as the goal in the package README so contributors push upstream, not just
maintain the patch.

## Phased delivery

| ver | scope |
|---|---|
| **v0.2** | `packaging/` in-repo: PKGBUILD, `dkms.conf` + `build.sh` (option A), vendored `mkbdlib`, `/usr/bin/mkbd-pair`, `modules-load.d`. `makepkg -si` works on this box. Manual pairing (`sudo mkbd-pair`). |
| **v0.3** | udev + `mkbd-pair@.service` → auto-pair on cable attach; `mkbd-autoconnect`; `post-update.d` DKMS-health hook; `post-boot.d` hook. |
| **v0.4** | AUR publish; `omarchy pkg aur add` path; `install.sh` bootstrap; man page; uninstall (`.install` `post_remove`); CI patch-drift check. |
| **v1.0** | Phase 1 upstream (MGMT field) + BlueZ D-Bus method merged → drop `-dkms`; package becomes pure userspace + config. |

## Open questions

- Does the Arch `dkms` pacman hook rebuild a conf that replaces a core in-tree
  module, on `linux` upgrade, without `BUILD_EXCLUSIVE_*`? Test.
- Kernel source at build time: cdn.kernel.org tarball keyed to `uname -r`
  SUBLEVEL (works — Arch doesn't patch `net/bluetooth`), vs `pkgctl repo clone
  linux` (needs `devtools`). Cache either way.
- `phase4.py` uses a raw L2CAP CID-4 socket with `BT_SECURITY_HIGH`. If a future
  kernel restricts non-bluetoothd ATT sockets, need a fallback (`btgatt-client`,
  or a tiny C helper).
- Multi-adapter boxes: the tool assumes `hci0`. Package config
  (`/etc/mkbd/pair.conf`) for adapter selection.
- License: kernel patch is GPL-2.0 (derivative of `smp.c`); userspace can match
  the `modernkeyboard` repo's license. Keep the DKMS patch a clean
  `git format-patch` with a real commit message for eventual upstream.
