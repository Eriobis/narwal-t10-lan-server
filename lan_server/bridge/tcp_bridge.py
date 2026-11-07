"""
TcpBridge -- connects the LAN server to a real robot_server.py over a plain
newline-delimited JSON TCP socket.

This is the *fallback* transport for when the Python 2 robot has no usable MQTT
library (limited third-party libs on the robot's Tina Linux / Python 2.7). The
robot runs the TCP server; the LAN server connects as a client and reconnects
automatically. Payloads are identical to the MQTT transport (see base.py).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .base import Bridge


class TcpBridge(Bridge):
    def __init__(self, host: str, port: int = 8765) -> None:
        super().__init__()
        self._host, self._port = host, port
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tcp-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread:
            self._thread.join(timeout=2.0)

    def send_command(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
        cid = uuid.uuid4().hex
        line = json.dumps({"type": "command", "id": cid, "cmd": cmd, "args": args or {}})
        with self._lock:
            if self._sock:
                try:
                    self._sock.sendall((line + "\n").encode("utf-8"))
                except OSError:
                    self._close()
        return cid

    # --- internals ---------------------------------------------------------
    def _run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            if self._sock is None:
                if not self._connect():
                    time.sleep(2.0)
                    continue
                buf = b""
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                self._close()
                continue
            if not chunk:
                self._close()
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle_line(line)

    def _connect(self) -> bool:
        try:
            s = socket.create_connection((self._host, self._port), timeout=5)
            s.settimeout(None)
            with self._lock:
                self._sock = s
            self._set_online(True)
            return True
        except OSError:
            return False

    def _close(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        self._set_online(False)

    def _handle_line(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, TypeError):
            return
        if isinstance(msg, dict):
            self._dispatch(msg)
