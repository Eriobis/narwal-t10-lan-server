#!/usr/bin/env bash
# narwal.sh — Narwal T10 local access toolkit
# Hard-won facts baked in (see ./NARWAL-ACCESS.md):
#   * adb EXEC-OUT is unsupported by this old adbd -> always "error: closed". Use `adb shell`.
#   * SYNC (pull/push) is reliable over TCP (:5555), flaky over USB. SHELL works on both.
#   * The robot SUSPENDS its CPU when idle -> adb (USB *and* TCP) drops. A wakelock fixes it.
#   * adb is key-authed via /mnt/UDISK/adb_keys (this host's ~/.android/adbkey is trusted).
#
# Usage:
#   ./narwal.sh connect           # USB+TCP, hold wakelock, print status  (do this first)
#   ./narwal.sh status            # device / uptime / wifi IP / cloud reachability
#   ./narwal.sh shell             # stable interactive root shell (adb shell -x)
#   ./narwal.sh pull <remote> [localdir]   # recursive pull (uses TCP)
#   ./narwal.sh push <local> <remote>      # push a file (uses TCP)
#   ./narwal.sh wakelock on|off   # hold / release the no-suspend wakelock
#   ./narwal.sh ip                # print the robot's current wlan0 IP
#   ./narwal.sh exec '<cmd>'      # run one shell command (adb shell -x), retries windows
#   ./narwal.sh map [localdir]    # capture + pull the house map. RUN DURING/AFTER A CLEANING
#                                 # TASK — the map only exists while maphub is actively mapping
#                                 # (autosaves to /tmp, persists to /userdata on dock). Grabs
#                                 # pita_grid_seg.map (rooms) + room-attr.data + map_rotate.png.
#
# Env overrides: NARWAL_USB_SERIAL (default 20080411), WAKELOCK (default narwaldbg)
set -u

USB="${NARWAL_USB_SERIAL:-20080411}"
WL="${WAKELOCK:-narwaldbg}"
IPFILE="$(cd "$(dirname "$0")" && pwd)/.narwal-last-ip"

say(){ printf '%s\n' "$*" >&2; }

usb_up(){ adb devices 2>/dev/null | awk -v s="$USB" '$1==s && $2=="device"{f=1} END{exit !f}'; }

# read wlan0 IP from the robot (over whichever transport answers), cache it
robot_ip(){
  local ip=""
  for d in "$USB" "$(cat "$IPFILE" 2>/dev/null):"; do
    d="${d%:}"; [ -z "$d" ] && continue
    ip=$(adb -s "$d" shell -x 'ip -4 addr show wlan0 2>/dev/null || ifconfig wlan0 2>/dev/null' 2>/dev/null \
         | tr -d '\r' | grep -oE 'inet (addr:)?[0-9.]+' | grep -oE '[0-9.]+' | grep -v '^255' | head -1)
    [ -n "$ip" ] && break
  done
  [ -n "$ip" ] && { echo "$ip" > "$IPFILE"; echo "$ip"; return 0; }
  # last resort: cached
  cat "$IPFILE" 2>/dev/null
}

# connect TCP and return "ip:5555", or empty
tcp_dev(){
  # fast path: a TCP device is already connected -> use it directly. Robust to a
  # stale/empty IP cache and to robot_ip's bare-serial probe missing (the real adb
  # serial is "<ip>:5555", not "<ip>"), which otherwise yields a spurious
  # "no device" even while the robot is plainly connected.
  local cur; cur=$(adb devices 2>/dev/null | awk '$2=="device" && $1 ~ /:5555$/ {print $1; exit}')
  [ -n "$cur" ] && { echo "$cur"; return 0; }
  local ip; ip=$(robot_ip); [ -z "$ip" ] && return 1
  adb connect "$ip:5555" >/dev/null 2>&1
  echo "$ip:5555"
}

# best device for SHELL (USB or TCP), for SYNC (prefer TCP)
shell_dev(){ if usb_up; then echo "$USB"; else tcp_dev; fi; }
sync_dev(){  tcp_dev || { usb_up && echo "$USB"; }; }

# run a shell command, retrying until a wake window lands it (robot may be asleep)
run_retry(){ # $1=device $2=cmd $3=tries
  local dev="$1" cmd="$2" tries="${3:-40}" out
  for _ in $(seq 1 "$tries"); do
    out=$(adb -s "$dev" shell -x "$cmd" 2>/dev/null | tr -d '\r')
    [ -n "$out" ] && { printf '%s\n' "$out"; return 0; }
    adb reconnect >/dev/null 2>&1; sleep 1
  done
  return 1
}

