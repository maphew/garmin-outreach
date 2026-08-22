"""Tests for the `serve` SSE event bus and `GET /api/events`.

docs/spec-serve-ui.md section 7 Phase B ("SSE contract"). Mirrors the
`importorskip` guards in test_serve_app.py/test_serve_jobs.py so a bare
environment (no `ui` extra) degrades to skip rather than a collection
error.

Streaming-response testing note: neither `starlette.testclient.TestClient`
(fully synchronous -- its `_TestClientTransport.handle_request()` blocks on
`portal.call(self.app, ...)` until the *entire* ASGI call returns) nor
`httpx.ASGITransport` (its `handle_async_request()` does the same: `await
self.app(...)` before constructing a `Response`) deliver body chunks to the
caller incrementally while the ASGI app is still running -- both fully
drain the response first. `GET /api/events` is a deliberately unbounded
stream (it only ever ends on client disconnect), so neither client flavor
can be used to read it. This file drives the ASGI `app(scope, receive,
send)` callable directly instead: `send()` pushes each body chunk onto a
queue as it is produced, and "disconnect" is simulated by flipping a flag
that `receive()` turns into an `http.disconnect` message -- the same
signal `starlette.responses.StreamingResponse.listen_for_disconnect` reacts
to for a real dropped TCP connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources as resources
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("uvicorn")
pytest.importorskip("jinja2")
pytest.importorskip("datastar_py")

import uvicorn
from datastar_py import consts as datastar_consts
from starlette.applications import Starlette

import garmin_outreach.services as services_module
from garmin_outreach.serve.app import create_app
from garmin_outreach.serve.events import EventBus
from garmin_outreach.serve.views import discover_datastar_filename


class _SSEStream:
    """Drives one `GET /api/events` connection directly at the ASGI level.

    See module docstring for why this bypasses both TestClient flavors.
    Use as `async with _SSEStream(app) as stream: ...`.
    """

    def __init__(
        self,
        app: Starlette,
        *,
        path: str = "/api/events",
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self._app = app
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1"), *(extra_headers or [])],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
            "state": {},
        }
        self._body_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._task: asyncio.Task | None = None
        self._buffer = ""
        self.status_code: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []

    async def _receive(self) -> dict:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status_code = message["status"]
            self.headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await self._body_queue.put(body)
            if not message.get("more_body", False):
                await self._body_queue.put(None)

    async def __aenter__(self) -> _SSEStream:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        return self

    async def wait_for_start(self, *, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.status_code is None:
            if loop.time() > deadline:
                raise AssertionError("response never started")
            await asyncio.sleep(0.01)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.decode().lower() == lowered:
                return value.decode()
        return None

    async def read_frame(self, *, timeout: float = 5.0) -> str:
        """Read one complete `\\n\\n`-terminated SSE frame (event or comment)."""
        while "\n\n" not in self._buffer:
            item = await asyncio.wait_for(self._body_queue.get(), timeout=timeout)
            if item is None:
                raise AssertionError("stream ended before a complete SSE frame arrived")
            self._buffer += item.decode("utf-8")
        frame, _, rest = self._buffer.partition("\n\n")
        self._buffer = rest
        return frame + "\n\n"

    async def read_body_only(self, *, timeout: float = 5.0) -> bytes:
        """For non-streaming (e.g. 503) responses: read to completion."""
        parts: list[bytes] = []
        while True:
            item = await asyncio.wait_for(self._body_queue.get(), timeout=timeout)
            if item is None:
                break
            parts.append(item)
        return b"".join(parts)

    async def aclose(self) -> None:
        self._disconnect.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def _run(coro):
    asyncio.run(asyncio.wait_for(coro, timeout=20.0))


async def _start_app(app: Starlette) -> None:
    """Run the app's lifespan startup phase (`EventBus.attach_loop`) without
    the full ASGI lifespan handshake -- consistent with driving `/api/events`
    at the raw ASGI level throughout this file (see module docstring)."""
    await app.router.lifespan_context(app).__aenter__()


# --- Connect / reconnect / Cache-Control ------------------------------------


def test_connect_first_frame_is_full_state_patch_with_cache_control(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            assert stream.status_code == 200
            assert stream.header("cache-control") == "no-store, no-transform"
            frame = await stream.read_frame()
            assert "event: datastar-patch-elements" in frame
            assert 'id="job-status"' in frame
            assert "No job has run yet." in frame

    _run(scenario())


def test_reconnect_receives_authoritative_snapshot_of_prior_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = threading.Event()
    monkeypatch.setattr(
        services_module, "run_build", lambda *a, **k: called.set() or {"output": {}}
    )

    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        runner = app.state.job_runner
        accepted, _snapshot = runner.start("build", formats=("gpkg",))
        assert accepted
        assert called.wait(timeout=5)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while runner.snapshot()["current"] is not None:
            if loop.time() > deadline:
                raise AssertionError("job never finished")
            await asyncio.sleep(0.01)

        # No client was connected while the job ran and finished -- the
        # *next* connection must still see the outcome, proving the
        # first-frame snapshot is authoritative rather than event-replay.
        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            frame = await stream.read_frame()
            assert "build" in frame
            assert "succeeded" in frame

    _run(scenario())


# --- Fan-out -----------------------------------------------------------------


def test_fanout_reaches_all_connected_clients(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        bus: EventBus = app.state.event_bus
        async with _SSEStream(app) as a, _SSEStream(app) as b:
            await a.wait_for_start()
            await b.wait_for_start()
            await a.read_frame()  # initial connect snapshot
            await b.read_frame()  # initial connect snapshot

            bus.publish({"kind": "build", "type": "job", "snapshot": {}})

            frame_a = await a.read_frame()
            frame_b = await b.read_frame()
            assert "event: datastar-patch-elements" in frame_a
            assert "event: datastar-patch-elements" in frame_b

    _run(scenario())


# --- Live dashboard refresh (#summary-live, docs/spec-serve-ui.md section 7
# Phase 4) --------------------------------------------------------------


def test_job_event_patches_summary_live_with_fresh_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = threading.Event()

    def _stub(*args, **kwargs):
        called.set()
        return {"output": {}}

    monkeypatch.setattr(services_module, "run_build", _stub)

    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        store = app.state.artifact_store
        original_shaped_summary = store.shaped_summary

        def _poisoned_summary():
            # `freshness.state` is rendered verbatim inside
            # _summary_section.html (`<strong>{{ ...state }}</strong>`) --
            # an unrecognized value is a harmless marker here since
            # `_freshness_command()` degrades to no fix-command hint for any
            # state it does not recognize.
            summary = original_shaped_summary()
            summary["freshness"]["state"] = "marker-999999"
            return summary

        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            connect_frame = await stream.read_frame()
            # The connect-time patch is #job-status only -- #summary-live is
            # only ever patched by a subsequent "job" event, never the
            # initial connect (the full dashboard render already shipped a
            # fresh summary).
            assert 'id="summary-live"' not in connect_frame

            # Only start reflecting the marker once the job actually runs --
            # proves the eventual #summary-live patch is a *fresh* re-render
            # triggered by the "job" event, not something baked in earlier.
            store.shaped_summary = _poisoned_summary

            runner = app.state.job_runner
            accepted, _snapshot = runner.start("build", formats=("gpkg",))
            assert accepted
            assert called.wait(timeout=5)

            # Read frames until a #summary-live patch carrying the marker
            # arrives -- robust to whichever exact order the "start" and
            # "completion" job events' #job-status/#summary-live pairs
            # interleave in (both are published per the SSE contract's
            # "start AND completion").
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            summary_frame = None
            while summary_frame is None:
                if loop.time() > deadline:
                    raise AssertionError("no #summary-live patch ever arrived")
                frame = await stream.read_frame(timeout=5.0)
                if 'id="summary-live"' in frame:
                    summary_frame = frame

            assert "marker-999999" in summary_frame

    _run(scenario())


def test_completion_patch_reflects_raw_scan_cache_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ArtifactStore._raw_scan_cache` is short-lived (~5s), but long enough
    for a job-start event's `#summary-live` render to cache a "no new raw
    data yet" scan result. If that same job then archives raw data and
    fails its rebuild within the window, the completion patch must not
    reuse that stale cached value -- `api_events` calls
    `invalidate_raw_scan_cache()` first, per artifacts.py/views.py."""
    summary_path = tmp_path / "output" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps({"layers": {"messages": 1}}), encoding="utf-8")
    os.utime(summary_path, (1_700_000_000, 1_700_000_000))

    job_started = threading.Event()

    def _stub(*args, **kwargs):
        job_started.set()
        # Simulate "data acquired and archived; rebuild failed" within the
        # cache window: a raw file newer than summary.json appears, then
        # the job raises.
        raw_path = tmp_path / "raw" / "imports" / "a.kml"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(b"x")
        raise RuntimeError("rebuild failed after data was archived")

    monkeypatch.setattr(services_module, "run_build", _stub)

    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        store = app.state.artifact_store

        # Populate the cache deterministically the way a job-start event's
        # own #summary-live render normally would, before any raw data has
        # been acquired -- avoids depending on exactly how the job-start
        # event and the worker thread happen to race in this test.
        pre_job = store.shaped_summary()
        assert pre_job["freshness"]["state"] == "ok"

        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            await stream.read_frame()  # initial connect snapshot (#job-status)

            runner = app.state.job_runner
            accepted, _snapshot = runner.start("build", formats=("gpkg",))
            assert accepted
            assert job_started.wait(timeout=5)

            # The #summary-live patch immediately following the #job-status
            # patch that reports "failed" belongs to the completion event
            # (views.py's `_stream()` always yields that pair back-to-back
            # for a "job" event) -- read forward until that pair is found.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            failed_status_seen = False
            summary_frame = None
            while summary_frame is None:
                if loop.time() > deadline:
                    raise AssertionError("no post-completion #summary-live patch ever arrived")
                frame = await stream.read_frame(timeout=5.0)
                if failed_status_seen and 'id="summary-live"' in frame:
                    summary_frame = frame
                elif 'id="job-status"' in frame and "failed" in frame:
                    failed_status_seen = True

            assert "outputs_stale" in summary_frame

    _run(scenario())


