"""Route handlers for the loopback-only `serve` surface.

Every handler renders shaped data from `artifacts.ArtifactStore` — never the
raw `summary.json` — and all artifact-derived text reaches templates through
Jinja2's default autoescape (no `|safe`, no `Markup`).
"""

from __future__ import annotations

import asyncio
import importlib.resources as resources
import os
import re
from pathlib import PurePosixPath

from datastar_py.sse import ServerSentEventGenerator
from datastar_py.starlette import DatastarResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import Receive, Scope, Send

from . import jobs
from .artifacts import LAYERS
from .events import EventBus
from .security import _ALLOWED_SEC_FETCH_SITE as _GET_ALLOWED_SEC_FETCH_SITE
from .security import BASELINE_HEADERS, check_job_csrf

_STATIC_PACKAGE = "garmin_outreach.serve"
_STATIC_RESOURCE_NAME = "static"

# Default keepalive interval for GET /api/events (docs/spec-serve-ui.md
# section 7 Phase B SSE contract, "~15 s"). `create_app(sse_keepalive_seconds=...)`
# overrides this per-app, which tests use to keep the keepalive assertion fast.
DEFAULT_SSE_KEEPALIVE_SECONDS = 15.0

# A bare SSE comment line -- protocol-legal SSE, but deliberately not a
# datastar-py protocol event (there is no keepalive helper on
# `ServerSentEventGenerator`; hand-formatting only this inert framing line,
# never an actual `datastar-patch-*` event, keeps the "protocol events go
# through the SDK" rule from docs/spec-serve-ui.md section 7 Phase B intact).
_SSE_KEEPALIVE_COMMENT = ": keepalive\n\n"

# Point vs. line rendering hint for map.js, mirrored from docs/spec-serve-ui.md
# section 6/8 -- kept next to the registry rather than duplicated client-side.
POINT_LAYERS = frozenset({"track_points", "messages", "waypoints", "events"})
LINE_LAYERS = frozenset({"tracks", "routes", "courses", "trips"})
# Every layer in the registry must be classified as exactly point or line --
# a layer added to LAYERS without updating one of the sets above would
# otherwise silently fall through to the line-rendering branch below.
assert set(LAYERS) == POINT_LAYERS | LINE_LAYERS

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

# Job states that end a job for good (jobs.py's `_Job.state`, set once in
# `JobRunner._run`'s completion block). A "job" SSE event carrying one of
# these must invalidate `ArtifactStore._raw_scan_cache` before the
# `#summary-live` patch is rendered -- see `api_events` below.
_TERMINAL_JOB_STATES = frozenset({"succeeded", "partial_success", "failed"})


def _freshness_command(state: str) -> str | None:
    return _FRESHNESS_COMMANDS.get(state)


def _layer_kind(name: str) -> str:
    # `capabilities["layers_present"]` normally only ever contains registry
    # layer names (LAYERS), guaranteed classified by the module-level assert
    # above -- but it is derived from summary.json, which is untrusted data
    # (docs/spec-serve-ui.md section 8), so an unrecognized name here must
    # degrade to a safe default rather than raise.
    if name in POINT_LAYERS:
        return "point"
    if name in LINE_LAYERS:
        return "line"
    return "line"


def _jobs_view_state(request: Request) -> dict:
    """Shaped jobs-section state for the dashboard template.

    Capability reads (env vars, `mapshare-state.json`, `rookiepy` presence)
    live in `jobs.py`, never in the template (docs/spec-serve-ui.md section 7
    Phase B).
    """
    store = request.app.state.artifact_store
    runner: jobs.JobRunner = request.app.state.job_runner
    snapshot = runner.snapshot()
    return {
        # Re-checked on every render: `mapshare-state.json` can change
        # mid-session (this server's own mapshare job, or a concurrent CLI
        # `mapshare` run, can write it) -- unlike `explore_available` below.
        "mapshare_available": jobs.mapshare_available(store.data_dir),
        "mapshare_hint": jobs.MAPSHARE_UNAVAILABLE_HINT,
        # Cached at app-creation time (`app.py::create_app`): whether the
        # `browser` extra's cookie reader is importable is a
        # process-lifetime-stable fact, so a per-render `importlib` scan
        # would be pure overhead.
        "explore_available": request.app.state.explore_available,
        "explore_hint": jobs.EXPLORE_UNAVAILABLE_HINT,
        "current": snapshot["current"],
        "last": snapshot["last"],
    }


