# NEXT-STEPS — what must be discovered on the live robot to finish

Phase 1 (this repo) is a runnable skeleton: the LAN server + React UI + HA MQTT
discovery all work **today** against the built-in mock. The on-robot bridge
(`robot/robot_server.py`) is stubbed against the **documented** ROS topics, and
every robot-internal UNKNOWN is marked in the code with `>>> TODO(DISCOVER) <<<`.

Nothing below is a blocker in the "no access / need a secret" sense — we have
root + persistent adb (`../narwal.sh`). They are **discovery + decode** tasks on
the live robot. Do them with the robot awake (hold a wakelock:
`../narwal.sh wakelock on`, or send a cleaning task from the app to wake it).

---

## 0. FIRST: does pita even use a standard ROS master?  ⚠️ framework question
The pita stack is built on a **custom framework ("pita::frame")**, and may have
**no standard roscore / rostopic pub-sub** at all. Everything in sections 1–4
depends on the answer. On the robot:
```
../narwal.sh shell
  echo $ROS_MASTER_URI ; echo $ROS_IP
  rostopic list 2>&1 | head          # does this work at all?
  ps | grep -E 'roscore|rosmaster|pita'
  ls /opt/ros/kinetic ; cat /oem/DISK/bin/pita/script/pita_init/app_start 2>/dev/null
```
- If `rostopic list` works → the documented topics in sections 1/3 may be used
  directly via rospy (as `robot_server.py` currently attempts).
- If it does **not** → the bridge must read the same data through pita::frame
  (REA `pita_driver` / `base_comm` / `pita_clean_system` ELFs to learn how nodes
  talk), or tap the serial/driver layer, or scrape files. `robot_server.py`
  already **degrades to a file-only mode** (map image + rooms from disk, status
  heartbeat) when `rospy.init_node` fails — that path works regardless.

**Record the answer in `../NARWAL-ACCESS.md` — it changes the whole bridge design.**

---

## ✅ DONE (2026-11-02) — real map formats decoded + loaded
The dumped map in `captures/house-map-20261102/` is now fully loaded by
`lan_server/realmap.py` (`--transport real`):
- `maps/map.bag` → `nav_msgs/OccupancyGrid` (`static_map`, 416×429 @ 3 cm) via `rosbags`.
- `maps/pita_grid_seg.map:2` (gzip → Boost text archive) → a 234×210 @ 6 cm **room
  bitmask** grid (bit *i* = room *i*, i∈0..7; bit 8 = background), **resampled by
  world coordinates onto the occupancy grid** (seg is stored top-down). 8 rooms.
- `maps/:3` → room names/ids; `communicator/clean_scheme/info` → sweep/mop order.
- `maps/pita_grid.map.png` → the robot's own rendered preview (map image).
A **map editor** (rename rooms, no-go/virtual-wall rectangles, clean order)
persists to a sidecar `data/maps/<map_id>/edits.json` (`/api/edits`).