# --- Coalescing ----------------------------------------------------------


def test_queue_overflow_coalesces_progress_events_and_bounds_queue_size(
    tmp_path: Path,
) -> None:
    queue_size = 4

    async def scenario() -> None:
        app = create_app(tmp_path, event_queue_size=queue_size)
        await _start_app(app)
        bus: EventBus = app.state.event_bus
        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            await stream.read_frame()  # drain the initial connect snapshot

            (subscriber_queue,) = tuple(bus._subscribers)

            # Publish far more progress events than the queue can hold,
            # entirely synchronously (no `await` between calls) so the SSE
            # generator task -- cooperatively scheduled on this same loop --
            # cannot drain any of them until this loop turn ends. Only then
            # does the coalescing path in `_EventQueue.put_coalescing` get a
            # real, unavoidable test: every `put_nowait` after the queue
            # first fills must go through it.
            #
            # One `{"type": "job"}` event is interleaved into the flood,
            # published right as the queue first reaches capacity (index
            # `queue_size`, i.e. the first publish that is *guaranteed* to
            # go through the coalescing path rather than a plain
            # `put_nowait`). Naive drop-oldest coalescing -- unable to tell
            # a state-change "job" event from an idempotent "progress"
            # counter -- would have evicted it during one of the many
            # progress publishes that follow; the policy under test never
            # may, as long as a droppable progress event remains (see
            # `_EventQueue.put_coalescing`'s own comment), so this is what
            # actually distinguishes the two.
            last_index = queue_size * 3 - 1
            job_index = queue_size + 1
            job_event = {"type": "job", "kind": "build", "snapshot": {"marker": "job-survivor"}}
            for index in range(queue_size * 3):
                if index == job_index:
                    bus.publish(job_event)
                else:
                    bus.publish(
                        {
                            "type": "progress",
                            "snapshot": {"kind": "mapshare", "progress": {"windows_done": index}},
                        }
                    )

            # `publish()` only schedules a loop callback per event (never
            # runs `_fanout` synchronously, even when called -- as here --
            # from the loop's own thread); one `sleep(0)` lets every already
            # -scheduled `_fanout` callback run to completion in FIFO order
            # while the SSE generator's own queue-drain wakeup (scheduled
            # only *during* that batch) is deferred to the loop turn after
            # this one, so the inspection below sees the fully-coalesced
            # state before anything has been drained.
            await asyncio.sleep(0)

            # Bound respected at all times, the interleaved job event
            # survived untouched, and coalescing always prefers the newest
            # progress event over older ones -- the very last progress
            # event published must have survived alongside it.
            assert subscriber_queue.qsize() <= queue_size
            queued = list(subscriber_queue._queue)
            job_events = [item for item in queued if item["type"] == "job"]
            progress_events = [item for item in queued if item["type"] == "progress"]
            assert job_events == [job_event]
            assert len(progress_events) == queue_size - 1
            retained = [item["snapshot"]["progress"]["windows_done"] for item in progress_events]
            assert max(retained) == last_index

            # Draining must not raise and the stream stays coherent.
            frame = await stream.read_frame(timeout=5.0)
            assert "event: datastar-patch-elements" in frame

    _run(scenario())


