"""
Tiny dependency-free PNG encoder (stdlib zlib only).

Used by the mock to synthesise a `map_rotate.png` (the robot's own rendered
map image) so the React UI's "robot map image" view is demonstrable without
the robot. The real robot_server.py forwards the actual map_rotate.png bytes,
so this encoder is NOT on the real-robot path.
"""

from __future__ import annotations

import struct
import zlib
from typing import List, Tuple


def encode_rgb(width: int, height: int, rows: List[bytes]) -> bytes:
    """
    rows: list of `height` byte strings, each width*3 bytes (R,G,B per pixel).
    Returns a complete PNG byte string (8-bit truecolor).
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, color=2
    raw = bytearray()
    for row in rows:
        raw.append(0)          # filter type 0 (none)
        raw.extend(row)
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def grid_to_png(width: int, height: int, data: List[int],
                room_of=None, tints: List[Tuple[int, int, int]] = None,
                scale: int = 4) -> bytes:
    """
    Render an occupancy grid to a PNG, flipping y to image order and scaling up
    by `scale` for a crisper phone image.

    Cell values: <0 unknown (grey), >=65 occupied (near-black wall),
    else free (light, or room tint if room_of/tints given).
    """
    tints = tints or []
    out_w, out_h = width * scale, height * scale

    def cell_rgb(gi: int) -> Tuple[int, int, int]:
        v = data[gi]
        if v < 0:
            return (58, 62, 70)
        if v >= 65:
            return (18, 20, 26)
        if room_of is not None and tints:
            rid = room_of[gi]
            if rid is not None and rid >= 0:
                return tints[rid % len(tints)]
        return (208, 212, 218)

    rows = []
    for y in range(out_h):
        src_y = height - 1 - (y // scale)
        row = bytearray(out_w * 3)
        for x in range(out_w):
            r, g, b = cell_rgb(src_y * width + (x // scale))
            p = x * 3
            row[p] = r
            row[p + 1] = g
            row[p + 2] = b
        rows.append(bytes(row))
    return encode_rgb(out_w, out_h, rows)
