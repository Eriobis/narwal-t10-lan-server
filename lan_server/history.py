"""
Cleaning-session history store.

Persists completed cleaning sessions to a small JSON file in the data dir, so
the record of "previously done tasks" survives restarts and going off-cloud.
Each entry:
    {id, started_at, ended_at, mode, rooms, room_names, area_m2,
     duration_s, completed}

Thread-safe (the bridge/status thread appends; the API reads).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Any, Dict, List

_MAX = 200   # keep the last N sessions


class HistoryStore:
    def __init__(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, "history.json")
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                data = json.load(f)
            if isinstance(data, list):
                self._items = data[-_MAX:]
        except (OSError, ValueError):
            self._items = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._items, f)
            os.replace(tmp, self._path)
        except OSError:
            pass

    def add(self, session: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            session.setdefault("id", uuid.uuid4().hex)
            self._items.append(session)
            self._items = self._items[-_MAX:]
            self._save()
            return session

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = self._items[-limit:] if limit else list(self._items)
            return list(reversed(items))   # newest first
