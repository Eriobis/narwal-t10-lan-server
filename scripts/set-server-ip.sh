#!/usr/bin/env bash
# set-server-ip.sh — point the robot at the LAN server running on <ip>.
#
#   ./scripts/set-server-ip.sh                  # use this machine's LAN IP
#   ./scripts/set-server-ip.sh 192.168.5.113    # server runs on another host
#   ./scripts/set-server-ip.sh 192.168.5.113 8099
#
# Run it again whenever the server moves to a new IP. It:
#   1. (re)generates the server's TLS cert for <ip> in data/maps/tls/ if needed
#      — the robot uploads logs over TLS and checks the cert's IP;
#   2. installs that cert as trusted on the robot;
#   3. rewrites the robot's cloud endpoints (websocket + http) to <ip>:<port>;
#   4. re-points the robot's queue of failed HTTP calls — pita_communicator stores
#      them with the absolute URL and retries them forever, so without this it
#      keeps knocking on the old server;
#   5. restarts the robot's cloud bridge so it connects to the LAN server.
# The originals are saved once as *.narwal-orig; restore with ./scripts/restore-cloud.sh.
#
# Afterwards restart the LAN server so it loads the new cert (docker compose restart).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"; N="$HERE/narwal.sh"

IP="${1:-$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' || true)}"
PORT="${2:-8099}"
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 <server-ip> [port]"; exit 1; }

TLS="$ROOT/data/maps/tls"; CERT="$TLS/cert.pem"; KEY="$TLS/key.pem"
CONF=/oem/DISK/bin/pita/pita_communicator/conf
WS="ws://$IP:$PORT/cloud/robot_websocket"; HTTP="http://$IP:$PORT/cloud"
echo ">>> robot -> LAN server at $IP:$PORT"

# 1) server cert for this IP (the robot's TLS client matches the IP against CN/SAN)
if ! openssl x509 -in "$CERT" -noout -checkip "$IP" 2>/dev/null | grep -q 'does match'; then
  echo "1) generating TLS cert for $IP in $TLS"
  mkdir -p "$TLS"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout "$KEY" -out "$CERT" \
    -subj "/CN=$IP" -addext "subjectAltName=IP:$IP,DNS:$IP" 2>/dev/null
else
  echo "1) TLS cert already valid for $IP"
fi
HASH=$(openssl x509 -hash -noout -in "$CERT")

# 2) trust it on the robot
"$N" connect >/dev/null || { echo "   cannot reach the robot over adb (see above). Robot IP: NARWAL_IP=<ip> $0 $*"; exit 1; }
echo "2) installing cert on the robot"
"$N" push "$CERT" /etc/ssl/certs/narwal-lan-cloud.crt >/dev/null
out=$("$N" exec "cd /etc/ssl/certs && for l in *.0; do [ \"\$(readlink \$l)\" = narwal-lan-cloud.crt ] && rm -f \$l; done; ln -sf narwal-lan-cloud.crt $HASH.0 && sync && echo CA_OK" || true); grep -q CA_OK <<<"$out" || { echo "   failed — check ./scripts/narwal.sh status"; exit 1; }

# 3+4) repoint the cloud endpoints and restart the bridge
echo "3) rewriting cloud endpoints + restarting pita_communicator"
out=$("$N" exec "mount -o remount,rw /oem
cd $CONF || exit 1
for f in websocket_env_us.json http_env_us.json; do [ -e \$f.narwal-orig ] || cp \$f \$f.narwal-orig; done
cat > websocket_env_us.json <<EOF
{\"env\":\"prod\",\"config\":{\"prod\":{\"endpoint\":\"$WS\",\"decrypt_incoming_messages\":false,\"encrypt_output_messages\":false},
 \"test\":{\"endpoint\":\"$WS\",\"decrypt_incoming_messages\":false,\"encrypt_output_messages\":false}}}
EOF
cat > http_env_us.json <<EOF
{\"env\":\"prod\",\"config\":{\"prod\":{\"http\":{\"base_url\":\"$HTTP\"},\"upload_file\":{\"server\":\"amazon\"}},
 \"test\":{\"http\":{\"base_url\":\"$HTTP\"},\"upload_file\":{\"server\":\"amazon\"}}}}
EOF
sed -i 's#\"uri\":\"http://[^\"]*/cloud/#\"uri\":\"$HTTP/#' /userdata/usr/data/communicator/failed_apis/* 2>/dev/null
sync; /oem/DISK/bin/pita/script/pita_init/pita_communicator_service restart >/dev/null 2>&1; echo LAN_CLOUD_ON" || true); grep -q LAN_CLOUD_ON <<<"$out" || { echo "   failed — check ./scripts/narwal.sh status"; exit 1; }

echo ">>> done. Restart the server so it serves the cert:  docker compose restart"
echo "    The Narwal phone app no longer reaches the robot. Undo: ./scripts/restore-cloud.sh"
