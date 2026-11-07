"""
CloudBridge -- the LAN server impersonates the Narwal cloud.

The robot's cloud bridge (pita_communicator) dials a WebSocket and calls an
HTTP API whose URLs come from two config files on the robot
(conf/websocket_env_us.json, conf/http_env_us.json). Point them at this server
(toolkit/lan-cloud-on.sh) and the robot connects HERE instead of narwaltech.com,
with its whole stack (cleaning, docking, scheduling, maps) untouched.

Protocol (decoded from the robot's own debug logs, 2026-11-03):
  server -> robot  {"request_id","machine_id","service","params"}
  robot  -> server {"code","machine_id","request_id","result","service","type","version"}

  /pita_clean_system/command  {cmd:3, clean_order:[ids], floor_type:[]}  start rooms
                              {cmd:3}  toggle pause/resume
                              {cmd:4}  stop        {cmd:1}  dock (TOGGLE: undocks if docked!)
  /pita/config/set_sweep_strength {cmd:10, name:"silent_mode|normal_mode|powerful_mode|super_powerful_mode"}
  /pita/info/all {}           status snapshot
  app_net_flag {state:true}   "an app is watching" -> robot broadcasts /pita/map
                              (live x,y,theta in the map frame) + /pita/info

robot_status: 100 idle, 300 cleaning, 301 paused, 600 returning/charging,
700 leaving dock, 800 cleaning sub-state.
Encryption is turned OFF by lan-cloud-on.sh (decrypt_incoming_messages /
encrypt_output_messages = false), so frames are plain JSON.

Threading: FastAPI runs the robot WebSocket on its asyncio loop; the rest of the
server is thread-based. send_command() hops onto the loop with
run_coroutine_threadsafe; robot messages are dispatched synchronously.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from .base import Bridge

log = logging.getLogger("narwal.cloud")

FAN_TO_ROBOT = {"quiet": "silent_mode", "standard": "normal_mode",
                "strong": "powerful_mode", "max": "super_powerful_mode"}
ROBOT_TO_FAN = {v: k for k, v in FAN_TO_ROBOT.items()}

CMD_START_OR_TOGGLE = 3
CMD_STOP = 4
CMD_DOCK_TOGGLE = 1

_OK = {"code": 0, "err_code": 10000, "msg": "ok"}


class CloudBridge(Bridge):
    def __init__(self, data_dir: str, map_dir: str = "") -> None:
        super().__init__()
        self._data_dir = data_dir
        self._map_dir = os.path.expanduser(map_dir or "")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None                       # starlette WebSocket of the robot
        self.machine_id: Optional[str] = None
        self._pending: Dict[str, str] = {}    # request_id -> our command id
        self._waiters: Dict[str, "asyncio.Future"] = {}   # request_id -> reply future
        self._info: Dict[str, Any] = {}       # last /pita/info(/all) result
        self._pose: Optional[Dict[str, float]] = None
        # dock pose from the dumped map: shown until the robot broadcasts a live
        # pose (it only sends /pita/map while it is moving)
        self._dock_pose: Optional[Dict[str, float]] = None
        try:
            with open(os.path.join(self._map_dir, "maps", "base_pose.pose")) as f:
                x, y, th = (float(v) for v in f.read().split()[:3])
                self._dock_pose = {"x": x, "y": y, "theta": th}
        except (OSError, ValueError):
            pass
        self._last_robot_msg = 0.0
        self._log_path = os.path.join(data_dir, "cloud-traffic.log")
        self._kick: Optional[asyncio.Event] = None   # wakes the poller after a command
        self._fast_until = 0.0                        # poll at 1 Hz until this time

    # --- Bridge lifecycle ------------------------------------------------
    UPLOAD_TLS_PORT = 8443

    def start(self) -> None:
        self._set_online(False)      # online once the robot dials in
        self._start_upload_tls()

    def _start_upload_tls(self) -> None:
        """The robot's AwsUpload ALWAYS uses Poco HTTPSClientSession(host, port), even
        for an http:// URL, so uploads need a TLS listener. Self-signed cert from
        <data_dir>/tls/{cert,key}.pem; skipped (with a log line) if absent."""
        import ssl, threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        cert = os.path.join(self._data_dir, "tls", "cert.pem")
        key = os.path.join(self._data_dir, "tls", "key.pem")
        if not (os.path.exists(cert) and os.path.exists(key)):
            log.info("no %s -> robot uploads disabled (they need TLS)", cert)
            return
        bridge = self

        class H(BaseHTTPRequestHandler):
            # HTTP/1.1 so "Expect: 100-continue" gets its 100 reply (HTTP/1.0 makes
            # the robot wait for a go-ahead that never comes, then time out).
            protocol_version = "HTTP/1.1"

            def _recv(self):
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    data = b""
                    while True:
                        n = int(self.rfile.readline().strip() or b"0", 16)
                        if n == 0:
                            self.rfile.readline(); break
                        data += self.rfile.read(n); self.rfile.readline()
                    return data
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _store(self):
                log.info("upload-tls %s %s (len=%s, expect=%s)", self.command, self.path,
                         self.headers.get("Content-Length"), self.headers.get("Expect"))
                body = self._recv()
                rel = self.path.split("?")[0]
                rel = rel[len("/cloud/upload/"):] if rel.startswith("/cloud/upload/") else rel.lstrip("/")
                bridge.save_upload(rel, body)
                self.send_response(200); self.send_header("ETag", '"lan"')
                self.send_header("Content-Length", "0"); self.end_headers()

            do_PUT = _store
            do_POST = _store

            def log_message(self, fmt, *a):
                log.debug("upload-tls: " + fmt, *a)

        try:
            class S(ThreadingHTTPServer):
                # TLS handshakes happen inside accept(); socketserver silently drops
                # the OSError, so log them -- that's where cert/protocol problems show.
                def get_request(self):
                    try:
                        conn, addr = self.socket.accept()
                    except OSError as e:
                        log.warning("upload-tls handshake failed: %s", e)
                        bridge._trace("TLS-FAIL", str(e))
                        raise
                    log.info("upload-tls connection from %s (%s)", addr[0],
                             getattr(conn, "version", lambda: "?")())
                    return conn, addr

            srv = S(("0.0.0.0", self.UPLOAD_TLS_PORT), H)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1   # the robot's 2020-era OpenSSL
            try:
                ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            except ssl.SSLError:
                pass
            ctx.load_cert_chain(cert, key)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            threading.Thread(target=srv.serve_forever, name="upload-tls", daemon=True).start()
            log.info("robot upload endpoint: https://0.0.0.0:%d/cloud/upload/", self.UPLOAD_TLS_PORT)
        except Exception as e:                       # noqa: BLE001
            log.warning("upload TLS listener failed: %s", e)

    def stop(self) -> None:
        pass

    # --- traffic log (everything, for RE) ----------------------------------
    def _trace(self, direction: str, what: Any) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            # keep the RE log bounded: rotate once at 5 MB (one .1 kept)
            if os.path.exists(self._log_path) and os.path.getsize(self._log_path) > 5_000_000:
                os.replace(self._log_path, self._log_path + ".1")
            with open(self._log_path, "a") as f:
                s = what if isinstance(what, str) else json.dumps(what, separators=(",", ":"))
                f.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), direction, s[:4000]))
        except OSError:
            pass

    # --- robot WebSocket (called from api.py) ------------------------------
    async def handle_ws(self, ws) -> None:
        await ws.accept()
        self._loop = asyncio.get_running_loop()
        self._kick = asyncio.Event()
        self._ws = ws
        log.info("robot connected to fake cloud (headers: %s)", dict(ws.headers))
        self._trace("CONNECT", {"headers": dict(ws.headers), "query": str(ws.url.query)})
        self._set_online(True)
        poller = asyncio.ensure_future(self._poll_loop())
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                raw = msg.get("text") if msg.get("text") is not None else msg.get("bytes")
                if raw is None:
                    continue
                self._on_robot_frame(raw)
        except Exception as e:                      # noqa: BLE001
            log.info("robot websocket closed: %s", e)
        finally:
            poller.cancel()
            if self._ws is ws:
                self._ws = None
                self._set_online(False)
            self._trace("DISCONNECT", "")

    async def _send_obj(self, obj: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("robot not connected")
        text = json.dumps(obj, separators=(",", ":"))
        self._trace(">>", text)
        await ws.send_text(text)

    def _request(self, service: str, params: Optional[Dict[str, Any]] = None) -> str:
        rid = str(uuid.uuid4())
        obj = {"request_id": rid, "machine_id": self.machine_id or "",
               "service": service, "params": params or {}}
        if self._loop is None or self._ws is None:
            raise RuntimeError("robot not connected to the LAN cloud")
        if self._in_loop():                  # called from an async route -> don't block
            asyncio.ensure_future(self._send_obj(obj))
        else:
            asyncio.run_coroutine_threadsafe(self._send_obj(obj), self._loop).result(5)
        return rid

    def _in_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    async def call(self, service: str, params: Optional[Dict[str, Any]] = None,
                   timeout: float = 15.0) -> Dict[str, Any]:
        """Send a request to the robot and await its full reply (run on the loop)."""
        if self._ws is None or self._loop is None:
            raise RuntimeError("robot not connected to the LAN cloud")
        rid = str(uuid.uuid4())
        fut = self._loop.create_future(); self._waiters[rid] = fut
        await self._send_obj({"request_id": rid, "machine_id": self.machine_id or "",
                              "service": service, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(rid, None)

    PLAN_OPS = ["map_data", "user_forbidden", "room_attr", "clean_order", "humidity"]

    async def fetch_plan_map(self) -> Dict[str, Any]:
        """Pull the robot's live plan (rooms grid, no-go zones, order) and publish it."""
        rep = await self.call("/pita/plan_map/get", {"operations": self.PLAN_OPS}, 20)
        res = rep.get("result") or {}
        if rep.get("code") == 0 and res.get("map_data"):
            self._plan_map_id = res.get("map_id")
            self._dispatch({"type": "plan_map", "data": res})
        return res

    async def set_plan_map(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """plan_map/set, then re-read the robot's processed result (it re-smooths
        room borders and may renumber) so the server mirrors what it really has."""
        params = dict(payload)
        params.setdefault("map_id", getattr(self, "_plan_map_id", None) or "")
        rep = await self.call("/pita/plan_map/set", params, 20)
        ok = rep.get("code") == 0 and (rep.get("result") or {}).get("msg", "ok") == "ok"
        if ok:
            await asyncio.sleep(2.5)
            await self.fetch_plan_map()
        return {"ok": ok, "reply": rep}

    async def _poll_loop(self) -> None:
        # tell the robot an "app" is watching -> it broadcasts /pita/map + /pita/info
        await asyncio.sleep(1.0)
        try:
            await self.fetch_plan_map()            # rooms + zones straight from the robot
        except Exception as e:                       # noqa: BLE001
            log.warning("plan_map/get failed: %s", e)
        while self._ws is not None:
            try:
                await self._send_obj({"result_type": "", "service": "app_net_flag",
                                      "params": {"state": True}, "device_id": "",
                                      "request_id": "", "machine_id": self.machine_id or ""})
                for _ in range(60):          # re-assert the flag every ~minute
                    await self._send_obj({"request_id": str(uuid.uuid4()),
                                          "machine_id": self.machine_id or "",
                                          "service": "/pita/info/all", "params": {}})
                    active = self._info.get("robot_status") not in (None, 100) \
                        or self._info.get("main_task") not in (None, 0) \
                        or time.time() < self._fast_until
                    # the robot streams /pita/map only while polled at ~1 Hz;
                    # a command kicks an immediate re-poll so state is never stale
                    self._kick.clear()
                    try:
                        await asyncio.wait_for(self._kick.wait(), 1.0 if active else 5.0)
                        await asyncio.sleep(0.4)     # let the robot apply the command
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                return
            except Exception as e:                   # noqa: BLE001
                log.debug("poll error: %s", e)
                await asyncio.sleep(3.0)

    # --- inbound from robot -------------------------------------------------
    def _on_robot_frame(self, raw) -> None:
        self._last_robot_msg = time.time()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            d = json.loads(raw)
        except ValueError:
            self._trace("<< (non-json)", raw)
            log.warning("non-JSON frame from robot (encryption still on?): %r", raw[:80])
            return
        svc = d.get("service")
        if svc != "/pita/map":                      # map frames are big; keep the log readable
            self._trace("<<", d)
        if d.get("machine_id") and not self.machine_id:
            self.machine_id = d["machine_id"]
            log.info("robot machine_id = %s", self.machine_id)
        res = d.get("result") if isinstance(d.get("result"), dict) else {}

        if svc in ("/pita/info", "/pita/info/all") and res:
            self._info = res
            self._emit_status()
        elif svc == "/pita/map" and res:
            try:
                pose = {"x": float(res["x"]), "y": float(res["y"]),
                        "theta": float(res.get("theta", 0.0))}
                self._record_pose(pose)
                self._pose = pose
                self._emit_status()
            except (KeyError, TypeError, ValueError):
                pass

        rid = d.get("request_id")
        if rid and rid in self._waiters:
            fut = self._waiters.pop(rid)
            if not fut.done():
                fut.set_result(d)
        if rid and rid in self._pending:
            cid = self._pending.pop(rid)
            ok = d.get("code", 0) == 0 and res.get("success", True) is not False
            self._dispatch({"type": "ack", "id": cid, "ok": ok,
                            "detail": res.get("msg") or "code %s" % d.get("code")})

    def _record_pose(self, pose: Dict[str, float]) -> None:
        """Append live poses (map frame) to data_dir/pose-trail/<date>.jsonl when the
        robot moved >2 cm -- ground truth for calibrating the map layers."""
        last = self._pose
        if last and abs(last["x"] - pose["x"]) < 0.02 and abs(last["y"] - pose["y"]) < 0.02:
            return
        try:
            d = os.path.join(self._data_dir, "pose-trail"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, time.strftime("%Y%m%d") + ".jsonl"), "a") as f:
                f.write(json.dumps({"t": round(time.time(), 2), **pose,
                                    "robot_status": self._info.get("robot_status")}) + "\n")
        except OSError:
            pass

    def _state(self) -> str:
        i = self._info
        rs = i.get("robot_status")
        on_dock = bool(i.get("station_contact") or i.get("charge_status"))
        if rs == 301:
            return "paused"
        if rs in (300, 700, 800):
            return "cleaning"
        if rs == 600:
            return "docked" if on_dock else "returning"
        if rs == 100 or rs is None:
            return "docked" if on_dock else "idle"
        if i.get("main_task") not in (None, 0):
            return "cleaning"
        return "docked" if on_dock else "idle"

    def _emit_status(self) -> None:
        i = self._info
        msg: Dict[str, Any] = {
            "type": "status", "ts": time.time(), "state": self._state(),
            "battery": i.get("battery_per"),
            "fan_speed": ROBOT_TO_FAN.get(i.get("mode_strength"), i.get("mode_strength") or "standard"),
            # physical module the user fitted: 2 = brush (vacuum); other values = mop (TBC)
            "mode": ("vacuum" if i.get("clean_mode") in (None, 2) else "mop"),
            "error": (str(i["error_code"]) if i.get("error_code") else None),
            "clean_area_m2": round(float(i.get("cleaned_area") or 0.0), 2),
            "raw": {k: i.get(k) for k in ("robot_status", "work_status", "main_task",
                                          "clean_mode", "working_progress", "task_name",
                                          "station_contact", "charge_status")},
        }
        pose = self._pose or self._dock_pose
        if pose:
            msg["pose"] = pose
        self._dispatch(msg)

    # --- commands from the LAN server ----------------------------------------
    def _default_order(self) -> List[int]:
        scheme = self.clean_scheme()
        try:
            return list(dict.fromkeys(scheme["sweep_scheme"][0]["room_id"]))
        except (KeyError, IndexError, TypeError):
            return []

    def send_command(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> str:
        args = args or {}
        cid = uuid.uuid4().hex
        state = self._state()
        svc, params, note = "/pita_clean_system/command", None, None
        if cmd in ("start", "clean", "clean_room"):
            if cmd == "clean_room":
                order = [int(args.get("room_id", 0))]
            else:
                order = [int(r) for r in (args.get("rooms") or [])] or self._default_order()
            params = {"cmd": CMD_START_OR_TOGGLE, "clean_order": order, "floor_type": []}
        # cmd 3 is a pause/resume TOGGLE: only refuse when it would do the opposite
        elif cmd == "pause":
            if state in ("paused", "docked", "idle"):
                note = "nothing to pause (%s)" % state
            params = {"cmd": CMD_START_OR_TOGGLE}
        elif cmd == "resume":
            if state in ("cleaning", "docked", "idle"):
                note = "nothing to resume (%s)" % state
            params = {"cmd": CMD_START_OR_TOGGLE}
        elif cmd == "stop":
            params = {"cmd": CMD_STOP}
        elif cmd == "dock":
            if state == "docked":
                note = "already docked (cmd 1 would UNDOCK it)"
            params = {"cmd": CMD_DOCK_TOGGLE}
        elif cmd == "set_fan_speed":
            name = FAN_TO_ROBOT.get(str(args.get("fan_speed")), str(args.get("fan_speed")))
            svc, params = "/pita/config/set_sweep_strength", {"cmd": 10, "name": name}
        else:
            note = "command %r not supported by the T10 cloud protocol" % cmd

        if note:
            self._dispatch({"type": "ack", "id": cid, "ok": False, "detail": note})
            return cid
        try:
            rid = self._request(svc, params)
            self._pending[rid] = cid
            self._fast_until = time.time() + 30
            if self._kick is not None and self._loop is not None:
                self._loop.call_soon_threadsafe(self._kick.set)
        except Exception as e:                       # noqa: BLE001
            self._dispatch({"type": "ack", "id": cid, "ok": False, "detail": str(e)})
        return cid

    # --- fake HTTP API (called from api.py) ----------------------------------
    def clean_scheme(self) -> Dict[str, Any]:
        """The robot's cleaning scheme, served back unchanged so nothing drifts."""
        for p in (os.path.join(self._data_dir, "cloud", "clean_scheme.json"),
                  os.path.join(self._map_dir, "communicator", "clean_scheme", "info")):
            try:
                with open(p) as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
        return {}

    # --- robot file uploads (logs, bags, map packs) --------------------------
    # pita_communicator asks GET /robots/log/temporaryauthorization?file_type=&
    # timestamp=&file_name= for a RobotGetAwsTokenResponse whose "url" is a
    # pre-signed upload URL, then sends the file (application/octet-stream) there.
    # We hand out a URL on this server, so the files land here instead of S3.
    UPLOAD_CAP_BYTES = 2 * 1024 ** 3      # keep at most ~2 GB; oldest files go first

    def _upload_dir(self) -> str:
        return os.path.join(self._data_dir, "robot-uploads")

    def save_upload(self, rel: str, body: bytes) -> Dict[str, Any]:
        rel = "/".join(p for p in rel.split("/") if p not in ("", ".", ".."))  # no traversal
        dst = os.path.join(self._upload_dir(), time.strftime("%Y%m%d"), rel or "unnamed")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(body)
        self._trace("UPLOAD", "%s (%d bytes)" % (dst, len(body)))
        log.info("robot uploaded %s (%d bytes)", rel, len(body))
        self._prune_uploads()
        return {"ok": True, "bytes": len(body)}

    def _prune_uploads(self) -> None:
        files = []
        for root, _d, names in os.walk(self._upload_dir()):
            for n in names:
                fp = os.path.join(root, n)
                try:
                    st = os.stat(fp); files.append((st.st_mtime, st.st_size, fp))
                except OSError:
                    pass
        total = sum(f[1] for f in files)
        for _mt, size, fp in sorted(files):
            if total <= self.UPLOAD_CAP_BYTES:
                break
            try:
                os.remove(fp); total -= size
            except OSError:
                pass

    def handle_http(self, method: str, path: str, query: str, body: bytes,
                    host: str = "") -> Dict[str, Any]:
        text = body.decode("utf-8", "replace") if body else ""
        p = "/" + path.strip("/")
        if p.startswith("/upload/"):
            return self.save_upload(p[len("/upload/"):], body)
        self._trace("HTTP", "%s /%s?%s %s" % (method, path, query, text[:1500]))
        if p == "/robots/map/clean_scheme":
            return dict(_OK, result=self.clean_scheme())
        if p == "/robots/log/temporaryauthorization":
            from urllib.parse import parse_qs, quote
            q = {k: v[0] for k, v in parse_qs(query).items()}
            name = q.get("file_name") or "file-%d" % int(time.time())
            # https on the TLS upload port (AwsUpload always speaks TLS)
            url = "https://%s:%d/cloud/upload/%s/%s" % ((host or "192.168.5.100").split(":")[0],
                                                       self.UPLOAD_TLS_PORT,
                                                   quote(q.get("file_type", "0")), quote(name))
            self._trace("GRANT", url)
            # RobotGetAwsTokenResult = {method, url, formParams?, path_prefix} (from_json
            # @0x3d2008). formParams is OPTIONAL: when null the parser skips the form
            # struct entirely (0x3d20e8) -> plain pre-signed-URL upload, i.e. the robot
            # sends the file with `method` to `url`. (A non-null form with any missing
            # field throws "type must be string, but is null".)
            return dict(_OK, result={"method": "PUT", "url": url, "formParams": None,
                                     "path_prefix": ""})
        if p == "/regularcleaning/pull":
            return dict(_OK, result={"data": [], "version": int(time.time() * 1000)})
        if p == "/robots/clean_report":
            # end-of-task report: map geometry + cleaned area (+ path); keep it whole
            try:
                d = os.path.join(self._data_dir, "clean-reports"); os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".json"), "w") as f:
                    f.write(text)
            except OSError:
                pass
        if p == "/robots/state":
            try:
                st = json.loads(text).get("state")
                self._trace("STATE", st)
            except (ValueError, AttributeError):
                pass
        return dict(_OK, result={})
