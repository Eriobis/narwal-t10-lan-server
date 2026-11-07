"""
Data model for the Narwal T10 LAN server.

These dataclasses are the *canonical* in-memory representation of robot
state and map data on the LAN server (Python 3 side). The on-robot bridge
(robot_server.py, Python 2) sends JSON that maps onto these structures; see
the wire protocol documented in bridge/base.py.

Nothing here talks to the robot directly -- it is pure state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# --- Canonical robot states -------------------------------------------------
# We normalise everything the robot reports into this small set. These happen
# to be exactly the states Home Assistant's MQTT vacuum (state schema) accepts,
# which keeps the HA mapping trivial.
STATE_CLEANING = "cleaning"
STATE_DOCKED = "docked"
STATE_PAUSED = "paused"
STATE_IDLE = "idle"
STATE_RETURNING = "returning"
STATE_ERROR = "error"

VALID_STATES = {
    STATE_CLEANING,
    STATE_DOCKED,
    STATE_PAUSED,
    STATE_IDLE,
    STATE_RETURNING,
    STATE_ERROR,
}


@dataclass
class Pose:
    """Robot pose in the map frame (metres, radians). ROS REP-103 convention."""
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0


@dataclass
class RobotState:
    """Everything we know about the robot right now."""
    state: str = STATE_IDLE
    battery: int = 0                      # 0..100 %
    fan_speed: str = "standard"           # quiet|standard|strong|max (TODO confirm names)
    mode: str = "vacuum"                  # vacuum|mop -- current cleaning mode
    error: Optional[str] = None
    pose: Pose = field(default_factory=Pose)
    clean_area_m2: float = 0.0
    clean_time_s: int = 0
    online: bool = False                  # is the bridge/robot reachable
    updated_at: float = field(default_factory=time.time)

    def apply_status(self, msg: Dict[str, Any]) -> None:
        """Merge a 'status' message from the bridge into this state."""
        if "state" in msg and msg["state"] in VALID_STATES:
            self.state = msg["state"]
        if "battery" in msg and msg["battery"] is not None:
            try:
                self.battery = max(0, min(100, int(round(float(msg["battery"])))))
            except (TypeError, ValueError):
                pass
        if "fan_speed" in msg and msg["fan_speed"]:
            self.fan_speed = str(msg["fan_speed"])
        if "mode" in msg and msg["mode"]:
            self.mode = str(msg["mode"])
        if "error" in msg:
            self.error = msg["error"] or None
        if "pose" in msg and isinstance(msg["pose"], dict):
            p = msg["pose"]
            self.pose = Pose(
                x=float(p.get("x", self.pose.x)),
                y=float(p.get("y", self.pose.y)),
                theta=float(p.get("theta", self.pose.theta)),
            )
        if "clean_area_m2" in msg and msg["clean_area_m2"] is not None:
            self.clean_area_m2 = float(msg["clean_area_m2"])
        if "clean_time_s" in msg and msg["clean_time_s"] is not None:
            self.clean_time_s = int(msg["clean_time_s"])
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class OccupancyGrid:
    """
    A ROS-style occupancy grid (nav_msgs/OccupancyGrid semantics).

    data is row-major, length width*height, cell values:
        -1  = unknown
         0  = free
       100  = occupied
    origin is the real-world pose of cell (0,0) (bottom-left), metres.
    resolution is metres per cell.
    """
    map_id: str
    width: int
    height: int
    resolution: float
    origin: Pose
    data: List[int]
    created_at: float = field(default_factory=time.time)

    def to_meta(self) -> Dict[str, Any]:
        """Metadata only (no giant data array) -- for listings & YAML."""
        return {
            "map_id": self.map_id,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "origin": asdict(self.origin),
            "created_at": self.created_at,
            "cells": self.width * self.height,
        }

    def to_dict(self) -> Dict[str, Any]:
        d = self.to_meta()
        d["data"] = self.data
        return d


@dataclass
class Room:
    """One segmented room from the robot's clean_scheme."""
    id: int
    name: str
    # optional polygon / cell list -- segmentation geometry is a TODO
    # (we only reliably have room *ids* from communicator/clean_scheme/info).
    cells: Optional[List[int]] = None


@dataclass
class RoomScheme:
    """
    Room segmentation + cleaning schemes, mirroring the robot's local
    communicator/clean_scheme/info JSON (sample has rooms with a sweep_scheme and
    mop_scheme each carrying a scheme_id), optionally ENRICHED by the real-map
    loader with room names, a clean order, and a per-cell segmentation grid
    aligned to the occupancy grid.
    """
    map_id: str
    rooms: List[Room] = field(default_factory=list)
    sweep_scheme_id: Optional[str] = None
    mop_scheme_id: Optional[str] = None
    sweep_rooms: List[int] = field(default_factory=list)   # room ids the robot sweeps (vacuum)
    mop_rooms: List[int] = field(default_factory=list)     # room ids the robot mops
    clean_order: Dict[str, List[int]] = field(default_factory=dict)  # {"sweep":[],"mop":[]}
    segmentation: Optional[Dict[str, Any]] = None   # {width,height,resolution,origin,data}
    raw: Dict[str, Any] = field(default_factory=dict)   # keep original JSON verbatim
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "map_id": self.map_id,
            "rooms": [asdict(r) for r in self.rooms],
            "sweep_scheme_id": self.sweep_scheme_id,
            "mop_scheme_id": self.mop_scheme_id,
            "sweep_rooms": self.sweep_rooms,
            "mop_rooms": self.mop_rooms,
            "clean_order": self.clean_order,
            "segmentation": self.segmentation,
            "raw": self.raw,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_clean_scheme(map_id: str, info: Dict[str, Any]) -> "RoomScheme":
        """
        Parse the robot's native clean_scheme/info JSON, plus the optional
        enrichment keys the real-map loader adds ("names", "clean_order",
        "segmentation", "present_room_ids").

        Base shape (captured):
          {"mop_scheme":[{"room_id":[...],"scheme_id":"..","humidity":[..]}],
           "sweep_scheme":[{"room_id":[...],"scheme_id":".."}]}
        When the loader supplies real room names (from ':3') we use them;
        otherwise we synthesise "Room N".
        """
        sweep = (info.get("sweep_scheme") or [{}])[0]
        mop = (info.get("mop_scheme") or [{}])[0]
        names = info.get("names") or {}
        present = info.get("present_room_ids")
        if present:
            room_ids = sorted(present)
        else:
            room_ids = sorted(set(sweep.get("room_id", []) or mop.get("room_id", [])))

        def name_for(rid: int) -> str:
            n = names.get(str(rid)) or names.get(rid)
            # robot placeholder names like "-2".."-9" aren't useful; fall back.
            if n and not str(n).lstrip("-").isdigit():
                return str(n)
            return "Room %d" % rid

        rooms = [Room(id=rid, name=name_for(rid)) for rid in room_ids]
        return RoomScheme(
            map_id=map_id,
            rooms=rooms,
            sweep_scheme_id=sweep.get("scheme_id"),
            mop_scheme_id=mop.get("scheme_id"),
            sweep_rooms=list(dict.fromkeys(sweep.get("room_id", []) or [])),
            mop_rooms=list(dict.fromkeys(mop.get("room_id", []) or [])),
            clean_order=info.get("clean_order") or {},
            segmentation=info.get("segmentation"),
            raw={k: v for k, v in info.items() if k != "segmentation"},  # keep raw small
        )
