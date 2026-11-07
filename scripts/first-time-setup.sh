#!/usr/bin/env bash
# first-time-setup.sh — bootstrap persistent adb access on a fresh/reset Narwal T10.
#
# The robot's adb USB gadget is only up ~1 s per boot (boot_app stops adbd), and adb
# SYNC (push) works as root with no /bin/login. So this script tight-loops `adb push`
# to land our rc.local the instant the boot window opens, then pushes our adb key.
# After a reboot the rc.local makes adb persistent (USB + TCP:5555) with a wakelock.
#
# Usage:
#   ./scripts/first-time-setup.sh                       # USB-only access (no wifi block)
#   ./scripts/first-time-setup.sh --ssid SSID --psk PSK # also self-join wifi -> adb over TCP
#   NARWAL_SSID=.. NARWAL_PSK=.. ./scripts/first-time-setup.sh
#
# Then: reboot the robot once; `./scripts/narwal.sh connect` to use it.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SSID="${NARWAL_SSID:-}"; PSK="${NARWAL_PSK:-}"
while [ $# -gt 0 ]; do case "$1" in
  --ssid) SSID="$2"; shift 2;; --psk) PSK="$2"; shift 2;;
  *) echo "unknown arg: $1"; exit 2;; esac; done

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
RC="$TMP/rc.local"
cp "$HERE/rc.local" "$RC"
if [ -n "$SSID" ] && [ -n "$PSK" ]; then
  sed -i "s/__SSID__/${SSID//\//\\/}/; s/__PSK__/${PSK//\//\\/}/" "$RC"
  echo "provisioning WITH wifi self-join (ssid=$SSID)"
else
  # strip the wifi block -> USB-only access
  sed -i '/#WIFI_BEGIN/,/#WIFI_END/d' "$RC"
  echo "provisioning USB-only (no wifi creds given; pass --ssid/--psk for TCP access)"
fi

adb start-server >/dev/null 2>&1
echo ">>> POWER-CYCLE / REBOOT THE ROBOT NOW (USB into this PC). 180 s window..."
end=$((SECONDS+180)); n=0; ok=0
while [ $SECONDS -lt $end ]; do
  n=$((n+1))
  SER=$(adb devices | awk '/\tdevice$/{print $1}' | grep -v '^[0-9.]*:' | head -1)
  if [ -n "$SER" ]; then
    if adb -s "$SER" push "$RC" /etc/rc.local >/dev/null 2>&1; then
      echo "[$(date +%H:%M:%S)] pushed rc.local (attempt $n, serial $SER)"
      [ -f "$HOME/.android/adbkey.pub" ] && \
        adb -s "$SER" push "$HOME/.android/adbkey.pub" /mnt/UDISK/adb_keys >/dev/null 2>&1 \
        && echo "    + installed adbkey.pub -> /mnt/UDISK/adb_keys (TCP auth)"
      if adb -s "$SER" pull /etc/rc.local "$TMP/readback" >/dev/null 2>&1 && \
         diff -q "$RC" "$TMP/readback" >/dev/null; then echo "    verified byte-identical."; fi
      ok=1; break
    fi
  fi
done
if [ $ok -eq 1 ]; then
  echo "=== SUCCESS (attempts=$n). Now REBOOT the robot once to activate. ==="
  echo "Then:  ./scripts/narwal.sh connect"
else
  echo "=== Never caught the adb window in 180 s. Re-run and power-cycle again ==="
  echo "   (ensure USB is to this PC, not the dock; another boot window will come)."
  exit 1
fi