# --- Subscriber cap --------------------------------------------------------


def test_subscriber_cap_returns_503_json(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path, max_event_subscribers=2)
        await _start_app(app)
        async with _SSEStream(app) as a, _SSEStream(app) as b:
            await a.wait_for_start()
            await b.wait_for_start()
            async with _SSEStream(app) as c:
                await c.wait_for_start()
                assert c.status_code == 503
                # Datastar's "auto" retry mode does not retry a 503, so a
                # tab stuck here needs a manual reload -- see the comment at
                # this response in views.py.
                assert c.header("cache-control") == "no-store, no-transform"
                body = await c.read_body_only()
                payload = json.loads(body)
                assert "error" in payload

    _run(scenario())


# --- Subscriber slot release (review finding 7) -----------------------------


def test_subscriber_slot_released_after_normal_teardown(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        bus: EventBus = app.state.event_bus
        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            await stream.read_frame()  # initial connect snapshot
            assert len(bus._subscribers) == 1
        assert len(bus._subscribers) == 0

    _run(scenario())


def test_subscriber_slot_released_after_aborted_before_first_send(tmp_path: Path) -> None:
    """`api_events`'s `bus.subscribe()` runs before the response is ever
    asked to send anything -- if the very first `send()` call
    (`http.response.start`) fails, `_stream()`'s async generator body never
    starts executing even once (its body only begins running on the first
    `__anext__()`), so that generator's own `finally` never runs either.
    This drives the ASGI callable directly with a `send` that raises
    immediately on `http.response.start`, simulating a client that has
    already disconnected before headers went out; the subscriber cap must
    still recover (`_AlwaysReleasingDatastarResponse` in views.py)."""

    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        bus: EventBus = app.state.event_bus

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
            "state": {},
        }

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def failing_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                raise OSError("simulated client abort before headers were sent")

        with contextlib.suppress(Exception):
            await app(scope, receive, failing_send)

        assert len(bus._subscribers) == 0

    _run(scenario())


