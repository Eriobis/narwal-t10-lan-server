"""
Bridge abstraction: the link between the LAN server and the on-robot
robot_server.py (or, for development, a simulated robot).

================================  WIRE PROTOCOL  ===========================

The LAN server and the on-robot bridge exchange newline-free JSON objects.
The *same* JSON payloads are used whether the transport is MQTT (preferred)
or a stdlib JSON-over-TCP socket (fallback for a dependency-starved Python 2
robot). This keeps robot_server.py and the server implementations in lockstep.

Robot -> server messages
------------------------
  {"type":"status", "ts":<float>,
   "state":"cleaning|docked|returning|paused|idle|error",
   "battery":0..100, "fan_speed":"standard", "error":null|"...",
   "pose":{"x":..,"y":..,"theta":..},
   "clean_area_m2":..,"clean_time_s":..}

  {"type":"map", "ts":<float>, "map_id":"...",
   "resolution":0.05, "width":W, "height":H,
   "origin":{"x":..,"y":..,"theta":..},
   "data":[ -1|0|100, ... ]}            # row-major, length W*H

  {"type":"rooms", "ts":<float>, "map_id":"...",
   "scheme": <robot's clean_scheme/info JSON verbatim>}

  {"type":"map_image", "ts":<float>, "map_id":"...",
   "format":"png", "data_b64":"<base64 of map_rotate.png>"}
   # The robot's OWN rendered map (maphub writes map_rotate.png to
   # /userdata/usr/data/maps/ during/after a cleaning task). Forwarded verbatim.

  {"type":"ack", "id":"<command id>", "ok":true|false, "detail":"..."}

Server -> robot messages
------------------------
  {"type":"command", "id":"<uuid>", "cmd":"start|stop|pause|resume|dock|
                                          locate|clean_room|set_fan_speed",
   "args":{...}}       # e.g. {"room_id":3} or {"fan_speed":"strong"}

MQTT topic layout (transport = mqtt)
------------------------------------
  <prefix>/status      (retained)   robot -> server
  <prefix>/map         (retained)   robot -> server
  <prefix>/rooms       (retained)   robot -> server
  <prefix>/map_image   (retained)   robot -> server
  <prefix>/ack                      robot -> server
  <prefix>/command                  server -> robot
  <prefix>/online      (retained, LWT)  robot availability ("online"/"offline")
  default prefix: "narwal/bridge"

TCP transport (transport = tcp)
-------------------------------
  robot_server.py runs a TCP server; the LAN server connects as a client.
  One newline-delimited JSON object per line, bidirectional on one socket.
===========================================================================
"""

from __future__ import annotations

import abc
from typing import Any, Callable, Dict, Optional

# Callback signatures the server registers on a bridge.
StatusCb = Callable[[Dict[str, Any]], None]
MapCb = Callable[[Dict[str, Any]], None]
RoomsCb = Callable[[Dict[str, Any]], None]
MapImageCb = Callable[[Dict[str, Any]], None]
AckCb = Callable[[Dict[str, Any]], None]
OnlineCb = Callable[[bool], None]


class Bridge(abc.ABC):
    """
    A transport-agnostic connection to the robot.

    The server sets the on_* callbacks, calls start(), then issues commands
    with send_command(). Implementations: MockBridge, MqttBridge, TcpBridge.
    """

    def __init__(self) -> None:
        self.on_status: Optional[StatusCb] = None
        self.on_map: Optional[MapCb] = None
        self.on_rooms: Optional[RoomsCb] = None
        self.on_map_image: Optional[MapImageCb] = None
        self.on_ack: Optional[AckCb] = None
        self.on_online: Optional[OnlineCb] = None
        self.on_plan_map: Optional[Callable[[Dict[str, Any]], None]] = None

    # --- lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def start(self) -> None:
        """Begin the connection / background thread. Non-blocking."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Tear down cleanly."""

    # --- outbound ----------------------------------------------------------
    @abc.abstractmethod
    def send_command(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
        """
        Send a command to the robot. Returns the generated command id.
        Must be safe to call from any thread.
        """

    # --- helpers for subclasses -------------------------------------------
    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """Route a decoded robot->server message to the right callback."""
        mtype = msg.get("type")
        if mtype == "status" and self.on_status:
            self.on_status(msg)
        elif mtype == "map" and self.on_map:
            self.on_map(msg)
        elif mtype == "rooms" and self.on_rooms:
            self.on_rooms(msg)
        elif mtype == "map_image" and self.on_map_image:
            self.on_map_image(msg)
        elif mtype == "ack" and self.on_ack:
            self.on_ack(msg)
        elif mtype == "plan_map" and self.on_plan_map:
            self.on_plan_map(msg.get("data") or {})

    def _set_online(self, online: bool) -> None:
        if self.on_online:
            self.on_online(online)
