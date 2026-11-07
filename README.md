# Narwal T10 — LAN control & map server

A local-network control + map server for a **de-clouded Narwal T10** robot
vacuum (the owner's own robot). The robot's own cloud bridge is repointed at this
server, which then acts as "the cloud": it keeps robot state, **persists maps +
room segmentation locally**, exposes a **Home Assistant MQTT-discovery** vacuum
entity, and serves a **phone-first React UI** over a JSON REST + WebSocket API.

What was changed on the robot, and why: **[MODIFICATION.md](MODIFICATION.md)**.

## Install & use

You need Docker on the server machine, and `adb` + `openssl` on the PC that is
plugged into the robot over USB (they can be the same machine).

```bash
git clone <this repo> && cd narwal-t10-lan-server

# 1. Once per robot: get root adb access. USB cable to the robot, run this,
#    power-cycle the robot when asked, then reboot it once more.
./scripts/first-time-setup.sh --ssid 'YourWifi' --psk 'YourPassword'

# 2. Point the robot at the server (defaults to this PC's IP; or give the server's IP)
./scripts/set-server-ip.sh 192.168.1.50

# 3. Start the server
docker compose up -d --build
```

Open **http://&lt;server-ip&gt;:8099/** on your phone.

| To… | Run |
|---|---|
| move the server to a new IP | `./scripts/set-server-ip.sh <new-ip>` then `docker compose restart` |
| upload the on-robot code (`robot/robot_server.py`) | `./scripts/upload-to-robot.sh` |
| put the robot back on the Narwal cloud | `./scripts/restore-cloud.sh` |
| open a root shell / find the robot's IP | `./scripts/narwal.sh shell` / `./scripts/narwal.sh ip` |
| try the UI with no robot | `./run.sh` (mock robot) |

Optional: copy a dump of the robot's `/userdata/usr/data` (its `maps/` and
`communicator/` folders) into `bundled-map/`, or set `NARWAL_MAP_PATH=/path/to/dump`.
It is shown before the robot first reports in. Maps and `data/` are git-ignored, so your
house layout never ends up in the repo.

> The remaining sections are the detailed reference.

---

## Architecture (in words)

```
                         ROBOT (Allwinner R16, Tina Linux, Python 2.7, pita::frame)
                         ┌───────────────────────────────────────────────┐
                         │ robot_server.py  (the de-cloud hook target)     │
   ROS topics / files ──▶│  • reads status/odom/battery (documented topics)│
   /userdata/.../maps/   │  • reads map files (map_rotate.png, grid_seg)   │
                         │  • issues start/stop/pause/dock (TODO codes)    │
                         └───────────────┬─────────────────────────────────┘
                                         │  JSON over MQTT (preferred)
                                         │  or stdlib JSON-over-TCP (fallback)
                                         ▼
  LAN SERVER (this package, Python 3)  ┌───────────────────────────────┐
     bridge/ (mock | mqtt | tcp) ─────▶│ NarwalServer                  │
                                       │  • RobotState (in memory)     │
                                       │  • MapStore → disk (PGM+YAML, │
                                       │    grid.json, rooms.json, PNG)│
                                       │  • pub/sub for live push      │
                                       └───┬───────────────┬───────────┘
                                           │               │
                   Home Assistant  ◀───────┘               └──────▶  React UI
                   (MQTT discovery:                        (FastAPI REST + WS;
                    vacuum + sensors)                       phone-first canvas map)
```

- **One transport, two wire formats.** The bridge and server exchange the same
  JSON payloads over either MQTT (joins the homelab broker HA already uses) or a
  plain stdlib TCP socket (for a dependency-starved Python 2 robot). Protocol is
  defined once in [`lan_server/bridge/base.py`](lan_server/bridge/base.py).
- **Maps persist locally** in the **ROS map_server standard format** (`map.pgm` +
  `map.yaml`) plus a lossless `grid.json`, the room scheme `rooms.json`, and the
  robot's own `map_rotate.png` — under `data/maps/<map_id>/`. This is exactly
  what a future `move_base` / `map_server` nav stack will load.
