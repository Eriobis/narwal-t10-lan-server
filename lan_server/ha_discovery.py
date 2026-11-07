"""
Home Assistant MQTT discovery for the Narwal T10.

Publishes HA discovery configs so a vacuum entity + a few sensors appear in
Home Assistant automatically (MQTT integration with discovery enabled). The
LAN server then publishes state to those topics and relays HA's command
payloads back into a callback (which the server routes to the bridge).

This is the HA-facing side; it is independent of which bridge transport the
robot uses. It needs an MQTT broker (the homelab's existing one). If the broker
is unreachable the server still runs (web UI works); HA discovery just retries.

Entity: a vacuum using the MQTT "state" schema.
  https://www.home-assistant.io/integrations/vacuum.mqtt/
Supported: start, stop, pause, return_home, locate, fan_speed, battery, status,
and send_command (used for room cleaning: "clean_room <id>").
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt

from .model import (
    RobotState,
    STATE_CLEANING, STATE_DOCKED, STATE_PAUSED,
    STATE_IDLE, STATE_RETURNING, STATE_ERROR,
)

# HA command callback: (cmd, args) -> None
CommandCb = Callable[[str, Dict[str, Any]], None]

FAN_SPEEDS = ["quiet", "standard", "strong", "max"]  # TODO: confirm robot names


class HaDiscovery:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        node_id: str = "narwal_t10",
        discovery_prefix: str = "homeassistant",
        state_prefix: str = "narwal/ha",
        name: str = "Narwal T10",
    ) -> None:
        self._node = node_id
        self._disc = discovery_prefix.rstrip("/")
        self._sp = state_prefix.rstrip("/")
        self._name = name
        self.on_command: Optional[CommandCb] = None

        # Topics
        self.t_state = self._sp + "/state"           # vacuum JSON state
        self.t_avail = self._sp + "/availability"
        self.t_cmd = self._sp + "/command"           # HA -> us (start/stop/...)
        self.t_fan = self._sp + "/fan_speed/set"
        self.t_send = self._sp + "/send_command"     # HA -> us (free-form)
        self.t_attr = self._sp + "/attributes"       # extra attributes JSON

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="narwal-ha-" + node_id,
            clean_session=True,
        )
        if username:
            self._client.username_pw_set(username, password or "")
        self._client.will_set(self.t_avail, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._host, self._port = host, port
        self._connected = False

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.publish(self.t_avail, "offline", qos=1, retain=True)
        except Exception:
            pass
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected

    # --- paho callbacks ----------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = True
        self._publish_discovery()
        client.publish(self.t_avail, "online", qos=1, retain=True)
        for t in (self.t_cmd, self.t_fan, self.t_send):
            client.subscribe(t, qos=1)

    def _on_message(self, client, userdata, message):
        if not self.on_command:
            return
        try:
            payload = message.payload.decode("utf-8").strip()
        except Exception:
            return
        topic = message.topic

        if topic == self.t_cmd:
            # HA sends the configured payload strings (we set them below).
            mapping = {
                "start": ("start", {}),
                "pause": ("pause", {}),
                "stop": ("stop", {}),
                "return_to_base": ("dock", {}),
                "locate": ("locate", {}),
                "clean_spot": ("start", {}),
            }
            if payload in mapping:
                cmd, args = mapping[payload]
                self.on_command(cmd, args)
        elif topic == self.t_fan:
            self.on_command("set_fan_speed", {"fan_speed": payload})
        elif topic == self.t_send:
            # Free-form; used for room cleaning: "clean_room 3" or JSON.
            self._handle_send_command(payload)

    def _handle_send_command(self, payload: str) -> None:
        cmd, args = None, {}
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict) and "cmd" in obj:
                cmd = obj["cmd"]
                args = obj.get("args", {}) or {}
        except (ValueError, TypeError):
            parts = payload.split()
            if parts:
                cmd = parts[0]
                if cmd == "clean_room" and len(parts) > 1:
                    args = {"room_id": int(parts[1])}
        if cmd and self.on_command:
            self.on_command(cmd, args)

    # --- discovery ---------------------------------------------------------
    def _device_block(self) -> Dict[str, Any]:
        return {
            "identifiers": [self._node],
            "name": self._name,
            "manufacturer": "Narwal",
            "model": "T10 (de-clouded)",
            "sw_version": "narwal-lan-server",
        }

    def _publish_discovery(self) -> None:
        dev = self._device_block()
        avail = [{"topic": self.t_avail}]

        # --- vacuum entity ---
        vac_cfg = {
            "schema": "state",
            "name": None,  # use device name
            "unique_id": self._node + "_vacuum",
            "device": dev,
            "availability": avail,
            "supported_features": [
                "start", "stop", "pause", "return_home",
                "battery", "status", "locate", "fan_speed", "send_command",
            ],
            "command_topic": self.t_cmd,
            "payload_start": "start",
            "payload_stop": "stop",
            "payload_pause": "pause",
            "payload_return_to_base": "return_to_base",
            "payload_locate": "locate",
            "payload_clean_spot": "clean_spot",
            "send_command_topic": self.t_send,
            "set_fan_speed_topic": self.t_fan,
            "fan_speed_list": FAN_SPEEDS,
            "state_topic": self.t_state,
            "json_attributes_topic": self.t_attr,
        }
        self._client.publish(
            "%s/vacuum/%s/config" % (self._disc, self._node),
            json.dumps(vac_cfg), qos=1, retain=True,
        )

        # --- extra sensors (read from the attributes topic) ---
        sensors = [
            ("clean_area", "Clean area", "area", "m²", "{{ value_json.clean_area_m2 }}"),
            ("clean_time", "Clean time", "duration", "s", "{{ value_json.clean_time_s }}"),
            ("status_text", "Status", None, None, "{{ value_json.status }}"),
        ]
        for sid, sname, dev_class, unit, tmpl in sensors:
            cfg = {
                "name": sname,
                "unique_id": "%s_%s" % (self._node, sid),
                "device": dev,
                "availability": avail,
                "state_topic": self.t_attr,
                "value_template": tmpl,
            }
            if dev_class:
                cfg["device_class"] = dev_class
            if unit:
                cfg["unit_of_measurement"] = unit
            self._client.publish(
                "%s/sensor/%s/%s/config" % (self._disc, self._node, sid),
                json.dumps(cfg), qos=1, retain=True,
            )

    # --- state publishing --------------------------------------------------
    def publish_state(self, st: RobotState) -> None:
        if not self._connected:
            return
        ha_state = st.state if st.state in (
            STATE_CLEANING, STATE_DOCKED, STATE_PAUSED,
            STATE_IDLE, STATE_RETURNING, STATE_ERROR,
        ) else STATE_IDLE
        state_doc = {
            "state": ha_state,
            "battery_level": st.battery,
            "fan_speed": st.fan_speed,
        }
        self._client.publish(self.t_state, json.dumps(state_doc), qos=1, retain=True)
        self._client.publish(self.t_avail, "online" if st.online else "offline",
                             qos=1, retain=True)
        attrs = {
            "clean_area_m2": st.clean_area_m2,
            "clean_time_s": st.clean_time_s,
            "status": st.error or st.state,
            "pose_x": st.pose.x,
            "pose_y": st.pose.y,
            "error": st.error,
        }
        self._client.publish(self.t_attr, json.dumps(attrs), qos=1, retain=True)
