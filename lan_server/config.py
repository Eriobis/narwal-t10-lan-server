"""Configuration for the Narwal T10 LAN server (CLI flags + env vars)."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass


def _env(name: str, default):
    return os.environ.get(name, default)


@dataclass
class Config:
    # Bridge transport to the robot: mock | real | mqtt | tcp | cloud (LAN fake-cloud)
    transport: str = "mock"

    # Real map (transport=real): directory of a map dumped from the robot.
    map_dir: str = "~/narwal-re/captures/house-map-20261102"
    occ_topic: str = "static_map"

    # Web server
    web_host: str = "0.0.0.0"
    web_port: int = 8099

    # Where captured maps are persisted
    data_dir: str = "./data/maps"

    # Home Assistant MQTT discovery
    ha_enabled: bool = False
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    ha_node_id: str = "narwal_t10"
    ha_discovery_prefix: str = "homeassistant"

    # Bridge (robot) MQTT namespace (transport=mqtt)
    bridge_prefix: str = "narwal/bridge"

    # Robot TCP endpoint (transport=tcp)
    robot_host: str = ""
    robot_port: int = 8765

    # Mock options
    mock_tick: float = 1.0


def parse_args(argv=None) -> Config:
    p = argparse.ArgumentParser(
        prog="narwal-lan-server",
        description="Local-network control & map server for a de-clouded Narwal T10.",
    )
    p.add_argument("--transport", choices=["mock", "real", "mqtt", "tcp", "cloud"],
                   default=_env("NARWAL_TRANSPORT", "mock"),
                   help="how to source robot data (default: mock; "
                        "'real' loads a dumped map from --map-dir)")
    p.add_argument("--map-dir", default=_env("NARWAL_MAP_DIR",
                   "~/narwal-re/captures/house-map-20261102"),
                   help="directory of a dumped robot map (transport=real)")
    p.add_argument("--occ-topic", default=_env("NARWAL_OCC_TOPIC", "static_map"),
                   help="OccupancyGrid topic to use from map.bag")
    p.add_argument("--web-host", default=_env("NARWAL_WEB_HOST", "0.0.0.0"))
    p.add_argument("--web-port", type=int, default=int(_env("NARWAL_WEB_PORT", "8099")))
    p.add_argument("--data-dir", default=_env("NARWAL_DATA_DIR", "./data/maps"))

    # HA / MQTT
    p.add_argument("--ha", action="store_true",
                   default=_env("NARWAL_HA", "") not in ("", "0", "false"),
                   help="enable Home Assistant MQTT discovery")
    p.add_argument("--mqtt-host", default=_env("NARWAL_MQTT_HOST", "127.0.0.1"))
    p.add_argument("--mqtt-port", type=int, default=int(_env("NARWAL_MQTT_PORT", "1883")))
    p.add_argument("--mqtt-user", default=_env("NARWAL_MQTT_USER", ""))
    p.add_argument("--mqtt-pass", default=_env("NARWAL_MQTT_PASS", ""))
    p.add_argument("--ha-node-id", default=_env("NARWAL_HA_NODE", "narwal_t10"))
    p.add_argument("--bridge-prefix", default=_env("NARWAL_BRIDGE_PREFIX", "narwal/bridge"))

    # TCP transport
    p.add_argument("--robot-host", default=_env("NARWAL_ROBOT_HOST", ""),
                   help="robot IP for transport=tcp")
    p.add_argument("--robot-port", type=int, default=int(_env("NARWAL_ROBOT_PORT", "8765")))

    p.add_argument("--mock-tick", type=float, default=float(_env("NARWAL_MOCK_TICK", "1.0")))

    a = p.parse_args(argv)
    return Config(
        transport=a.transport, map_dir=a.map_dir, occ_topic=a.occ_topic,
        web_host=a.web_host, web_port=a.web_port, data_dir=a.data_dir,
        ha_enabled=a.ha, mqtt_host=a.mqtt_host, mqtt_port=a.mqtt_port,
        mqtt_user=a.mqtt_user, mqtt_pass=a.mqtt_pass,
        ha_node_id=a.ha_node_id, ha_discovery_prefix="homeassistant",
        bridge_prefix=a.bridge_prefix,
        robot_host=a.robot_host, robot_port=a.robot_port,
        mock_tick=a.mock_tick,
    )
