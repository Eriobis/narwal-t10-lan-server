"""
FastAPI app: JSON REST + WebSocket for the Narwal T10 LAN server.

The React frontend (frontend/) is the sole UI and consumes this API. When a
production build exists at frontend/dist it is served from "/"; otherwise "/"
serves a small no-build fallback page (so the mock is demonstrable even without
Node installed) that points at the dev instructions.

REST
  GET  /api/status        current robot state (+ transport / ha status)
  GET  /api/map           latest occupancy grid (raw -1/0/100 + meta)
  GET  /api/rooms         room segmentation scheme
  GET  /api/maps          persisted maps on disk
  GET  /api/map_image     latest robot-rendered map (map_rotate.png) [image/png]
  POST /api/command       {"cmd":"start","args":{...}} -> robot

WebSocket
  GET  /ws                live push. On connect: a burst of
                          {type:status|map|rooms|map_image, data:...} snapshots,
                          then the same events as they happen. Client may also
                          send {"cmd":...,"args":...} to issue commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Dict

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .server import NarwalServer

log = logging.getLogger("narwal.api")

_FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "frontend", "dist")


def create_app(server: NarwalServer) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # on shutdown: stop the bridge + HA cleanly
        server.stop()

    app = FastAPI(title="Narwal T10 LAN server", version="0.1.0", lifespan=lifespan)

    # ------------------------------------------------------------------ REST
    @app.get("/api/status")
    def api_status():
        return server.snapshot_state()

    @app.get("/api/map")
    def api_map():
        m = server.snapshot_map()
        if m is None:
            return JSONResponse({"error": "no map yet"}, status_code=404)
        return m

    @app.get("/api/rooms")
    def api_rooms():
        r = server.snapshot_rooms()
        if r is None:
            return JSONResponse({"error": "no rooms yet"}, status_code=404)
        return r

    @app.get("/api/history")
    def api_history(limit: int = 50):
        return {"sessions": server.snapshot_history(limit)}

    @app.get("/api/maps")
    def api_maps():
        return {"maps": server.list_maps()}

    @app.get("/api/map_vector")
    def api_map_vector():
        v = server.snapshot_vector()
        if v is None:
            return JSONResponse({"error": "no map yet"}, status_code=404)
        if "error" in v:
            return JSONResponse(v, status_code=503)
        return v

    @app.get("/api/map_image")
    def api_map_image():
        raw = server.map_image_bytes()
        if raw is None:
            return JSONResponse({"error": "no map image yet"}, status_code=404)
        return Response(content=raw, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/edits")
    def api_edits_get(map_id: str = ""):
        return server.load_edits(map_id or None)

    def _robot_cloud():
        b = server.bridge
        return b if getattr(b, "set_plan_map", None) and getattr(b, "_ws", None) else None

    @app.put("/api/edits")
    async def api_edits_put(body: Dict[str, Any]):
        from .planmap import forbidden_from_zones
        body = dict(body or {})
        current = server.load_edits(body.get("map_id"))
        same = forbidden_from_zones(body.get("zones") or []) == forbidden_from_zones(current.get("zones") or [])
        stale = body.get("zones_rev") is None or int(body.get("zones_rev")) != int(current.get("zones_rev") or 0)
        if stale or same:
            # never let an editor loaded before the robot's latest zone list overwrite it,
            # and don't rewrite the robot for nothing: keep the current zones
            body["zones"] = current.get("zones") or []
        body["zones_rev"] = int(current.get("zones_rev") or 0) + (0 if (stale or same) else 1)
        saved = server.save_edits(body, body.get("map_id"))
        if stale and not same:
            return {"ok": True, "edits": saved, "robot": {"sent": False, "stale": True,
                    "detail": "zones were changed elsewhere since this page loaded -- reloaded, redo your zone edit"}}
        if same:
            return {"ok": True, "edits": saved, "robot": {"sent": False, "detail": "zones unchanged"}}
        robot = {"sent": False, "detail": "robot not connected (zones saved on the server only)"}
        cloud = _robot_cloud()
        if cloud is not None:
            from .planmap import forbidden_from_zones
            try:
                r = await cloud.set_plan_map({"operations": ["user_forbidden"],
                                              "user_forbidden": forbidden_from_zones(saved["zones"])})
                robot = {"sent": True, "ok": r["ok"], "detail": "no-go zones sent to the robot"
                         if r["ok"] else "robot refused: %s" % r["reply"]}
            except Exception as e:                   # noqa: BLE001
                robot = {"sent": False, "detail": "send failed: %s" % e}
        return {"ok": True, "edits": saved, "robot": robot}

    @app.post("/api/rooms/edit")
    async def api_rooms_edit(body: Dict[str, Any]):
        """{op: split|merge, line:[[x,y],[x,y]] | rooms:[keep, absorb], apply: bool}"""
        from .planmap import EditError, encode_room_layer
        body = body or {}
        try:
            res = server.room_edit(body.get("op", ""), body)
        except (EditError, KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not body.get("apply"):
            return {"ok": True, "preview": res["segmentation"], "info": res["info"]}
        cloud = _robot_cloud()
        if cloud is None:
            return JSONResponse({"error": "robot not connected to the LAN cloud"}, status_code=409)
        from .planmap import plan_payload
        # re-read the robot's current plan, then recompute the edit on it, so a change
        # made elsewhere since the preview is never overwritten with stale rooms/zones
        await cloud.fetch_plan_map()
        try:
            res = server.room_edit(body.get("op", ""), body)
        except (EditError, KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": "the map changed since the preview: %s" % e}, status_code=409)
        r = await cloud.set_plan_map(plan_payload(server._plan_map, res["cells"], res["info"]))
        if r["ok"]:
            server.remap_names_after_edit(res["info"])
        return {"ok": r["ok"], "info": res["info"], "reply": r["reply"]}

    @app.post("/api/planmap/refresh")
    async def api_planmap_refresh():
        cloud = _robot_cloud()
        if cloud is None:
            return JSONResponse({"error": "robot not connected"}, status_code=409)
        res = await cloud.fetch_plan_map()
        return {"ok": bool(res), "zones": len((server.load_edits() or {}).get("zones", []))}

    @app.post("/api/command")
    async def api_command(body: Dict[str, Any]):
        cmd = (body or {}).get("cmd")
        if not cmd:
            return JSONResponse({"error": "missing cmd"}, status_code=400)
        cid = server.command(cmd, (body or {}).get("args") or {})
        return {"ok": True, "id": cid}

    # --------------------------------------------- fake Narwal cloud (robot side)
    # Only when transport=cloud: the robot's pita_communicator is repointed at
    #   ws://<this>:8099/cloud/robot_websocket   and   http://<this>:8099/cloud
    cloud = getattr(server.bridge, "handle_ws", None) and server.bridge
    if cloud:
        @app.websocket("/cloud/robot_websocket")
        async def cloud_ws(websocket: WebSocket):
            await cloud.handle_ws(websocket)

        @app.post("/api/robot/call")
        async def robot_call(body: Dict[str, Any]):
            """LAN-only: send {service, params} to the robot, return its raw reply."""
            try:
                return await cloud.call(body.get("service", ""), body.get("params") or {},
                                        float(body.get("timeout", 15)))
            except Exception as e:                   # noqa: BLE001
                return JSONResponse({"error": str(e)}, status_code=504)

        @app.api_route("/cloud/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        async def cloud_http(path: str, request: Request):
            ctype = request.headers.get("content-type", "")
            if path.startswith("upload/") and ctype.startswith("multipart/form-data"):
                # S3/OBS-style form POST: fields (key, policy, ...) + the file part
                form = await request.form()
                key, data = None, b""
                for name, val in form.multi_items():
                    if hasattr(val, "read"):
                        data = await val.read()
                        key = key or getattr(val, "filename", None)
                    elif name == "key":
                        key = str(val)
                return cloud.save_upload(key or path[len("upload/"):], data)
            body = await request.body()
            return cloud.handle_http(request.method, path, request.url.query, body,
                                     host=request.headers.get("host", ""))

    # ------------------------------------------------------------- WebSocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)

        def on_event(event: Dict[str, Any]) -> None:
            # Called from the bridge thread -> hop onto the event loop.
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass  # loop closing

        server.subscribe(on_event)
        try:
            # Initial snapshot burst so a fresh client has full state at once.
            await websocket.send_json({"type": "status", "data": server.snapshot_state()})
            if server.snapshot_map():
                await websocket.send_json({"type": "map", "data": server.snapshot_map()})
            if server.snapshot_rooms():
                await websocket.send_json({"type": "rooms", "data": server.snapshot_rooms()})
            await websocket.send_json({"type": "history_all",
                                       "data": server.snapshot_history(50)})
            if server.map_image_bytes():
                await websocket.send_json({"type": "map_image",
                                           "data": {"bytes": len(server.map_image_bytes())}})

            # Pump live events; also accept inbound commands from the client.
            recv_task = asyncio.ensure_future(websocket.receive_json())
            send_task = asyncio.ensure_future(queue.get())
            while True:
                done, _pending = await asyncio.wait(
                    {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED)
                if send_task in done:
                    event = send_task.result()
                    await websocket.send_json(event)
                    send_task = asyncio.ensure_future(queue.get())
                if recv_task in done:
                    msg = recv_task.result()
                    if isinstance(msg, dict) and msg.get("cmd"):
                        server.command(msg["cmd"], msg.get("args") or {})
                    recv_task = asyncio.ensure_future(websocket.receive_json())
        except WebSocketDisconnect:
            pass
        except Exception as e:   # pragma: no cover
            log.debug("ws error: %s", e)
        finally:
            server.unsubscribe(on_event)
            for t in ("recv_task", "send_task"):
                task = locals().get(t)
                if task:
                    task.cancel()

    # --------------------------------------------------------------- static
    if os.path.isdir(_FRONTEND_DIST):
        log.info("serving built React frontend from %s", _FRONTEND_DIST)
        app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="spa")
    else:
        log.warning("frontend/dist not found -> serving no-build fallback page. "
                    "Build it with: cd frontend && npm ci && npm run build")

        @app.get("/", response_class=HTMLResponse)
        def fallback():
            return _FALLBACK_HTML

    return app


_FALLBACK_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Narwal T10 LAN server</title>
<style>body{font:15px/1.6 system-ui;background:#0f1115;color:#e8eaed;margin:0;padding:24px}
code{background:#1b1f27;padding:2px 6px;border-radius:5px}a{color:#4ea1ff}
.card{max-width:640px;margin:0 auto;background:#1b1f27;padding:20px;border-radius:12px}</style>
</head><body><div class="card">
<h1>Narwal T10 LAN server is running</h1>
<p>The REST + WebSocket API is live, but the React UI has not been built yet.</p>
<p><b>Build the UI:</b></p>
<pre><code>cd frontend
npm ci
npm run build</code></pre>
<p>then restart the server. Or run the UI in dev mode (hot reload):</p>
<pre><code>cd frontend
npm run dev    # http://localhost:5173 (proxies /api and /ws to this server)</code></pre>
<p>Meanwhile the API works now:
<a href="/api/status">/api/status</a> ·
<a href="/api/map">/api/map</a> ·
<a href="/api/rooms">/api/rooms</a> ·
<a href="/api/map_image">/api/map_image</a></p>
</div></body></html>"""
