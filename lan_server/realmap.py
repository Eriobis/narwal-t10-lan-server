"""
Real-robot map loader for the Narwal T10.

Loads a map dumped from the robot and exposes it through the SAME wire shape the
mock uses, so the UI / HA / server need no changes. Sources (all verified on the
2026-11-02 dump in captures/house-map-20261102/maps/):

  map.bag              ROS1 bag. Several nav_msgs/OccupancyGrid topics @ 3cm
                       (static_map = the finished map). Read with `rosbags`
                       (pure Python, no ROS install). Also robot_pose (Pose2D).
  pita_grid_seg.map:2  gzip -> Boost text-serialization archive. A 234x210 @ 6cm
                       grid whose cell value is a ROOM BITMASK (bit i = room i,
                       i in 0..7; bit 8 = background/boundary). We resample it
                       onto the occupancy grid's cells via world coordinates
                       (row 0 = min-y, same as ROS -- verified against the robot's
                       live pose; an earlier top-down assumption mirrored the rooms).
  :3                   gzip -> Boost archive wrapping JSON: room names + ids.
  communicator/clean_scheme/info   sweep/mop room-id order + scheme ids.

Everything degrades gracefully: a missing file just drops that layer.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .bridge.base import Bridge

log = logging.getLogger("narwal.realmap")

# Occupancy topic in the bag to use as THE map (configurable).
DEFAULT_OCC_TOPIC = os.environ.get("NARWAL_OCC_TOPIC", "static_map")

# Room bitmask (verified 2026-11-03 against the robot's live pose while it
# cleaned room 6): bit 0 (value 1) = OUTSIDE / unassigned (one component spanning
# the grid edge to edge), bit k (k=1..8) = room id k-1. Value 0 = wall/unknown.
# Cells on a room boundary carry two room bits; we take the lowest.
_MAX_ROOMS = 8


def decode_room_bitmask(v: int) -> int:
    """Lowest room bit (1..8) set -> room id (0..7); outside/wall -> -1."""
    if v <= 1:
        return -1
    for b in range(1, _MAX_ROOMS + 1):
        if v & (1 << b):
            return b - 1
    return -1


# --------------------------------------------------------------------------- #
# Low-level parsers
# --------------------------------------------------------------------------- #
def load_occupancy_from_bag(bag_path: str, topic: str = DEFAULT_OCC_TOPIC) -> Dict[str, Any]:
    """Return {width,height,resolution,origin{x,y,theta},data[-1/0..100]} + robot pose."""
    from rosbags.rosbag1 import Reader
    from rosbags.typesys import Stores, get_typestore
    ts = get_typestore(Stores.ROS1_NOETIC)

    occ = None
    robot_pose = None
    with Reader(bag_path) as reader:
        # Only touch topics we understand (the bag also has a custom
        # pita_msgs/PointCloud2f the typestore can't deserialize).
        want = {topic, "robot_pose", "base_pose"}
        conns = [c for c in reader.connections if c.topic in want]
        for conn, _t, raw in reader.messages(connections=conns):
            msg = ts.deserialize_ros1(raw, conn.msgtype)
            if conn.topic == topic:
                info = msg.info
                occ = {
                    "width": int(info.width),
                    "height": int(info.height),
                    "resolution": float(info.resolution),
                    "origin": {
                        "x": float(info.origin.position.x),
                        "y": float(info.origin.position.y),
                        "theta": 0.0,
                    },
                    "data": [int(v) for v in msg.data],
                }
            elif conn.topic in ("robot_pose", "base_pose") and robot_pose is None:
                robot_pose = {"x": float(msg.x), "y": float(msg.y),
                              "theta": float(getattr(msg, "theta", 0.0))}
    if occ is None:
        raise ValueError("occupancy topic %r not found in %s" % (topic, bag_path))
    occ["robot_pose"] = robot_pose
    return occ


def load_rotated_grid(path: str) -> Dict[str, Any]:
    """
    Parse maps/pita_grid.map: the robot's OWN occupancy grid (0.03 m) in the
    ROTATED map frame -- the same frame as pita_grid_seg.map (rooms), the robot's
    live /pita/map pose and its map_rotate.png. (map.bag's static_map is the raw
    SLAM frame, rotated by maps/pita_grid.maprotate_pose.pose = 0.0554 rad and
    with a different origin: drawing it under the rooms put them visibly off.)
    Cell value bits: 0 = unknown, bit0 = seen/free, bit4 = obstacle (wall),
    bits 5/9 = special areas. Returns the same dict shape as the bag loader.
    """
    toks = _read_boost_tokens(path)
    H = W = None; res = None
    for i in range(3, 40):
        a, b, c = toks[i], toks[i + 1], toks[i + 2]
        if a.isdigit() and b.isdigit() and int(a) > 10 and int(b) > 10 and ("e" in c or "." in c):
            H, W, res = int(a), int(b), float(c); break
    if not H:
        raise ValueError("no grid header in %s" % path)
    n = H * W
    ci = toks.index(str(n))
    cells = toks[ci + 5: ci + 5 + n]          # 4 Boost object-header tokens after the count
    ox, oy = _find_origin(toks) or (0.0, 0.0)
    data = []
    for x in cells:
        v = int(x)
        data.append(100 if v & 16 else (0 if v & 1 else -1))
    return {"width": W, "height": H, "resolution": res,
            "origin": {"x": ox, "y": oy, "theta": 0.0}, "data": data, "robot_pose": None}


def _read_boost_tokens(path: str) -> List[str]:
    """gunzip a file and split the Boost text archive into whitespace tokens."""
    with gzip.open(path, "rb") as f:
        return f.read().decode("latin1").split()


def load_segmentation(seg_path: str) -> Dict[str, Any]:
    """
    Parse pita_grid_seg.map:2 -> {width,height,resolution,origin,bitmask[cells]}.
    The grid is stored row-major, row 0 = world min-y (ROS order, NOT image order).
    """
    toks = _read_boost_tokens(seg_path)
    # The archive repeats "<H> <W> <res> ... <ox> <oy> ...". We locate the cell
    # array by its length token == H*W. Dims appear as two adjacent ints right
    # before the resolution float; grab the first such pair.
    H = W = None
    res = ox = oy = None
    for i in range(3, len(toks) - 3):
        a, b, c = toks[i], toks[i + 1], toks[i + 2]
        if a.isdigit() and b.isdigit() and ("e" in c or "." in c):
            H, W = int(a), int(b)
            res = float(c)
            break
    if not (H and W):
        raise ValueError("could not find seg dims in %s" % seg_path)
    cells_len = H * W
    # origin: the archive carries it; find the cell-count token then read cells.
    try:
        ci = toks.index(str(cells_len))
    except ValueError:
        raise ValueError("seg cell-count token %d not found" % cells_len)
    # Layer layout (verified 2026-11-03 against the robot's own cleaned-area layer
    # and its live trail): "0 0 H W res ox oy angle bx by ba sx sy sa 0 0 <count>
    # <class_id> <tracking> <version> <item_version> <count cells...> <flag>".
    # The 4 Boost object-header tokens after the count are NOT cells; reading them
    # as cells shifted every row 4 cells (24 cm) sideways.
    _BOOST_VEC_HEADER = 4
    start = ci + 1 + _BOOST_VEC_HEADER
    bitmask = [int(x) for x in toks[start: start + cells_len]]
    # origin floats: the two negative-looking floats near the dims (best effort)
    origin = _find_origin(toks)
    if origin:
        ox, oy = origin
    return {
        "width": W, "height": H, "resolution": res or 0.06,
        "origin": {"x": ox if ox is not None else 0.0,
                   "y": oy if oy is not None else 0.0, "theta": 0.0},
        "bitmask": bitmask,
    }


def _find_origin(toks: List[str]) -> Optional[Tuple[float, float]]:
    """Heuristic: the first pair of same-valued negative floats = map origin."""
    floats = []
    for t in toks[:60]:
        try:
            if "e" in t or "." in t:
                floats.append(float(t))
        except ValueError:
            pass
    negs = [f for f in floats if f < 0]
    if len(negs) >= 2:
        return negs[0], negs[1]
    return None


def load_room_names(names_path: str) -> Dict[int, Dict[str, Any]]:
    """Parse ':3' -> {room_id: {"name":str, "type":int}}."""
    try:
        toks_text = gzip.open(names_path, "rb").read().decode("latin1")
    except OSError:
        return {}
    lb, rb = toks_text.find("["), toks_text.rfind("]")
    if lb < 0 or rb < 0:
        return {}
    try:
        arr = json.loads(toks_text[lb:rb + 1])
    except ValueError:
        return {}
    out = {}
    for e in arr:
        try:
            out[int(e["room_id"])] = {"name": str(e.get("name", "")),
                                      "type": int(e.get("type", 0))}
        except (KeyError, ValueError, TypeError):
            continue
    return out


def load_clean_scheme(info_path: str) -> Dict[str, Any]:
    try:
        with open(info_path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Resample segmentation onto the occupancy grid
# --------------------------------------------------------------------------- #
def resample_rooms_to_occ(seg: Dict[str, Any], occ: Dict[str, Any]) -> List[int]:
    """
    Produce a per-occupancy-cell room id array (same length/order as occ.data,
    ROS row-major y-up), using world coordinates. seg is stored y-up too.
    """
    occW, occH, occR = occ["width"], occ["height"], occ["resolution"]
    occOx, occOy = occ["origin"]["x"], occ["origin"]["y"]
    segW, segH, segR = seg["width"], seg["height"], seg["resolution"]
    segOx, segOy = seg["origin"]["x"], seg["origin"]["y"]
    bm = seg["bitmask"]

    room_grid = [-1] * (occW * occH)
    for y in range(occH):
        wy = occOy + (y + 0.5) * occR
        srow = int((wy - segOy) / segR)        # seg stored y-up (row 0 = min-y)
        if srow < 0 or srow >= segH:
            continue
        base = srow * segW
        row_off = y * occW
        for x in range(occW):
            wx = occOx + (x + 0.5) * occR
            scol = int((wx - segOx) / segR)
            if 0 <= scol < segW:
                room_grid[row_off + x] = decode_room_bitmask(bm[base + scol])
    return room_grid


# --------------------------------------------------------------------------- #
# Assemble the full map payload
# --------------------------------------------------------------------------- #
def load_real_map(map_dir: str, occ_topic: str = DEFAULT_OCC_TOPIC) -> Dict[str, Any]:
    """
    Load everything and return {map_id, occ, rooms_scheme, map_image_path, pose}.
    'occ' and 'rooms_scheme' already match the server's expected wire shapes.
    """
    maps = os.path.join(map_dir, "maps")
    comm = os.path.join(map_dir, "communicator")
    # NARWAL_MAP_ID pins the id so every run mode (container, venv, tcp) shares
    # one edits.json / history (otherwise it follows the folder name).
    map_id = (os.environ.get("NARWAL_MAP_ID")
              or os.path.basename(os.path.normpath(map_dir)) or "real-map")

    # walls: the robot's rotated grid (same frame as the rooms + live pose);
    # map.bag's raw SLAM grid only as a fallback
    rot = os.path.join(maps, "pita_grid.map")
    if os.path.exists(rot):
        occ = load_rotated_grid(rot)
        try:   # keep the bag's robot pose if any
            occ["robot_pose"] = load_occupancy_from_bag(os.path.join(maps, "map.bag"), occ_topic).get("robot_pose")
        except Exception:                            # noqa: BLE001
            pass
    else:
        occ = load_occupancy_from_bag(os.path.join(maps, "map.bag"), occ_topic)

    # Room overlay (best-effort; keep going if seg missing)
    room_grid = []
    seg = None
    # the robot bumps the version suffix when it re-segments (:2 -> :6 ...): use newest
    import glob
    cands = [(int(p.rsplit(":", 1)[1]), p) for p in glob.glob(os.path.join(maps, "pita_grid_seg.map:*"))
             if p.rsplit(":", 1)[1].isdigit()]
    seg_path = max(cands)[1] if cands else os.path.join(maps, "pita_grid_seg.map:2")
    if os.path.exists(seg_path):
        try:
            seg = load_segmentation(seg_path)
            room_grid = resample_rooms_to_occ(seg, occ)
        except Exception as e:                       # noqa: BLE001
            log.warning("segmentation parse failed: %s", e)

    names = load_room_names(os.path.join(maps, ":3"))
    scheme = load_clean_scheme(os.path.join(comm, "clean_scheme", "info"))

    # Build the rooms "scheme" dict the server/RoomScheme understands, enriched.
    present_ids = sorted(set(r for r in room_grid if r >= 0)) or sorted(names)
    sweep = (scheme.get("sweep_scheme") or [{}])[0].get("room_id", [])
    mop = (scheme.get("mop_scheme") or [{}])[0].get("room_id", [])
    rooms_scheme = dict(scheme)  # verbatim mop/sweep
    rooms_scheme["names"] = {str(k): v["name"] for k, v in names.items()}
    rooms_scheme["room_types"] = {str(k): v["type"] for k, v in names.items()}
    rooms_scheme["present_room_ids"] = present_ids
    rooms_scheme["clean_order"] = {
        "sweep": list(dict.fromkeys(sweep)),        # dedup, keep order
        "mop": list(dict.fromkeys(mop)),
    }
    if room_grid:
        rooms_scheme["segmentation"] = {
            "width": occ["width"], "height": occ["height"],
            "resolution": occ["resolution"], "origin": occ["origin"],
            "data": room_grid,                      # aligned 1:1 to occ.data
        }

    # Robot pose: prefer the bag's robot_pose; fall back to base_pose.pose file.
    pose = occ.get("robot_pose")
    if not pose or (pose["x"] == 0.0 and pose["y"] == 0.0):
        bp = os.path.join(maps, "base_pose.pose")
        if os.path.exists(bp):
            try:
                with open(bp) as f:
                    parts = f.read().split()
                pose = {"x": float(parts[0]), "y": float(parts[1]),
                        "theta": float(parts[2]) if len(parts) > 2 else 0.0}
            except (OSError, ValueError, IndexError):
                pass

    # Robot's own rendered map preview (full-res) as the map_image.
    img = None
    for cand in ("pita_grid.map.png", "pita_grid_seg.map.png"):
        p = os.path.join(maps, cand)
        if os.path.exists(p):
            img = p
            break

    return {
        "map_id": map_id,
        "occ": occ,
        "rooms_scheme": rooms_scheme,
        "map_image_path": img,
        "pose": pose,
    }


# --------------------------------------------------------------------------- #
# Bridge that serves a loaded real map (no live robot)
# --------------------------------------------------------------------------- #
class RealMapBridge(Bridge):
    """
    Serves a map dumped from the robot via the normal Bridge interface. There is
    no live robot connection. By default (NARWAL_SIMULATE=1) clean commands run a
    SIMULATED session over the real room cells -- pose moves through the chosen
    rooms, area/time accrue, then it returns to the dock -- so the whole UI
    (controls, history) can be exercised before going live. With
    NARWAL_SIMULATE=0 commands are only acked ("no live robot").
    """

    _TICK = 1.0
    _TICKS_PER_ROOM = 8

    def __init__(self, map_dir: str, occ_topic: str = DEFAULT_OCC_TOPIC) -> None:
        super().__init__()
        self._dir = os.path.expanduser(map_dir)
        self._occ_topic = occ_topic
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pose = {"x": 0.0, "y": 0.0, "theta": 0.0}
        self._dock = dict(self._pose)
        self._simulate = os.environ.get("NARWAL_SIMULATE", "1") not in ("0", "false", "")
        self._room_cells: Dict[int, List[Tuple[float, float]]] = {}
        # simulation state
        self._state = "docked"
        self._mode = "vacuum"
        self._battery = 100.0
        self._fan = "standard"
        self._area = 0.0
        self._time = 0
        self._queue: List[int] = []
        self._target: Optional[int] = None
        self._ticks = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="real-map", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    # --- commands ----------------------------------------------------------
    def send_command(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
        args = args or {}
        cid = uuid.uuid4().hex
        if not self._simulate:
            self._dispatch({"type": "ack", "id": cid, "ok": False,
                            "detail": "static map loaded from dump; no live robot "
                                      "(command '%s' not sent)" % cmd})
            return cid
        ok, detail = True, "simulated"
        with self._lock:
            if cmd in ("start", "clean", "clean_room"):
                if cmd == "clean_room":
                    rooms = [int(args.get("room_id", 0))]
                elif cmd == "clean":
                    rooms = [int(r) for r in (args.get("rooms") or [])]
                else:
                    rooms = []
                if not rooms:                       # whole house = every room
                    rooms = sorted(self._room_cells)
                self._mode = args.get("mode") if args.get("mode") in ("vacuum", "mop") else "vacuum"
                self._queue = list(rooms)
                self._target = self._queue.pop(0) if self._queue else None
                self._ticks, self._area, self._time = 0, 0.0, 0
                self._state = "cleaning"
                detail = "simulated %s of %s" % (self._mode, rooms)
            elif cmd == "pause":
                if self._state == "cleaning":
                    self._state = "paused"
            elif cmd == "resume":
                if self._state == "paused":
                    self._state = "cleaning"
            elif cmd == "stop":
                self._state, self._queue, self._target = "idle", [], None
            elif cmd == "dock":
                self._state, self._queue, self._target = "returning", [], None
            elif cmd == "set_fan_speed":
                self._fan = str(args.get("fan_speed", self._fan))
            elif cmd == "locate":
                detail = "beep (simulated)"
            else:
                ok, detail = False, "unknown command: %s" % cmd
        self._dispatch({"type": "ack", "id": cid, "ok": ok, "detail": detail})
        self._emit_status()
        return cid

    # --- simulation --------------------------------------------------------
    def _index_rooms(self, occ: Dict[str, Any], seg: List[int]) -> None:
        W, H, res = occ["width"], occ["height"], occ["resolution"]
        ox, oy = occ["origin"]["x"], occ["origin"]["y"]
        cells: Dict[int, List[Tuple[float, float]]] = {}
        for i, rid in enumerate(seg):
            if rid is None or rid < 0:
                continue
            x, y = i % W, i // W
            cells.setdefault(int(rid), []).append((ox + (x + 0.5) * res, oy + (y + 0.5) * res))
        # thin out (we only need sample points to wander through)
        self._room_cells = {k: v[::7] or v for k, v in cells.items()}

    def _step(self) -> None:
        import math
        import random
        if self._state == "cleaning":
            self._battery = max(0.0, self._battery - 0.4)
            self._time += int(self._TICK)
            self._area += 0.3
            pts = self._room_cells.get(self._target) if self._target is not None else None
            if pts:
                x, y = random.choice(pts)
                self._pose = {"x": round(x, 3), "y": round(y, 3),
                              "theta": round(random.uniform(-math.pi, math.pi), 3)}
            self._ticks += 1
            if self._ticks >= self._TICKS_PER_ROOM:
                self._ticks = 0
                self._target = self._queue.pop(0) if self._queue else None
                if self._target is None:
                    self._state = "returning"
            if self._battery <= 15:
                self._state = "returning"
        elif self._state == "returning":
            dx = self._dock["x"] - self._pose["x"]
            dy = self._dock["y"] - self._pose["y"]
            d = math.hypot(dx, dy)
            if d < 1.0:
                self._pose = dict(self._dock)
                self._state = "docked"
            else:
                k = 1.0 / d
                self._pose = {"x": self._pose["x"] + dx * k, "y": self._pose["y"] + dy * k,
                              "theta": math.atan2(dy, dx)}
        elif self._state == "docked":
            self._battery = min(100.0, self._battery + 1.0)

    def _run(self) -> None:
        import base64
        self._set_online(True)
        try:
            m = load_real_map(self._dir, self._occ_topic)
        except Exception as e:                       # noqa: BLE001
            log.error("failed to load real map from %s: %s", self._dir, e)
            self._set_online(False)
            return

        occ = m["occ"]
        self._pose = m["pose"] or self._pose
        self._dock = dict(self._pose)
        seg = (m["rooms_scheme"].get("segmentation") or {}).get("data") or []
        self._index_rooms(occ, seg)

        self._dispatch({
            "type": "map", "ts": time.time(), "map_id": m["map_id"],
            "resolution": occ["resolution"], "width": occ["width"],
            "height": occ["height"], "origin": occ["origin"], "data": occ["data"],
        })
        self._dispatch({
            "type": "rooms", "ts": time.time(), "map_id": m["map_id"],
            "scheme": m["rooms_scheme"],
        })
        if m["map_image_path"]:
            try:
                with open(m["map_image_path"], "rb") as f:
                    raw = f.read()
                self._dispatch({
                    "type": "map_image", "ts": time.time(), "map_id": m["map_id"],
                    "format": "png", "data_b64": base64.b64encode(raw).decode("ascii"),
                })
            except OSError:
                pass
        log.info("real map %s loaded: occ %dx%d, %d rooms (simulate=%s)",
                 m["map_id"], occ["width"], occ["height"],
                 len(m["rooms_scheme"].get("present_room_ids", [])), self._simulate)

        self._emit_status()
        n = 0
        while not self._stop.wait(self._TICK):
            with self._lock:
                busy = self._state in ("cleaning", "returning", "paused")
                self._step()
            n += 1
            if busy or n % 5 == 0:     # 1 Hz while active, 0.2 Hz heartbeat idle
                self._emit_status()

    def _emit_status(self) -> None:
        with self._lock:
            msg = {
                "type": "status", "ts": time.time(), "state": self._state,
                "battery": int(round(self._battery)) if self._simulate else None,
                "fan_speed": self._fan, "mode": self._mode, "error": None,
                "pose": dict(self._pose),
                "clean_area_m2": round(self._area, 1), "clean_time_s": self._time,
            }
        self._dispatch(msg)
