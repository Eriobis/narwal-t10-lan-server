"""Bridge transports between the LAN server and the robot."""

from .base import Bridge
from .mock import MockBridge

__all__ = ["Bridge", "MockBridge", "build_bridge"]


def build_bridge(cfg):
    """
    Factory: construct the right Bridge from config.transport.

    mqtt / tcp are imported lazily so the mock works even if paho-mqtt is not
    installed.
    """
    t = cfg.transport
    if t == "mock":
        return MockBridge(tick=cfg.mock_tick)
    if t == "real":
        from ..realmap import RealMapBridge
        return RealMapBridge(map_dir=cfg.map_dir, occ_topic=cfg.occ_topic)
    if t == "mqtt":
        from .mqtt_bridge import MqttBridge
        return MqttBridge(
            host=cfg.mqtt_host,
            port=cfg.mqtt_port,
            username=cfg.mqtt_user,
            password=cfg.mqtt_pass,
            prefix=cfg.bridge_prefix,
        )
    if t == "tcp":
        from .tcp_bridge import TcpBridge
        return TcpBridge(host=cfg.robot_host, port=cfg.robot_port)
    if t == "cloud":
        from .cloud import CloudBridge
        return CloudBridge(data_dir=cfg.data_dir, map_dir=cfg.map_dir)
    raise ValueError("unknown transport: %r" % (t,))
