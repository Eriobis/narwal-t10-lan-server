"""
MapStore -- persist captured maps + room segmentation to local disk.

The user specifically wants the house map kept locally (the robot keeps none;
it is cloud-only, rebuilt in RAM each run). We persist each captured map in the
**ROS map_server standard format** -- a binary PGM image plus a YAML sidecar --
because that is exactly what a future move_base / map_server stack will load,
and it is a documented, tool-friendly format (no custom binary, no numpy).

On-disk layout (data_dir defaults to ./data/maps):
    data/maps/
        <map_id>/
            map.pgm        occupancy grid as grayscale PGM (P5)
            map.yaml       ROS map metadata (resolution, origin, thresholds)
            grid.json      raw grid + meta (exact -1/0/100 values, lossless)
            rooms.json     room segmentation (our model + robot's raw scheme)
        latest.txt         map_id of the most recently captured map

PGM pixel convention (ROS map_server): occupied=0 (black), free=254 (white),
unknown=205 (gray). Rows are written top-down (image y=0 is the top), which is
the ROS image convention; the YAML origin + negate=0 make it line up.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional

from .model import OccupancyGrid, Pose, RoomScheme

# PGM grayscale values
_PX_OCC = 0
_PX_FREE = 254
_PX_UNKNOWN = 205


class MapStore:
    def __init__(self, data_dir: str) -> None:
        self._dir = os.path.abspath(data_dir)
        self._lock = threading.Lock()
        os.makedirs(self._dir, exist_ok=True)

    # --- paths -------------------------------------------------------------
    def _map_dir(self, map_id: str) -> str:
        safe = "".join(c for c in map_id if c.isalnum() or c in "-_.")
        return os.path.join(self._dir, safe or "map")

    # --- save --------------------------------------------------------------
    def save_map(self, grid: OccupancyGrid) -> str:
        """Persist an occupancy grid. Returns the map directory path."""
        d = self._map_dir(grid.map_id)
        with self._lock:
            os.makedirs(d, exist_ok=True)
            self._write_pgm(os.path.join(d, "map.pgm"), grid)
            self._write_yaml(os.path.join(d, "map.yaml"), grid)
            _write_json(os.path.join(d, "grid.json"), grid.to_dict())
            _write_text(os.path.join(self._dir, "latest.txt"), grid.map_id)
        return d

    def save_rooms(self, scheme: RoomScheme) -> str:
        d = self._map_dir(scheme.map_id)
        with self._lock:
            os.makedirs(d, exist_ok=True)
            _write_json(os.path.join(d, "rooms.json"), scheme.to_dict())
        return os.path.join(d, "rooms.json")

    def _write_pgm(self, path: str, grid: OccupancyGrid) -> None:
        # Binary P5. Map occupancy -> grayscale, flipping y to image order.
        header = ("P5\n%d %d\n255\n" % (grid.width, grid.height)).encode("ascii")
        pixels = bytearray(grid.width * grid.height)
        w, h = grid.width, grid.height
        for y in range(h):
            src_row = (h - 1 - y) * w     # flip: ROS grid y=0 is bottom
            dst_row = y * w
            for x in range(w):
                v = grid.data[src_row + x]
                if v < 0:
                    px = _PX_UNKNOWN
                elif v >= 65:            # occupied threshold (ROS default)
                    px = _PX_OCC
                elif v <= 25:            # free threshold
                    px = _PX_FREE
                else:
                    px = _PX_UNKNOWN
                pixels[dst_row + x] = px
        with open(path, "wb") as f:
            f.write(header)
            f.write(pixels)

    def _write_yaml(self, path: str, grid: OccupancyGrid) -> None:
        # Hand-written YAML (keeps PyYAML out of requirements).
        yaml = (
            "image: map.pgm\n"
            "resolution: %s\n"
            "origin: [%s, %s, %s]\n"
            "negate: 0\n"
            "occupied_thresh: 0.65\n"
            "free_thresh: 0.196\n"
            "# map_id: %s\n"
        ) % (
            repr(grid.resolution),
            repr(grid.origin.x), repr(grid.origin.y), repr(grid.origin.theta),
            grid.map_id,
        )
        _write_text(path, yaml)

    # --- load --------------------------------------------------------------
    def latest_map_id(self) -> Optional[str]:
        p = os.path.join(self._dir, "latest.txt")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read().strip() or None

    def load_grid(self, map_id: str) -> Optional[OccupancyGrid]:
        p = os.path.join(self._map_dir(map_id), "grid.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            d = json.load(f)
        o = d.get("origin", {})
        return OccupancyGrid(
            map_id=d["map_id"], width=d["width"], height=d["height"],
            resolution=d["resolution"],
            origin=Pose(o.get("x", 0.0), o.get("y", 0.0), o.get("theta", 0.0)),
            data=d["data"], created_at=d.get("created_at", 0.0),
        )

    def load_rooms(self, map_id: str) -> Optional[Dict[str, Any]]:
        p = os.path.join(self._map_dir(map_id), "rooms.json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def list_maps(self) -> List[Dict[str, Any]]:
        out = []
        for name in sorted(os.listdir(self._dir)):
            gj = os.path.join(self._dir, name, "grid.json")
            if os.path.exists(gj):
                try:
                    with open(gj) as f:
                        d = json.load(f)
                    out.append({
                        "map_id": d.get("map_id", name),
                        "width": d.get("width"),
                        "height": d.get("height"),
                        "resolution": d.get("resolution"),
                        "created_at": d.get("created_at"),
                        "has_rooms": os.path.exists(
                            os.path.join(self._dir, name, "rooms.json")),
                    })
                except (ValueError, OSError):
                    continue
        return out


# --- small IO helpers -------------------------------------------------------
def _write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
