"""In-process pub/sub bus bridging job-runner worker threads to `GET
/api/events` SSE connections.

Phase 4 of docs/spec-serve-ui.md section 7 ("SSE contract"). This module
owns no filesystem or job state -- it only fans typed, already-sanitized
event dicts (built by `jobs.JobRunner`) out to whichever browser tabs are
currently connected. Event payloads are never constructed here from raw
exceptions or filesystem data; callers are responsible for scrubbing before
`publish()`.

Threading model: exactly one asyncio event loop is "the" loop for the
server's lifetime (uvicorn runs a single worker, per docs/spec-serve-ui.md
section 5). `attach_loop()` records it once, from the Starlette startup
hook. `subscribe()`/`unsubscribe()`/the internal fan-out all run on that
loop. `publish()` is the only method meant to be called from a different
thread (a job's worker thread) -- it bridges via
`loop.call_soon_threadsafe()`, per the spec's explicit requirement to never
block on `queue.get()` in the async generator and never call queue methods
directly from a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable


class _EventQueue(asyncio.Queue):
    """A bounded per-subscriber queue with publish-side coalescing.

    Plain `asyncio.Queue.put_nowait()` would raise `QueueFull` and force the
    publisher (running via `call_soon_threadsafe` on the loop, so no
    exception handler upstream is useful for propagating anything to the
    worker thread) to just drop the newest event. Instead, when full, this
    tries to make room by dropping the *oldest queued "progress" event*
    first -- progress events are idempotent counters (they always carry the
    latest `windows_done`), so losing an intermediate one is harmless as
    long as a later one lands. If the queue is still full after that (every
    queued event is a state-change "job" event, none of which may be
    silently preferred away), the oldest event is dropped regardless of
    kind: the client resyncs from the next authoritative full-state patch
    (every reconnect gets one), so losing one intermediate event here does
    not wedge the UI, only delays it reaching the currently-connected view
    by one tick.
    """

    def put_coalescing(self, event: dict) -> None:
        try:
            self.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        if not self._drop_oldest(event_type="progress"):
            self._drop_oldest(event_type=None)
        # If the queue is still (improbably) full here -- e.g. maxsize=0
        # meaning "unbounded" was never actually requested, or another
        # coalescing pass raced in in the same synchronous callback -- let
        # QueueFull propagate; `EventBus._fanout` is the sole caller and its
        # per-subscriber loop already tolerates one bad subscriber without
        # affecting the others (see its own comment).
        self.put_nowait(event)

    def _drop_oldest(self, *, event_type: str | None) -> bool:
        # `asyncio.Queue` stores pending items in a `collections.deque` at
        # `self._queue` -- there is no public peek/remove API for "delete
        # the oldest item matching a predicate", so this reaches into that
        # attribute deliberately. It has been stable across CPython's
        # `asyncio.queues` implementation for many releases (verified
        # against the vendored 3.11 stdlib in this project's toolchain).
        # This also bypasses `asyncio.Queue`'s `join()`/`task_done()`
        # unfinished-task accounting entirely (deleting an item straight out
        # of the deque never decrements it) -- fine here because nothing in
        # this codebase ever calls `join()`/`task_done()` on an
        # `_EventQueue`, but this method must not be reused anywhere that
        # bookkeeping matters.
        for index, queued in enumerate(self._queue):
            if event_type is None or queued.get("type") == event_type:
                del self._queue[index]
                return True
        return False


class EventBus:
    """Process-wide fan-out of job events to connected `/api/events` clients.

    `attach_loop()` must be called exactly once, from the server's asyncio
    event loop, before `publish()` (called from worker threads) has any
    effect. Publishing before attachment is a silent no-op -- there is
    nothing sensitive in that no-op, so it is not logged.
    """

    def __init__(self, *, max_subscribers: int = 8, queue_size: int = 64) -> None:
        self._max_subscribers = max_subscribers
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[_EventQueue] = set()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the server's event loop. Call once, from app startup."""
        self._loop = loop

    def subscribe(self) -> _EventQueue | None:
        """Register a new bounded queue for a connecting client.

        Must be called on the attached loop (i.e. from an async request
        handler). Returns `None` when `max_subscribers` connections are
        already registered -- the route layer turns that into a 503,
        never a queue that silently starves everyone else.
        """
        if len(self._subscribers) >= self._max_subscribers:
            return None
        queue = _EventQueue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: _EventQueue) -> None:
        """Deregister a queue. Callers must do this in a `finally` block so
        a disconnected/cancelled client does not leak a subscriber slot.
        `set.discard()` is a no-op if `queue` is already gone, so calling
        this more than once for the same queue (e.g. once from a response's
        `background` task and once from the stream generator's own
        `finally`) is always safe."""
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        """Fan `event` out to every subscriber. Safe to call from any thread.

        Bridges onto the attached loop via `call_soon_threadsafe` -- never
        touches subscriber queues directly from the calling thread. A
        silent no-op if `attach_loop()` has not run yet or the loop has
        already been closed (server shutting down); a worker thread's
        publish must never raise.
        """
        loop = self._loop
        if loop is None:
            return
        # Loop already closed (shutdown race) -- never propagate into the
        # calling worker thread.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict) -> None:
        # Runs on the attached loop as a plain (non-async) callback, so it
        # executes atomically with respect to every other loop callback --
        # no `subscribe()`/`unsubscribe()` call can interleave mid-iteration
        # even without copying `self._subscribers` first. The copy below is
        # cheap insurance, not a correctness requirement.
        for queue in _snapshot_subscribers(self._subscribers):
            # One subscriber's queue raising (e.g. `QueueFull` surviving
            # both coalescing passes in `_EventQueue.put_coalescing`, per
            # its own comment) must never abort delivery to every other
            # already-connected client.
            with contextlib.suppress(Exception):
                queue.put_coalescing(event)


def _snapshot_subscribers(subscribers: Iterable[_EventQueue]) -> tuple[_EventQueue, ...]:
    return tuple(subscribers)
