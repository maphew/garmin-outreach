"""In-process job runner for the `serve` UI's build/mapshare/explore buttons.

Phase B of docs/spec-serve-ui.md section 7. This module owns *no* filesystem
mutation itself -- every job dispatches to the shared orchestration services
in `garmin_outreach.services` (`run_build`/`run_mapshare`/`run_explore_http`),
which in turn own the interprocess writer lock (`garmin_outreach.locking`).
This keeps CLI and UI behavior from drifting apart.

Job state is exposed both via a polling-friendly JSON snapshot
(`JobRunner.snapshot()`, consumed by `GET /api/jobs`) and, when an
`event_bus` is supplied, pushed live to `GET /api/events` (phase 4,
`events.py`) on job start, mapshare progress, and completion.

Threading model: `services` module is imported (not its individual
functions) so tests can monkeypatch `garmin_outreach.services.run_build`
etc. and have this module's calls pick up the stub.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import services
from ..mapshare import feed_url_for
from .events import EventBus

# --- CLI-default job parameters (docs/spec-serve-ui.md section 7 Phase B) --

DEFAULT_FORMATS: tuple[str, ...] = ("gpkg", "geojson", "shp")
DEFAULT_GAP_HOURS = 6.0
DEFAULT_MAX_SPEED_KMH = 200.0
DEFAULT_JUMP_KM = 10.0
DEFAULT_EXPLORE_FORMATS: tuple[str, ...] = ("kml",)

# (min, max) inclusive bounds for the only overrides a job POST body may
# supply. Anything else in the body is rejected outright (see
# `parse_cleanup_params`) -- never an identifier, feed_url, password, or
# cookie material from the browser.
_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "trip_gap_hours": (0.1, 168.0),
    "max_speed_kmh": (1.0, 2000.0),
    "jump_km": (0.1, 1000.0),
}
_PARAM_TO_SERVICE_KWARG = {
    "trip_gap_hours": "gap_hours",
    "max_speed_kmh": "max_speed_kmh",
    "jump_km": "jump_km",
}

MAPSHARE_UNAVAILABLE_HINT = "set GARMIN_MAPSHARE_ID or run garmin-outreach mapshare once"
EXPLORE_UNAVAILABLE_HINT = (
    "install the browser extra (uv sync --extra browser); the ui extra must also be installed"
)

JOB_KINDS: tuple[str, ...] = ("build", "mapshare", "explore")

_MAX_DETAIL_CHARS = 300


class InvalidJobParams(ValueError):
    """A job POST body failed validation (unknown key or out-of-range value)."""


def parse_cleanup_params(body: object) -> dict[str, float]:
    """Validate a job POST body against the bounded numeric overrides.

    Accepts only `trip_gap_hours`, `max_speed_kmh`, `jump_km` (all optional);
    anything else raises `InvalidJobParams`. Returns a dict keyed by the
    `services.run_*` kwarg names (`gap_hours`, `max_speed_kmh`, `jump_km`),
    filled in with CLI defaults for any key the body omitted.
    """
    if not isinstance(body, dict):
        raise InvalidJobParams("request body must be a JSON object")
    unknown = set(body) - set(_PARAM_BOUNDS)
    if unknown:
        raise InvalidJobParams(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    result: dict[str, float] = {
        "gap_hours": DEFAULT_GAP_HOURS,
        "max_speed_kmh": DEFAULT_MAX_SPEED_KMH,
        "jump_km": DEFAULT_JUMP_KM,
    }
    for name, (low, high) in _PARAM_BOUNDS.items():
        if name not in body:
            continue
        value = body[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidJobParams(f"{name} must be a number")
        numeric = float(value)
        if not (low <= numeric <= high):
            raise InvalidJobParams(f"{name} must be between {low} and {high}")
        result[_PARAM_TO_SERVICE_KWARG[name]] = numeric
    return result


# --- Capability detection ---------------------------------------------


def resolve_mapshare_identifier(data_dir: Path) -> str | None:
    """Resolve a MapShare identifier without ever taking one from the browser.

    `GARMIN_MAPSHARE_ID` (env) wins; otherwise the `feed_url` already
    recorded in `data/mapshare-state.json` is read tolerantly and
    revalidated through `mapshare.feed_url_for` (the same host allowlist the
    CLI uses). Returns `None` if neither source yields a usable identifier --
    callers surface that as "job not startable", never a 500.
    """
    env_value = os.environ.get("GARMIN_MAPSHARE_ID")
    if env_value:
        return env_value
    state_path = Path(data_dir) / "mapshare-state.json"
    try:
        raw = state_path.read_bytes()
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    feed_url = parsed.get("feed_url")
    if not isinstance(feed_url, str) or not feed_url.strip():
        return None
    try:
        return feed_url_for(feed_url)
    except ValueError:
        return None


def mapshare_available(data_dir: Path) -> bool:
    return resolve_mapshare_identifier(data_dir) is not None


def explore_available() -> bool:
    """Whether the `browser` extra's cookie reader (`rookiepy`) is importable."""
    return importlib.util.find_spec("rookiepy") is not None