- **HA discovery is independent of the UI** — it publishes a `vacuum` entity
  (start/stop/pause/return/locate, battery, fan speed, `send_command` for room
  cleaning) plus a few sensors, and relays HA's commands back to the robot.

---

## Use the REAL dumped map (works today)

Load a map dumped from the robot (`./scripts/narwal.sh map <dir>`) instead of the mock:

```bash
./run.sh --transport real --map-dir ./bundled-map
# open http://<host>:8099/
```

> **Runtime note:** real-map loading + vectorization need native wheels
> (`rosbags`, `opencv-python-headless`, `shapely`, `numpy`) that the host's
> Python **3.14 can't build**. Use the **Python 3.11** path: the bundled
> `Dockerfile`/`docker-compose.yml` (`docker compose up`; mounts `./bundled-map`
> or `$NARWAL_MAP_PATH`), or the local `./.venv311` (`run.sh` auto-prefers it). The 3.14
> `.venv` only runs the mock.

What it loads (all parsed with no ROS install — `rosbags` + small parsers):
- **Occupancy grid** from `maps/map.bag` (`static_map`, a `nav_msgs/OccupancyGrid`,
  416×429 @ 3 cm) → `/api/map`, rendered on the **Map** tab.
- **Room segmentation** from `maps/pita_grid_seg.map:2` (gzipped Boost archive; a
  234×210 room **bitmask** grid, bit *i* = room *i*), **resampled by world
  coordinates onto the occupancy grid** so it overlays 1:1 → the colored room
  overlay. 8 rooms (IDs 0–7).
- **Room names** from `maps/:3`, **clean order** + scheme IDs from
  `communicator/clean_scheme/info` → `/api/rooms`.
- The robot's **own rendered map** (`pita_grid.map.png`) → the **Robot map** tab.

The **mock stays the default** (`--transport mock`) and the fallback when no map
is configured. The UI and HA are identical for mock vs. real — same wire shape.

> Note: a loaded dump is static (no live robot). Status shows `docked`; commands
> are acknowledged but not sent (the ack says so).

## Vector map (crisp, non-pixelated)

The raw raster (room blobs + speckled wall pixels) is **vectorized** into clean
geometry in world metres and served at **`GET /api/map_vector`**:

- **Room polygons** — per-room mask → `cv2.findContours` (with hole hierarchy) →
  shapely Douglas–Peucker simplify → **orthogonal regularization** (near-axis
  edges snapped to H/V by iterative relaxation + grid-snap, so rooms read as
  clean rectilinear shapes, not pixel staircases) → holes kept as interiors.
- **Walls** — occupied cells → speckle removal → contours → simplify +
  orthogonalize → closed **polylines**.
- Payload: `{rooms:[{id,name,color,area_m2,label,polygons:[{exterior,holes}],
  polygon,holes}], walls:[[[x,y]…]], bounds, resolution, origin, counts}`.
  Built in `lan_server/vectorize.py` from the loaded real map and **cached**
  (invalidated when the map changes).

The React UI renders this as **SVG** (sharp at any zoom): filled room polygons
with labels, thin wall strokes (`vector-effect:non-scaling-stroke`), the robot
pose, and the editor's no-go/virtual-wall rects on top. The vector view is the
**default**; a **"raster underlay"** toggle overlays the original grid to verify
alignment. If the vector deps aren't present (e.g. the 3.14 host), the endpoint
returns 503 and the UI falls back to the raster canvas.

> These same room polygons + wall polylines are the **geometry model** for
> room-clean targeting / no-go zones / future off-cloud nav — see NEXT-STEPS.md.

## Map editor

The **Edit** tab (mobile-first, same canvas) lets you:
- **Rename rooms** (text per room),
- **Draw no-go zones / virtual walls** — pick the tool, then drag a rectangle on
  the map (stored in world metres, so they overlay at any resolution),
