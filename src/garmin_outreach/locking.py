"""Advisory interprocess writer lock for the data directory.

Every mutating CLI path (ingest, build, mapshare, explore) holds this lock
for the duration of its work so a terminal invocation cannot interleave
writes with, e.g., a future `serve` job runner touching the same data/.

The lock is OS-level (msvcrt on Windows, fcntl.flock on POSIX) on an open
file handle, not a "lock file exists" convention: the OS releases it when
the holding process dies, so a leftover lock file from a crashed process is
harmless and must never block acquisition on its own.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

LOCK_FILENAME = ".writer.lock"


@contextlib.contextmanager
def writer_lock(data_dir: Path, *, label: str = "unknown") -> Iterator[None]:
    """Acquire the advisory writer lock under `data_dir` for the block body.

    Raises RuntimeError immediately (no blocking wait) if another process
    already holds the lock. Not reentrant; acquire once at the outermost
    mutating boundary.
    """
    data_dir = Path(data_dir)
    lock_path = data_dir / LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT
    if sys.platform == "win32":
        # Avoid CRLF text-mode translation: os.write() length must match
        # the bytes actually placed on disk for our ftruncate() math below.
        flags |= os.O_BINARY
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, flags, 0o644)
    except OSError as error:
        raise RuntimeError(f"Cannot use data directory {data_dir}: {error}") from error
    try:
        _acquire(fd, lock_path)
        _write_diagnostics(fd, label)
        yield
    finally:
        _release(fd)
        os.close(fd)


def _acquire(fd: int, lock_path: Path) -> None:
    if sys.platform == "win32":
        try:
            # msvcrt locks a byte range starting at the current file position;
            # make sure that byte exists before locking a brand new (empty) file.
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise _contention_error(lock_path) from error
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise _contention_error(lock_path) from error


def _release(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def _write_diagnostics(fd: int, label: str) -> None:
    payload = {
        "pid": os.getpid(),
        "started": datetime.now(UTC).isoformat(),
        "label": label,
    }
    # Byte 0 is the msvcrt lock region on Windows; keep diagnostics past it
    # so writing them never touches the locked byte.
    body = json.dumps(payload, indent=2).encode("utf-8") + b"\n"
    # Truncate the stale payload first so a contender that reads mid-write
    # sees no detail rather than the previous (possibly dead) holder's.
    os.ftruncate(fd, 1)
    os.lseek(fd, 1, os.SEEK_SET)
    os.write(fd, body)
    os.ftruncate(fd, 1 + len(body))
    os.fsync(fd)


def _contention_error(lock_path: Path) -> RuntimeError:
    holder = _read_holder(lock_path)
    if holder is not None and holder.get("pid") == os.getpid():
        detail = _format_holder(holder)
        detail_suffix = f" ({detail})" if detail else ""
        return RuntimeError(
            f"This process (pid={os.getpid()}) already holds the writer lock on "
            f"{lock_path.parent}{detail_suffix}; writer_lock() is not reentrant, so "
            "nested acquisition within the same process is not permitted."
        )
    detail = _format_holder(holder)
    detail_suffix = f" (held by {detail})" if detail else ""
    return RuntimeError(
        f"Another garmin-outreach process is already writing to "
        f"{lock_path.parent}{detail_suffix}; retry once it finishes."
    )


def _read_holder(lock_path: Path) -> dict | None:
    try:
        # Skip byte 0: on Windows it's the locked region, and reading it
        # through a second handle while another process holds the lock
        # raises PermissionError even though the diagnostics past it are
        # readable.
        with open(lock_path, "rb") as handle:
            handle.seek(1)
            raw = handle.read()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _format_holder(holder: dict | None) -> str:
    if not holder:
        return ""
    bits = []
    if holder.get("pid") is not None:
        bits.append(f"pid={holder['pid']}")
    if holder.get("label"):
        bits.append(f"label={holder['label']}")
    if holder.get("started"):
        bits.append(f"started={holder['started']}")
    return ", ".join(bits)
