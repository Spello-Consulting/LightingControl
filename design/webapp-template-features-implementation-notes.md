# Implementation notes — "Copy in features from the template web app"

Reference implementation notes for porting three template-webapp features into an
app's web UI. Written from the LightingControl implementation (issue #24); reuse
this as the spec when applying the same change to **water-info**, **PowerController**,
and **PowerControllerViewer**.

## Features delivered

1. **Day / Night / System display-mode toggle** in the title bar (persists in `localStorage`).
2. **System Information page** (`/system`) — `platform` info + psutil host metrics + app-specific counters.
3. **Config page** (`/config`) — dumps the contents of the active YAML config file.
4. Title-bar right side, in order: **Clock → display-mode toggle → System link → Config link**.

Reference source app: `/Users/nick/dev/py_templates/webapp` (the `webapp` variant).
Note: the template itself shows only `platform` info on its System page — the
**psutil metrics (uptime/memory/CPU) are an addition we standardised on**, so they
are not copied from the template; add them per §3 below.

## Structure adopted (template's split)

Move the web layer under `src/` and separate routes from the app factory:

```
src/
  webapp.py     # app factory: sets app.state.*, mounts src/static, includes router
  routes.py     # APIRouter: GET / , GET /system , GET /config , WS /ws
  templates/    # base.html, home.html (was index.html), system.html, config.html
  static/       # style.css (CSS-variable theming), app.js (WS client)
```