- **Set the per-room clean order** (▲/▼ reorder).

Edits persist to a **server-side sidecar JSON** (`data/maps/<map_id>/edits.json`)
via `GET`/`PUT /api/edits` and are layered over the map. They are **not** written
back into the robot's binary map format — that's a stretch goal (see NEXT-STEPS).

## Quick start (mock — works today)

```bash
./run.sh                       # installs deps, builds the React UI, serves it
# open http://<this-host>:8099/
```

`run.sh` creates/uses a Python venv-free install (it just `pip install`s the
requirements), builds `frontend/` if Node is present, and starts the server. If
Node is **not** installed it skips the UI build and serves a no-build fallback
page (the REST/WS API + mock still work).

### Manual / dev

```bash
# backend
python3 -m pip install -r requirements.txt
python3 -m lan_server                       # mock, web UI on :8099

# frontend in dev mode (hot reload, proxies /api + /ws to :8099)
cd frontend && npm install && npm run dev    # http://localhost:5173

# or build once and let FastAPI serve it:
cd frontend && npm run build                 # -> frontend/dist
python3 -m lan_server
```

### Useful flags

```
--transport mock|real|cloud|mqtt|tcp   how to reach the robot (default: mock)
--web-host / --web-port       default 0.0.0.0:8099
--data-dir DIR                where maps persist (default ./data/maps)
--ha                          enable Home Assistant MQTT discovery
--mqtt-host/--mqtt-port       broker for HA + the mqtt transport
--robot-host/--robot-port     robot endpoint for --transport tcp
```
All flags also have `NARWAL_*` env equivalents (see `lan_server/config.py`).

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | current robot state (+ transport / HA status) |
| GET | `/api/map` | latest occupancy grid (raw `-1/0/100` + metadata) |
| GET | `/api/rooms` | room segmentation scheme |
| GET | `/api/maps` | persisted maps on disk |
| GET | `/api/map_image` | robot-rendered map (`map_rotate.png`), `image/png` |
| GET | `/api/map_vector` | vectorized map: room polygons + wall polylines (world m) |
| GET | `/api/edits` | editor sidecar (room names, zones, clean order) |
| PUT | `/api/edits` | save editor sidecar |
| POST | `/api/command` | `{"cmd":"start","args":{}}` → robot |
| WS | `/ws` | live push: `{type:status\|map\|rooms\|map_image, data}`; also accepts `{cmd,args}` |

Commands: `start`, `stop`, `pause`, `resume`, `dock`, `locate`,
`clean_room {room_id}`, `set_fan_speed {fan_speed}`.

---

## Home Assistant setup

1. Have an MQTT broker reachable (the homelab's Mosquitto; HA's MQTT integration
   with **discovery enabled** — the default).
2. Run the server pointed at the broker:
   ```bash
   python3 -m lan_server --ha --mqtt-host 192.168.5.113 \
       [--mqtt-user U --mqtt-pass P]
   ```
   (Use the same broker in `--transport mqtt` when you later connect the real
   robot.)
3. A **`vacuum.narwal_t10`** entity appears automatically under the device
   *Narwal T10*, plus sensors (clean area, clean time, status). Start/Stop/
   Pause/Return-to-dock/Locate and fan-speed work from the HA vacuum card.
4. **Room cleaning from HA:** call `vacuum.send_command` with
   `command: "clean_room 3"` (or a JSON payload `{"cmd":"clean_room","args":
   {"room_id":3}}`). The discovery topic is `homeassistant/vacuum/narwal_t10/config`.

> Discovery is published **retained**, so HA re-discovers the entity on restart.
> The entity goes `unavailable` when the bridge/robot drops (LWT + availability
> topic).

---

## Local ROS mode (experimental, instead of the fake cloud)

1. Work through **[NEXT-STEPS.md](NEXT-STEPS.md)** on the live robot (map format,
   command integer codes, status fields, Python 2 lib availability, ROS-master
   question). Fill the `>>> TODO(DISCOVER) <<<` slots in `robot/robot_server.py`.
