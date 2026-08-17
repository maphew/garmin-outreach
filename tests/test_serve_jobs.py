"""Tests for the `serve` job runner, CSRF defenses, and job routes.

docs/spec-serve-ui.md section 7 Phase B ("Job semantics") and section 8
("Cross-site request defense"). Mirrors `test_serve_app.py`'s
`importorskip` guards so a bare environment (no `ui` extra) degrades to
skip rather than a collection error.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("uvicorn")
pytest.importorskip("jinja2")
pytest.importorskip("datastar_py")

from starlette.testclient import TestClient

import garmin_outreach.serve.jobs as jobs_module
import garmin_outreach.services as services_module
from garmin_outreach.serve.app import create_app
from garmin_outreach.services import BuildFailedAfterAcquisition


def _app(data_dir: Path):
    return create_app(data_dir)


def _client(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _job_headers(token: str, *, content_type: str = "application/json") -> dict:
    return {
        "Sec-Fetch-Site": "same-origin",
        "X-Garmin-Outreach-Job": token,
        "Content-Type": content_type,
    }


def _wait_for_idle(client: TestClient, *, timeout: float = 5.0) -> dict:
    """Bounded wait for the job runner to clear `current` after a stub
    returns/raises. The concurrency-sensitive part of every test below is
    synchronized with a `threading.Event`; this only covers the brief,
    unavoidable window between a stub returning and the runner thread
    re-acquiring its lock to record the final snapshot.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get("/api/jobs").json()
        if body["current"] is None:
            return body
        time.sleep(0.01)
    pytest.fail("job runner did not go idle in time")


# --- CSRF defenses (docs/spec-serve-ui.md section 8) -----------------------


def test_missing_sec_fetch_site_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    del headers["Sec-Fetch-Site"]
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 403


def test_cross_site_sec_fetch_site_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    headers["Sec-Fetch-Site"] = "cross-site"
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 403


def test_missing_custom_header_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    del headers["X-Garmin-Outreach-Job"]
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 403


def test_wrong_custom_header_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    headers["X-Garmin-Outreach-Job"] = "wrong-token"
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 403


def test_wrong_content_type_is_415(tmp_path, monkeypatch):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token, content_type="text/plain")
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 415


