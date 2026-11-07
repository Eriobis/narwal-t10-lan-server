"""
MqttBridge -- connects the LAN server to a real robot_server.py over MQTT.

This is the preferred transport: the homelab already runs an MQTT broker for
Home Assistant / Zigbee2MQTT, so the robot bridge and the LAN server simply
meet on it. See bridge/base.py for the topic layout and payloads.

Uses paho-mqtt 2.x (CallbackAPIVersion.VERSION2).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt

from .base import Bridge


class MqttBridge(Bridge):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        prefix: str = "narwal/bridge",
        client_id: str = "narwal-lan-server",
    ) -> None:
        super().__init__()
        self._prefix = prefix.rstrip("/")
        self._cmd_topic = self._prefix + "/command"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
        )
        if username:
            self._client.username_pw_set(username, password or "")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._host, self._port = host, port

    def start(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass

    def send_command(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
        cid = uuid.uuid4().hex
        payload = {"type": "command", "id": cid, "cmd": cmd, "args": args or {}}
        self._client.publish(self._cmd_topic, json.dumps(payload), qos=1)
        return cid

    # --- paho callbacks ----------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # Subscribe to everything the robot publishes.
        for leaf in ("status", "map", "rooms", "map_image", "ack", "online"):
            client.subscribe(self._prefix + "/" + leaf, qos=1)

    def _on_message(self, client, userdata, message):
        topic = message.topic
        try:
            raw = message.payload.decode("utf-8")
        except Exception:
            return
        if topic.endswith("/online"):
            self._set_online(raw.strip().lower() == "online")
            return
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(msg, dict):
            # Topic implies type if the payload omits it.
            msg.setdefault("type", topic.rsplit("/", 1)[-1])
            self._dispatch(msg)
