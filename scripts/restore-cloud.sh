#!/usr/bin/env bash
# restore-cloud.sh — undo both modifications and put the robot back on the Narwal cloud.
#   * restores the original cloud endpoints (saved by set-server-ip.sh)
#   * removes the LAN server cert from the robot's trust store
#   * removes robot_server.py from the local-mode hook (uploaded by upload-to-robot.sh)
#   * reboots the robot
# adb access (/etc/rc.local) is left in place; it does not affect cloud operation.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; N="$HERE/narwal.sh"
CONF=/oem/DISK/bin/pita/pita_communicator/conf
HOOK=/oem/DISK/bin/pita/remote_control/robot_server.py

echo ">>> restoring the Narwal cloud"
"$N" connect >/dev/null 2>&1 || true
out=$("$N" exec "mount -o remount,rw /oem
cd $CONF && for f in websocket_env_us.json http_env_us.json; do [ -e \$f.narwal-orig ] && cp \$f.narwal-orig \$f; done
cd /etc/ssl/certs && for l in *.0; do [ \"\$(readlink \$l)\" = narwal-lan-cloud.crt ] && rm -f \$l; done; rm -f narwal-lan-cloud.crt
rm -f $HOOK; sync; echo RESTORED" || true); grep -q RESTORED <<<"$out" || { echo "   failed — check ./scripts/narwal.sh status"; exit 1; }
"$N" reboot
echo ">>> done. After boot the phone app works again."
