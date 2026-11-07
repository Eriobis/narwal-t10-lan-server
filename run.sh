#!/usr/bin/env bash
# One command to bring up the Narwal T10 LAN server + UI against the mock.
#
#   ./run.sh                 # mock robot, web UI on :8099, HA discovery off
#   ./run.sh --ha            # also publish Home Assistant MQTT discovery
#   ./run.sh --transport mqtt --mqtt-host 192.168.5.113   # real robot via MQTT
#
# It builds the React UI first if (a) Node is installed and (b) frontend/dist
# is missing. If Node is absent, the server still runs and serves a no-build
# fallback page (the REST/WS API + mock work regardless).
set -euo pipefail
cd "$(dirname "$0")"

# Prefer the Python 3.11 venv (has the native wheels: rosbags, opencv, shapely).
# The host's 3.14 can't build those, so the vector map + real-map loading only
# work there. Fall back to python3 for the mock-only demo.
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -x ./.venv311/bin/python ]; then PY="./.venv311/bin/python"
else PY="python3"; fi
echo ">> using interpreter: $PY ($($PY --version 2>&1))"

# 1) Python deps
if ! "$PY" -c 'import fastapi, uvicorn' 2>/dev/null; then
  echo ">> installing Python requirements..."
  "$PY" -m pip install -r requirements.txt
fi

# 2) Build the React UI if possible and not already built
if [ ! -d frontend/dist ]; then
  if command -v npm >/dev/null 2>&1; then
    echo ">> building React UI (frontend/)..."
    # npm ci needs a lockfile; fall back to npm install if absent.
    if [ -f frontend/package-lock.json ]; then
      ( cd frontend && npm ci --silent && npm run build )
    else
      ( cd frontend && npm install --silent && npm run build )
    fi
  else
    echo ">> npm not found -- skipping UI build; server will serve the"
    echo "   no-build fallback page. Install Node + run 'cd frontend && npm ci && npm run build'."
  fi
fi

# 3) Run the server (passes through any extra flags)
echo ">> starting server on http://0.0.0.0:8099/"
exec "$PY" -m lan_server "$@"