If an app currently keeps `templates/` and `static/` at the repo root with routes
inline in `webapp.py` (LightingControl's starting point), `git mv` them under `src/`
and switch the app factory to `_SRC_DIR = Path(__file__).resolve().parent`.

## Step-by-step

### 1. Dependency
`uv add psutil` (adds to `pyproject.toml` + `uv.lock`).

### 2. `webapp.py` — app factory
- Keep the **public signature** `create_asgi_app(controller, config, logger) -> (FastAPI, notifier)` so `main.py` is untouched.
- Populate `app.state.controller/config/logger/templates/connection_manager`, and set `app.state.update_queue` in the lifespan (the WS handler enqueues onto it to trigger a coalesced broadcast).
- Mount static from `src/static`. Optional but recommended: a `NoCacheStaticFiles(StaticFiles)` subclass that sets `Cache-Control: no-cache` so updated `style.css`/`app.js` aren't served stale.
- `from routes import router; app.include_router(router)` (import inside the factory to avoid a circular import — `routes` imports helpers from `webapp`).
- **Access-key helper compatibility:** rename the validator to a public `validate_access_key` (and `sanitize_mode`), imported by `routes.py`. Keep a `_validate_access_key = validate_access_key` alias if an existing test imports the private name (LightingControl's `tests/test_webapp_access_key.py` does).

### 3. `routes.py` — the routes
- `APIRouter`; small `_controller/_config/_logger/_templates(request)` accessors read from `request.app.state`.
- `GET /` — validate key, snapshot via `controller.get_webapp_data()`, render `home.html`. **Pass `access_key` into the context** so the nav links keep `?key=`. Also pass `page_auto_refresh`.
- `GET /system` — build a `system_info` dict and render `system.html`:
  - `platform`: Operating system, Platform, Architecture, Hostname, Python version.
  - `psutil`: Uptime (`time.time() - psutil.boot_time()`, formatted `Xd Yh Zm`), Memory used (`virtual_memory().percent`), CPU load. **Call `psutil.cpu_percent(interval)` off the event loop** (`await asyncio.to_thread(psutil.cpu_percent, 0.3)`) — it blocks.
  - App-specific counters (adapt per app): Number of groups / Number of lights (LightingControl); the equivalent domain counts for the other apps. Derive counts from the snapshot for thread-safety.
  - A disabled/override flag rendered true/false (LightingControl: `config.get("General", "DisableAllSwitches", default=False)`).
  - **Exclude** the template's `Simulation mode` and `Cities loaded` rows.
- `GET /config` — read `config.config_path.read_text(encoding="utf-8")` inside `try/except OSError`; render `config.html` with `config_text` + `config_path`. Secrets live in `.env`, not the YAML, so dumping the config is safe.
- `WS /ws` — port the app's existing WebSocket handler; on auth failure `await ws.accept()` **then** `ws.close(code=1008)` so the client actually receives the code.

### 4. Templates
- **`base.html`** — the shared shell: `<head>` with a pre-paint theme script (reads `localStorage["theme"]`, default `"system"`, sets `data-theme` on `<html>` before first paint to avoid a flash); header with `<h1>` app label + `<nav class="header-right">` containing `#clock`, `#theme-toggle`, `/system` link, `/config` link (each link carries `?key={{ access_key }}` when set); footer with `#conn-status` + `#last-refresh`; then `window.__ACCESS_KEY__` / `window.__PAGE_AUTO_REFRESH__` globals, `<script src="/static/app.js">`, and inline clock + theme-toggle-cycle scripts. Blocks: `{% block content %}`, `{% block scripts %}`, `{% block title %}`.
  - Theme cycle: `["system","light","dark"]`, icons `🖥️ / ☀️ / 🌙`, persisted to `localStorage`.
  - Drop any SIMULATION badge unless the app actually has a simulation mode.
- **`home.html`** — `{% extends "base.html" %}`; the app's existing page body goes in `{% block content %}`. Move page-specific inline JS to `app.js`.
- **`system.html`** — extends base; iterate `system_info.items()` into `.info-row`s + a back-to-home `.btn`.
- **`config.html`** — extends base; `config_path` in `.config-path`, contents in `<pre class="config-dump">`.

### 5. Static
- **`style.css`** — the key work is theming. Define the palette as **CSS custom properties** in three places so all three theme states work:
  - `:root { … }` (light defaults),
  - `:root[data-theme="dark"] { … }` (explicit dark),
  - `@media (prefers-color-scheme: dark) { :root[data-theme="system"], :root:not([data-theme]) { … } }` (system-follows-OS).
  Then rewrite **every** hardcoded colour in the app's existing rules (header, cards, tables, buttons, badges, indicators, text) to reference the variables — a header-only theme looks broken in dark mode. Add the header-nav rules (`.header-right`, `.header-link`, `#theme-toggle`) and the info-page rules (`.info-page/.info-card/.info-row/.info-label/.info-value/.config-path/.config-dump`, `.btn`, `#conn-status` colours).
- **`app.js`** — the WebSocket client: connect/reconnect (`1008` close ⇒ stop + "unauthorized"; otherwise "offline" + retry), an `applySnapshot`/`applyState` that updates the page and **no-ops on pages without those elements** (so it's harmless on `/system` and `/config`), plus `conn-status` + `last-refresh` updates and the `page_auto_refresh` reload driven from `window.__PAGE_AUTO_REFRESH__`. Read the access key from `window.__ACCESS_KEY__`.

## Gotchas / decisions

- **psutil `cpu_percent` blocks** — always run it via `asyncio.to_thread`.
- **Access-key propagation** — nav links and the back-to-home button must include `?key=` (from `access_key` in the template context) or a keyed session breaks when navigating.
- **Circular import** — `routes.py` imports helpers from `webapp`; import `routes` *inside* `create_asgi_app`, not at `webapp.py` module top.
- **Test compatibility** — keep the access-key validator importable under whatever name existing tests use (alias if you rename it public).
- **No simulation mode** in most of these apps — omit the template's SIMULATION badge and the "Simulation mode"/"Cities loaded" system rows.

## Verification checklist
- `uv run ruff check src/ && uv run ruff format src/` clean; `uv run pytest` green.
- All three pages return 200; header right shows Clock, toggle, System, Config in order.
- Toggle cycles System → Light → Dark; the whole page re-themes; choice persists across reloads.
- `/system` shows platform + psutil metrics + the app-specific counters; no Simulation/Cities rows.
- `/config` dumps the active YAML and shows its path.
- Live WS updates still work; footer conn-status flips live/offline.
- If an access key is configured, nav links and the WS preserve `?key=`.

A fast way to verify without real hardware: build the app with fake `controller`/`config`/`logger`
objects and exercise the routes with Starlette's `TestClient` (including `websocket_connect`),
or run it under uvicorn on a spare port and drive it in a browser to check light/dark.
