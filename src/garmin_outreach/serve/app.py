"""App assembly and CLI entry point for `garmin-outreach serve`.

`create_app()` builds the loopback-only Starlette app
(docs/spec-serve-ui.md sections 5-9); `run()` is the CLI entry point that
validates the loopback-only host, starts uvicorn as a single worker, and
opens a browser tab only after the socket has bound.

This module (not `garmin_outreach.serve`) is where the `ui` extra's
third-party imports (starlette/uvicorn/jinja2) live, so that
`garmin_outreach.serve.artifacts` stays importable without the extra
installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import webbrowser
from pathlib import Path

import jinja2
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from . import jobs as jobs_module
from .artifacts import ArtifactStore
from .events import EventBus
from .jobs import JobRunner
from .security import (
    ALLOWED_HOSTS,
    HostAllowlistMiddleware,
    SecurityHeadersMiddleware,
    new_job_csrf_token,
)
from .views import (
    DEFAULT_SSE_KEEPALIVE_SECONDS,
    api_events,
    api_jobs_build,
    api_jobs_explore,
    api_jobs_mapshare,
    api_jobs_snapshot,
    api_layer_geojson,
    api_summary,
    dashboard,
    discover_datastar_filename,
    discover_maplibre_css_filename,
    discover_maplibre_js_filename,
    discover_maplibre_worker_filename,
    map_view,
    messages,
    not_found,
    server_error,
    static_asset,
)

_BROWSER_OPEN_TIMEOUT_SECONDS = 10.0
_BROWSER_OPEN_POLL_SECONDS = 0.05


def create_app(
    data_dir: Path,
    *,
    sse_keepalive_seconds: float = DEFAULT_SSE_KEEPALIVE_SECONDS,
    max_event_subscribers: int = 8,
    event_queue_size: int = 64,
) -> Starlette:
    """Build the Starlette app over `data_dir` (docs/spec-serve-ui.md).

    Routes: `GET /` (dashboard), `GET /messages` (paged messages timeline),
    `GET /map` (MapLibre island + layer toggles), `GET /api/summary` (shaped
    JSON, never the raw file), `GET /api/layers/{name}.geojson` (allowlisted
    layer content), `GET /api/jobs*` (job snapshot + build/mapshare/explore
    triggers), `GET /api/events` (SSE live job updates, section 7 Phase B),
    `GET /static/{filename}` (vendored assets). Anything else is a friendly
    404 (no traceback; `debug=False`).

    `sse_keepalive_seconds`/`max_event_subscribers`/`event_queue_size` exist
    so tests can shrink the keepalive interval and subscriber cap rather
    than waiting out real production defaults.
    """
    artifact_store = ArtifactStore(data_dir)
    templates = jinja2.Environment(
        loader=jinja2.PackageLoader("garmin_outreach.serve", "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    templates.globals["datastar_filename"] = discover_datastar_filename()
    # Resolved once at app-creation time rather than per-/map-request (the
    # datastar filename above gets the same treatment): a re-vendor changes
    # these hashed filenames rarely enough that per-request iterdir() scans
    # are pure overhead.
    templates.globals["maplibre_js_filename"] = discover_maplibre_js_filename()
    templates.globals["maplibre_css_filename"] = discover_maplibre_css_filename()
    templates.globals["maplibre_worker_filename"] = discover_maplibre_worker_filename()

    routes = [
        Route("/", dashboard, methods=["GET"]),
        Route("/messages", messages, methods=["GET"]),
        Route("/map", map_view, methods=["GET"]),
        Route("/api/summary", api_summary, methods=["GET"]),
        Route("/api/layers/{name}.geojson", api_layer_geojson, methods=["GET"]),
        Route("/api/jobs", api_jobs_snapshot, methods=["GET"]),
        Route("/api/jobs/build", api_jobs_build, methods=["POST"]),
        Route("/api/jobs/mapshare", api_jobs_mapshare, methods=["POST"]),
        Route("/api/jobs/explore", api_jobs_explore, methods=["POST"]),
        Route("/api/events", api_events, methods=["GET"]),
        Route("/static/{filename}", static_asset, methods=["GET"]),
    ]
    middleware = [
        # SecurityHeadersMiddleware is outermost so it also wraps the
        # host-allowlist's 400s (and, via the 500 exception handler below,
        # uncaught errors get the same headers even though those bypass this
        # middleware stack entirely -- see views.server_error).
        Middleware(SecurityHeadersMiddleware),
        # DNS-rebinding defense: reject any request whose Host header does
        # not resolve to a loopback name (docs/spec-serve-ui.md section 8).
        Middleware(HostAllowlistMiddleware),
        # v1 ships no GZipMiddleware anywhere (docs/spec-serve-ui.md section
        # 8): Starlette's gzip cannot exclude the SSE path and buffers
        # streams, which would defeat /api/events entirely. Loopback
        # bandwidth is free, so there is no compression middleware to
        # configure around this -- simply never add one.
    ]
    event_bus = EventBus(max_subscribers=max_event_subscribers, queue_size=event_queue_size)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette):
        # Starlette's lifespan startup phase runs on the server's event loop
        # (the only one this process ever uses -- section 5 requires a
        # single uvicorn worker), so this is the one and only place
        # `EventBus` learns which loop to bridge worker-thread `publish()`
        # calls onto. Nothing runs on shutdown: job worker threads are
        # daemon threads and are abandoned, not joined (section 7 Phase B).
        event_bus.attach_loop(asyncio.get_running_loop())
        yield

    app = Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
        exception_handlers={404: not_found, 500: server_error},
        lifespan=_lifespan,
    )
    app.state.artifact_store = artifact_store
    app.state.templates = templates
    app.state.event_bus = event_bus
    app.state.sse_keepalive_seconds = sse_keepalive_seconds
    app.state.job_runner = JobRunner(artifact_store.data_dir, event_bus=event_bus)
    # Per-process CSRF forcing-function token (docs/spec-serve-ui.md section
    # 8): minted once at app-creation time, embedded in the dashboard page
    # for jobs.js's fetch calls, never logged or put in a URL.
    app.state.job_csrf_token = new_job_csrf_token()
    # Cached once here rather than re-checked on every dashboard render
    # (unlike `jobs.mapshare_available()`, called per-render in views.py):
    # whether the `browser` extra's cookie reader (`rookiepy`) is importable
    # is a process-lifetime-stable fact, but `mapshare-state.json` can
    # change mid-session (this server's own mapshare job, or a concurrent
    # CLI `mapshare` run, can write it), so that one must stay live.
    # Referenced through the `jobs_module` import (not a direct
    # `from .jobs import explore_available`) so tests that monkeypatch
    # `garmin_outreach.serve.jobs.explore_available` before calling
    # `create_app()` still take effect here.
    app.state.explore_available = jobs_module.explore_available()
    return app


def run(
    data_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8477,
    open_browser: bool = True,
) -> None:
    """Run the serve app in the foreground until Ctrl-C.

    Only loopback hosts are accepted; remote access is out of scope for v1
    (docs/spec-serve-ui.md section 5). Nothing is printed to stdout — human
    logs go to stderr via uvicorn's default loggers, and the process exits 0
    on a clean shutdown.
    """
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(
            f"--host {host!r} is out of scope: garmin-outreach serve binds to loopback only "
            f"({', '.join(ALLOWED_HOSTS)}). For remote access, reach this machine over "
            "Tailscale or WireGuard instead; an authenticated remote-access mode may land in "
            "a future release."
        )
    # Build the app (and let creation failures surface) before anything
    # opens a browser tab.
    app = create_app(data_dir)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        workers=1,
        reload=False,
        access_log=False,
        # Bounds Ctrl-C: without this, uvicorn's default graceful shutdown
        # waits (`timeout=None`, i.e. forever) for every in-flight response
        # to finish on its own, and `GET /api/events` is a deliberately
        # unbounded stream that never finishes by itself -- an open browser
        # tab would otherwise hang shutdown indefinitely. 2s is generous for
        # the SSE generator's `finally` (queue unsubscribe, no I/O) to run.
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    if open_browser:
        display_host = f"[{host}]" if ":" in host else host
        url = f"http://{display_host}:{port}/"
        threading.Thread(target=_open_when_started, args=(server, url), daemon=True).start()
    try:
        server.run()
    except SystemExit as error:
        # uvicorn's startup() calls sys.exit(STARTUP_FAILURE) on an OSError
        # from the socket bind (e.g. the port is already in use) without
        # ever setting server.started; a clean Ctrl-C shutdown returns
        # normally instead of raising, so this branch is bind-failure-only.
        if not server.started:
            raise RuntimeError(
                f"Could not bind to {host}:{port} (already in use?). Pick another port with --port."
            ) from error
        raise


def _open_when_started(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + _BROWSER_OPEN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.started:
            webbrowser.open(url)
            return
        time.sleep(_BROWSER_OPEN_POLL_SECONDS)
