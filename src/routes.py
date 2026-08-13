"""HTTP and WebSocket routes for the LightingControl webapp.

Routes read their shared dependencies (controller, config, logger, templates,
connection manager) from ``request.app.state``, which is populated by
:func:`webapp.create_asgi_app`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import time
from typing import TYPE_CHECKING, Any

import psutil
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from webapp import sanitize_mode, validate_access_key

if TYPE_CHECKING:
    from sc_foundation import SCConfigManager, SCLogger
    from starlette.templating import Jinja2Templates

    from controller import LightingController

router = APIRouter()


def _controller(request: Request) -> LightingController:
    return request.app.state.controller  # type: ignore[no-any-return]


def _config(request: Request) -> SCConfigManager:
    return request.app.state.config  # type: ignore[no-any-return]


def _logger(request: Request) -> SCLogger:
    return request.app.state.logger  # type: ignore[no-any-return]


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates  # type: ignore[no-any-return]


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Access forbidden.", status_code=403)


def _format_uptime(seconds: float) -> str:
    """Format a boot-relative uptime as ``Xd Yh Zm``.

    Returns:
        A human-readable uptime string (days/hours are omitted when zero).
    """
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, key: str | None = None) -> Any:
    """Render the home page.

    Returns:
        A rendered home page, a 403, or a 503 if no data is available yet.
    """
    if not validate_access_key(_config(request), _logger(request), key):
        return _forbidden()

    snapshot = await asyncio.to_thread(_controller(request).get_webapp_data)
    if not snapshot:
        return HTMLResponse("No data available yet.", status_code=503)

    refresh_raw = _config(request).get("Website", "PageAutoRefresh", default=60)
    try:
        refresh_seconds = int(refresh_raw or 0)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        refresh_seconds = 60

    return _templates(request).TemplateResponse(
        request,
        "home.html",
        {
            "app_label": _config(request).get(
                "General", "AppName", default="LightingControl"
            ),
            "groups": snapshot.get("groups", {}),
            "page_auto_refresh": refresh_seconds,
            "access_key": key or "",
        },
    )


@router.get("/system", response_class=HTMLResponse)
async def system(request: Request, key: str | None = None) -> Any:
    """Render the system-information page.

    Returns:
        A rendered system page, or a 403 response.
    """
    if not validate_access_key(_config(request), _logger(request), key):
        return _forbidden()

    controller = _controller(request)
    config = _config(request)
    snapshot = await asyncio.to_thread(controller.get_webapp_data)
    groups = snapshot.get("groups", {})
    num_groups = len(groups)
    num_lights = sum(len(g.get("switches", {})) for g in groups.values())

    # cpu_percent with a short interval blocks; run it off the event loop.
    cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 0.3)
    memory = psutil.virtual_memory()
    uptime = time.time() - psutil.boot_time()
    disabled = bool(config.get("General", "DisableAllSwitches", default=False))

    system_info = {
        "Operating system": f"{platform.system()} {platform.release()}",
        "Platform": platform.platform(),
        "Architecture": platform.machine(),
        "Hostname": platform.node(),
        "Python version": platform.python_version(),
        "Uptime": _format_uptime(uptime),
        "Memory used": f"{memory.percent:.0f}%",
        "CPU load": f"{cpu_percent:.0f}%",
        "Number of groups": num_groups,
        "Number of lights": num_lights,
        "System disabled": "true" if disabled else "false",
    }
    return _templates(request).TemplateResponse(
        request,
        "system.html",
        {
            "app_label": config.get("General", "AppName", default="LightingControl"),
            "system_info": system_info,
            "access_key": key or "",
        },
    )


@router.get("/config", response_class=HTMLResponse)
async def show_config(request: Request, key: str | None = None) -> Any:
    """Render the raw configuration file contents.

    Secrets live in the environment (`.env`), not in the YAML, so displaying the
    config file does not leak credentials.

    Returns:
        A rendered config page, or a 403 response.
    """
    if not validate_access_key(_config(request), _logger(request), key):
        return _forbidden()

    config = _config(request)
    config_path = getattr(config, "config_path", None)
    try:
        config_text = (
            config_path.read_text(encoding="utf-8")
            if config_path
            else "Config path unavailable."
        )
    except OSError as exc:
        config_text = f"Could not read config file: {exc}"

    return _templates(request).TemplateResponse(
        request,
        "config.html",
        {
            "app_label": config.get("General", "AppName", default="LightingControl"),
            "config_text": config_text,
            "config_path": str(config_path) if config_path else "",
            "access_key": key or "",
        },
    )


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Push live state updates to a client and accept mode-override commands."""
    config = ws.app.state.config
    logger = ws.app.state.logger
    manager = ws.app.state.connection_manager
    controller = ws.app.state.controller

    key = ws.query_params.get("key")
    if not validate_access_key(config, logger, key):
        await ws.accept()
        await ws.close(code=1008)
        return

    await manager.connect(ws)
    try:
        snapshot = await asyncio.to_thread(controller.get_webapp_data)
        await ws.send_text(
            json.dumps({"type": "state_update", "state": snapshot}, default=str)
        )

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") != "command":
                continue

            mode = sanitize_mode(msg.get("mode"))
            if mode is None:
                continue

            action = msg.get("action")
            if action == "set_group_mode":
                group_name = msg.get("group_id")
                if isinstance(group_name, str) and controller.is_valid_group(
                    group_name
                ):
                    await asyncio.to_thread(controller.set_group_mode, group_name, mode)
                    _enqueue_broadcast(ws)
            elif action == "set_switch_mode":
                switch_name = msg.get("switch_id")
                if isinstance(switch_name, str) and controller.is_valid_switch(
                    switch_name
                ):
                    await asyncio.to_thread(
                        controller.set_switch_mode, switch_name, mode
                    )
                    _enqueue_broadcast(ws)

    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except RuntimeError:
        await manager.disconnect(ws)


def _enqueue_broadcast(ws: WebSocket) -> None:
    """Signal the broadcast worker that state changed (coalesced snapshot push)."""
    queue = getattr(ws.app.state, "update_queue", None)
    if queue is None:
        return
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(None)
