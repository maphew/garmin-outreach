import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import garmin_outreach.mapshare as mapshare_module
import garmin_outreach.services as services
from garmin_outreach.services import (
    BuildFailedAfterAcquisition,
    run_build,
    run_explore_http,
    run_mapshare,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _RecordingLock:
    """Records enter/exit against a shared events list, standing in for
    writer_lock so tests can assert ordering without touching real files."""

    def __init__(self, events, data_dir, *, label):
        self.events = events
        self.data_dir = data_dir
        self.label = label

    def __enter__(self):
        self.events.append(("enter", self.label))
        return self

    def __exit__(self, *exc_info):
        self.events.append(("exit", self.label))
        return False


def _install_recording_lock(monkeypatch, events):
    def make_lock(data_dir, *, label):
        return _RecordingLock(events, data_dir, label=label)

    monkeypatch.setattr(services, "writer_lock", make_lock)


# --- run_build ---------------------------------------------------------


def test_run_build_acquires_lock_and_returns_output(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)
    monkeypatch.setattr(services, "rebuild", lambda *args, **kwargs: {"summary": True})

    result = run_build(
        tmp_path, formats=("geojson",), gap_hours=1.0, max_speed_kmh=1.0, jump_km=1.0
    )

    assert result == {"output": {"summary": True}}
    assert events == [("enter", "build"), ("exit", "build")]


# --- run_mapshare --------------------------------------------------------


def test_run_mapshare_lock_ordering_wraps_sync_then_rebuild(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)

    def fake_sync(*args, **kwargs):
        events.append("sync")
        return {"new_features": 1}

    def fake_rebuild(*args, **kwargs):
        events.append("rebuild")
        return {"summary": True}

    monkeypatch.setattr(services, "sync_mapshare", fake_sync)
    monkeypatch.setattr(services, "rebuild", fake_rebuild)

    result = run_mapshare(
        "sample",
        tmp_path,
        formats=("geojson",),
        gap_hours=1.0,
        max_speed_kmh=1.0,
        jump_km=1.0,
    )

    assert result == {"mapshare": {"new_features": 1}, "output": {"summary": True}}
    assert events == [("enter", "mapshare"), "sync", "rebuild", ("exit", "mapshare")]


def test_run_mapshare_no_build_skips_rebuild(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)
    monkeypatch.setattr(services, "sync_mapshare", lambda *args, **kwargs: {"new_features": 0})
    monkeypatch.setattr(services, "rebuild", lambda *args, **kwargs: pytest.fail("rebuild called"))

    result = run_mapshare(
        "sample",
        tmp_path,
        no_build=True,
        formats=("geojson",),
        gap_hours=1.0,
        max_speed_kmh=1.0,
        jump_km=1.0,
    )

    assert result == {"mapshare": {"new_features": 0}}
    assert events == [("enter", "mapshare"), ("exit", "mapshare")]


def test_run_mapshare_empty_identifier_raises_without_locking(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)

    with pytest.raises(RuntimeError, match="Supply a MapShare identifier"):
        run_mapshare(
            "",
            tmp_path,
            formats=("geojson",),
            gap_hours=1.0,
            max_speed_kmh=1.0,
            jump_km=1.0,
        )

    assert events == []


def test_run_mapshare_build_failure_raises_partial_success(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)
    sync_result = {"new_features": 2}
    monkeypatch.setattr(services, "sync_mapshare", lambda *args, **kwargs: sync_result)

    def failing_rebuild(*args, **kwargs):
        raise RuntimeError("rebuild boom")

    monkeypatch.setattr(services, "rebuild", failing_rebuild)

    with pytest.raises(BuildFailedAfterAcquisition) as excinfo:
        run_mapshare(
            "sample",
            tmp_path,
            formats=("geojson",),
            gap_hours=1.0,
            max_speed_kmh=1.0,
            jump_km=1.0,
        )

    error = excinfo.value
    assert error.acquisition_key == "mapshare"
    assert error.acquisition_result == sync_result
    assert str(error) == "rebuild boom"
    assert events == [("enter", "mapshare"), ("exit", "mapshare")]


def test_run_mapshare_progress_reaches_sync_mapshare(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)
    captured = {}

    def fake_sync(*args, **kwargs):
        captured["progress"] = kwargs.get("progress")
        return {"new_features": 0}

    monkeypatch.setattr(services, "sync_mapshare", fake_sync)
    monkeypatch.setattr(services, "rebuild", lambda *args, **kwargs: {"summary": True})

    def my_progress(event):
        pass  # pragma: no cover - only identity is asserted

    run_mapshare(
        "sample",
        tmp_path,
        no_build=True,
        formats=("geojson",),
        gap_hours=1.0,
        max_speed_kmh=1.0,
        jump_km=1.0,
        progress=my_progress,
    )

    assert captured["progress"] is my_progress


# --- run_explore_http ------------------------------------------------------


def test_run_explore_http_lock_ordering_wraps_export_then_rebuild(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)

    def fake_export(*args, **kwargs):
        events.append("export")
        return {"exports": []}

    def fake_rebuild(*args, **kwargs):
        events.append("rebuild")
        return {"summary": True}

    monkeypatch.setattr(services, "browserless_export", fake_export)
    monkeypatch.setattr(services, "rebuild", fake_rebuild)

    result = run_explore_http(
        tmp_path,
        export_formats=("kml",),
        formats=("geojson",),
        gap_hours=1.0,
        max_speed_kmh=1.0,
        jump_km=1.0,
    )

    assert result == {"explore": {"exports": []}, "output": {"summary": True}}
    assert events == [("enter", "explore"), "export", "rebuild", ("exit", "explore")]


def test_run_explore_http_no_build_skips_rebuild(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)
    monkeypatch.setattr(services, "browserless_export", lambda *args, **kwargs: {"exports": []})
    monkeypatch.setattr(services, "rebuild", lambda *args, **kwargs: pytest.fail("rebuild called"))

    result = run_explore_http(
        tmp_path,
        export_formats=("kml",),
        no_build=True,
        formats=("geojson",),
        gap_hours=1.0,
        max_speed_kmh=1.0,
        jump_km=1.0,
    )

    assert result == {"explore": {"exports": []}}
    assert events == [("enter", "explore"), ("exit", "explore")]


def test_run_explore_http_build_failure_raises_partial_success(tmp_path, monkeypatch):
    events = []
    _install_recording_lock(monkeypatch, events)
    export_result = {"exports": [{"format": "kml"}]}
    monkeypatch.setattr(services, "browserless_export", lambda *args, **kwargs: export_result)

    def failing_rebuild(*args, **kwargs):
        raise RuntimeError("explore rebuild boom")

    monkeypatch.setattr(services, "rebuild", failing_rebuild)

    with pytest.raises(BuildFailedAfterAcquisition) as excinfo:
        run_explore_http(
            tmp_path,
            export_formats=("kml",),
            formats=("geojson",),
            gap_hours=1.0,
            max_speed_kmh=1.0,
            jump_km=1.0,
        )

    error = excinfo.value
    assert error.acquisition_key == "explore"
    assert error.acquisition_result == export_result
    assert str(error) == "explore rebuild boom"


# --- sync_mapshare progress callback (typed, pre-sanitized events) --------


class _FakeMapshareClient:
    def __init__(self, content, *args, **kwargs):
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params):
        request = httpx.Request("GET", url, params=params)
        return httpx.Response(200, content=self._content, request=request)