### Remaining map items
- **Write edits BACK to the robot (stretch goal).** We persist edits locally only.
  Pushing them to the robot means re-encoding the Boost-archive segmentation
  (`pita_grid_seg.map:2`) and/or the clean-order files
  (`pita_grid_seg.map_clean_order:2`, a Boost archive we've already read) and
  writing them to `/userdata/usr/data/maps/` over adb, then signalling maphub to
  reload — none of which is reverse-engineered yet. No-go zones have no obvious
  native representation in the dumped files; they may be cloud-only, so enforcing
  them might require intercepting the planner (big). Treat as a separate project.
- **Verify the live file path** matches the dump layout (`/userdata/usr/data/maps/`)
  and whether `:2`/`:3` suffixes are versioning (atomic-write temp names).

## ✅ DONE (2026-11-02) — vector map (geometry model for display + robot)
`lan_server/vectorize.py` turns the occupancy grid + resampled room segmentation
into **world-metre vector geometry** (`GET /api/map_vector`): 8 room polygons
(rectilinear-regularized, holes kept) + ~105 wall polylines. The UI renders it as
crisp SVG. This is also the **geometry model the robot/planner should consume**.

### Robot use of the vector geometry — mapping polygon → robot action
The vector payload is in the SAME world frame as the robot's map (derived from
the bag's OccupancyGrid origin/resolution), so it maps directly to robot actions
once the command codes (§3) are known:
- **Room-clean targeting:** `clean_room <id>` → the robot already cleans by room
  id; the polygon for that id is the on-map region to verify/visualize. If we
  later drive nav ourselves, use the polygon centroid (`room.label`) as a goal
  and the polygon as the coverage boundary for a boustrophedon planner.
- **No-go zones / virtual walls (editor rects, world metres):** feed them to the
  planner as forbidden regions / line obstacles. Two routes: (a) if the robot's
  own no-go format is reverse-engineered, re-encode + push (stretch goal, §1);
  (b) in an off-cloud `move_base`-style stack, add them to the costmap as
  inflated obstacles / a "keepout" layer. The wall polylines likewise seed the
  static costmap.
- **Clean order (editor):** an ordered list of room ids → issue `clean_room` per
  room in sequence (or encode into the robot's `clean_order` file once its Boost
  format is written back, §1).
`robot_server.py` can consume the identical polygons: have the LAN server send
the vector (or the robot bridge compute it) so the planner and the UI share one
geometry. No live wiring yet — needs the command codes (§3) and, for pushing
zones onto the robot, the native-format write-back (§1).

## 1. (SUPERSEDED for the dumped map) occupancy-grid MAP — on DISK, not a service
The robot keeps **no map while idle**; maphub writes map files to
`/userdata/usr/data/maps/` **during/after an active cleaning task**:
```
pita_grid_seg.map   segmented occupancy grid (rooms)   <-- format UNKNOWN
room-attr.data      per-room attributes                <-- format UNKNOWN
map_rotate.png      the robot's own rendered map image  <-- already usable (PNG)
```
`robot_server.py` already forwards `map_rotate.png` verbatim (the UI shows it on
the **"Robot map"** tab). What's left is to **decode the grid** so we can serve a
real `nav_msgs/OccupancyGrid` (the "Grid" tab / future move_base costmap):

How to discover:
```
# start a clean so the files appear, then pull them:
../narwal.sh pull /userdata/usr/data/maps ./grab-maps
# inspect:
file grab-maps/*; xxd grab-maps/pita_grid_seg.map | head
xxd grab-maps/room-attr.data | head
```
Then reverse the header/encoding (width/height/resolution/origin + cell bytes,
and the room-id layer). Likely a small binary header + run or raw cells. If it's
opaque, REA-decompile `maphub` / `map_server` (in `oem/DISK_B/bin/pita/`) to find
the writer. Fill in `_poll_map_files()` → emit a `{"type":"map", ...}` message
(see `lan_server/bridge/base.py` for the exact shape the server expects).

**Verify the timing claim:** an earlier idle sweep found `/userdata/usr/data/maps/`
EMPTY. Confirm the files are present *during* a clean and whether they persist
after docking (they must, for a persistent local map).

## 2. Room segmentation  (mostly in hand)
We already read `communicator/clean_scheme/info` (room ids 0–9 + sweep/mop
`scheme_id`s — captured sample confirmed). `robot_server.py` forwards it; the
server parses it (`RoomScheme.from_clean_scheme`). Remaining:
- **Human room names** are NOT in that file (the app names them cloud-side). Find
  where names live (another `communicator/*` JSON, or only in the cloud). Until
  then the UI shows "Room N".
- **Per-cell room geometry** (which cells belong to which room) is in the map
  files from §1 (`pita_grid_seg.map` / `room-attr.data`) — decode to tint rooms
  on the Grid canvas and to validate a `clean_room` command.

## 3. High-level task COMMAND codes  (start / stop / pause / dock / clean-room)
We have the message *definitions* (`RosFuncCommand`, `ControllerCmd`) but **not
the integer `command` values**, nor the exact topic/service `pita_clean_system`
listens on. These are the `CMD_CODES = {... None ...}` in `robot_server.py`.

Two ways to decode (do whichever matches §0):
- **If a ROS master exists:** drive the robot from the phone app and watch:
  ```
  rostopic list | grep -iE 'clean|command|func|controller'
  rostopic echo <that topic>     # press start/pause/dock in the app, read codes
  rosmsg show <pkg>/RosFuncCommand
  ```
- **Otherwise / cross-check:** the **cloud WebSocket** service
  `/pita_clean_system/command` already takes `{cmd:<int>}` for start/stop/pause/
  dock (fully RE'd — see `../../.claude/.../narwal-t10-re.md` and
  `tools/ha-narwal-t10/`). **Those same integers are the most likely on-device
  codes.** Capture them by driving from the app with the WS capture rig already
  built, or REA `pita_clean_system` / `pita_communicator`.

Then set `TOPIC_CLEAN_CMD`, create the publisher in `start()`, and fill
`_dispatch()` (there's a worked comment there). Also confirm how `room_id` is
passed for `clean_room`.

## 4. Status fields  (battery / state / fan / clean area+time)
`robot_server.py` reads `BatteryInfo` and `Odometry` via the documented topics,
but the **exact field names** are guessed (`percentage` vs `battery`, Pose2D
layout). Verify with `rosmsg show pita_msgs/BatteryInfo` etc. (or REA the msg
defs in the firmware). Also find the **cleaning state + fan speed** source — it
may be a pita topic, or derivable from `/pita/info/all` (cloud WS status) /
`pita_clean_system`. Map whatever it is onto the 6 canonical states in
`lan_server/model.py`.

## 5. Python 2 library availability on the robot  (transport choice)
`robot_server.py` prefers **MQTT** (so it joins the homelab broker that HA uses)
and falls back to **stdlib JSON-over-TCP** if paho is absent. Check what Python 2
libs the robot actually has:
```
../narwal.sh shell
  python --version                       # expect 2.7
  python -c 'import paho.mqtt.client; print("paho OK")'   # likely MISSING
  python -c 'import rospy; print("rospy OK")'
  ls /usr/lib/python2.7/site-packages 2>/dev/null; pip2 list 2>/dev/null
```
- If paho is missing (likely) either (a) push a pure-Python paho wheel into
  `/userdata/usr/lib/...` and set `PYTHONPATH`, or (b) **use the TCP fallback**
  (zero third-party deps — already implemented). Set the LAN server to
  `--transport tcp --robot-host <robot-ip>` to match.
- Decide and record which transport is the production one.

## 6. Install the bridge via the de-cloud hook  (deployment)
`app_normal_mode.sh` runs `/oem/DISK/bin/pita/remote_control/robot_server.py`
**instead of** `pita_communicator` **if that file exists**. To go off-cloud:
```
# /oem is normally read-only; boot_app remounts it rw only with an OTA flag.
# Safer first test: run the bridge by hand over adb with the cloud still on,
# pointing the LAN server at it, before committing to the hook.
../narwal.sh push robot/robot_server.py /userdata/robot_server.py
../narwal.sh shell
  NARWAL_TCP_PORT=8765 python /userdata/robot_server.py   # observe
```
Only once it's proven: place it at the hook path (remount `/oem` rw via the OTA
mechanism in `ARCHITECTURE-and-nav-plan.md §6b`) so the cloud bridge never starts.
Keep a copy of the original `app_normal_mode.sh`.

---

### Summary — the precise discoveries still required
| # | Discovery | Where / how | Blocks |
|---|---|---|---|
| 0 | Is there a standard ROS master, or pita::frame only? | `rostopic list`, `ps`, REA | the whole bridge design |
| 1 | `pita_grid_seg.map` / `room-attr.data` binary formats | pull during a clean + reverse / REA `maphub` | the Grid map + costmap |
| 1b| Do the map files persist after docking? | pull after dock | persistent local map |
| 2 | Human room names; per-cell room geometry | find the JSON / decode §1 | room labels + tinting |
| 3 | Integer command codes + the clean-command topic | app-drive + `rostopic echo`, or cloud WS `{cmd:int}`, or REA | start/stop/pause/dock/clean-room |
| 4 | Exact status field names + state/fan source | `rosmsg show` / REA / `/pita/info/all` | accurate status |
| 5 | Python2 lib availability (paho?) on the robot | `python -c import ...` | MQTT vs TCP transport |
| 6 | Remount `/oem` rw + hook placement | OTA flag (see arch doc §6b) | going fully off-cloud |
