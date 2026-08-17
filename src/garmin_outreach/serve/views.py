"""Route handlers for the phase-1 read-only `serve` surface.

Every handler renders shaped data from `artifacts.ArtifactStore` — never the
raw `summary.json` — and all artifact-derived text reaches templates through
Jinja2's default autoescape (no `|safe`, no `Markup`).
"""

from __future__ import annotations

import importlib.resources as resources
import re
from pathlib import PurePosixPath

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .artifacts import LAYERS
from .security import BASELINE_HEADERS

_STATIC_PACKAGE = "garmin_outreach.serve"
_STATIC_RESOURCE_NAME = "static"

# Letters, digits, dash, dot only; no separators. Belt-and-braces on top of
# the route's own inability to match a segment containing "/" (Starlette
# decodes percent-escapes before matching, so `..%2Fx` and `%2e%2e/` never
# reach this handler at all — see docs/spec-serve-ui.md section 8).
_STATIC_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_HASHED_FILENAME_RE = re.compile(r"-[0-9a-f]{8}\.")
_STATIC_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# Command hints per freshness state (docs/spec-serve-ui.md section 7). "ok"
# has no fix command; mapshare_last_success_utc is rendered separately when
# present, regardless of the primary state.
_FRESHNESS_COMMANDS = {
    "outputs_missing": "garmin-outreach build",
    "outputs_stale": "garmin-outreach build",
    "freshness_unknown": "garmin-outreach build",
}


def _freshness_command(state: str) -> str | None:
    return _FRESHNESS_COMMANDS.get(state)


def dashboard(request: Request) -> HTMLResponse:
    # Sync handler: `ArtifactStore.shaped_summary()` does blocking file IO,
    # and Starlette runs sync endpoints in a threadpool instead of on the
    # event loop (docs/spec-serve-ui.md section 8).
    store = request.app.state.artifact_store
    summary = store.shaped_summary()
    template = request.app.state.templates.get_template("dashboard.html")
    html = template.render(
        summary=summary,
        layers=LAYERS,
        freshness_command=_freshness_command(summary["freshness"]["state"]),
    )
    return HTMLResponse(html)


def api_summary(request: Request) -> JSONResponse:
    # Sync for the same reason as `dashboard()` above.
    store = request.app.state.artifact_store
    return JSONResponse(store.shaped_summary())


def _read_static_bytes(filename: str) -> bytes | None:
    try:
        resource = resources.files(_STATIC_PACKAGE).joinpath(_STATIC_RESOURCE_NAME, filename)
        if not resource.is_file():
            return None
        return resource.read_bytes()
    except OSError:
        return None


async def static_asset(request: Request) -> Response:
    filename = request.path_params["filename"]
    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
        or not _STATIC_FILENAME_RE.match(filename)
    ):
        raise HTTPException(status_code=404)
    suffix = PurePosixPath(filename).suffix.lower()
    content_type = _STATIC_CONTENT_TYPES.get(suffix)
    if content_type is None:
        # Also excludes VENDORED.md / LICENSE-datastar.md: only .js/.css ship.
        raise HTTPException(status_code=404)
    data = _read_static_bytes(filename)
    if data is None:
        raise HTTPException(status_code=404)
    cache_control = (
        "public, max-age=31536000, immutable"
        if _HASHED_FILENAME_RE.search(filename)
        else "no-store"
    )
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": cache_control},
    )


async def not_found(request: Request, exc: HTTPException) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return HTMLResponse(
        "<!doctype html><title>Not found</title><p>Not found.</p>",
        status_code=404,
    )


async def server_error(request: Request, exc: Exception) -> Response:
    """Inert 500 fallback for uncaught exceptions.

    Registered under Starlette's special `500` exception-handler key, which
    Starlette wires into `ServerErrorMiddleware` as its `handler` rather than
    `ExceptionMiddleware`. `ServerErrorMiddleware` is always the outermost
    layer, so it calls this handler with the raw ASGI `send` -- our own
    `SecurityHeadersMiddleware` never gets a chance to touch the response.
    The headers are therefore set here directly rather than relied upon from
    the middleware stack.
    """
    return Response(
        "Internal Server Error",
        status_code=500,
        media_type="text/plain",
        headers=dict(BASELINE_HEADERS),
    )


def discover_datastar_filename() -> str:
    """Find the vendored `datastar-<hash>.js` filename by globbing static/.

    Keeps the content-hashed name in one place so a re-vendor never needs a
    template edit.
    """
    static_dir = resources.files(_STATIC_PACKAGE).joinpath(_STATIC_RESOURCE_NAME)
    for entry in static_dir.iterdir():
        if entry.name.startswith("datastar-") and entry.name.endswith(".js"):
            return entry.name
    raise RuntimeError(
        f"No vendored datastar-*.js asset found under {_STATIC_PACKAGE}/{_STATIC_RESOURCE_NAME}/"
    )
