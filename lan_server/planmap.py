"""
Robot "plan map" (rooms + no-go zones) -- decode, edit, encode.

Protocol (decoded 2026-11-03 from an app capture + the robot's own parsers):
  /pita/plan_map/get {operations:[...]}             -> the robot's live plan
  /pita/plan_map/set {map_id, operations:[...], map_data?, user_forbidden?, ...}
      map_data       {room_layer, width, height, origin_x, origin_y}
      user_forbidden {sweep_polygon_list, mop_polygon_list, mop_sweep_polygon_list}
                     each [{points:[{x,y},...]}] in world metres; the FULL list
                     is sent every time (it replaces what the robot has).
  room_layer = base64(zlib(JSON list of width*height ints)), row 0 = min-y, same
  bitmask as pita_grid_seg: value 1 = outside, bit k+1 = room k (k = 0..7),
  boundary cells carry two room bits, 0 = wall/unknown.

Room ids are a scarce 0..7 range and the robot REUSES freed ids, so callers must
remap per-id data (names) using the old->new mapping these functions return.
"""

from __future__ import annotations

import base64
import json
import zlib
from typing import Any, Dict, List, Optional, Tuple

MAX_ROOMS = 8
MIN_PART_CELLS = 40          # 40 cells x 0.06^2 m = 0.14 m^2: smaller pieces are slivers

# editor zone type <-> robot list
ZONE_LISTS = {
    "nogo": "mop_sweep_polygon_list",    # no vacuum, no mop
    "nosweep": "sweep_polygon_list",     # no vacuum only
    "nomop": "mop_polygon_list",         # no mop only
    "wall": "mop_sweep_polygon_list",    # virtual wall = thin no-go strip
}
WALL_THICKNESS = 0.06


# --------------------------------------------------------------------------- #
# room_layer codec
# --------------------------------------------------------------------------- #
def decode_room_layer(b64: str) -> List[int]:
    return json.loads(zlib.decompress(base64.b64decode(b64)))


def encode_room_layer(cells: List[int]) -> str:
    raw = json.dumps([int(v) for v in cells], separators=(", ", ": "))
    return base64.b64encode(zlib.compress(raw.encode())).decode()


def room_bit(rid: int) -> int:
    return 1 << (rid + 1)


def present_rooms(cells: List[int]) -> List[int]:
    found = 0
    for v in cells:
        found |= v
    return [r for r in range(MAX_ROOMS) if found & room_bit(r)]


# --------------------------------------------------------------------------- #
# edits (pure: return a NEW cell list)
# --------------------------------------------------------------------------- #
class EditError(ValueError):
    pass


def _grid(cells, w, h):
    import numpy as np
    return np.array(cells, dtype=np.int64).reshape(h, w)


def _world_to_cell(x, y, meta) -> Tuple[float, float]:
    return ((x - meta["origin_x"]) / meta["resolution"],
            (y - meta["origin_y"]) / meta["resolution"])


def room_at(cells: List[int], meta: Dict[str, Any], x: float, y: float) -> Optional[int]:
    c, r = _world_to_cell(x, y, meta)
    c, r = int(c), int(r)
    if not (0 <= r < meta["height"] and 0 <= c < meta["width"]):
        return None
    v = cells[r * meta["width"] + c]
    for k in range(MAX_ROOMS):
        if v & room_bit(k):
            return k
    return None


