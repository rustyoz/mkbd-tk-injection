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
       3. on yes: ../../pairmodernkeyboard.sh --option-a
                  (F1/F2/F3 -> MGMT Add Remote OOB Data len-88 -> MGMT Pair
                  Device -> phase 4 GATT provisioning -> bond written;
                  bluetoothd stopped/masked for the duration)
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

## The D-Bus pairing experiment (tried, failed, reverted)

Step 3 briefly called `Adapter1.AddRemoteLegacyOOB()` over D-Bus +
`StartDiscovery()`/`Device1.Pair()` instead (`../../test/optionA-dbus-pair.py`),
so `bluetoothd` would never need stopping. **Tested on real hardware
2026-09-11: failed 5/5.** BlueZ discovery never saw the keyboard's directed
advertisement within 20s, any number of retries — confirming
`docs/PROTOCOL.md`'s (sibling repo) original caution that BlueZ's normal
discovery/pair path can't catch this keyboard's directed advertising window.

Worse, the failed attempts had a real cost: **each one re-runs F1/F2/F3, and
this keyboard appears to abandon its currently active bond as soon as a new
pairing exchange starts — whether or not that attempt then succeeds.** Five
failed D-Bus attempts in a row cost the previously-working bond from an
earlier successful `--option-a` pair, which had to be re-established with
the raw-mgmt path afterward. `test/optionA-dbus-pair.py` and the patched
`bluetoothd` (`../bluez/`) are kept as-is for reference, but **don't retry
this path** without first fixing the discovery-miss (e.g. driving the LE
scan/connection at the HCI level directly instead of through
`Adapter1.StartDiscovery()`), and be aware that testing it at all risks the
current bond.

## What's not handled here

- **Multi-user / multi-seat**: picks the first non-root `loginctl` session.
  Fine for a single-user desktop; not seat-aware.
- **This `autopair/` orchestrator itself — the udev rule, the service, the
  zenity prompt flow — has been installed and exercised (2026-09-11)**, but
  every run so far has gone through the D-Bus path above (all failed) or was
  declined by the user; it has not yet completed a successful pair through
  this wrapper. The underlying `pairmodernkeyboard.sh --option-a` engine it
  now calls again is hardware-verified on its own (`../BUILD.md` "Status").