def _install_fake_mapshare_client(monkeypatch):
    content = (FIXTURES / "mapshare.kml").read_bytes()
    monkeypatch.setattr(
        mapshare_module.httpx, "Client", lambda *args, **kwargs: _FakeMapshareClient(content)
    )


def test_sync_mapshare_progress_called_per_window_with_typed_sanitized_events(
    tmp_path, monkeypatch
):
    _install_fake_mapshare_client(monkeypatch)
    events = []

    start = datetime(2024, 6, 1, tzinfo=UTC)
    end = datetime(2024, 6, 3, tzinfo=UTC)

    result = mapshare_module.sync_mapshare(
        "super-secret-identifier",
        tmp_path,
        start=start,
        end=end,
        chunk_days=1,
        progress=events.append,
    )

    assert result["requests"] == 2
    assert len(events) == 2
    for event in events:
        assert set(event.keys()) == {
            "stage",
            "window_start_utc",
            "window_end_utc",
            "new_features",
        }
        assert event["stage"] == "window"
        blob = json.dumps(event)
        assert "super-secret-identifier" not in blob
        assert "feed_url" not in blob
        assert "garmin.com" not in blob
    assert events[0]["new_features"] == 3
    assert events[1]["new_features"] == 0


def test_sync_mapshare_progress_callback_raising_does_not_break_sync(tmp_path, monkeypatch):
    _install_fake_mapshare_client(monkeypatch)

    def raising_progress(event):
        raise RuntimeError("UI delivery failed, must not propagate")

    start = datetime(2024, 6, 1, tzinfo=UTC)
    end = datetime(2024, 6, 3, tzinfo=UTC)

    result = mapshare_module.sync_mapshare(
        "sample",
        tmp_path,
        start=start,
        end=end,
        chunk_days=1,
        progress=raising_progress,
    )

    assert result["requests"] == 2
    assert result["new_archives"] == 1
