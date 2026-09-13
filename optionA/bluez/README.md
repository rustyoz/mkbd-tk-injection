# Option A — BlueZ patches

Two patches against upstream **bluez-5.87** (`https://mirrors.edge.kernel.org/pub/linux/bluetooth/bluez-5.87.tar.xz`,
sha256 `26bdcf2cebd7310c6f598850606b037ef0c515fe6608ebc54d22c50c4c32b35f`) — the
same version as the installed `bluez 5.87-2` package. Design rationale for
both is `../BLUEZ-NOTES.md`.

| Patch | What |
|---|---|
| `0001-Bluetooth-adapter-add-AddRemoteLegacyOOB.patch` | `lib/bluetooth/mgmt.h` struct/flag/opcode-length mirror of the kernel UAPI (`../0001-Bluetooth-mgmt-accept-LE-legacy-OOB-TK.patch`); `btd_adapter_add_remote_le_legacy_oob()` in `src/adapter.c`/`.h`; the `Adapter1.AddRemoteLegacyOOB()` D-Bus method. |
| `0002-doc-org.bluez.Adapter-document-AddRemoteLegacyOOB.patch` | `doc/org.bluez.Adapter.rst` entry for the new method. |

## Apply + build

```bash
curl -LO https://mirrors.edge.kernel.org/pub/linux/bluetooth/bluez-5.87.tar.xz
tar -xf bluez-5.87.tar.xz && cd bluez-5.87
git init -q && git add -A && git commit -q -m baseline   # optional, for git am
git am /path/to/optionA/bluez/0001-*.patch /path/to/optionA/bluez/0002-*.patch

./configure --disable-obex --disable-cups --disable-manpages
#   --disable-obex/--disable-cups: avoids needing libical, unrelated to this
#     feature. --disable-manpages: avoids needing rst2man. Drop either if
#     your build environment has them and you want the full daemon.
make -j"$(nproc)" src/builtin.h    # BUILT_SOURCES target; `make src/bluetoothd`
                                    # alone fails looking for src/builtin.h
                                    # without this — run it first, once.
make -j"$(nproc)" src/bluetoothd

strings src/bluetoothd | grep AddRemoteLegacyOOB   # sanity check
```

Verified 2026-09-11: both patches apply clean with `git am` on a fresh
bluez-5.87 tree, and `src/bluetoothd` links with the new method present.
Installing it replaces the system's `bluetoothd` — a much bigger blast
radius than the kernel module (every Bluetooth device on the box goes
through it) — so swapping it in wants its own care (`test/install-optionA-bluetoothd.sh`
backs up and can roll back the stock binary).

**Status update, 2026-09-12: installed and hardware-verified live** — see
`../BUILD.md`'s "Adapter1.AddRemoteLegacyOOB() exercised live over D-Bus"
entry for the full account, and `../../rust/mkbd-pair/README.md` for the
Rust port that's now the recommended way to drive it
(`Adapter1.AddRemoteLegacyOOB()` → `Adapter1.ConnectDevice()` →
`Device1.Pair()`, `bluetoothd` never stopped). `../BUILD.md` is the
authoritative status doc; the checklist below is this file's own
build-only status, kept for the patches themselves.

## Status (patch build only — see `../BUILD.md` for live-hardware status)

- [x] Patches apply clean against bluez-5.87.
- [x] `bluetoothd` builds and links with `AddRemoteLegacyOOB` present.
- [x] Installed and exercised live over D-Bus, hardware-verified — see
      `../BUILD.md`.
- [ ] `monitor/packet.c` btmon decode and the `src/device.c`
      premature-OOB-removal check (`BLUEZ-NOTES.md` 2.2 / 2.6) are still
      spec-only, not patched.