def _render_job_status_html(request: Request) -> str:
    """Render the `#job-status` fragment shared by the dashboard's full page
    render (`_jobs_section.html` includes `_job_status.html`) and every
    `GET /api/events` patch (`api_events` below) -- one source of truth so
    the two can never drift (docs/spec-serve-ui.md section 7 Phase B).
    """
    template = request.app.state.templates.get_template("_job_status.html")
    return template.render(jobs=_jobs_view_state(request))


def _render_summary_html(request: Request) -> str:
    """Render the `#summary-live` fragment (`_summary_section.html`) from a
    freshly re-read `shaped_summary()` -- freshness state, layer counts, and
    capabilities. Shared by `dashboard()`'s full page render and every
    `GET /api/events` "job" patch (`api_events` below), same one-source-of-
    truth reasoning as `_render_job_status_html` above (docs/spec-serve-ui.md
    section 7 Phase B/4: "on completion push refreshed fragments" so a tab
    left open across a job run does not show stale freshness/layer counts).
    """
    store = request.app.state.artifact_store
    summary = store.shaped_summary()
    template = request.app.state.templates.get_template("_summary_section.html")
    return template.render(
        summary=summary,
        layers=LAYERS,
        freshness_command=_freshness_command(summary["freshness"]["state"]),
    )


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
        jobs=_jobs_view_state(request),
        job_csrf_token=request.app.state.job_csrf_token,
    )
    return HTMLResponse(html)


def api_summary(request: Request) -> JSONResponse:
    # Sync for the same reason as `dashboard()` above.
    store = request.app.state.artifact_store
    return JSONResponse(store.shaped_summary())


def _parse_page(raw: str | None) -> int:
    """Tolerant page-number parse: missing/non-numeric input -> page 1.

    Out-of-range values (negative, past the last page) are clamped by
    `ArtifactStore.messages()` itself, not here.
    """
    if raw is None:
        return 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def messages(request: Request) -> HTMLResponse:
    # Sync for the same reason as `dashboard()` above.
    store = request.app.state.artifact_store
    page = _parse_page(request.query_params.get("page"))
    data = store.messages(page)
    template = request.app.state.templates.get_template("messages.html")
    html = template.render(messages=data)
    return HTMLResponse(html)


def map_view(request: Request) -> HTMLResponse:
    # Sync for the same reason as `dashboard()` above.
    store = request.app.state.artifact_store
    summary = store.shaped_summary()
    capabilities = summary["capabilities"]
    layers_config: dict[str, dict] = {}
    if capabilities["geojson_available"]:
        for name in capabilities["layers_present"]:
            layers_config[name] = {
                "count": summary["layers"].get(name),
                "bbox": summary["bbox"].get(name),
                "kind": _layer_kind(name),
            }
    map_config = {"layers": layers_config}
    template = request.app.state.templates.get_template("map.html")
    html = template.render(
        summary=summary,
        map_config=map_config,
        freshness_command=_freshness_command(summary["freshness"]["state"]),
    )
    return HTMLResponse(html)


