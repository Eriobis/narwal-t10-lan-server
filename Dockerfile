# syntax=docker/dockerfile:1
# Narwal T10 LAN server — self-contained image (Python 3.11; the host's 3.14
# can't build rosbags/etc, which is why this exists). No map is baked in: mount
# your own robot map dump at /app/bundled-map (see docker-compose.yml).

# ---- stage 1: build the React UI ----
FROM node:20-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci 2>/dev/null || npm install
COPY frontend/ ./
RUN npm run build          # -> /ui/dist

# ---- stage 2: runtime ----
FROM python:3.11-slim-bookworm
WORKDIR /app

# opencv-python-headless (map vectorizer) needs libGL + glib at runtime on slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY lan_server/ ./lan_server/
RUN mkdir -p /app/bundled-map
COPY --from=ui /ui/dist ./frontend/dist

# Defaults: LAN fake-cloud on all interfaces; state persists in /app/data.
ENV NARWAL_WEB_HOST=0.0.0.0 \
    NARWAL_WEB_PORT=8099 \
    NARWAL_DATA_DIR=/app/data/maps \
    NARWAL_TRANSPORT=cloud \
    NARWAL_MAP_DIR=/app/bundled-map \
    NARWAL_OCC_TOPIC=static_map

EXPOSE 8099 8443
VOLUME ["/app/data"]
CMD ["python", "-m", "lan_server"]
