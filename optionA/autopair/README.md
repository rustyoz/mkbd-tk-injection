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
                  Device -> phase 4 GATT provisioning -> bond written)
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

## What's not handled here

- **Multi-user / multi-seat**: picks the first non-root `loginctl` session.
  Fine for a single-user desktop; not seat-aware.
- **BlueZ patches exist (`../bluez/`) but aren't wired in here yet**: pairing
  still goes straight to the kernel `mgmt` control socket, bypassing
  `bluetoothd` for the duration of the pair (same as `test/optionA-pair.py` /
  `test/tk-pair.py`), because the patched `bluetoothd` (`../bluez/README.md`)
  hasn't been installed on this machine. Once it is, this orchestrator's step
  3 can call `Adapter1.AddRemoteLegacyOOB()` over D-Bus instead and
  `bluetoothd` never needs masking — that swap is not done here.
- **Not yet run against real hardware.** The kernel module and this script are
  both new; see `../BUILD.md` "Status" for exactly what has and hasn't been
  exercised on the physical keyboard.
