import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import garmin_outreach.cli as cli
import garmin_outreach.services as services
from garmin_outreach import locking
from garmin_outreach.locking import writer_lock

_CHILD_LOCK_HOLDER = """
import sys
import time
from pathlib import Path

from garmin_outreach.locking import writer_lock

data_dir = Path(sys.argv[1])
acquired_sentinel = Path(sys.argv[2])
release_sentinel = Path(sys.argv[3])

with writer_lock(data_dir, label="child"):
    acquired_sentinel.write_text("ok", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not release_sentinel.exists():
        if time.monotonic() > deadline:
            raise SystemExit("timed out waiting for release sentinel")
        time.sleep(0.05)
"""


def _spawn_child_lock_holder(
    tmp_path: Path,
) -> tuple[subprocess.Popen, Path, Path, Path]:
    data_dir = tmp_path / "data"
    acquired_sentinel = tmp_path / "acquired"
    release_sentinel = tmp_path / "release"
    child_script = tmp_path / "child_lock_holder.py"
    child_script.write_text(_CHILD_LOCK_HOLDER, encoding="utf-8")

    proc = subprocess.Popen(
        [
            sys.executable,
            str(child_script),
            str(data_dir),
            str(acquired_sentinel),
            str(release_sentinel),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, data_dir, acquired_sentinel, release_sentinel


def _wait_for(
    path: Path,
    *,
    proc: subprocess.Popen | None = None,
    deadline_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + deadline_seconds
    while not path.exists():
        if proc is not None:
            returncode = proc.poll()
            if returncode is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(
                    f"child process exited early (code {returncode}) before {path} "
                    f"appeared; output:\n{output}"
                )
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.05)


def test_acquires_writes_diagnostics_and_releases(tmp_path):
    data_dir = tmp_path / "data"
    lock_path = data_dir / locking.LOCK_FILENAME

    with writer_lock(data_dir, label="build"):
        assert lock_path.exists()

    # Byte 0 is reserved for the Windows lock region; diagnostics start at 1.
    with open(lock_path, "rb") as handle:
        handle.seek(1)
        payload = json.loads(handle.read().decode("utf-8"))
    assert payload["label"] == "build"
    assert isinstance(payload["pid"], int)
    assert "started" in payload


def test_can_reacquire_sequentially_in_same_process(tmp_path):
    data_dir = tmp_path / "data"

    with writer_lock(data_dir, label="first"):
        pass
    with writer_lock(data_dir, label="second"):
        pass


def test_contending_process_is_rejected_then_succeeds_after_release(tmp_path):
    proc, data_dir, acquired_sentinel, release_sentinel = _spawn_child_lock_holder(tmp_path)
    try:
        _wait_for(acquired_sentinel, proc=proc)

        with (
            pytest.raises(RuntimeError, match="[Aa]nother garmin-outreach process"),
            writer_lock(data_dir, label="parent"),
        ):
            pass
    finally:
        release_sentinel.write_text("go", encoding="utf-8")
        proc.wait(timeout=15)

    assert proc.returncode == 0

    with writer_lock(data_dir, label="parent-after-release"):
        pass


def test_killing_the_holder_releases_the_lock(tmp_path):
    proc, data_dir, acquired_sentinel, _release_sentinel = _spawn_child_lock_holder(tmp_path)
    try:
        _wait_for(acquired_sentinel, proc=proc)

        with (
            pytest.raises(RuntimeError, match="[Aa]nother garmin-outreach process"),
            writer_lock(data_dir, label="parent"),
        ):
            pass
    finally:
        # The OS releases the lock when the holding process dies, even
        # without a clean release_sentinel handshake.
        proc.kill()
        proc.wait(timeout=15)

    with writer_lock(data_dir, label="parent-after-kill"):
        pass


def test_stale_lock_file_with_junk_content_does_not_block_acquisition(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / locking.LOCK_FILENAME).write_bytes(b"not json and no live OS lock behind it")

    with writer_lock(data_dir, label="after-stale"):
        pass


class _RecordingLock:
    def __init__(self, events, data_dir, *, label):
        self.events = events
        self.data_dir = data_dir
        self.label = label

    def __enter__(self):
        self.events.append(("enter", self.data_dir, self.label))
        return self

    def __exit__(self, *exc_info):
        self.events.append(("exit", self.data_dir, self.label))
        return False


# ingest keeps its own inline `writer_lock`/`rebuild` in cli.py (there is no
# UI ingest job); build/mapshare/explore(http) now lock and rebuild via
# garmin_outreach.services, the single outermost mutating boundary shared
# with the future UI job runner.
_LOCK_MODULE_BY_COMMAND = {
    "ingest": cli,
    "mapshare": services,
    "explore": services,
    "build": services,
}


@pytest.mark.parametrize("command", ["ingest", "mapshare", "explore", "build"])
def test_cli_records_lock_label_and_runs_command_inside_it(tmp_path, monkeypatch, command):
    events = []
    lock_module = _LOCK_MODULE_BY_COMMAND[command]

    def make_lock(data_dir, *, label):
        return _RecordingLock(events, data_dir, label=label)

    def fake_rebuild(data_dir, *, formats, gap_hours, max_speed_kmh, jump_km):
        events.append(("rebuild", data_dir))
        return {"rebuilt": True}

    monkeypatch.setattr(lock_module, "writer_lock", make_lock)
    monkeypatch.setattr(lock_module, "rebuild", fake_rebuild)

    argv = ["--data-dir", str(tmp_path), command]
    side_effect_name = None
    if command == "ingest":
        gpx = tmp_path / "track.gpx"
        gpx.write_text("<gpx/>", encoding="utf-8")
        argv.append(str(gpx))
        side_effect_name = "archive_file"

        def fake_archive_file(path, directory):
            # `directory` is `data_dir / "raw" / "imports"`, not `data_dir`
            # itself; record a fixed marker rather than the path so this
            # assertion doesn't depend on that internal join.
            events.append((side_effect_name, "called"))
            return directory / path.name, True

        monkeypatch.setattr(cli, "archive_file", fake_archive_file)
    elif command == "mapshare":
        monkeypatch.setenv("GARMIN_MAPSHARE_ID", "sample-id")
        monkeypatch.setenv("GARMIN_MAPSHARE_PASSWORD", "sample-password")
        side_effect_name = "sync_mapshare"

        def fake_sync_mapshare(identifier, data_dir, **kwargs):
            events.append((side_effect_name, "called"))
            return {"new_features": 0}

        monkeypatch.setattr(services, "sync_mapshare", fake_sync_mapshare)
    elif command == "explore":
        side_effect_name = "browserless_export"

        def fake_browserless_export(data_dir, **kwargs):
            events.append((side_effect_name, "called"))
            return {"exported": []}

        monkeypatch.setattr(services, "browserless_export", fake_browserless_export)

    cli.main(argv)

    assert events[0] == ("enter", tmp_path, command)
    assert events[-1] == ("exit", tmp_path, command)
    inner = events[1:-1]
    assert ("rebuild", tmp_path) in inner
    if side_effect_name is None:
        # build has no acquisition function of its own; it only rebuilds.
        assert inner == [("rebuild", tmp_path)]
    else:
        side_effect_event = (side_effect_name, "called")
        assert side_effect_event in inner
        assert inner.index(side_effect_event) < inner.index(("rebuild", tmp_path))


@pytest.mark.parametrize("command", ["ingest", "build"])
def test_cli_exits_2_with_error_message_when_lock_is_held(tmp_path, monkeypatch, capsys, command):
    def raising_lock(data_dir, *, label):
        raise RuntimeError(f"Another garmin-outreach process is already writing to {data_dir}")

    monkeypatch.setattr(_LOCK_MODULE_BY_COMMAND[command], "writer_lock", raising_lock)

    argv = ["--data-dir", str(tmp_path), command]
    if command == "ingest":
        gpx = tmp_path / "track.gpx"
        gpx.write_text("<gpx/>", encoding="utf-8")
        argv.append(str(gpx))

    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "already writing" in captured.err
