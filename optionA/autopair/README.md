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
       3. on yes: ../../test/optionA-dbus-pair.py
                  (F1/F2/F3 -> Adapter1.AddRemoteLegacyOOB() over D-Bus ->
                  StartDiscovery + wait for the Device1 object -> Device1.Pair()
                  with a NoInputNoOutput agent — bluetoothd stays running
                  throughout, unlike the raw-mgmt pairmodernkeyboard.sh path)
       4. zenity --info: "Paired. Unplug the USB cable now."
       5. poll: USB gone, then `bluetoothctl info <addr>` shows Connected: yes
       6. notify-send: "Connected over Bluetooth." (or a timeout warning)
```

Step 3's D-Bus path depends on BlueZ's own discovery actually catching the
keyboard's short directed advertisement — unproven when `optionA-dbus-pair.py`
was written (see that script's docstring). If it turns out not to work
reliably, the fix is switching step 3 back to `../../pairmodernkeyboard.sh
--option-a` (the raw-mgmt path, hardware-verified, but stops `bluetoothd` for
the duration of every pair).

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
2. `sudo test/install-optionA-bluetoothd.sh` — the patched `bluetoothd`
   (`Adapter1.AddRemoteLegacyOOB`) has to be the one actually running (see
   `../bluez/README.md`). No reboot needed for this one, just a service
   restart, which the install script does.
3. `zenity` and a `notify-send`/libnotify-compatible agent available in the
   graphical session that will get prompted. The script borrows that session's
   `DISPLAY`/`WAYLAND_DISPLAY`/`DBUS_SESSION_BUS_ADDRESS` via
   `systemctl --user show-environment`, so it works under Wayland or X11
   without hardcoding a compositor.
4. `sudo optionA/autopair/install.sh`.

## What's not handled here

- **Multi-user / multi-seat**: picks the first non-root `loginctl` session.
  Fine for a single-user desktop; not seat-aware.
- **The D-Bus pairing path (step 3) is new and its core assumption —**
  **that BlueZ discovery catches the keyboard's directed advert — was**
  **unproven as of the last test run.** If `optionA-dbus-pair.py` turns out
  not to reliably find the Device1 object, this whole flow fails at step 3
  with that diagnosis in the error dialog; the documented fix is reverting
  step 3 to `../../pairmodernkeyboard.sh --option-a` (raw mgmt, hardware-
  verified, but stops `bluetoothd` for the pair).
- **The mgmt-based pairing engine is hardware-verified (2026-09-11)** — see
  `../BUILD.md` "Status": `pairmodernkeyboard.sh --option-a` (run by hand, not
  through this `autopair/` wrapper) produced a real bonded, connected,
  GATT-resolved keyboard with working `uhid` input devices. The newer D-Bus
  engine this wrapper now calls has not yet had the same hardware
  confirmation.
- **This `autopair/` orchestrator itself — the udev rule, the service, the**
  **zenity prompt flow — has not been installed or triggered yet.** `install.sh`
  has not been run on this machine.
