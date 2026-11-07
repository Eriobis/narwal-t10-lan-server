"""
Vectorize a raster occupancy map + room segmentation into clean vector geometry
in WORLD coordinates (metres), for both display (crisp SVG) and the geometry /
robot model (room polygons as clean-targets + no-go geometry).

Pipeline (opencv + shapely + numpy; the lighter dep set vs scikit-image):

  rooms:
    per-room mask (from the resampled segmentation, aligned to the occ grid)
      -> morphological close (knit 1px gaps)
      -> cv2.findContours RETR_CCOMP  (outer rings + hole rings via hierarchy)
      -> map pixels to world coords
      -> shapely .simplify (Douglas-Peucker) to cut the pixel staircase
      -> ORTHOGONAL REGULARIZATION (snap near-axis edges to H/V by iterative
         relaxation -- homes are mostly axis-aligned) so rooms read rectilinear
      -> keep holes (obstacles inside a room) as polygon interiors
  walls:
    occupied (>=65) mask -> drop speckle components -> close
      -> contours -> simplify + orthogonalize -> closed polylines

Everything is derived from the grid resolution + origin parsed in realmap.py, so
rooms and walls share one transform and stay aligned.

Output: a compact GeoJSON-ish dict (see vectorize_map()).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

# Room palette (hex) -- matches the frontend ROOM_TINTS.
ROOM_PALETTE = [
    "#4682dc", "#e69628", "#46c878", "#c846a0", "#e6d23c",
    "#78c8e6", "#b478e6", "#e65a5a", "#78b45a", "#c88cc8",
]


def room_color(rid: int) -> str:
    return ROOM_PALETTE[rid % len(ROOM_PALETTE)]


# --------------------------------------------------------------------------- #
# Orthogonal regularization
# --------------------------------------------------------------------------- #
def orthogonalize(pts: List[List[float]], angle_thr_deg: float = 20.0,
                  iters: int = 6) -> List[List[float]]:
    """
    Snap near-horizontal / near-vertical edges of a closed ring to exact H/V by
    iterative relaxation (shared vertices converge), leaving genuine diagonals.
    pts: list of [x,y] (ring, NOT repeating the first point).
    """
    n = len(pts)
    if n < 4:
        return pts
    p = [[float(x), float(y)] for x, y in pts]
    thr = math.radians(angle_thr_deg)
    for _ in range(iters):
        kinds = []
        for i in range(n):
            a, b = p[i], p[(i + 1) % n]
            ang = math.atan2(b[1] - a[1], b[0] - a[0])
            dh = min(abs(ang), abs(abs(ang) - math.pi))      # dist to horizontal
            dv = abs(abs(ang) - math.pi / 2)                 # dist to vertical
            if dh <= thr and dh <= dv:
                kinds.append("H")
            elif dv <= thr:
                kinds.append("V")
            else:
                kinds.append("D")
        for i, k in enumerate(kinds):
            a, b = p[i], p[(i + 1) % n]
            if k == "H":
                m = (a[1] + b[1]) / 2.0
                a[1] = b[1] = m
            elif k == "V":
                m = (a[0] + b[0]) / 2.0
                a[0] = b[0] = m
    return p


def _snap(pts: List[List[float]], grid: float) -> List[List[float]]:
    """Snap coords to a `grid` lattice so near-collinear runs become exactly
    collinear (then Douglas-Peucker drops the redundant vertices)."""
    return [[round(x / grid) * grid, round(y / grid) * grid] for x, y in pts]


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def vectorize_map(occ: Dict[str, Any], room_grid: List[int],
                  rooms_meta: Optional[List[Dict[str, Any]]] = None,
                  map_id: str = "map") -> Dict[str, Any]:
    """
    occ: {width,height,resolution,origin{x,y},data[-1/0..100]}
    room_grid: per-occ-cell room id (aligned to occ.data), -1 = none.
    rooms_meta: optional [{id,name}] for labels.
    Returns the vector payload.
    """
    import numpy as np
    import cv2
    from shapely.geometry import Polygon, LineString
    from shapely.validation import make_valid

    W, H = occ["width"], occ["height"]
    res = occ["resolution"]
    ox, oy = occ["origin"]["x"], occ["origin"]["y"]
    names = {m["id"]: m.get("name", "Room %d" % m["id"]) for m in (rooms_meta or [])}

    def to_world(px: float, py: float) -> List[float]:
        # contour coords are (col,row); row 0 = world y min (grid is y-up)
        return [round(ox + px * res, 4), round(oy + py * res, 4)]

    def ring_world(cnt) -> List[List[float]]:
        return [to_world(float(x), float(y)) for [[x, y]] in cnt]

    min_room_area = max(0.06, (res * 3) ** 2)   # m^2 noise floor

    # ---- rooms ----
    arr = np.array(room_grid, dtype=np.int16).reshape(H, W)
    rooms_out: List[Dict[str, Any]] = []
    present = sorted(int(v) for v in np.unique(arr) if v >= 0)
    kernel = np.ones((3, 3), np.uint8)

    for rid in present:
        mask = (arr == rid).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hier is None:
            continue
        hier = hier[0]
        polygons = []
        for i, c in enumerate(cnts):
            if hier[i][3] != -1:          # a hole -> handled with its parent
                continue
            if len(c) < 3:
                continue
            shell = ring_world(c)
            holes = []
            j = hier[i][2]                # first child
            while j != -1:
                if len(cnts[j]) >= 3:
                    holes.append(ring_world(cnts[j]))
                j = hier[j][0]            # next sibling
            poly = _clean_polygon(shell, holes, res, Polygon, make_valid)
            for pg in poly:
                if pg.area >= min_room_area:
                    polygons.append(pg)
        if not polygons:
            continue
        polygons.sort(key=lambda g: g.area, reverse=True)
        poly_dicts = [{
            "exterior": [[round(x, 4), round(y, 4)] for x, y in pg.exterior.coords[:-1]],
            "holes": [[[round(x, 4), round(y, 4)] for x, y in r.coords[:-1]]
                      for r in pg.interiors],
        } for pg in polygons]
        rep = polygons[0].representative_point()
        rooms_out.append({
            "id": rid,
            "name": names.get(rid, "Room %d" % rid),
            "color": room_color(rid),
            "area_m2": round(sum(pg.area for pg in polygons), 3),
            "label": [round(rep.x, 4), round(rep.y, 4)],
            "polygons": poly_dicts,
            # convenience: the largest polygon's shell/holes (GeoJSON-ish)
            "polygon": poly_dicts[0]["exterior"],
            "holes": poly_dicts[0]["holes"],
        })

    # ---- walls ----
    occ_arr = np.array(occ["data"], dtype=np.int16).reshape(H, W)
    wall = (occ_arr >= 65).astype(np.uint8) * 255
    nlab, lbl, stats, _ = cv2.connectedComponentsWithStats(wall, 8)
    clean = np.zeros_like(wall)
    for k in range(1, nlab):
        if stats[k, cv2.CC_STAT_AREA] >= 4:      # drop speckle
            clean[lbl == k] = 255
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
    wcnts, _ = cv2.findContours(clean, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    walls_out: List[List[List[float]]] = []
    min_wall_len = res * 6
    for c in wcnts:
        if len(c) < 2 or cv2.arcLength(c, True) < min_wall_len:
            continue
        ring = ring_world(c)
        try:
            ls = LineString(ring + [ring[0]]).simplify(res * 1.6)
            pts = _snap(orthogonalize([list(p) for p in ls.coords[:-1]]), res)
            pts = [p for p in LineString(pts + [pts[0]]).simplify(res * 0.9).coords[:-1]]
        except Exception:
            pts = ring
        if len(pts) >= 2:
            closed = [[round(x, 4), round(y, 4)] for x, y in pts]
            closed.append(closed[0])
            walls_out.append(closed)

    bounds = {
        "minx": round(ox, 4), "miny": round(oy, 4),
        "maxx": round(ox + W * res, 4), "maxy": round(oy + H * res, 4),
    }
    return {
        "map_id": map_id,
        "resolution": res,
        "origin": {"x": ox, "y": oy},
        "bounds": bounds,
        "rooms": rooms_out,
        "walls": walls_out,
        "counts": {
            "rooms": len(rooms_out),
            "room_polygons": sum(len(r["polygons"]) for r in rooms_out),
            "walls": len(walls_out),
            "wall_points": sum(len(w) for w in walls_out),
        },
    }


def _clean_polygon(shell, holes, res, Polygon, make_valid):
    """simplify -> orthogonalize -> revalidate. Returns a list of shapely polys."""
    try:
        poly = Polygon(shell, holes)
        if not poly.is_valid:
            poly = make_valid(poly)
        poly = poly.simplify(res * 1.2, preserve_topology=True)   # cut staircase (tight hug)
    except Exception:
        return []
    polys = _as_polys(poly)
    out = []
    for pg in polys:
        try:
            ext = _snap(orthogonalize([list(p) for p in pg.exterior.coords[:-1]]), res)
            ints = [_snap(orthogonalize([list(p) for p in r.coords[:-1]]), res)
                    for r in pg.interiors]
            pg2 = Polygon(ext, ints)
            if not pg2.is_valid:
                pg2 = pg2.buffer(0)
            # drop collinear vertices left by snapping -> clean rectilinear shape
            pg2 = pg2.simplify(res * 0.9, preserve_topology=True)
            out.extend(_as_polys(pg2))
        except Exception:
            out.append(pg)
    return [p for p in out if not p.is_empty and p.area > 0]


def _as_polys(geom):
    t = geom.geom_type
    if t == "Polygon":
        return [geom]
    if t in ("MultiPolygon", "GeometryCollection"):
        # flatten recursively: make_valid() can return a GeometryCollection holding
        # a MultiPolygon (+ stray LineStrings); keeping only direct Polygon members
        # silently dropped a whole room (room 7, 2026-11-03).
        out = []
        for g in geom.geoms:
            out.extend(_as_polys(g))
        return out
    return []