# --- Message scrubbing --------------------------------------------------

# Four alternatives, tried in order at each scan position:
#
# - `url`: any `scheme://...` (http(s), file, whatever) up to the next
#   whitespace/quote/angle-bracket, replaced wholesale with "<url>" -- a
#   MapShare feed URL's identifier lives in the path, not just the host, so
#   nothing short of dropping the whole URL is safe. Trying this alternative
#   first is what stops the drive-letter alternative below from matching the
#   "s://" in "https://" as if "s" were a drive letter (the lookbehind on
#   the drive alternative is defense in depth for the same failure mode when
#   a URL match isn't attempted at that position, e.g. mid-string).
# - `unc`: a `\\server\share\...` UNC path.
# - `drive`: a Windows drive-letter path (either slash style). The
#   lookbehind requires the char before the letter to not be alphanumeric or
#   ":" (or be the start of the string), so "https" can't donate its "s" as
#   a fake drive letter.
# - `posix`: a POSIX absolute path, stopping at whitespace/quotes/brackets
#   so trailing punctuation in a sentence ("... in <path>.") is not
#   swallowed. Its own negative lookbehind avoids matching mid-URL segments.
#
# `unc` and `drive` deliberately do NOT stop at whitespace -- Windows paths
# routinely contain spaces (`C:\Users\Matt Wilkie\...`) -- only at the next
# colon/quote/angle-bracket/pipe/wildcard/newline, so a trailing
# "<path>: <reason>" clause still separates cleanly. Whatever tail is
# captured is then reduced to its last `\`/`/`-delimited segment.
_SCRUB_PATTERN = re.compile(
    r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)"
    r"|(?P<unc>\\\\[^:\"'<>|?*\r\n]*)"
    r"|(?P<drive>(?<![A-Za-z0-9:])[A-Za-z]:[\\/][^:\"'<>|?*\r\n]*)"
    r"|(?P<posix>(?<![\w:\\/])/[^\s\"'()<>\[\]{};,]*)"
)


def _basename(path_text: str) -> str:
    trimmed = path_text.rstrip("\\/")
    for part in reversed(re.split(r"[\\/]+", trimmed)):
        if part:
            return part
    return path_text


def _scrub_match(match: re.Match[str]) -> str:
    if match.group("url") is not None:
        return "<url>"
    return _basename(match.group(0))


def scrub_message(text: str | None, data_dir: Path | None = None) -> str | None:
    """Replace absolute-path/URL substrings with a safe stand-in, capped length.

    Never trust an exception message to be free of `data_dir` (writer-lock
    contention messages embed it -- see `locking._contention_error`). When
    `data_dir` is given, its exact string form is replaced with "data"
    first -- it is the one path this module always knows in full, so an
    exact literal replacement is more reliable than the regex pass that
    follows. The regex pass is then a blunt, string-level scrub, not a
    parser: any substring that *looks* like an absolute path or URL is
    reduced to its last path segment (or, for URLs, dropped entirely).
    """
    if text is None:
        return None
    scrubbed = text
    if data_dir is not None:
        scrubbed = scrubbed.replace(str(data_dir), "data")
    scrubbed = _SCRUB_PATTERN.sub(_scrub_match, scrubbed)
    if len(scrubbed) > _MAX_DETAIL_CHARS:
        scrubbed = scrubbed[:_MAX_DETAIL_CHARS].rstrip() + "\u2026"
    return scrubbed


# --- Job runner -----------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _Job:
    __slots__ = ("kind", "state", "started_utc", "finished_utc", "detail", "windows_done")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.state = "running"
        self.started_utc = _now_iso()
        self.finished_utc: str | None = None
        self.detail: str | None = None
        self.windows_done = 0


def _snapshot(job: _Job) -> dict[str, Any]:
    return {
        "kind": job.kind,
        "state": job.state,
        "started_utc": job.started_utc,
        "finished_utc": job.finished_utc,
        "detail": job.detail,
        "progress": {"windows_done": job.windows_done} if job.kind == "mapshare" else None,
    }


def _invoke_build(data_dir: Path, params: dict[str, Any], progress: Callable[[dict], None]) -> Any:
    return services.run_build(data_dir, **params)


def _invoke_mapshare(
    data_dir: Path, params: dict[str, Any], progress: Callable[[dict], None]
) -> Any:
    identifier = params.pop("identifier")
    username = params.pop("username", "")
    password = params.pop("password", None)
    return services.run_mapshare(
        identifier,
        data_dir,
        username=username,
        password=password,
        progress=progress,
        **params,
    )


