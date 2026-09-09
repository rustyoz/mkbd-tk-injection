# Kernel patches

## `0001-Bluetooth-SMP-inject-LE-legacy-OOB-Temporary-Key-via-.patch` (Phase 0)

Base: **linux-7.2.3** (`cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.3.tar.xz`,
`sha256 8ba259e8e7b13ec6ef0941c8a39ad90b24bd4a4d6c0010ba6bafb794550ecd03`).
Matches Arch `linux 7.2.3.arch1-3` — `net/bluetooth/` is not Arch-patched.

Touches `net/bluetooth/{smp.c,smp.h,hci_debugfs.c}`, +189 lines. Adds a
per-adapter debugfs knob and wires an LE-legacy-OOB TK into the Security
Manager. Throwaway PoC — see `../PLAN.md` Phase 0 and `../notes/smp-codepaths.md`.

### Apply

```bash
cd linux-7.2.3
patch -p1 < /path/to/0001-Bluetooth-SMP-inject-LE-legacy-OOB-Temporary-Key-via-.patch
# or: git am < 0001-*.patch
```

### Build

Needs `bc` (`pacman -S bc`) for `make prepare`.

Whole-tree (produces a bootable kernel or a loadable `bluetooth.ko` with correct
vermagic):

```bash
cd linux-7.2.3
zcat /proc/config.gz > .config
make olddefconfig
make -j"$(nproc)" modules_prepare
make -j"$(nproc)" M=net/bluetooth            # compile check
# full module with modpost (needs a built vmlinux / Module.symvers):
make -j"$(nproc)"                            # or the Arch PKGBUILD route
```

Compile-check only, no `bc`: copy `include/generated/timeconst.h` from
`/usr/lib/modules/$(uname -r)/build/` (or use `../build/shim/bc`), then
`make W=1 net/bluetooth/smp.o net/bluetooth/hci_debugfs.o`.

Preferred for hardware testing: patch the Arch `linux` PKGBUILD
(`pkgctl repo clone linux` or ABS), add this file to `source=()` + `prepare()`,
`makepkg`, `pacman -U`, reboot.

### Load / test

The running kernel and the module must be the same version. On this box the
running kernel was `7.1.9-arch1-2` while `7.2.3` was installed-pending-reboot —
reboot into `7.2.3` first.

```bash
sudo rmmod <deps> bluetooth        # or just boot the patched kernel package
sudo insmod net/bluetooth/bluetooth.ko
```

Then `../test/hw-test.sh`.

### Debugfs interface

`/sys/kernel/debug/bluetooth/hciX/le_legacy_oob_tk`, write-only:

```
echo "C9:6C:7E:DB:6C:7E 1 <32 hex digits, F3 TK bytes as-is>" \
  | sudo tee /sys/kernel/debug/bluetooth/hci0/le_legacy_oob_tk
```

- address MSB-first, as displayed by `bluetoothctl`
- type: `0` = public, `1` = random (keyboard is static random → `1`)
- 32 hex digits = the 16-byte TK from the `F3` response. Try **as-is** first
  (matches `mkbd-bumble-pair`); if the keyboard sends `Pairing Failed 0x04`
  after our Confirm, try the byte-reversed order.
- write an all-zero TK to remove the entry.