2. Upload `robot/robot_server.py` into the `app_normal_mode.sh` de-cloud hook with
   `./scripts/upload-to-robot.sh` (reboots the robot into local ROS mode).
3. Point the LAN server at it:
   - **MQTT:** `python3 -m lan_server --transport mqtt --mqtt-host <broker> --ha`
     (and set `NARWAL_MQTT_HOST` on the robot so the bridge publishes there).
   - **TCP fallback:** `python3 -m lan_server --transport tcp --robot-host <robot-ip>`
     (the bridge runs the TCP server; `NARWAL_TCP_PORT` defaults to 8765).

The mock and the real robot are interchangeable from the server's point of view —
same wire protocol, same UI, same HA entity.

---

## Project layout

```
lan-server/
├── run.sh                      one-command: deps + UI build + serve (no Docker)
├── Dockerfile  docker-compose.yml
├── MODIFICATION.md             what was changed on the robot + how to undo it
├── scripts/                    robot-side tooling (needs adb + USB/LAN access)
│   ├── first-time-setup.sh     once: root adb on the robot (installs rc.local)
│   ├── rc.local                the boot script it installs
│   ├── set-server-ip.sh        point the robot at the server IP (+ TLS cert)
│   ├── upload-to-robot.sh      upload robot/robot_server.py to the robot
│   ├── restore-cloud.sh        undo: back to the Narwal cloud
│   └── narwal.sh               adb helper: connect/shell/push/pull/map/ip
├── requirements.txt            exact-pinned Python deps (fastapi/uvicorn/paho)
├── README.md  NEXT-STEPS.md
├── lan_server/                 Python 3 LAN server
│   ├── __main__.py             entry (uvicorn)
│   ├── config.py               CLI flags + env
│   ├── model.py                RobotState / OccupancyGrid / RoomScheme
│   ├── server.py               orchestrator + live pub/sub
│   ├── api.py                  FastAPI REST + WebSocket + static SPA serving
│   ├── mapstore.py             disk persistence (ROS PGM+YAML, json, png)
│   ├── ha_discovery.py         Home Assistant MQTT discovery
│   ├── pngutil.py              stdlib PNG encoder (mock's map_rotate.png)
│   ├── realmap.py              REAL dumped-map loader (bag + seg + names)
│   ├── vectorize.py            raster -> room polygons + wall polylines (world m)
│   ├── edits.py                editor sidecar store (names/zones/order)
│   └── bridge/
│       ├── base.py             Bridge interface + WIRE PROTOCOL spec
│       ├── mock.py             simulated robot (demo without hardware)
│       ├── cloud.py            real robot via the LAN fake-cloud (default)
│       ├── mqtt_bridge.py      real robot via MQTT (paho 2.x)
│       └── tcp_bridge.py       real robot via stdlib JSON-over-TCP
├── frontend/                   phone-first React + Vite UI (own pinned deps)
│   ├── index.html  vite.config.js  package.json
│   └── src/ (App.jsx, MapCanvas.jsx, VectorMap.jsx, MapEditor.jsx, api.js, styles.css, main.jsx)
├── robot/
│   └── robot_server.py         ON-ROBOT bridge (Python 2/rospy) — skeleton
├── bundled-map/                your robot map dump (optional, git-ignored)
└── data/maps/                  persisted maps, TLS cert, logs (runtime, git-ignored)
```

## Design notes
- **No numpy / Pillow / PyYAML.** Grids are plain Python lists, the UI renders
  the map client-side on a `<canvas>`, maps persist as PGM + hand-written YAML,
  and the mock's PNG comes from a tiny stdlib-`zlib` encoder.
- **Map canvas is devicePixelRatio-aware**: the grid is painted 1:1 offscreen
  then blitted with smoothing off, so it's crisp on retina phones at any size.
- **Backup/consistency** of the persisted maps is out of scope here; put
  `data/maps/` on a dataset with snapshots if you want history.