def _invoke_explore(
    data_dir: Path, params: dict[str, Any], progress: Callable[[dict], None]
) -> Any:
    return services.run_explore_http(data_dir, **params)


_DISPATCH: dict[str, Callable[[Path, dict[str, Any], Callable[[dict], None]], Any]] = {
    "build": _invoke_build,
    "mapshare": _invoke_mapshare,
    "explore": _invoke_explore,
}


class JobRunner:
    """Process-wide single-flight runner for build/mapshare/explore jobs.

    One job at a time: `start()` while a job is already running returns
    `(False, <running job's snapshot>)` instead of starting a second one
    (the route layer turns that into a 409). Jobs run on daemon threads --
    Ctrl-C exits the server; in-flight network work is abandoned, not
    cancelled (docs/spec-serve-ui.md section 7 Phase B).
    """

    def __init__(self, data_dir: Path, *, event_bus: EventBus | None = None) -> None:
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._current: _Job | None = None
        self._last: dict[str, dict[str, Any]] = {}
        self._event_bus = event_bus

    def _publish(self, event: dict[str, Any]) -> None:
        # A dying/misbehaving event bus must never break job execution --
        # this is called from the worker thread (progress/completion) and
        # from the request-handling thread (job start), and both call
        # sites must be able to trust it can't raise.
        if self._event_bus is None:
            return
        with contextlib.suppress(Exception):
            self._event_bus.publish(event)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            current = _snapshot(self._current) if self._current is not None else None
            last = dict(self._last)
        return {"current": current, "last": last}

    def start(self, kind: str, **params: Any) -> tuple[bool, dict[str, Any]]:
        if kind not in _DISPATCH:
            raise ValueError(f"unknown job kind: {kind!r}")
        with self._lock:
            if self._current is not None:
                return False, _snapshot(self._current)
            job = _Job(kind)
            self._current = job
            accepted_snapshot = _snapshot(job)
        thread = threading.Thread(
            target=self._run, args=(job, params), name=f"garmin-outreach-job-{kind}", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:
            # thread.start() failing (e.g. can't start a new thread) must not
            # wedge the runner: `_current` was committed above, but nothing
            # will ever clear it if we let this propagate as-is. Re-acquire
            # the lock, roll back the commit, and let the caller see the
            # failure (the route layer turns an unhandled exception into a
            # 500, which is correct here -- this is a real, if rare, error).
            with self._lock:
                if self._current is job:
                    self._current = None
            raise
        # Published only once the worker thread has actually started (never
        # on the roll-back path above): docs/spec-serve-ui.md section 7
        # Phase B SSE contract, "on job start".
        self._publish({"kind": job.kind, "type": "job", "snapshot": accepted_snapshot})
        return True, accepted_snapshot

    def _progress_callback(self, job: _Job) -> Callable[[dict], None]:
        def _callback(event: dict) -> None:
            # Non-blocking: only bumps a counter under the runner's lock.
            # Only "window" events represent a completed MapShare polling
            # window; other stages (if ever added) must not inflate the
            # windows-done count the dashboard shows.
            if event.get("stage") != "window":
                return
            snapshot: dict[str, Any] | None = None
            with self._lock:
                if self._current is job:
                    job.windows_done += 1
                    snapshot = _snapshot(job)
            if snapshot is not None:
                # Published outside the lock: `EventBus.publish()` only
                # schedules a loop callback (`call_soon_threadsafe`), but
                # there is no reason to hold the runner's lock across it.
                self._publish({"type": "progress", "snapshot": snapshot})

        return _callback

    def _run(self, job: _Job, params: dict[str, Any]) -> None:
        invoke = _DISPATCH[job.kind]
        progress = self._progress_callback(job) if job.kind == "mapshare" else None
        reraise: BaseException | None = None
        try:
            invoke(self.data_dir, dict(params), progress)
            final_state = "succeeded"
            detail = None
        except services.BuildFailedAfterAcquisition as error:
            final_state = "partial_success"
            detail = (
                f"data acquired and archived; rebuild failed: "
                f"{scrub_message(str(error), self.data_dir)}"
            )
        except BaseException as error:  # noqa: BLE001 - a dying worker must still clear `_current`
            final_state = "failed"
            detail = scrub_message(str(error), self.data_dir)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                reraise = error
        with self._lock:
            job.state = final_state
            job.finished_utc = _now_iso()
            job.detail = detail
            final_snapshot = _snapshot(job)
            self._last[job.kind] = final_snapshot
            if self._current is job:
                self._current = None
        # "on completion" (docs/spec-serve-ui.md section 7 Phase B SSE
        # contract) -- published even on the KeyboardInterrupt/SystemExit
        # reraise path below, since `_current`/`_last` are already final by
        # this point regardless.
        self._publish({"kind": job.kind, "type": "job", "snapshot": final_snapshot})
        if reraise is not None:
            raise reraise
