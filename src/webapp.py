"""Web application module for LightingControl.

Serves a Jinja2-rendered page and pushes state updates to clients over WebSocket.
Accepts mode-override commands from clients.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from enumerations import AppMode

if TYPE_CHECKING:
    from threading import Event

    from sc_foundation import SCConfigManager, SCLogger

    from controller import LightingController


def _get_repo_root() -> Path:
    # src/webapp.py -> repo_root
    return Path(__file__).resolve().parent.parent


def _validate_access_key(config: SCConfigManager, logger: SCLogger, key_from_request: str | None) -> bool:
    expected_key = os.environ.get("WEBAPP_ACCESS_KEY")
    if not expected_key:
        expected_key = config.get("Website", "AccessKey")
    if expected_key is None:
        return True
    if isinstance(expected_key, str) and not expected_key.strip():
        return True
    if key_from_request is None:
        logger.log_message("Missing access key.", "warning")
        return False
    key = key_from_request.strip()
    if not key:
        logger.log_message("Blank access key used.", "warning")
        return False
    if key != expected_key:
        logger.log_message("Invalid access key used.", "warning")
        return False
    return True


def _sanitize_mode(mode: Any) -> AppMode | None:
    if not isinstance(mode, str):
        return None
    mode_s = mode.strip().lower()
    try:
        return AppMode(mode_s)
    except ValueError:
        return None


class _WebAppNotifier:
    """Thread-safe notifier: LightingController calls notify() to trigger a WS broadcast."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[None] | None = None

    def bind(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[None]) -> None:
        self.loop = loop
        self.queue = queue

    def notify(self) -> None:
        loop = self.loop
        queue = self.queue
        if loop is None or queue is None:
            return

        def _enqueue() -> None:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

        loop.call_soon_threadsafe(_enqueue)


class _ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast_json(self, message: dict[str, Any]) -> None:
        text = json.dumps(message, default=str)
        async with self._lock:
            targets = list(self._connections)
        for ws in targets:
            try:
                await ws.send_text(text)
            except (RuntimeError, WebSocketDisconnect):
                await self.disconnect(ws)


def _register_routes(
    app: FastAPI,
    controller: LightingController,
    config: SCConfigManager,
    logger: SCLogger,
    templates: Jinja2Templates,
    manager: _ConnectionManager,
    notifier: _WebAppNotifier,
) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Any:
        key = request.query_params.get("key")
        if not _validate_access_key(config, logger, key):
            return HTMLResponse("Access forbidden.", status_code=403)

        snapshot = await asyncio.to_thread(controller.get_webapp_data)
        if not snapshot:
            return HTMLResponse("No data available yet.", status_code=503)

        refresh_raw = config.get("Website", "PageAutoRefresh", default=60)
        try:
            refresh_seconds = int(refresh_raw or 0)
        except (TypeError, ValueError):
            refresh_seconds = 60

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "app_label": config.get("General", "AppName", default="LightingControl"),
                "groups": snapshot.get("groups", {}),
                "page_auto_refresh": refresh_seconds,
            },
        )

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        key = ws.query_params.get("key")
        if not _validate_access_key(config, logger, key):
            await ws.close(code=1008)
            return

        await manager.connect(ws)
        try:
            snapshot = await asyncio.to_thread(controller.get_webapp_data)
            await ws.send_text(json.dumps({"type": "state_update", "state": snapshot}, default=str))

            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("type") != "command":
                    continue

                action = msg.get("action")
                mode = _sanitize_mode(msg.get("mode"))
                if mode is None:
                    continue

                if action == "set_group_mode":
                    group_name = msg.get("group_id")
                    if isinstance(group_name, str) and controller.is_valid_group(group_name):
                        await asyncio.to_thread(controller.set_group_mode, group_name, mode)
                        notifier.notify()
                elif action == "set_switch_mode":
                    switch_name = msg.get("switch_id")
                    if isinstance(switch_name, str) and controller.is_valid_switch(switch_name):
                        await asyncio.to_thread(controller.set_switch_mode, switch_name, mode)
                        notifier.notify()

        except WebSocketDisconnect:
            await manager.disconnect(ws)
        except RuntimeError:
            await manager.disconnect(ws)


def create_asgi_app(
    controller: LightingController,
    config: SCConfigManager,
    logger: SCLogger,
) -> tuple[FastAPI, _WebAppNotifier]:
    repo_root = _get_repo_root()
    templates = Jinja2Templates(directory=str(repo_root / "templates"))
    notifier = _WebAppNotifier()
    manager = _ConnectionManager()

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()

        async def _broadcast_worker() -> None:
            try:
                while True:
                    await app.state.update_queue.get()
                    # Coalesce rapid bursts into a single snapshot
                    while True:
                        try:
                            app.state.update_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    snapshot = await asyncio.to_thread(controller.get_webapp_data)
                    await manager.broadcast_json({"type": "state_update", "state": snapshot})
            except asyncio.CancelledError:
                return

        app.state.update_queue = asyncio.Queue(maxsize=100)
        notifier.bind(loop, app.state.update_queue)
        app.state.broadcast_task = asyncio.create_task(_broadcast_worker())

        yield

        task = app.state.broadcast_task
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=str(repo_root / "static")), name="static")
    _register_routes(app, controller, config, logger, templates, manager, notifier)

    return app, notifier


def serve_asgi_blocking(
    app: FastAPI,
    config: SCConfigManager,
    logger: SCLogger,
    stop_event: Event,
) -> None:
    """Run the ASGI server in the current thread, stopping when stop_event is set."""
    host_raw = config.get("Website", "HostingIP", default="127.0.0.1")
    host = host_raw if isinstance(host_raw, str) and host_raw else "127.0.0.1"
    port = int(config.get("Website", "Port", default=8080) or 8080)  # pyright: ignore[reportArgumentType]

    uv_config = uvicorn.Config(app, host=host, port=port, log_level="warning", reload=False)
    server = uvicorn.Server(uv_config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _run() -> None:
        async def _stop_watcher() -> None:
            await asyncio.to_thread(stop_event.wait)
            server.should_exit = True

        watcher = asyncio.create_task(_stop_watcher())
        try:
            logger.log_message(f"Web server listening on http://{host}:{port}", "summary")
            await server.serve()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            logger.log_message("Web server shutdown complete.", "detailed")

    try:
        asyncio.run(_run())
    except asyncio.CancelledError:
        logger.log_message("Web server cancelled during shutdown.", "debug")