def merge_rooms(cells: List[int], meta: Dict[str, Any], keep: int, absorb: int) -> Tuple[List[int], Dict[str, Any]]:
    """Merge room `absorb` into room `keep` (they must touch)."""
    import numpy as np
    import cv2
    if keep == absorb:
        raise EditError("pick two different rooms")
    w, h = meta["width"], meta["height"]
    g = _grid(cells, w, h)
    bk, ba = room_bit(keep), room_bit(absorb)
    mk, ma = (g & bk) > 0, (g & ba) > 0
    if not mk.any() or not ma.any():
        raise EditError("room not found in the map")
    near = cv2.dilate(mk.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0   # within ~2 cells
    if not (near & ma).any():
        raise EditError("rooms %d and %d do not touch" % (keep, absorb))
    g2 = g.copy()
    g2[ma] = (g2[ma] & ~ba) | bk
    return g2.flatten().tolist(), {"op": "merge", "kept": keep, "removed": absorb}


def split_room(cells: List[int], meta: Dict[str, Any], p1, p2) -> Tuple[List[int], Dict[str, Any]]:
    """Cut the room under the drawn segment p1->p2 into two rooms.

    The segment is rasterised as a 2-cell cut; the room minus the cut must fall
    apart into >= 2 pieces of >= MIN_PART_CELLS. The largest piece keeps the
    room id, the second gets the lowest free id; cut cells and slivers stay with
    the original room (the robot re-smooths borders when it saves the map).
    """
    import numpy as np
    import cv2
    w, h = meta["width"], meta["height"]
    mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    rid = room_at(cells, meta, *mid)
    if rid is None:
        raise EditError("draw the cut line across a room (its middle must be inside the room)")
    used = set(present_rooms(cells))
    free = [k for k in range(MAX_ROOMS) if k not in used]
    if not free:
        raise EditError("the robot supports at most %d rooms -- merge two first" % MAX_ROOMS)
    new = free[0]
    g = _grid(cells, w, h)
    br, bn = room_bit(rid), room_bit(new)
    mask = ((g & br) > 0).astype(np.uint8)
    cut = np.zeros_like(mask)
    a = tuple(int(round(v)) for v in _world_to_cell(*p1, meta))
    b = tuple(int(round(v)) for v in _world_to_cell(*p2, meta))
    cv2.line(cut, a, b, 1, thickness=2)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(mask & (1 - cut), 4)
    parts = sorted(((st[i, cv2.CC_STAT_AREA], i) for i in range(1, n)), reverse=True)
    parts = [p for p in parts if p[0] >= MIN_PART_CELLS]
    if len(parts) < 2:
        raise EditError("the line must cut %s completely in two (extend it past the walls)" % ("room %d" % rid))
    piece = lbl == parts[1][1]
    g2 = g.copy()
    g2[piece] = (g2[piece] & ~br) | bn
    return g2.flatten().tolist(), {"op": "split", "room": rid, "new": new,
                                   "cells": [int(parts[0][0]), int(parts[1][0])]}


def delete_room(cells: List[int], meta: Dict[str, Any], rid: int) -> Tuple[List[int], Dict[str, Any]]:
    """Remove a room (e.g. a staircase landing): its cells become 'outside' and the
    id is freed. The robot will no longer treat that area as a room to clean."""
    import numpy as np
    w, h = meta["width"], meta["height"]
    g = _grid(cells, w, h)
    b = room_bit(rid)
    m = (g & b) > 0
    if not m.any():
        raise EditError("room %d not found" % rid)
    only = m & ((g & ~b & ~1) == 0)
    g2 = g.copy()
    g2[only] = 1
    g2[m & ~only] &= ~b
    return g2.flatten().tolist(), {"op": "delete", "removed": rid}


def plan_payload(plan: Dict[str, Any], cells: List[int], info: Dict[str, Any]) -> Dict[str, Any]:
    """Full plan_map/set payload, like the app: rooms + attrs + clean order + zones in
    ONE call (sending the grid alone left the robot's room table inconsistent)."""
    md = plan["map_data"]
    co = dict(plan.get("clean_order") or {})
    sweep = [r for r in (co.get("sweep_default_order") or [])]
    mop = [r for r in (co.get("mop_default_order") or [])]
    gone = info.get("removed")
    if gone is not None:
        sweep = [r for r in sweep if r != gone]; mop = [r for r in mop if r != gone]
    new = info.get("new")
    if new is not None:
        if new not in sweep: sweep.append(new)
        if new not in mop: mop += [new, new]
    present = set(present_rooms(cells))
    sweep = [r for r in sweep if r in present] + [r for r in sorted(present) if r not in sweep]
    attrs = (plan.get("room_attr") or {}).get("attr") or []
    have = {a.get("room_id") for a in attrs}
    attrs = attrs + [{"name": str(254 - r), "room_id": r, "type": 254 - r} for r in sorted(present) if r not in have]
    return {"map_id": plan.get("map_id", ""),
            "operations": ["map_data", "room_attr", "clean_order", "user_forbidden"],
            "map_data": {"room_layer": encode_room_layer(cells), "width": md["width"], "height": md["height"],
                         "origin_x": md["origin_x"], "origin_y": md["origin_y"]},
            "room_attr": {"attr": attrs},
            "clean_order": {"sweep_default_order": sweep, "mop_default_order": mop},
            "user_forbidden": plan.get("user_forbidden") or
                {"sweep_polygon_list": [], "mop_polygon_list": [], "mop_sweep_polygon_list": []}}


# --------------------------------------------------------------------------- #
# zones <-> user_forbidden
# --------------------------------------------------------------------------- #
def zones_from_forbidden(uf: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Robot polygons -> editor zones (axis-aligned bbox for drawing + exact points)."""
    out = []
    for ztype, key in (("nogo", "mop_sweep_polygon_list"), ("nosweep", "sweep_polygon_list"),
                       ("nomop", "mop_polygon_list")):
        for i, poly in enumerate((uf or {}).get(key) or []):
            pts = [(float(p["x"]), float(p["y"])) for p in poly.get("points", [])]
            if len(pts) < 3:
                continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            out.append({"id": "robot-%s-%d" % (ztype, i), "type": ztype,
                        "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
                        "points": [{"x": x, "y": y} for x, y in pts]})
    return out


def _rect_points(x0, y0, x1, y1):
    xa, xb, ya, yb = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    # same winding the app uses: top-left, top-right, bottom-right, bottom-left
    return [{"x": xa, "y": yb}, {"x": xb, "y": yb}, {"x": xb, "y": ya}, {"x": xa, "y": ya}]


def _wall_points(z):
    """A dragged 'wall' rectangle -> thin strip along its long axis."""
    x0, y0, x1, y1 = z["x0"], z["y0"], z["x1"], z["y1"]
    t = WALL_THICKNESS / 2
    if abs(x1 - x0) >= abs(y1 - y0):           # horizontal wall
        yc = (y0 + y1) / 2
        return _rect_points(x0, yc - t, x1, yc + t)
    xc = (x0 + x1) / 2
    return _rect_points(xc - t, y0, xc + t, y1)


def forbidden_from_zones(zones: List[Dict[str, Any]]) -> Dict[str, Any]:
    uf = {"sweep_polygon_list": [], "mop_polygon_list": [], "mop_sweep_polygon_list": []}
    for z in zones or []:
        key = ZONE_LISTS.get(z.get("type", "nogo"))
        if not key:
            continue
        if z.get("type") == "wall":
            pts = _wall_points(z)
        elif z.get("points"):
            pts = [{"x": float(p["x"]), "y": float(p["y"])} for p in z["points"]]
        else:
            pts = _rect_points(z["x0"], z["y0"], z["x1"], z["y1"])
        uf[key].append({"points": pts})
    return uf