def api_layer_geojson(request: Request) -> Response:
    # Sync for the same reason as `dashboard()` above.
    name = request.path_params["name"]
    error_headers = {"Cache-Control": "no-store"}
    if name not in LAYERS or "/" in name or "\\" in name or ".." in name:
        return JSONResponse(
            {"error": "layer not available"}, status_code=404, headers=error_headers
        )
    store = request.app.state.artifact_store
    data = store.layer_geojson(name)
    if data is None:
        return JSONResponse(
            {"error": "layer not available"}, status_code=404, headers=error_headers
        )
    return Response(
        content=data,
        media_type="application/geo+json",
        headers={"Cache-Control": "no-store"},
    )


# --- Jobs (docs/spec-serve-ui.md section 7 Phase B, section 8) ------------


async def _job_csrf_and_params(request: Request) -> tuple[Response | None, dict | None]:
    """Shared CSRF + body-validation prologue for every job POST route.

    Returns `(error_response, None)` if the request must be rejected, or
    `(None, params)` with `params` ready to merge into `runner.start()`.
    """
    csrf_error = check_job_csrf(request, request.app.state.job_csrf_token)
    if csrf_error is not None:
        return csrf_error, None
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400), None
    try:
        params = jobs.parse_cleanup_params(body)
    except jobs.InvalidJobParams as error:
        return JSONResponse({"error": str(error)}, status_code=400), None
    return None, params


async def api_jobs_build(request: Request) -> Response:
    error, params = await _job_csrf_and_params(request)
    if error is not None:
        return error
    runner: jobs.JobRunner = request.app.state.job_runner
    accepted, snapshot = runner.start("build", formats=jobs.DEFAULT_FORMATS, **params)
    return JSONResponse(snapshot, status_code=202 if accepted else 409)


async def api_jobs_mapshare(request: Request) -> Response:
    error, params = await _job_csrf_and_params(request)
    if error is not None:
        return error
    store = request.app.state.artifact_store
    identifier = jobs.resolve_mapshare_identifier(store.data_dir)
    if identifier is None:
        # Naming the env var, never the (possibly-resolved) identifier or
        # feed_url itself, in the response body.
        return JSONResponse({"error": jobs.MAPSHARE_UNAVAILABLE_HINT}, status_code=400)
    runner: jobs.JobRunner = request.app.state.job_runner
    accepted, snapshot = runner.start(
        "mapshare",
        identifier=identifier,
        username=os.environ.get("GARMIN_MAPSHARE_USERNAME", ""),
        password=os.environ.get("GARMIN_MAPSHARE_PASSWORD"),
        formats=jobs.DEFAULT_FORMATS,
        **params,
    )
    return JSONResponse(snapshot, status_code=202 if accepted else 409)


async def api_jobs_explore(request: Request) -> Response:
    error, params = await _job_csrf_and_params(request)
    if error is not None:
        return error
    if not jobs.explore_available():
        return JSONResponse({"error": jobs.EXPLORE_UNAVAILABLE_HINT}, status_code=400)
    runner: jobs.JobRunner = request.app.state.job_runner
    accepted, snapshot = runner.start(
        "explore",
        export_formats=jobs.DEFAULT_EXPLORE_FORMATS,
        formats=jobs.DEFAULT_FORMATS,
        **params,
    )
    return JSONResponse(snapshot, status_code=202 if accepted else 409)


def api_jobs_snapshot(request: Request) -> JSONResponse:
    runner: jobs.JobRunner = request.app.state.job_runner
    return JSONResponse(runner.snapshot())