# --- Cross-site request defense on GET (review finding 4) -------------------


def test_cross_site_sec_fetch_site_is_rejected_with_security_headers(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        async with _SSEStream(app, extra_headers=[(b"sec-fetch-site", b"cross-site")]) as stream:
            await stream.wait_for_start()
            assert stream.status_code == 403
            assert stream.header("cache-control") == "no-store"
            assert stream.header("content-security-policy") is not None
            body = await stream.read_body_only()
            assert json.loads(body) == {"error": "request rejected"}

    _run(scenario())


def test_head_request_to_events_is_405(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        stream = _SSEStream(app)
        stream._scope["method"] = "HEAD"
        async with stream:
            await stream.wait_for_start()
            assert stream.status_code == 405

    _run(scenario())


@pytest.mark.parametrize(
    "extra_headers",
    [
        [],  # absent Sec-Fetch-Site -- older browsers/non-fetch EventSource clients
        [(b"sec-fetch-site", b"same-origin")],
        [(b"sec-fetch-site", b"none")],
    ],
    ids=["absent", "same-origin", "none"],
)
def test_same_origin_or_absent_sec_fetch_site_streams_normally(
    tmp_path: Path, extra_headers: list[tuple[bytes, bytes]]
) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        async with _SSEStream(app, extra_headers=extra_headers) as stream:
            await stream.wait_for_start()
            assert stream.status_code == 200
            frame = await stream.read_frame()
            assert "event: datastar-patch-elements" in frame

    _run(scenario())


# --- Keepalive --------------------------------------------------------------


def test_idle_stream_emits_keepalive_within_interval(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = create_app(tmp_path, sse_keepalive_seconds=0.05)
        await _start_app(app)
        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            await stream.read_frame()  # initial connect snapshot
            frame = await stream.read_frame(timeout=2.0)
            assert frame == ": keepalive\n\n"

    _run(scenario())


# --- Worker exception -> failed over SSE -----------------------------------


def test_worker_exception_failed_state_arrives_over_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _stub(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(services_module, "run_build", _stub)

    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            await stream.read_frame()  # initial connect snapshot

            runner = app.state.job_runner
            accepted, _snapshot = runner.start("build", formats=("gpkg",))
            assert accepted

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            failed_seen = False
            while not failed_seen:
                if loop.time() > deadline:
                    raise AssertionError("failed state never reached the SSE stream")
                frame = await stream.read_frame(timeout=5.0)
                failed_seen = "failed" in frame
            assert failed_seen

    _run(scenario())


# --- Shutdown ----------------------------------------------------------------


def test_abandoned_daemon_job_worker_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disconnect (or, in production, Ctrl-C) does not wait on a job's
    worker thread. This does *not* exercise a real server shutdown (see
    `test_ctrl_c_shutdown_with_open_sse_stream_is_bounded` below for that) --
    it only proves that disconnecting an `/api/events` stream while a job is
    permanently blocked on a daemon worker thread returns immediately rather
    than hanging the test's own event loop, because `JobRunner` never joins
    that thread (docs/spec-serve-ui.md section 7 Phase B)."""
    started = threading.Event()
    never_released = threading.Event()

    def _stub(*args, **kwargs):
        started.set()
        never_released.wait()  # deliberately never set: abandoned in-flight work
        return {"output": {}}  # pragma: no cover - unreachable in this test

    monkeypatch.setattr(services_module, "run_build", _stub)

    async def scenario() -> None:
        app = create_app(tmp_path)
        await _start_app(app)
        runner = app.state.job_runner
        accepted, _snapshot = runner.start("build", formats=("gpkg",))
        assert accepted
        assert started.wait(timeout=5)

        async with _SSEStream(app) as stream:
            await stream.wait_for_start()
            await stream.read_frame()
        # Exiting the `async with` disconnects the stream and returns
        # without ever waiting on the blocked worker thread. `JobRunner`
        # starts jobs on `daemon=True` threads (docs/spec-serve-ui.md
        # section 7 Phase B), so this scenario -- and, in production, the
        # whole process on Ctrl-C -- exits without joining it.

    _run(scenario())


def test_ctrl_c_shutdown_with_open_sse_stream_is_bounded(tmp_path: Path) -> None:
    """BLOCKER regression test: with an open `/api/events` stream, uvicorn's
    default graceful shutdown waits (`timeout_graceful_shutdown=None`, i.e.
    forever) for every in-flight response to finish on its own -- and the
    SSE generator loops forever by design. `run()` sets
    `timeout_graceful_shutdown=2` precisely so Ctrl-C is still bounded with a
    tab open; this drives a real `uvicorn.Server` (not the raw-ASGI harness
    the rest of this file uses) over a real socket, so uvicorn's actual
    shutdown machinery -- not a test double -- is what gets timed.
    """
    app = create_app(tmp_path)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        timeout_graceful_shutdown=2,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while not server.started:
            if time.monotonic() > deadline:
                raise AssertionError("server never started")
            time.sleep(0.01)

        port = server.servers[0].sockets[0].getsockname()[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=10.0)
        try:
            sock.sendall(
                b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
            )
            sock.settimeout(10.0)
            received = b""
            deadline = time.monotonic() + 10.0
            # Read until the first SSE frame (the connect-time full-state
            # patch of #job-status) has definitely arrived -- proof the
            # stream is open and would, without the bounded timeout, sit
            # forever in uvicorn's graceful-shutdown wait.
            while b'id="job-status"' not in received:
                if time.monotonic() > deadline:
                    raise AssertionError("first SSE frame never arrived")
                chunk = sock.recv(4096)
                if not chunk:
                    raise AssertionError("connection closed before first SSE frame arrived")
                received += chunk
        finally:
            sock.close()

        server.should_exit = True
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "server did not shut down within the bounded timeout"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


# --- No GZipMiddleware anywhere ---------------------------------------------


def test_no_gzip_middleware_anywhere(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    middleware_class_names = {middleware.cls.__name__ for middleware in app.user_middleware}
    assert not any("gzip" in name.lower() for name in middleware_class_names)


# --- Protocol-compat pin -----------------------------------------------------


def test_datastar_py_event_type_strings_agree_with_vendored_js() -> None:
    static_dir = resources.files("garmin_outreach.serve").joinpath("static")
    js_bytes = static_dir.joinpath(discover_datastar_filename()).read_bytes()
    assert datastar_consts.EventType.PATCH_ELEMENTS.value.encode("ascii") in js_bytes
    assert datastar_consts.EventType.PATCH_SIGNALS.value.encode("ascii") in js_bytes
