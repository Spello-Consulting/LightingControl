"""Web application module for LightingControl.

Builds the FastAPI ASGI app, bridges the (threaded) controller to WebSocket
clients via a thread-safe notifier, and provides a blocking server entry point
so the webapp can run inside a ``ThreadManager``-managed worker thread.

HTTP and WebSocket routes live in :mod:`routes`; they read their shared
dependencies (controller, config, logger, templates, connection manager) from
``request.app.state``, which is populated by :func:`create_asgi_app`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from enumerations import AppMode

if TYPE_CHECKING:
    from threading import Event

    from sc_foundation import SCConfigManager, SCLogger
    from starlette.responses import Response
    from starlette.types import Scope

    from controller import LightingController

_SRC_DIR = Path(__file__).resolve().parent


def validate_access_key(
    config: SCConfigManager, logger: SCLogger, key_from_request: str | None
) -> bool:
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


def sanitize_mode(mode: Any) -> AppMode | None:
    if not isinstance(mode, str):
        return None
    mode_s = mode.strip().lower()
    try:
        return AppMode(mode_s)
    except ValueError:
        return None


# Backwards-compatible private alias imported by tests/test_webapp_access_key.py.
_validate_access_key = validate_access_key


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
                if queue is not None:
                    queue.put_nowait(None)

        loop.call_soon_threadsafe(_enqueue)


class _NoCacheStaticFiles(StaticFiles):
    """Static files served with ``Cache-Control: no-cache``.

    Prevents browsers from serving a stale ``app.js``/``style.css`` after an
    update, which otherwise leads to hard-to-diagnose "my change isn't showing"
    problems.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


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


def create_asgi_app(
    controller: LightingController,
    config: SCConfigManager,
    logger: SCLogger,
) -> tuple[FastAPI, _WebAppNotifier]:
    templates = Jinja2Templates(directory=str(_SRC_DIR / "templates"))
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
                    await manager.broadcast_json({
                        "type": "state_update",
                        "state": snapshot,
                    })
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
    app.state.controller = controller
    app.state.config = config
    app.state.logger = logger
    app.state.templates = templates
    app.state.connection_manager = manager
    app.mount(
        "/static",
        _NoCacheStaticFiles(directory=str(_SRC_DIR / "static")),
        name="static",
    )

    from routes import router  # noqa: PLC0415

    app.include_router(router)

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

    uv_config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", reload=False
    )
    server = uvicorn.Server(uv_config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _run() -> None:
        async def _stop_watcher() -> None:
            await asyncio.to_thread(stop_event.wait)
            server.should_exit = True

        watcher = asyncio.create_task(_stop_watcher())
        try:
            logger.log_message(
                f"Web server listening on http://{host}:{port}", "summary"
            )
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