class _AlwaysReleasingDatastarResponse(DatastarResponse):
    """`DatastarResponse` that unconditionally releases an `/api/events`
    subscriber slot when its ASGI `__call__` returns *or raises* -- covering
    a gap `background=` alone does not (review finding 7): Starlette's
    `StreamingResponse.__call__` only awaits `self.background` on the
    success path, and skips it whenever `send()` itself raises (verified
    empirically against the vendored Starlette version in this project's
    toolchain). That matters here because a response cancelled/aborted
    before the very first `send()` call -- i.e. before `_stream()`'s async
    generator body has executed even once -- never reaches that generator's
    own `finally` either (an async generator's body does not start running
    until its first `__anext__()`), so neither release path would otherwise
    fire and the slot would leak until CPython's async-generator finalizer
    happens to garbage-collect it, at some unbounded later time. Wrapping
    the whole ASGI call in a plain `try/finally` here is unconditional
    regardless of *where* in that call `send()`/iteration fails.
    `background=` is still passed through to the base class as a second,
    now-redundant release path (`EventBus.unsubscribe()`'s `discard()` makes
    a double release harmless) -- kept because it is one line and free
    insurance against a future refactor of this class.
    """

    def __init__(
        self, *args: object, bus: EventBus, queue: asyncio.Queue, **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)
        self._release_bus = bus
        self._release_queue = queue

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release_bus.unsubscribe(self._release_queue)


