"""
MockBridge -- a simulated Narwal T10 so the LAN server, web UI and Home
Assistant discovery all work *today*, with no robot and no robot_server.py.

It fabricates:
  * a static occupancy grid shaped like a small apartment (walls + rooms),
  * a room-segmentation scheme in the robot's real clean_scheme/info shape
    (10 rooms, ids 0-9, sweep + mop scheme_ids) -- matching the captured sample,
  * a live status stream: battery drain while cleaning, a robot pose that
    wanders through the free space, dock/return behaviour, etc.

Commands (start/stop/pause/dock/clean_room/...) mutate the simulation so the
UI and HA buttons visibly do something.

Nothing here reflects real robot internals; it only emits the documented wire
protocol (see bridge/base.py) so the rest of the server can't tell it apart
from a real robot_server.py.
"""

from __future__ import annotations

import base64
import math
import random
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .base import Bridge
from ..pngutil import grid_to_png
from ..model import (
    STATE_CLEANING,
    STATE_DOCKED,
    STATE_PAUSED,
    STATE_RETURNING,
    STATE_IDLE,
)

# Grid geometry
_W = 160
_H = 120
_RES = 0.05  # 5 cm/cell -> 8.0 m x 6.0 m apartment

# Occupancy cell values (ROS convention)
UNKNOWN = -1
FREE = 0
OCC = 100


def _build_apartment():
    """
    Build a fixed occupancy grid: an outer wall, a few interior walls creating
    rooms, and a dock in the bottom-left. Returns (grid, room_id_grid).

    room_id_grid[i] is the room id (0..9) for free cells, or None.
    """
    grid = [UNKNOWN] * (_W * _H)
    room_of = [None] * (_W * _H)

    def idx(x, y):
        return y * _W + x

    # Define 10 rectangular rooms (x0,y0,x1,y1) in cell coords, tiled across
    # the apartment. Kept simple & readable rather than realistic.
    rooms = [
        (2,   2,  52,  40),   # 0
        (54,  2, 104,  40),   # 1
        (106, 2, 157,  40),   # 2
        (2,  42,  52,  78),   # 3
        (54, 42, 104,  78),   # 4
        (106,42, 157,  78),   # 5
        (2,  80,  52, 117),   # 6
        (54, 80,  88, 117),   # 7
        (90, 80, 122, 117),   # 8
        (124,80, 157, 117),   # 9
    ]

    # Fill rooms as free space and tag the room id.
    for rid, (x0, y0, x1, y1) in enumerate(rooms):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                grid[idx(x, y)] = FREE
                room_of[idx(x, y)] = rid

    # Outer wall + room borders: mark any free cell adjacent to unknown/edge
    # as occupied (1-cell wall). Build walls by drawing room rectangle borders.
    def draw_rect(x0, y0, x1, y1):
        for x in range(x0, x1 + 1):
            grid[idx(x, y0)] = OCC
            grid[idx(x, y1)] = OCC
        for y in range(y0, y1 + 1):
            grid[idx(x0, y)] = OCC
            grid[idx(x1, y)] = OCC

    for (x0, y0, x1, y1) in rooms:
        draw_rect(x0, y0, x1, y1)

    # Carve doorways so the robot can traverse (punch 6-cell gaps in shared walls)
    def carve(x, y, horizontal=True, span=6):
        for d in range(span):
            if horizontal:
                grid[idx(x + d, y)] = FREE
            else:
                grid[idx(x, y + d)] = FREE

    carve(24, 40, horizontal=False)   # 0<->3 area
    carve(78, 40, horizontal=False)
    carve(130, 40, horizontal=False)
    carve(52, 18, horizontal=True)    # 0<->1
    carve(104, 18, horizontal=True)   # 1<->2
    carve(24, 78, horizontal=False)   # 3<->6
    carve(78, 78, horizontal=False)   # 4<->7
    carve(130, 78, horizontal=False)  # 5<->9

    return grid, room_of


