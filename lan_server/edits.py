"""
Editor sidecar store.

User edits to a map -- room name overrides, no-go / virtual-wall rectangles, and
a per-room clean order -- are kept in a sidecar JSON next to the persisted map,
at  <data_dir>/<map_id>/edits.json.  They are LAYERED over the loaded map by the
UI and never written back into the robot's binary map format (that is a later
stretch goal; see NEXT-STEPS.md).

Rectangles are stored in WORLD metres (resolution-independent), so they overlay
correctly regardless of the grid resolution:
  {"id": "...", "type": "nogo"|"wall", "x0":m,"y0":m,"x1":m,"y1":m}
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Optional


def _empty(map_id: str) -> Dict[str, Any]:
    return {
        "map_id": map_id,
        "room_names": {},     # {"<room_id>": "Kitchen"}
        "zones": [],          # list of rectangles (world metres)
        "zones_rev": 0,       # bumps whenever the robot's no-go list changes (stale-editor guard)
        "clean_order": [],    # [room_id, ...] override
        "updated_at": 0.0,
    }


class EditsStore:
    def __init__(self, data_dir: str) -> None:
        self._dir = os.path.abspath(data_dir)
        self._lock = threading.Lock()

    def _path(self, map_id: str) -> str:
        safe = "".join(c for c in (map_id or "map") if c.isalnum() or c in "-_.")
        d = os.path.join(self._dir, safe or "map")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "edits.json")

    def load(self, map_id: str) -> Dict[str, Any]:
        p = self._path(map_id)
        with self._lock:
            if not os.path.exists(p):
                return _empty(map_id)
            try:
                with open(p) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                return _empty(map_id)
        # normalise shape
        base = _empty(map_id)
        base.update({k: d[k] for k in ("room_names", "zones", "clean_order",
                                       "updated_at", "zones_rev") if k in d})
        base["map_id"] = d.get("map_id", map_id)
        return base

    def save(self, map_id: str, edits: Dict[str, Any]) -> Dict[str, Any]:
        """Validate + persist. Returns the stored object."""
        out = _empty(map_id)
        # room name overrides
        rn = edits.get("room_names") or {}
        out["room_names"] = {str(k): str(v) for k, v in rn.items() if str(v).strip()}
        # clean order
        order = edits.get("clean_order") or []
        out["clean_order"] = [int(x) for x in order if _is_int(x)]
        # zones: give each an id, clamp type, coerce coords to float
        zones = []
        for z in (edits.get("zones") or []):
            try:
                # nogo = no vacuum + no mop, nosweep = no vacuum, nomop = no mop,
                # wall = virtual wall (sent to the robot as a thin no-go strip)
                ztype = z.get("type") if z.get("type") in ("nogo", "nosweep", "nomop", "wall") else "nogo"
                zone = {
                    "id": str(z.get("id") or uuid.uuid4().hex[:8]),
                    "type": ztype,
                    "x0": float(z["x0"]), "y0": float(z["y0"]),
                    "x1": float(z["x1"]), "y1": float(z["y1"]),
                }
                # exact robot polygon (kept so an unchanged zone is sent back verbatim)
                if isinstance(z.get("points"), list) and len(z["points"]) >= 3:
                    zone["points"] = [{"x": float(p["x"]), "y": float(p["y"])} for p in z["points"]]
                zones.append(zone)
            except (KeyError, TypeError, ValueError):
                continue
        out["zones"] = zones
        out["zones_rev"] = int(edits.get("zones_rev") or 0)
        out["updated_at"] = time.time()

        p = self._path(map_id)
        tmp = p + ".tmp"
        with self._lock:
            with open(tmp, "w") as f:
                json.dump(out, f, indent=2)
            os.replace(tmp, p)
        return out


def _is_int(x: Any) -> bool:
    try:
        int(x)
        return True
    except (TypeError, ValueError):
        return False
