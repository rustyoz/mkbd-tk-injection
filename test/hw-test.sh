#!/usr/bin/env bash
# Phase 0 hardware test — thin wrapper around tk-pair.py.
#
#   sudo test/hw-test.sh                    # normal run
#   sudo test/hw-test.sh --tk-order reversed
#
# Prereqs: root; patched bluetooth.ko loaded (test/install-module.sh + reboot);
# the Modern Keyboard on USB (045e:0815), switched on; the modernkeyboard repo
# at ~/Work/modernkeyboard (override with MKBD=...).
HERE=$(cd "$(dirname "$0")" && pwd)
exec python3 "$HERE/tk-pair.py" "$@"