async def api_events(request: Request) -> Response:
    """GET /api/events -- SSE job-state stream (docs/spec-serve-ui.md
    section 7 Phase B "SSE contract").

    Every connect (including a reconnect after a dropped stream) first
    receives an authoritative full-state patch of `#job-status`, so a
    missed event can never wedge the UI -- the client just resyncs on the
    next connection. From then on, each event published to the bus (job
    start, mapshare progress, completion) triggers exactly one more
    re-render of that same fragment; idempotent by construction, so the
    bus's queue-overflow coalescing (dropping intermediate events under
    backpressure) is always safe. A "job" event (start or completion, never
    "progress") additionally triggers a second patch of `#summary-live`
    (freshness/layers/capabilities) from a fresh `shaped_summary()` read, so
    a tab left open across a job run does not show stale freshness/layer
    counts (docs/spec-serve-ui.md section 7 Phase 4). Idle connections get a
    raw SSE comment line every `sse_keepalive_seconds` (app.state, default
    `DEFAULT_SSE_KEEPALIVE_SECONDS`) so intermediaries do not time the
    connection out.

    Same-origin GET only; no CSRF handshake is needed (unlike the job POST
    routes) -- the host allowlist and security-headers middleware already
    wrap this route like every other. `HEAD` is rejected outright (405): a
    HEAD request holding a subscriber slot open forever would let a hostile
    page pin the cap for free.
    """
    if request.method == "HEAD":
        return JSONResponse(
            {"error": "method not allowed"},
            status_code=405,
            headers={"Cache-Control": "no-store"},
        )
    # Cap-protection only, not a confidentiality control -- contrast
    # `check_job_csrf`'s POST defense, which also gates a *mutation* an
    # attacker's page could otherwise trigger. A GET here only opens a
    # stream the requesting page can already read same-origin, so an
    # *absent* `Sec-Fetch-Site` header (older browsers, non-fetch/
    # EventSource clients) is accepted rather than rejected -- unlike the
    # POST job routes' stricter all-three-checks defense. A cross-site page
    # sending this request (e.g. a `no-cors` `fetch`/`EventSource`, which
    # browsers do send with `Sec-Fetch-Site: cross-site`) is rejected purely
    # to stop it from pinning the subscriber cap; it can read no response
    # data either way (opaque `no-cors` response).
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site is not None and sec_fetch_site not in _GET_ALLOWED_SEC_FETCH_SITE:
        return JSONResponse(
            {"error": "request rejected"},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    bus: EventBus = request.app.state.event_bus
    queue = bus.subscribe()
    if queue is None:
        # Datastar's default `retryDuration`/"auto" retry mode does not
        # retry a `503` (or a clean `200` stream end) -- an already-open tab
        # that lands here needs a manual reload once a slot frees up, not
        # just time. `no-transform` alongside `no-store` for consistency
        # with the streaming response's own Cache-Control below.
        return JSONResponse(
            {"error": "too many /api/events connections"},
            status_code=503,
            headers={"Cache-Control": "no-store, no-transform"},
        )
    keepalive_seconds = getattr(
        request.app.state, "sse_keepalive_seconds", DEFAULT_SSE_KEEPALIVE_SECONDS
    )

    async def _stream():
        try:
            yield ServerSentEventGenerator.patch_elements(_render_job_status_html(request))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
                except TimeoutError:
                    yield _SSE_KEEPALIVE_COMMENT
                    continue
                yield ServerSentEventGenerator.patch_elements(_render_job_status_html(request))
                if event.get("type") == "job":
                    snapshot = event.get("snapshot")
                    state = snapshot.get("state") if isinstance(snapshot, dict) else None
                    if state in _TERMINAL_JOB_STATES:
                        # Force a rescan of raw/ instead of trusting the ~5s
                        # cache: a job-start event usually populated it
                        # before this job acquired anything, and a job that
                        # archives new raw data but fails its rebuild within
                        # that window must not have its completion patch
                        # reuse the pre-job cached value (no later event
                        # would ever correct it).
                        request.app.state.artifact_store.invalidate_raw_scan_cache()
                    # Blocking file IO (same as `dashboard()`'s own
                    # `shaped_summary()` call) -- off the loop via
                    # `to_thread` since, unlike a sync route handler,
                    # nothing runs this async generator in a threadpool for
                    # us.
                    summary_html = await asyncio.to_thread(_render_summary_html, request)
                    yield ServerSentEventGenerator.patch_elements(summary_html)
        finally:
            # Disconnect (CancelledError from the client closing the
            # connection) or any other exit path -- a leaked subscriber
            # slot would eventually starve every other client via the
            # bus's subscriber cap. Kept even though
            # `_AlwaysReleasingDatastarResponse.__call__` below also
            # releases unconditionally: this is the path that actually
            # fires for every ordinary disconnect, and
            # `EventBus.unsubscribe()` tolerates the double call.
            bus.unsubscribe(queue)

    return _AlwaysReleasingDatastarResponse(
        _stream(),
        # `DatastarResponse.default_headers` ships `Cache-Control: no-cache`;
        # explicitly override to `no-store, no-transform` per
        # docs/spec-serve-ui.md section 8 (SecurityHeadersMiddleware's own
        # `Cache-Control` default is `setdefault`-applied, so this value
        # survives that middleware layer unchanged).
        headers={"Cache-Control": "no-store, no-transform"},
        background=BackgroundTask(bus.unsubscribe, queue),
        bus=bus,
        queue=queue,
    )


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


def _discover_static_asset(*, prefix: str, suffix: str, exclude: str | None = None) -> str:
    """Find a vendored `<prefix>*<suffix>` asset by globbing static/.

    Keeps content-hashed names in one place so a re-vendor never needs a
    template edit. `exclude`, when given, skips filenames also containing
    that substring (e.g. so the CSP bundle glob doesn't also match its
    worker file, whose name shares the same prefix).
    """
    static_dir = resources.files(_STATIC_PACKAGE).joinpath(_STATIC_RESOURCE_NAME)
    for entry in static_dir.iterdir():
        name = entry.name
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        if exclude is not None and exclude in name:
            continue
        return name
    raise RuntimeError(
        f"No vendored {prefix}*{suffix} asset found under "
        f"{_STATIC_PACKAGE}/{_STATIC_RESOURCE_NAME}/"
    )


def discover_maplibre_js_filename() -> str:
    """Find the vendored CSP-bundle `maplibre-gl-csp-<hash>.js` filename."""
    return _discover_static_asset(prefix="maplibre-gl-csp-", suffix=".js", exclude="worker")


def discover_maplibre_worker_filename() -> str:
    """Find the vendored `maplibre-gl-csp-worker-<hash>.js` filename."""
    return _discover_static_asset(prefix="maplibre-gl-csp-worker-", suffix=".js")


def discover_maplibre_css_filename() -> str:
    """Find the vendored `maplibre-gl-<hash>.css` filename."""
    return _discover_static_asset(prefix="maplibre-gl-", suffix=".css")
