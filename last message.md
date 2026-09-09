Live-unloading bluetooth isn't possible on your machine — refcnt 11, bnep is pinned (an active PAN connection), and it's your daily driver with Bluetooth in use. So the test needs a reboot into the patched module.

Good news from the diagnostics: /sys/kernel/security/lockdown = [none], so the unsigned patched .ko will load at boot with no signing hassle.

New flow (committed)

cd ~/Work/mkbd-tk-injection
sudo test/install-module.sh          # backs up updates/bluetooth.ko, drops in the patched build, depmod
#   ↳ reboot
cat /sys/kernel/debug/bluetooth/hci0/le_legacy_oob_tk   # exists / "Permission denied" = good;  "No such file" = patch didn't load
#   ↳ plug in the Modern Keyboard over USB, switch it on
sudo test/hw-test.sh                 # F1/F2/F3 → inject TK → pair → dump bond + btmon

Revert whenever: sudo test/uninstall-module.sh then reboot.

hw-test.sh now preflights that the live module is the patched build and tells you to install+reboot if not, so you can't accidentally run it against the stock module.

What I need back

After the reboot + hw-test.sh, paste its output — especially:
- the btmon -r /tmp/mkbd-tkinj-*.btsnoop view around the SMP exchange (Pairing Request OOB flag, Confirm/Random, Encryption Change)
- dmesg | grep -i 'legacy OOB TK'
- the /var/lib/bluetooth/<adapter>/<addr>/info dump the script prints

If it fails with Pairing Failed 0x04 after our Confirm, re-run sudo TKORDER=reversed test/hw-test.sh (that's the F3-TK byte-order question).

needs input: run sudo test/install-module.sh, reboot, then sudo test/hw-test.sh with the keyboard attached, and paste the btmon/dmesg/bond output — I can't reboot the machine from here.