wl_on(){
  local d; d=$(shell_dev) || { say "no device"; return 1; }
  say "holding wakelock '$WL' (wake the robot with an app task if this stalls)..."
  if run_retry "$d" "echo $WL > /sys/power/wake_lock; cat /sys/power/wake_lock" 60 | grep -q "$WL"; then
    say "wakelock held — adb is now stable."
  else say "could not catch a wake window; send a cleaning task from the app and retry."; return 1; fi
}
wl_off(){
  local d; d=$(shell_dev) || { say "no device"; return 1; }
  run_retry "$d" "echo $WL > /sys/power/wake_unlock; echo released" 20 >/dev/null
  say "wakelock '$WL' released — normal idle power restored."
}

cmd="${1:-}"; shift 2>/dev/null || true
case "$cmd" in
  connect)
    adb start-server >/dev/null 2>&1
    usb_up && say "USB: $USB" || say "USB: (not attached)"
    ip=$(robot_ip); [ -n "$ip" ] && say "wifi IP: $ip  -> connecting adb:5555" || say "wifi IP: (unknown — attach USB once so I can read it)"
    [ -n "$ip" ] && adb connect "$ip:5555" >/dev/null 2>&1
    wl_on
    "$0" status
    ;;
  status)
    d=$(shell_dev) || { say "no device reachable"; exit 1; }
    echo "device: $d"
    run_retry "$d" 'echo "uptime: $(cut -d. -f1 /proc/uptime)s"; echo "wifi: $(wpa_cli status 2>/dev/null | grep -E "^ssid|^ip_address" | tr "\n" " ")"; echo "wakelock: $(cat /sys/power/wake_lock)"' 30
    ;;
  ip)    robot_ip ;;
  shell)
    d=$(shell_dev) || { say "no device"; exit 1; }
    wl_on
    say "opening root shell on $d (Ctrl-D to exit) ..."
    exec adb -s "$d" shell -x
    ;;
  exec)
    d=$(shell_dev) || { say "no device"; exit 1; }
    run_retry "$d" "$*" 40 || { say "command never landed (robot asleep?)"; exit 1; }
    ;;
  pull)
    [ $# -ge 1 ] || { say "usage: narwal.sh pull <remote> [localdir]"; exit 2; }
    rem="$1"; loc="${2:-.}"; mkdir -p "$loc"
    d=$(sync_dev) || { say "no sync-capable device (need TCP:5555 up — run connect)"; exit 1; }
    wl_on
    say "pull $rem -> $loc  (via $d)"
    for t in 1 2 3 4 5; do
      adb -s "$d" pull "$rem" "$loc" && { say "done."; break; } || { say "attempt $t dropped, retry"; adb connect "${d}" >/dev/null 2>&1; sleep 1; }
    done
    ;;
  push)
    [ $# -ge 2 ] || { say "usage: narwal.sh push <local> <remote>"; exit 2; }
    d=$(sync_dev) || { say "no sync-capable device"; exit 1; }
    wl_on
    for t in 1 2 3 4 5; do
      adb -s "$d" push "$1" "$2" && { say "done."; break; } || { say "attempt $t dropped, retry"; sleep 1; }
    done
    ;;
  map)
    # The pita stack uses its own framework (NO standard rosmaster), so we can't rosservice-call
    # the save. maphub writes the map itself: autosave -> /tmp/maphub/autosave/ every 600s WHILE
    # mapping, and persists -> /userdata/usr/data/maps/ on dock. The map only exists while maphub
    # is actively mapping, so RUN THIS DURING OR JUST AFTER A CLEANING TASK.
    loc="${1:-./narwal-map-$(date +%Y%m%d-%H%M%S)}"; mkdir -p "$loc"
    d=$(shell_dev) || { say "no device"; exit 1; }
    wl_on
    DEST="/userdata/map-backups/$(date +%s)"
    say "capturing maphub output on robot -> $DEST ..."
    say "(if this comes back empty: the robot isn't mapping — start a cleaning task first.)"
    adb -s "$d" shell -x "mkdir -p $DEST; \
      cp -a /tmp/maphub           $DEST/tmp-maphub     2>/dev/null; \
      cp -a /userdata/usr/data/maps $DEST/persist-maps 2>/dev/null; \
      cp -a /userdata/datas/maphub $DEST/debug-maphub  2>/dev/null; \
      echo '--- captured files ---'; find $DEST -type f 2>/dev/null" 2>&1 | tr -d '\r'
    sd=$(sync_dev) || sd="$d"
    say "pulling $DEST -> $loc (via $sd)"
    adb -s "$sd" pull "$DEST" "$loc" 2>&1 | tail -2
    say "done -> $loc  (rooms=pita_grid_seg.map, attrs=room-attr.data, image=map_rotate.png)"
    ;;
  reboot)
    d=$(shell_dev) || { say "no device"; exit 1; }
    say "rebooting robot ($d) ..."; adb -s "$d" reboot 2>&1 | head -1; say "sent." ;;
  wakelock) case "${1:-}" in on) wl_on;; off) wl_off;; *) say "usage: narwal.sh wakelock on|off";; esac ;;
  ""|-h|--help|help)
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) say "unknown command: $cmd (try: connect status shell pull push wakelock ip exec)"; exit 2 ;;
esac
