#!/usr/bin/env bash
# upload-to-robot.sh — copy the on-robot code (robot/robot_server.py) onto the robot.
#
#   ./scripts/upload-to-robot.sh              # upload + reboot the robot
#   ./scripts/upload-to-robot.sh --no-reboot  # upload only
#   ./scripts/upload-to-robot.sh path/to/robot_server.py
#
# The robot's start script (app_normal_mode.sh) runs roscore + this file INSTEAD of the
# Narwal cloud bridge whenever it exists at the hook path below. So once uploaded and
# rebooted, the robot is in LOCAL mode: off the cloud, reachable with --transport tcp.
# This is a different mode from set-server-ip.sh (fake cloud); use one or the other.
# The robot keeps cleaning on its own even if the file is broken. Undo: ./scripts/restore-cloud.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"; N="$HERE/narwal.sh"
HOOK=/oem/DISK/bin/pita/remote_control/robot_server.py

REBOOT=1; SRC="$ROOT/robot/robot_server.py"
for a in "$@"; do case "$a" in --no-reboot) REBOOT=0 ;; *) SRC="$a" ;; esac; done
[ -f "$SRC" ] || { echo "not found: $SRC"; exit 1; }
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$SRC" 2>/dev/null \
  || echo "   warning: $SRC does not parse as Python 3 (fine if it is Python 2 only)"

echo ">>> uploading $SRC -> robot:$HOOK"
"$N" connect >/dev/null 2>&1 || true
out=$("$N" exec "mount -o remount,rw /oem; mkdir -p ${HOOK%/*}; echo RW_OK" || true)
grep -q RW_OK <<<"$out" || { echo "   robot not reachable — run ./scripts/narwal.sh connect"; exit 1; }
"$N" push "$SRC" "$HOOK" >/dev/null
want=$(wc -c <"$SRC" | tr -d ' ')
got=$("$N" exec "chmod 755 $HOOK; sync; wc -c <$HOOK" | tr -dc '0-9')
[ "$got" = "$want" ] || { echo "   size mismatch (local $want, robot ${got:-none}) — retry"; exit 1; }
echo "   uploaded $got bytes"

if [ "$REBOOT" = 1 ]; then
  echo ">>> rebooting the robot into local mode"
  "$N" reboot
  echo "    Then: python3 -m lan_server --transport tcp --robot-host \$(./scripts/narwal.sh ip)"
else
  echo "    Takes effect at the next robot reboot (./scripts/narwal.sh reboot)."
fi