# Native clean_scheme/info shape (matches captured robot-cloud-state sample).
_MOCK_CLEAN_SCHEME = {
    "mop_scheme": [{
        "humidity": [0] * 20,
        "room_id": [5, 5, 6, 6, 7, 7, 8, 8, 3, 3, 0, 0, 1, 1, 2, 2, 4, 4, 9, 9],
        "scheme_id": "mock-mop-0000000000000000000000000000",
        "sync_station": 1,
    }],
    "sweep_scheme": [{
        "humidity": [],
        "room_id": [9, 2, 1, 0, 3, 8, 7, 6, 5, 4],
        "scheme_id": "mock-sweep-000000000000000000000000",
        "sync_station": 1,
    }],
}


class MockBridge(Bridge):
    def __init__(self, tick: float = 1.0) -> None:
        super().__init__()
        self._tick = tick
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._map_id = "mock-" + time.strftime("%Y%m%d-%H%M%S")

        self._grid, self._room_of = _build_apartment()
        self._free_cells = [i for i, v in enumerate(self._grid) if v == FREE]

        # dock near bottom-left room 6
        self._dock_cell = 10 * _W + 10
        self._cell = self._dock_cell

        # simulation state
        self._state = STATE_DOCKED
        self._battery = 100.0
        self._fan = "standard"
        self._clean_area = 0.0
        self._clean_time = 0
        self._target_room: Optional[int] = None
        self._mode = "vacuum"
        self._room_queue: list = []        # rooms left in this session (per-room clean)
        self._room_ticks = 0               # ticks spent in the current target room
        self._session_ticks = 0            # ticks in a whole-house clean

    _TICKS_PER_ROOM = 8
    _WHOLE_HOUSE_TICKS = 40

    def _begin(self, mode: str, rooms) -> None:
        self._mode = mode if mode in ("vacuum", "mop") else "vacuum"
        self._room_queue = [int(r) for r in (rooms or [])]
        self._target_room = self._room_queue.pop(0) if self._room_queue else None
        self._room_ticks = 0
        self._session_ticks = 0
        self._clean_area = 0.0
        self._clean_time = 0
        self._state = STATE_CLEANING

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mock-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # --- commands ----------------------------------------------------------
    def send_command(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
        args = args or {}
        cid = uuid.uuid4().hex
        ok, detail = True, "applied"
        with self._lock:
            if cmd == "start":
                self._begin(args.get("mode", "vacuum"), None)
            elif cmd == "clean":
                self._begin(args.get("mode", "vacuum"), args.get("rooms"))
                detail = "%s %s" % (self._mode, args.get("rooms") or "whole house")
            elif cmd in ("stop",):
                self._state = STATE_IDLE
                self._target_room = None
                self._room_queue = []
            elif cmd == "pause":
                self._state = STATE_PAUSED
            elif cmd == "resume":
                self._state = STATE_CLEANING
            elif cmd == "dock":
                self._state = STATE_RETURNING
                self._target_room = None
            elif cmd == "locate":
                detail = "beep"
            elif cmd == "clean_room":
                self._begin(args.get("mode", "vacuum"), [int(args.get("room_id", 0))])
                detail = "cleaning room %d" % self._target_room
            elif cmd == "set_fan_speed":
                self._fan = str(args.get("fan_speed", self._fan))
                detail = "fan=%s" % self._fan
            else:
                ok, detail = False, "unknown command: %s" % cmd
        # emit ack asynchronously-ish (direct call is fine for the mock)
        self._dispatch({"type": "ack", "id": cid, "ok": ok, "detail": detail})
        return cid

    # --- simulation loop ---------------------------------------------------
    def _run(self) -> None:
        # publish static artefacts once, then stream status
        self._set_online(True)
        self._emit_map()
        self._emit_rooms()
        self._emit_map_image()
        self._emit_status()
        last_map = time.time()

        while not self._stop.wait(self._tick):
            with self._lock:
                self._step()
            self._emit_status()
            # re-publish the map occasionally (a real robot refines it mid-run)
            if time.time() - last_map > 15:
                self._emit_map()
                self._emit_map_image()
                last_map = time.time()

    def _step(self) -> None:
        """Advance the simulation one tick."""
        if self._state == STATE_CLEANING:
            self._battery = max(0.0, self._battery - 0.3)
            self._clean_time += int(self._tick)
            self._clean_area += 0.25
            self._wander()
            if self._target_room is not None:
                self._room_ticks += 1
                if self._room_ticks >= self._TICKS_PER_ROOM:
                    self._room_ticks = 0
                    self._target_room = self._room_queue.pop(0) if self._room_queue else None
                    if self._target_room is None:
                        self._state = STATE_RETURNING
            else:
                self._session_ticks += 1
                if self._session_ticks >= self._WHOLE_HOUSE_TICKS:
                    self._state = STATE_RETURNING
            if self._battery <= 15:
                self._state = STATE_RETURNING
        elif self._state == STATE_RETURNING:
            if self._move_towards(self._dock_cell):
                self._state = STATE_DOCKED
        elif self._state == STATE_DOCKED:
            self._battery = min(100.0, self._battery + 0.8)  # charging
            self._cell = self._dock_cell
        elif self._state in (STATE_PAUSED, STATE_IDLE):
            pass

    def _wander(self) -> None:
        """Random walk through free cells, biased toward a target room."""
        neighbours = self._free_neighbours(self._cell)
        if not neighbours:
            return
        if self._target_room is not None:
            in_room = [c for c in neighbours if self._room_of[c] == self._target_room]
            if self._room_of[self._cell] == self._target_room and in_room:
                neighbours = in_room
        self._cell = random.choice(neighbours)

    def _move_towards(self, target: int) -> bool:
        """Greedy step toward target cell. Returns True when arrived."""
        if self._cell == target:
            return True
        cx, cy = self._cell % _W, self._cell // _W
        tx, ty = target % _W, target // _W
        best, best_d = self._cell, (cx - tx) ** 2 + (cy - ty) ** 2
        for c in self._free_neighbours(self._cell):
            x, y = c % _W, c // _W
            d = (x - tx) ** 2 + (y - ty) ** 2
            if d < best_d:
                best, best_d = c, d
        self._cell = best
        return self._cell == target

    def _free_neighbours(self, cell: int):
        x, y = cell % _W, cell // _W
        out = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < _W and 0 <= ny < _H:
                nc = ny * _W + nx
                if self._grid[nc] == FREE:
                    out.append(nc)
        return out

    # --- emit --------------------------------------------------------------
    def _pose(self) -> Dict[str, float]:
        cx, cy = self._cell % _W, self._cell // _W
        # map cell -> metres (origin at grid[0,0] which we place at (-4,-3))
        return {
            "x": -4.0 + cx * _RES,
            "y": -3.0 + cy * _RES,
            "theta": round(random.uniform(-math.pi, math.pi), 3),
        }

    def _emit_status(self) -> None:
        self._dispatch({
            "type": "status",
            "ts": time.time(),
            "state": self._state,
            "battery": int(round(self._battery)),
            "fan_speed": self._fan,
            "mode": self._mode,
            "error": None,
            "pose": self._pose(),
            "clean_area_m2": round(self._clean_area, 1),
            "clean_time_s": self._clean_time,
        })

    def _emit_map(self) -> None:
        self._dispatch({
            "type": "map",
            "ts": time.time(),
            "map_id": self._map_id,
            "resolution": _RES,
            "width": _W,
            "height": _H,
            "origin": {"x": -4.0, "y": -3.0, "theta": 0.0},
            "data": list(self._grid),
        })

    def _emit_rooms(self) -> None:
        self._dispatch({
            "type": "rooms",
            "ts": time.time(),
            "map_id": self._map_id,
            "scheme": _MOCK_CLEAN_SCHEME,
        })

    # Room tint palette (matches the React UI's ROOM_TINTS).
    _TINTS = [
        (32, 64, 106), (58, 42, 94), (38, 84, 63), (94, 67, 32), (74, 42, 62),
        (42, 78, 84), (80, 74, 32), (58, 58, 94), (94, 48, 48), (32, 74, 74),
    ]

    def _emit_map_image(self) -> None:
        """Synthesise the robot's rendered map (stand-in for map_rotate.png)."""
        png = grid_to_png(_W, _H, self._grid, room_of=self._room_of,
                          tints=self._TINTS, scale=4)
        self._dispatch({
            "type": "map_image",
            "ts": time.time(),
            "map_id": self._map_id,
            "format": "png",
            "data_b64": base64.b64encode(png).decode("ascii"),
        })
