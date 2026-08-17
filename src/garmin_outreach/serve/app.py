"""App assembly and CLI entry point for `garmin-outreach serve`.

`create_app()` builds the phase-1 read-only Starlette app
(docs/spec-serve-ui.md sections 5-9); `run()` is the CLI entry point that
validates the loopback-only host, starts uvicorn as a single worker, and
opens a browser tab only after the socket has bound.

This module (not `garmin_outreach.serve`) is where the `ui` extra's
third-party imports (starlette/uvicorn/jinja2) live, so that
`garmin_outreach.serve.artifacts` stays importable without the extra
installed.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from pathlib import Path

import jinja2
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from .artifacts import ArtifactStore
from .security import ALLOWED_HOSTS, HostAllowlistMiddleware, SecurityHeadersMiddleware
from .views import (
    api_summary,
    dashboard,
    discover_datastar_filename,
    not_found,
    server_error,
    static_asset,
)

_BROWSER_OPEN_TIMEOUT_SECONDS = 10.0
_BROWSER_OPEN_POLL_SECONDS = 0.05


def create_app(data_dir: Path) -> Starlette:
    """Build the phase-1 read-only Starlette app over `data_dir`.

    Routes: `GET /` (dashboard), `GET /api/summary` (shaped JSON, never the
    raw file), `GET /static/{filename}` (vendored assets). Anything else is
    a friendly 404 (no traceback; `debug=False`).
    """
    artifact_store = ArtifactStore(data_dir)
    templates = jinja2.Environment(
        loader=jinja2.PackageLoader("garmin_outreach.serve", "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    templates.globals["datastar_filename"] = discover_datastar_filename()

    routes = [
        Route("/", dashboard, methods=["GET"]),
        Route("/api/summary", api_summary, methods=["GET"]),
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
    ]
    app = Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
        exception_handlers={404: not_found, 500: server_error},
    )
    app.state.artifact_store = artifact_store
    app.state.templates = templates
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
