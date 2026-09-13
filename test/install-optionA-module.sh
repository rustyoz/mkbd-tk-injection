#!/usr/bin/env bash
# Install the Option A patched bluetooth.ko to disk (backing up the current
# one) so the NEXT BOOT loads it. No live unload — reboot after running this.
# Thin wrapper over install-module.sh pointed at the Option A artifact so both
# builds share one install/backup/uninstall mechanism.
#
#   sudo test/install-optionA-module.sh      # then reboot, then sudo ./pairmodernkeyboard.sh --option-a
#   sudo test/uninstall-module.sh            # then reboot to go back (shared with Phase 0)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
exec env KO="$HERE/../artifacts/bluetooth-7.1.9-arch1-2-optionA.ko" "$HERE/install-module.sh"