def test_all_correct_csrf_is_accepted(tmp_path, monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(
        services_module, "run_build", lambda *a, **k: called.set() or {"output": {}}
    )
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 202
    assert called.wait(timeout=5)


def test_csrf_error_bodies_carry_no_details(tmp_path):
    app = _app(tmp_path)
    client = _client(app)
    response = client.post(
        "/api/jobs/build",
        headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
        content=b"{}",
    )
    assert response.status_code == 403
    body = response.json()
    assert app.state.job_csrf_token not in str(body)


def test_same_site_sec_fetch_site_is_rejected(tmp_path, monkeypatch):
    """Locks in the allowlist excluding "same-site" -- only "same-origin"
    and "none" may proceed, even with an otherwise-correct token."""
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    headers["Sec-Fetch-Site"] = "same-site"
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 403


def test_non_ascii_job_header_is_403_not_500(tmp_path, monkeypatch):
    """A non-ASCII X-Garmin-Outreach-Job value must not crash
    secrets.compare_digest() into an uncaught TypeError/500."""
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = [
        ("Sec-Fetch-Site", "same-origin"),
        ("Content-Type", "application/json"),
        ("X-Garmin-Outreach-Job", "café-token".encode("latin-1")),
    ]
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 403


def test_content_type_with_charset_is_accepted(tmp_path, monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(
        services_module, "run_build", lambda *a, **k: called.set() or {"output": {}}
    )
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token, content_type="application/json; charset=utf-8")
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 202
    assert called.wait(timeout=5)


@pytest.mark.parametrize(
    "sec_fetch_site_values",
    [
        ["same-origin", "cross-site"],
        ["cross-site", "same-origin"],
    ],
)
def test_duplicate_sec_fetch_site_header_is_rejected(tmp_path, monkeypatch, sec_fetch_site_values):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = [("Sec-Fetch-Site", value) for value in sec_fetch_site_values]
    headers.append(("X-Garmin-Outreach-Job", app.state.job_csrf_token))
    headers.append(("Content-Type", "application/json"))
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("status_code", "make_headers"),
    [
        (403, lambda app: {"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}),
        (415, lambda app: _job_headers(app.state.job_csrf_token, content_type="text/plain")),
    ],
)
def test_csrf_and_content_type_errors_carry_security_headers(tmp_path, status_code, make_headers):
    app = _app(tmp_path)
    client = _client(app)
    headers = make_headers(app)
    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == status_code
    assert response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


# --- Single-flight -----------------------------------------------------


def test_single_flight_conflict_then_recovers(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls: list[dict] = []

    def _stub(data_dir, **kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(timeout=5), "test did not release the stub in time"
        return {"output": {}}

    monkeypatch.setattr(services_module, "run_build", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    first = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert first.status_code == 202
    assert started.wait(timeout=5)

    second = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert second.status_code == 409
    snapshot = second.json()
    assert snapshot["kind"] == "build"
    assert snapshot["state"] == "running"

    release.set()
    _wait_for_idle(client)

    third = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert third.status_code == 202
    assert len(calls) == 2


def test_thread_start_failure_does_not_wedge_the_runner(tmp_path, monkeypatch):
    """A failed `Thread.start()` must not leave `_current` permanently set:
    the route 500s once, but the *next* POST (once threads work again) gets
    202, not a phantom 409 against a job that never actually started."""
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    headers = _job_headers(app.state.job_csrf_token)

    original_start = threading.Thread.start

    def _failing_start(self):
        raise RuntimeError("can't start new thread")

    threading.Thread.start = _failing_start
    try:
        response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    finally:
        threading.Thread.start = original_start

    assert response.status_code == 500

    second = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert second.status_code == 202


# --- partial_success / failed -------------------------------------------


def test_build_failed_after_acquisition_is_partial_success_and_scrubbed(tmp_path, monkeypatch):
    fake_path = r"C:\Users\alice\secrets\garmin-outreach\data"
    called = threading.Event()

    def _stub(*args, **kwargs):
        called.set()
        raise BuildFailedAfterAcquisition(
            "mapshare",
            {"requests": 1},
            RuntimeError(f"rebuild failed near {fake_path}"),
        )

    monkeypatch.setattr(services_module, "run_build", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 202
    assert called.wait(timeout=5)

    body = _wait_for_idle(client)
    last = body["last"]["build"]
    assert last["state"] == "partial_success"
    assert "rebuild failed" in last["detail"]
    assert "alice" not in last["detail"]
    assert "C:\\Users" not in last["detail"]
    assert fake_path not in last["detail"]


def test_failed_job_scrubs_absolute_path(tmp_path, monkeypatch):
    fake_path = r"A:\dev\garmin-outreach\secret-project\data"
    called = threading.Event()

    def _stub(*args, **kwargs):
        called.set()
        raise RuntimeError(f"Cannot use data directory {fake_path}: access denied")

    monkeypatch.setattr(services_module, "run_build", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 202
    assert called.wait(timeout=5)

    body = _wait_for_idle(client)
    last = body["last"]["build"]
    assert last["state"] == "failed"
    assert "secret-project" not in last["detail"]
    assert fake_path not in last["detail"]
    assert "data" in last["detail"]  # basename survives


# --- Bounded params -------------------------------------------------------


def test_out_of_range_trip_gap_hours_is_400(tmp_path, monkeypatch):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    response = client.post("/api/jobs/build", headers=headers, content=b'{"trip_gap_hours": 5000}')
    assert response.status_code == 400


def test_unknown_param_key_is_400(tmp_path, monkeypatch):
    monkeypatch.setattr(services_module, "run_build", lambda *a, **k: {"output": {}})
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    response = client.post(
        "/api/jobs/build", headers=headers, content=b'{"feed_url": "https://evil.example/"}'
    )
    assert response.status_code == 400


def test_valid_overrides_reach_stubbed_service(tmp_path, monkeypatch):
    called = threading.Event()
    captured: dict = {}

    def _stub(data_dir, **kwargs):
        captured.update(kwargs)
        called.set()
        return {"output": {}}

    monkeypatch.setattr(services_module, "run_build", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)
    response = client.post(
        "/api/jobs/build",
        headers=headers,
        content=b'{"trip_gap_hours": 12.5, "max_speed_kmh": 150, "jump_km": 5}',
    )
    assert response.status_code == 202
    assert called.wait(timeout=5)
    assert captured["gap_hours"] == 12.5
    assert captured["max_speed_kmh"] == 150
    assert captured["jump_km"] == 5
    assert captured["formats"] == jobs_module.DEFAULT_FORMATS


# --- mapshare identifier resolution ---------------------------------------


def test_mapshare_env_identifier_is_startable(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MAPSHARE_ID", raising=False)
    monkeypatch.setenv("GARMIN_MAPSHARE_ID", "env-identifier-value")
    called = threading.Event()
    captured: dict = {}

    def _stub(identifier_or_url, data_dir, **kwargs):
        captured["identifier"] = identifier_or_url
        called.set()
        return {"mapshare": {}}

    monkeypatch.setattr(services_module, "run_mapshare", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    response = client.post("/api/jobs/mapshare", headers=headers, content=b"{}")
    assert response.status_code == 202
    assert called.wait(timeout=5)
    assert captured["identifier"] == "env-identifier-value"
    assert "env-identifier-value" not in response.text


def test_mapshare_state_file_feed_url_is_startable(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MAPSHARE_ID", raising=False)
    feed_url = "https://share.garmin.com/feed/share/testuser"
    state_path = tmp_path / "mapshare-state.json"
    state_path.write_text(f'{{"feed_url": "{feed_url}"}}', encoding="utf-8")

    called = threading.Event()
    captured: dict = {}

    def _stub(identifier_or_url, data_dir, **kwargs):
        captured["identifier"] = identifier_or_url
        called.set()
        return {"mapshare": {}}

    monkeypatch.setattr(services_module, "run_mapshare", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    response = client.post("/api/jobs/mapshare", headers=headers, content=b"{}")
    assert response.status_code == 202
    assert called.wait(timeout=5)
    assert captured["identifier"] == feed_url
    assert feed_url not in response.text

    idle_body = _wait_for_idle(client)
    assert feed_url not in str(idle_body)

    dashboard_response = client.get("/")
    assert feed_url not in dashboard_response.text


def test_mapshare_no_identifier_is_400_naming_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MAPSHARE_ID", raising=False)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    response = client.post("/api/jobs/mapshare", headers=headers, content=b"{}")
    assert response.status_code == 400
    assert "GARMIN_MAPSHARE_ID" in response.text


# --- GET /api/jobs shape ---------------------------------------------------


def test_get_api_jobs_shape(tmp_path):
    app = _app(tmp_path)
    client = _client(app)
    response = client.get("/api/jobs")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"current", "last"}
    assert body["current"] is None
    assert body["last"] == {}


def test_job_csrf_token_never_in_api_jobs_response(tmp_path):
    app = _app(tmp_path)
    client = _client(app)
    response = client.get("/api/jobs")
    assert app.state.job_csrf_token not in response.text


# --- Dashboard rendering ----------------------------------------------------


def test_dashboard_shows_disabled_hints_when_capabilities_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "mapshare_available", lambda data_dir: False)
    monkeypatch.setattr(jobs_module, "explore_available", lambda: False)
    app = _app(tmp_path)
    client = _client(app)
    response = client.get("/")
    assert response.status_code == 200
    assert jobs_module.MAPSHARE_UNAVAILABLE_HINT in response.text
    assert jobs_module.EXPLORE_UNAVAILABLE_HINT in response.text
    assert "disabled" in response.text


def test_dashboard_enables_buttons_when_capabilities_present(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "mapshare_available", lambda data_dir: True)
    monkeypatch.setattr(jobs_module, "explore_available", lambda: True)
    app = _app(tmp_path)
    client = _client(app)
    response = client.get("/")
    assert response.status_code == 200
    assert jobs_module.MAPSHARE_UNAVAILABLE_HINT not in response.text
    assert jobs_module.EXPLORE_UNAVAILABLE_HINT not in response.text


def test_csrf_token_appears_exactly_once_in_dashboard(tmp_path):
    app = _app(tmp_path)
    client = _client(app)
    response = client.get("/")
    assert response.text.count(app.state.job_csrf_token) == 1


def _job_config_from_html(html: str) -> dict:
    match = re.search(r'id="job-config">(.*?)</script>', html, re.S)
    assert match, "dashboard.html must contain the #job-config JSON block"
    return json.loads(match.group(1))


def test_job_config_running_is_false_when_no_job_active(tmp_path):
    response = _client(_app(tmp_path)).get("/")
    assert response.status_code == 200
    assert _job_config_from_html(response.text)["running"] is False


def test_job_config_running_is_true_while_a_job_is_in_progress(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def _stub(data_dir, **kwargs):
        started.set()
        assert release.wait(timeout=5), "test did not release the stub in time"
        return {"output": {}}

    monkeypatch.setattr(services_module, "run_build", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    response = client.post("/api/jobs/build", headers=headers, content=b"{}")
    assert response.status_code == 202
    assert started.wait(timeout=5)

    dashboard_response = client.get("/")
    assert _job_config_from_html(dashboard_response.text)["running"] is True

    release.set()
    _wait_for_idle(client)

    dashboard_after = client.get("/")
    assert _job_config_from_html(dashboard_after.text)["running"] is False


# --- scrub_message (docs/spec-serve-ui.md section 7/8) ---------------------


def test_scrub_message_windows_path_with_spaces_hides_username_and_tail(tmp_path):
    text = r"C:\Users\Matt Wilkie\garmin\data: denied"
    scrubbed = jobs_module.scrub_message(text)
    assert scrubbed is not None
    assert "Matt Wilkie" not in scrubbed
    assert r"C:\Users" not in scrubbed
    assert "denied" in scrubbed


def test_scrub_message_unc_path_is_scrubbed():
    text = r"\\nas01\private\garmin\data"
    scrubbed = jobs_module.scrub_message(text)
    assert scrubbed is not None
    assert "nas01" not in scrubbed
    assert "private" not in scrubbed


def test_scrub_message_url_is_replaced_and_identifier_removed():
    text = "https://inreach.garmin.com/feed/share/SECRETID failed"
    scrubbed = jobs_module.scrub_message(text)
    assert scrubbed is not None
    assert "SECRETID" not in scrubbed
    assert "<url>" in scrubbed
    assert "failed" in scrubbed


def test_scrub_message_drive_letter_does_not_falsely_match_inside_url():
    """Regression: the drive-letter alternative must not match the "s://"
    substring inside "https://" and reduce the whole URL to its last
    slash-delimited segment (which, for a MapShare feed URL, is exactly the
    secret identifier)."""
    text = "feed https://inreach.garmin.com/feed/share/SECRETID failed"
    scrubbed = jobs_module.scrub_message(text)
    assert scrubbed is not None
    assert "SECRETID" not in scrubbed


def test_scrub_message_replaces_known_data_dir_literally(tmp_path):
    data_dir = tmp_path / "secret-project" / "data"
    text = f"cannot write to {data_dir}"
    scrubbed = jobs_module.scrub_message(text, data_dir)
    assert scrubbed is not None
    assert "secret-project" not in scrubbed
    assert str(data_dir) not in scrubbed
    assert "data" in scrubbed


def test_scrub_message_truncates_long_text():
    text = "x" * 500
    scrubbed = jobs_module.scrub_message(text)
    assert scrubbed is not None
    assert len(scrubbed) <= 301
    assert scrubbed.endswith("\u2026")


def test_scrub_message_none_passthrough():
    assert jobs_module.scrub_message(None) is None


# --- Progress ---------------------------------------------------------


def test_mapshare_progress_callback_updates_windows_done(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_MAPSHARE_ID", raising=False)
    monkeypatch.setenv("GARMIN_MAPSHARE_ID", "progress-identifier")

    def _stub(identifier_or_url, data_dir, **kwargs):
        progress = kwargs.get("progress")
        for index in range(3):
            progress({"stage": "window", "window_start_utc": "x", "new_features": index})
        return {"mapshare": {}}

    monkeypatch.setattr(services_module, "run_mapshare", _stub)
    app = _app(tmp_path)
    client = _client(app)
    headers = _job_headers(app.state.job_csrf_token)

    response = client.post("/api/jobs/mapshare", headers=headers, content=b"{}")
    assert response.status_code == 202

    body = _wait_for_idle(client)
    last = body["last"]["mapshare"]
    assert last["state"] == "succeeded"
    assert last["progress"]["windows_done"] == 3
