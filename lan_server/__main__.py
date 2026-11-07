"""
Entry point: `python -m lan_server [options]`.

Starts the bridge (mock/mqtt/tcp), optional HA MQTT discovery, and the FastAPI
app (REST + WebSocket) under uvicorn. The built React UI (frontend/dist) is
served from the same port. Ctrl-C stops everything.
"""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import parse_args
from .server import NarwalServer


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    log = logging.getLogger("narwal")

    cfg = parse_args(argv)
    server = NarwalServer(cfg)
    server.start()

    app = create_app(server)   # bridge/HA are stopped via the app's lifespan

    log.info("web UI  -> http://%s:%d/  (transport=%s, HA=%s)",
             cfg.web_host, cfg.web_port, cfg.transport,
             "on" if cfg.ha_enabled else "off")

    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
