# Option A auto-pair — detect, prompt, pair, confirm, reconnect

End-to-end UX on top of the Option A kernel patches: plug the keyboard in over
USB, get asked whether to pair it, click yes, get told to unplug the cable,
and get confirmation once it's back on Bluetooth. No manual `bluetoothctl`.

```
USB plug-in
  -> udev (71-mkbd-optionA-autopair.rules) matches 045e:0815 interface 0
  -> systemd starts mkbd-optionA-autopair.service (root, oneshot)
  -> mkbd-optionA-autopair:
       1. revalidate the device is really there
       2. zenity --question in the logged-in graphical session: "Pair now?"
       3. on yes: try ../../test/optionA-dbus-pair.py first
                  (F1/F2/F3 -> Adapter1.AddRemoteLegacyOOB -> Adapter1.ConnectDevice
                  -> Device1.Pair -> bond written; bluetoothd never stopped,
                  it does GATT/HID profile connection itself) — NOT YET
                  hardware-verified, see that script's header.
                  On failure, fall back to ../../pairmodernkeyboard.sh --option-a
                  (F1/F2/F3 -> MGMT Add Remote OOB Data len-88 -> MGMT Pair
                  Device -> phase 4 GATT provisioning -> bond written;
                  bluetoothd stopped/masked for the duration) — the proven path.
       4. zenity --info: "Paired. Unplug the USB cable now."
       5. poll: USB gone, then `bluetoothctl info <addr>` shows Connected: yes
       6. notify-send: "Connected over Bluetooth." (or a timeout warning)
```

## Files

| File | What |
|---|---|
| `mkbd-optionA-autopair` | The orchestrator (bash, root). Runs standalone too: `sudo optionA/autopair/mkbd-optionA-autopair`. |
| `71-mkbd-optionA-autopair.rules` | udev rule, activates the service on hidraw add. |
| `mkbd-optionA-autopair.service` | systemd oneshot unit `ExecStart`ing the orchestrator. |
| `install.sh` | Copies the two above into `/etc/`, `udevadm control --reload` + `systemctl daemon-reload`. `--uninstall` reverses it. |

## Prerequisites

1. `sudo test/install-optionA-module.sh && reboot` — the Option A kernel patches
   have to be the booted `bluetooth.ko` (see `../BUILD.md` for how it's built).
2. `zenity` and a `notify-send`/libnotify-compatible agent available in the
   graphical session that will get prompted. The script borrows that session's
   `DISPLAY`/`WAYLAND_DISPLAY`/`DBUS_SESSION_BUS_ADDRESS` via
   `systemctl --user show-environment`, so it works under Wayland or X11
   without hardcoding a compositor.
3. `sudo optionA/autopair/install.sh`.

(The patched `bluetoothd` from `../bluez/` is *not* a prerequisite for this
flow — see below.)

## The D-Bus pairing path (bluetoothd never stops)

Step 3 calls `Adapter1.AddRemoteLegacyOOB()` + `Adapter1.ConnectDevice()`
over D-Bus, then `Device1.Pair()` (`../../test/optionA-dbus-pair.py`), so
`bluetoothd` never needs stopping.

**First attempt (2026-09-11) used `StartDiscovery()`/poll-for-`Device1`
instead of `ConnectDevice()` and failed 5/5.** BlueZ discovery never saw the
keyboard's directed advertisement within 20s, any number of retries —
confirming `docs/PROTOCOL.md`'s (sibling repo) original caution that BlueZ's
normal discovery/pair path can't catch this keyboard's directed advertising
window. Worse, the failed attempts had a real cost: **each one re-runs
F1/F2/F3, and this keyboard appears to abandon its currently active bond as
soon as a new pairing exchange starts — whether or not that attempt then
succeeds.** Five failed D-Bus attempts in a row cost the previously-working
bond from an earlier successful `--option-a` pair, which had to be
re-established with the raw-mgmt path afterward.

**Second attempt (2026-09-12) swapped in `Adapter1.ConnectDevice()` — a
stock, `[experimental]`-flagged BlueZ method whose own doc string is
literally "Connects to device without need of performing General Discovery"
(`man 5 org.bluez.Adapter`) — and it worked, first try, on real hardware:**

```
:: Adapter1.AddRemoteLegacyOOB(C9:6C:7E:11:6C:7E, random, tk=...) over D-Bus ...
   stored — kernel now has the TK for this identity
:: Adapter1.ConnectDevice(C9:6C:7E:11:6C:7E, random), timeout 20s ...
   connected, Device1 at /org/bluez/hci0/dev_C9_6C_7E_11_6C_7E after 0.1s
:: Device1.Pair() -> C9:6C:7E:11:6C:7E (timeout 30s) ...

OK  Paired=True Bonded=True Connected=True
*** D-BUS PAIRING WORKS — bluetoothd handled the whole thing, never stopped ***
```

Checked separately right after (a different, one-later bond address,
`C9:6C:7E:12:6C:7E` — see "note" below): `bluetoothctl info` showed
`Paired`/`Bonded`/`Trusted`/`Connected` all `yes`, full GATT resolution
(Human Interface Device, Battery Service at 85%, Device Information, the
vendor service), and `/proc/bus/input/devices` had a live `bluez-hog-device
Keyboard` uhid input device (`Uniq=c9:6c:7e:12:6c:7e`) with a full keymap —
i.e. bluetoothd's own HoGP-to-uhid bridge did the same job the raw-mgmt
path's in-kernel one did, matching `../BUILD.md`'s hardware-verification bar
for the raw-mgmt path, this time with `bluetoothd` never stopped or masked.

This is one successful run, not a five-run stress test, and the earlier
"5/5 failed" was specifically a discovery-pipeline problem this bypasses
entirely rather than evidence the pairing mechanism itself was flaky — so
treat it as verified-working, not yet stress-tested. The same caveats as
before still apply to *repeated* attempts: this keyboard abandons its
currently active bond as soon as a new F1/F2/F3 exchange starts regardless of
outcome, and same-boot repeated-attempt degradation has separately been
observed via the raw-mgmt path (`../BUILD.md` "Known issue") — a likely fix
for that landed in `mgmt_pair_device()`, which this D-Bus script doesn't even
use, so whether the same degradation can affect this path too is still open.

Note: the live bonded address (`...12...`) was one bond-address-increment
ahead of the one this test script paired (`...11...`) by the time it was
checked — most likely the already-installed system-wide autopair
(`/etc/udev/...`/`/etc/systemd/...`, installed and exercised on 2026-09-11
per `../BUILD.md`) auto-fired again from the same USB event using whatever
version of `mkbd-optionA-autopair` was installed at the time, not necessarily
the one in this checkout. Reinstall via `../autopair/install.sh` to sync it
if that matters for further testing.

## What's not handled here

- **Multi-user / multi-seat**: picks the first non-root `loginctl` session.
  Fine for a single-user desktop; not seat-aware.
- **This `autopair/` orchestrator itself — the udev rule, the service, the
  zenity prompt flow — has been installed and exercised (2026-09-11)**, but
  every run so far has gone through the D-Bus path above (all failed) or was
  declined by the user; it has not yet completed a successful pair through
  this wrapper. The underlying `pairmodernkeyboard.sh --option-a` engine it
  now calls again is hardware-verified on its own (`../BUILD.md` "Status").
