"""
NarwalServer -- the orchestrator.

Wires together:
  * a Bridge (mock / mqtt / tcp) -> the robot,
  * the in-memory RobotState,
  * the MapStore (persists captured maps + rooms to disk),
  * optional Home Assistant MQTT discovery,
and exposes a small command API that both the web UI and HA call into.

Thread model: the bridge runs its own thread(s) and calls our on_* handlers;
those handlers mutate shared state under a lock. The web app reads the same
state under the lock. Commands flow web/HA -> NarwalServer.command() -> bridge.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

import time

from .config import Config
from .bridge import build_bridge
from .edits import EditsStore
from .history import HistoryStore
from .mapstore import MapStore
from .model import (
    OccupancyGrid, Pose, RobotState, RoomScheme,
    STATE_CLEANING, STATE_DOCKED, STATE_IDLE,
)

log = logging.getLogger("narwal.server")


class NarwalServer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = RobotState()
        self.mapstore = MapStore(cfg.data_dir)
        self.edits = EditsStore(cfg.data_dir)
        self.history = HistoryStore(cfg.data_dir)
        self._lock = threading.Lock()

        # cleaning-session tracking (for history). _pending_clean records the
        # mode/rooms of the most recent clean command so the session that starts
        # right after it is labelled correctly.
        self._session: Optional[Dict[str, Any]] = None
        self._pending_clean: Dict[str, Any] = {"mode": "vacuum", "rooms": None}
        # user room-name overrides from the editor (edits.json), applied to every
        # view: room buttons, map labels, history. Cached; refreshed on map load
        # and on edits save (never read the file while holding _lock).
        self._names: Dict[int, str] = {}

        # Latest live artefacts (also persisted to disk on arrival).
        self._grid: Optional[OccupancyGrid] = None
        self._rooms: Optional[RoomScheme] = None
        self._current_map_id: Optional[str] = None

        self._map_image: Optional[bytes] = None    # latest map_rotate.png bytes
        self._vector: Optional[Dict[str, Any]] = None   # cached vector map
        self._vector_map_id: Optional[str] = None

        # Pub/sub for live push to WebSocket clients. Subscribers are called
        # from the bridge thread with an event dict {"type":..., "data":...};
        # the WS layer bounces each into its asyncio loop (thread-safe).
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
        self._sub_lock = threading.Lock()

        self.bridge = build_bridge(cfg)
        self.bridge.on_status = self._on_status
        self.bridge.on_map = self._on_map
        self.bridge.on_rooms = self._on_rooms
        self.bridge.on_map_image = self._on_map_image
        self.bridge.on_ack = self._on_ack
        self.bridge.on_online = self._on_online
        self.bridge.on_plan_map = self._on_plan_map
        self._plan_map: Optional[Dict[str, Any]] = None   # robot's live plan (cloud mode)

        self.ha = None
        if cfg.ha_enabled:
            from .ha_discovery import HaDiscovery
            self.ha = HaDiscovery(
                host=cfg.mqtt_host, port=cfg.mqtt_port,
                username=cfg.mqtt_user or None, password=cfg.mqtt_pass or None,
                node_id=cfg.ha_node_id, discovery_prefix=cfg.ha_discovery_prefix,
            )
            self.ha.on_command = self.command

        # Load any previously-persisted map so the UI has something at boot.
        self._load_last_map()

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.ha:
            log.info("starting Home Assistant MQTT discovery -> %s:%d",
                     self.cfg.mqtt_host, self.cfg.mqtt_port)
            self.ha.start()
        # Live transports only stream status from the robot. Seed the map +
        # room segmentation from the dumped map so the UI has the real house
        # (rooms, walls) with the live pose on top.
        if self.cfg.transport in ("tcp", "mqtt", "cloud"):
            self._seed_from_dump()
        log.info("starting bridge transport=%s", self.cfg.transport)
        self.bridge.start()

    def _seed_from_dump(self) -> None:
        d = os.path.expanduser(self.cfg.map_dir or "")
        if not d or not os.path.isdir(os.path.join(d, "maps")):
            log.info("no map dump at %r to seed from", d)
            return
        try:
            from .realmap import load_real_map
            m = load_real_map(d, self.cfg.occ_topic)
        except Exception as e:                       # noqa: BLE001
            log.warning("could not seed map from dump %s: %s", d, e)
            return
        occ = m["occ"]
        self._on_map({"map_id": m["map_id"], "resolution": occ["resolution"],
                      "width": occ["width"], "height": occ["height"],
                      "origin": occ["origin"], "data": occ["data"]})
        self._on_rooms({"map_id": m["map_id"], "scheme": m["rooms_scheme"]})
        if m.get("map_image_path"):
            try:
                with open(m["map_image_path"], "rb") as f:
                    self._on_map_image({"map_id": m["map_id"],
                                        "data_b64": base64.b64encode(f.read()).decode("ascii")})
            except OSError:
                pass
        log.info("seeded map %s + %d rooms from dump %s",
                 m["map_id"], len(self._rooms.rooms) if self._rooms else 0, d)

    def stop(self) -> None:
        self.bridge.stop()
        if self.ha:
            self.ha.stop()

    # --- command API (web UI + HA) ----------------------------------------
    def command(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
        """Send a command to the robot via the bridge. Returns command id."""
        args = args or {}
        log.info("command %s %s", cmd, args)
        # remember what kind of clean was asked for, to label the session
        if cmd in ("clean", "start", "clean_room"):
            mode = args.get("mode") or "vacuum"
            if cmd == "clean":
                rooms = args.get("rooms") or None
            elif cmd == "clean_room":
                rooms = [int(args.get("room_id", 0))]
            else:
                rooms = None
            with self._lock:
                self._pending_clean = {"mode": mode, "rooms": rooms}
        return self.bridge.send_command(cmd, args)

    # --- live pub/sub (for the WebSocket layer) ---------------------------
    def subscribe(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._sub_lock:
            self._subscribers.append(cb)

    def unsubscribe(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        with self._sub_lock:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

    def _broadcast(self, event_type: str, data: Any) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb({"type": event_type, "data": data})
            except Exception:   # never let one bad WS client break the bridge
                log.debug("subscriber callback failed", exc_info=True)

    # --- bridge callbacks --------------------------------------------------
    def _on_status(self, msg: Dict[str, Any]) -> None:
        with self._lock:
            self.state.online = True
            self.state.apply_status(msg)
            entry = self._track_session(self.state.state)
        self._push_ha()
        self._broadcast("status", self.snapshot_state())
        if entry:
            self._broadcast("history", entry)

    def _track_session(self, cur: str) -> Optional[Dict[str, Any]]:
        """Start/finish a cleaning session on state transitions. Caller holds _lock."""
        now = time.time()
        if cur == STATE_CLEANING and self._session is None:
            pc = self._pending_clean
            self._session = {
                "started_at": now,
                "mode": self.state.mode or "vacuum",   # module the robot reports (brush/mop)
                "rooms": pc.get("rooms"),
                "_area0": self.state.clean_area_m2,
                "_time0": self.state.clean_time_s,
            }
            return None
        if self._session is not None and cur in (STATE_DOCKED, STATE_IDLE):
            s, self._session = self._session, None
            self._pending_clean = {"mode": "vacuum", "rooms": None}
            area = self.state.clean_area_m2 - s["_area0"]
            if area <= 0:
                area = self.state.clean_area_m2
            dur = self.state.clean_time_s - s["_time0"]
            if dur <= 0:
                dur = now - s["started_at"]
            rooms = s["rooms"]
            names = None
            if rooms and self._rooms:
                byid = {r.id: self._names.get(r.id, r.name) for r in self._rooms.rooms}
                names = [byid.get(r, "Room %d" % r) for r in rooms]
            return self.history.add({
                "started_at": s["started_at"], "ended_at": now,
                "mode": s["mode"], "rooms": rooms, "room_names": names,
                "area_m2": round(max(0.0, area), 1),
                "duration_s": int(max(0, dur)),
                "completed": cur == STATE_DOCKED,
            })
        return None

    def _on_map(self, msg: Dict[str, Any]) -> None:
        try:
            grid = OccupancyGrid(
                map_id=msg.get("map_id", "map"),
                width=int(msg["width"]), height=int(msg["height"]),
                resolution=float(msg["resolution"]),
                origin=Pose(**(msg.get("origin") or {})),
                data=list(msg["data"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            log.warning("bad map message: %s", e)
            return
        with self._lock:
            self._grid = grid
            self._current_map_id = grid.map_id
            self._vector = None          # invalidate vector cache
        self._refresh_names(grid.map_id)
        path = self.mapstore.save_map(grid)
        log.info("map %s persisted (%dx%d) -> %s",
                 grid.map_id, grid.width, grid.height, path)
        self._broadcast("map", grid.to_dict())

    def _on_rooms(self, msg: Dict[str, Any]) -> None:
        map_id = msg.get("map_id") or self._current_map_id or "map"
        scheme_raw = msg.get("scheme") or {}
        scheme = RoomScheme.from_clean_scheme(map_id, scheme_raw)
        with self._lock:
            prev = self._rooms
            # The live robot sends its raw clean_scheme only (no segmentation
            # grid, placeholder names). Keep what the dump gave us so the vector
            # rooms + real names survive.
            if prev is not None:
                if not scheme.segmentation and prev.segmentation:
                    scheme.segmentation = prev.segmentation
                    scheme.map_id = prev.map_id
                prev_names = {r.id: r.name for r in prev.rooms}
                for r in scheme.rooms:
                    if r.name == "Room %d" % r.id and r.id in prev_names:
                        r.name = prev_names[r.id]
                if not scheme.rooms:
                    scheme.rooms = prev.rooms
            self._rooms = scheme
            self._vector = None          # invalidate vector cache
        self.mapstore.save_rooms(scheme)
        log.info("rooms for %s persisted (%d rooms)", map_id, len(scheme.rooms))
        self._broadcast("rooms", self.snapshot_rooms())

    def _on_map_image(self, msg: Dict[str, Any]) -> None:
        b64 = msg.get("data_b64")
        if not b64:
            return
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            log.warning("bad map_image: %s", e)
            return
        map_id = msg.get("map_id") or self._current_map_id or "map"
        with self._lock:
            self._map_image = raw
        # Persist as the robot's native filename alongside the grid.
        try:
            d = self.mapstore._map_dir(map_id)   # noqa: SLF001 (same package)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "map_rotate.png"), "wb") as f:
                f.write(raw)
        except OSError as e:
            log.warning("could not persist map_rotate.png: %s", e)
        # Tell WS clients a fresh image is available (they refetch the bytes).
        self._broadcast("map_image", {"map_id": map_id, "bytes": len(raw)})

    def _on_ack(self, msg: Dict[str, Any]) -> None:
        log.info("ack id=%s ok=%s detail=%s",
                 msg.get("id"), msg.get("ok"), msg.get("detail"))
        self._broadcast("ack", msg)

    def _on_online(self, online: bool) -> None:
        with self._lock:
            self.state.online = online
        log.info("bridge online=%s", online)
        self._push_ha()
        self._broadcast("status", self.snapshot_state())

    # --- helpers -----------------------------------------------------------
    def _push_ha(self) -> None:
        if self.ha:
            with self._lock:
                snapshot = RobotState(**{**self.state.__dict__})
            self.ha.publish_state(snapshot)

    def _load_last_map(self) -> None:
        mid = self.mapstore.latest_map_id()
        if not mid:
            return
        grid = self.mapstore.load_grid(mid)
        if grid:
            self._grid = grid
            self._current_map_id = mid
            log.info("loaded persisted map %s from disk", mid)
            self._refresh_names(mid)
        # Load a persisted robot map image if present.
        img = os.path.join(self.mapstore._map_dir(mid), "map_rotate.png")  # noqa: SLF001
        if os.path.exists(img):
            try:
                with open(img, "rb") as f:
                    self._map_image = f.read()
            except OSError:
                pass

    # --- read accessors for the web app -----------------------------------
    def snapshot_state(self) -> Dict[str, Any]:
        with self._lock:
            d = self.state.to_dict()
            d["current_map_id"] = self._current_map_id
            d["ha_connected"] = bool(self.ha and self.ha.connected)
            d["transport"] = self.cfg.transport
            return d

    def snapshot_map(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._grid.to_dict() if self._grid else None

    def snapshot_rooms(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._rooms:
                return None
            d = self._rooms.to_dict()
        for r in d["rooms"]:
            r["name"] = self._names.get(r["id"], r["name"])
        return d

    def _refresh_names(self, map_id: Optional[str] = None) -> None:
        try:
            rn = self.edits.load(map_id or self._current_map_id or "map").get("room_names") or {}
            self._names = {int(k): str(v) for k, v in rn.items() if str(v).strip()}
        except Exception:                            # noqa: BLE001
            log.debug("could not load room-name overrides", exc_info=True)

    def snapshot_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.history.list(limit)

    def map_image_bytes(self) -> Optional[bytes]:
        with self._lock:
            return self._map_image

    def snapshot_vector(self) -> Optional[Dict[str, Any]]:
        """
        Build (and cache) the vector map from the current occupancy grid + room
        segmentation. Returns None if there's no map yet, or {"error": ...} if
        the vector deps (opencv/shapely/numpy) aren't installed.
        """
        with self._lock:
            grid = self._grid
            rooms = self._rooms
            if grid is None:
                return None
            if self._vector is not None and self._vector_map_id == grid.map_id:
                return self._vector
            seg = rooms.segmentation if rooms else None
            rooms_meta = ([{"id": r.id, "name": self._names.get(r.id, r.name)} for r in rooms.rooms]
                          if rooms else [])
            map_id = grid.map_id
            occ = {
                "width": grid.width, "height": grid.height,
                "resolution": grid.resolution,
                "origin": {"x": grid.origin.x, "y": grid.origin.y},
                "data": grid.data,
            }
        if not seg or not seg.get("data"):
            return {"error": "no room segmentation to vectorize (mock map?)",
                    "map_id": map_id}
        try:
            from .vectorize import vectorize_map
        except ImportError as e:
            return {"error": "vector deps missing (%s); build the 3.11 image "
                             "or .venv311" % e, "map_id": map_id}
        try:
            vec = vectorize_map(occ, seg["data"], rooms_meta, map_id)
        except Exception as e:                       # noqa: BLE001
            log.warning("vectorize failed: %s", e)
            return {"error": "vectorize failed: %s" % e, "map_id": map_id}
        with self._lock:
            self._vector = vec
            self._vector_map_id = map_id
        return vec

    def current_map_id(self) -> str:
        with self._lock:
            return self._current_map_id or "map"

    # --- editor sidecar ----------------------------------------------------
    def load_edits(self, map_id: Optional[str] = None) -> Dict[str, Any]:
        return self.edits.load(map_id or self.current_map_id())

    def save_edits(self, edits: Dict[str, Any], map_id: Optional[str] = None) -> Dict[str, Any]:
        mid = map_id or edits.get("map_id") or self.current_map_id()
        saved = self.edits.save(mid, edits)
        self._refresh_names(mid)
        with self._lock:
            self._vector = None              # labels changed -> rebuild vector
        self._broadcast("edits", saved)
        rooms = self.snapshot_rooms()
        if rooms:
            self._broadcast("rooms", rooms)
        return saved

    # --- robot plan map (rooms grid + no-go zones), transport=cloud --------------
    def _plan_meta(self) -> Optional[Dict[str, Any]]:
        pm = self._plan_map
        if not pm or not pm.get("map_data"):
            return None
        md = pm["map_data"]
        return {k: md[k] for k in ("width", "height", "origin_x", "origin_y", "resolution")}

    def _room_grid_from_cells(self, cells: List[int]) -> Optional[List[int]]:
        """Robot room grid (seg frame) -> per-occupancy-cell room ids for display."""
        from .realmap import resample_rooms_to_occ
        meta = self._plan_meta()
        with self._lock:
            grid = self._grid
        if meta is None or grid is None:
            return None
        seg = {"width": meta["width"], "height": meta["height"], "resolution": meta["resolution"],
               "origin": {"x": meta["origin_x"], "y": meta["origin_y"]}, "bitmask": cells}
        occ = {"width": grid.width, "height": grid.height, "resolution": grid.resolution,
               "origin": {"x": grid.origin.x, "y": grid.origin.y}}
        return resample_rooms_to_occ(seg, occ)

    def _on_plan_map(self, pm: Dict[str, Any]) -> None:
        """The robot is the source of truth for rooms and no-go zones."""
        from . import planmap
        self._plan_map = pm
        md = pm.get("map_data") or {}
        try:
            cells = planmap.decode_room_layer(md["room_layer"])
        except Exception as e:                       # noqa: BLE001
            log.warning("bad room_layer from robot: %s", e); return
        room_grid = self._room_grid_from_cells(cells)
        co = pm.get("clean_order") or {}
        if room_grid is not None:
            with self._lock:
                grid = self._grid
                old = self._rooms
            scheme = RoomScheme.from_clean_scheme(grid.map_id, {
                "sweep_scheme": [{"room_id": co.get("sweep_default_order") or []}],
                "mop_scheme": [{"room_id": co.get("mop_default_order") or []}],
                "present_room_ids": planmap.present_rooms(cells),
                "segmentation": {"width": grid.width, "height": grid.height,
                                 "resolution": grid.resolution,
                                 "origin": {"x": grid.origin.x, "y": grid.origin.y},
                                 "data": room_grid},
            })
            if old is not None:
                scheme.raw = old.raw
            with self._lock:
                self._rooms = scheme
                self._vector = None
            self._broadcast("rooms", self.snapshot_rooms())
        # zones: mirror the robot's no-go list into the editor sidecar
        mid = self.current_map_id()
        ed = self.edits.load(mid)
        robot_zones = planmap.zones_from_forbidden(pm.get("user_forbidden") or {})
        if planmap.forbidden_from_zones(robot_zones) != planmap.forbidden_from_zones(ed.get("zones") or []):
            ed["zones_rev"] = int(ed.get("zones_rev") or 0) + 1   # robot list changed -> older editors are stale
        ed["zones"] = robot_zones
        saved = self.edits.save(mid, ed)
        self._broadcast("edits", saved)
        log.info("plan map from robot: rooms %s, %d no-go zones",
                 planmap.present_rooms(cells), len(ed["zones"]))

    def room_edit(self, op: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Compute a split/merge on the robot's live room grid (no side effects).
        Returns {cells, info, segmentation} -- segmentation is for the UI preview."""
        from . import planmap
        meta = self._plan_meta()
        if meta is None:
            raise planmap.EditError("no room map from the robot yet (is it connected?)")
        cells = planmap.decode_room_layer(self._plan_map["map_data"]["room_layer"])
        if op == "merge":
            a, b = (int(x) for x in args["rooms"])
            new_cells, info = planmap.merge_rooms(cells, meta, a, b)
        elif op == "split":
            (x1, y1), (x2, y2) = args["line"]
            new_cells, info = planmap.split_room(cells, meta, (x1, y1), (x2, y2))
        elif op == "delete":
            new_cells, info = planmap.delete_room(cells, meta, int(args["room"]))
        else:
            raise planmap.EditError("unknown op %r" % op)
        return {"cells": new_cells, "info": info,
                "segmentation": self._room_grid_from_cells(new_cells)}

    def remap_names_after_edit(self, info: Dict[str, Any]) -> None:
        """Ids are reused by the robot: drop the name of a removed room and make a
        freshly split room start unnamed (its id may have belonged to another)."""
        mid = self.current_map_id()
        ed = self.edits.load(mid)
        names = dict(ed.get("room_names") or {})
        if info.get("op") in ("merge", "delete"):
            names.pop(str(info["removed"]), None)
        elif info.get("op") == "split":
            names.pop(str(info["new"]), None)
        ed["room_names"] = names
        self.save_edits(ed, mid)

    def list_maps(self):
        return self.mapstore.list_maps()
