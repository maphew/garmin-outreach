import hashlib
import importlib.metadata
import importlib.resources as resources
import json
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("uvicorn")
pytest.importorskip("jinja2")
pytest.importorskip("datastar_py")

from starlette.testclient import TestClient

from garmin_outreach.serve.app import create_app, run
from garmin_outreach.serve.security import CONTENT_SECURITY_POLICY
from garmin_outreach.serve.views import (
    discover_datastar_filename,
    discover_maplibre_css_filename,
    discover_maplibre_js_filename,
    discover_maplibre_worker_filename,
)


def _client(data_dir: Path, **kwargs) -> TestClient:
    # base_url must be a loopback name the host-allowlist middleware
    # accepts; TestClient's default "http://testserver" would be rejected.
    return TestClient(create_app(data_dir), base_url="http://127.0.0.1", **kwargs)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_geojson_features(path: Path, features: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _message_feature(
    feature_id, timestamp_utc=None, text=None, event=None, device_name=None
) -> dict:
    properties: dict = {"feature_id": feature_id}
    if timestamp_utc is not None:
        properties["timestamp_utc"] = timestamp_utc
    if text is not None:
        properties["text"] = text
    if event is not None:
        properties["event"] = event
    if device_name is not None:
        properties["device_name"] = device_name
    return {"type": "Feature", "geometry": None, "properties": properties}


# --- Dashboard / API / 404 ---------------------------------------------


def test_dashboard_empty_data_dir_renders_outputs_missing(tmp_path):
    response = _client(tmp_path).get("/")
    assert response.status_code == 200
    assert "outputs_missing" in response.text
    assert "garmin-outreach build" in response.text


def test_api_summary_shape(tmp_path):
    response = _client(tmp_path).get("/api/summary")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "layers",
        "bbox",
        "input_files",
        "parse_errors",
        "capabilities",
        "freshness",
    }


def test_unknown_route_is_friendly_404(tmp_path):
    response = _client(tmp_path).get("/nope")
    assert response.status_code == 404
    assert "Traceback" not in response.text


def test_unknown_api_route_is_json_404(tmp_path):
    response = _client(tmp_path).get("/api/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


# --- Security headers -----------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/summary", "/messages", "/map"])
def test_csp_header_exact_string(tmp_path, path):
    response = _client(tmp_path).get(path)
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("path", ["/", "/api/summary", "/nope"])
def test_no_store_on_non_static(tmp_path, path):
    response = _client(tmp_path).get(path)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/api/summary", "/nope", "/static/app.css"])
def test_baseline_headers_present_everywhere(tmp_path, path):
    response = _client(tmp_path).get(path)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


@pytest.mark.parametrize("path", ["/", "/api/summary", "/static/app.css", "/messages", "/map"])
def test_trusted_host_rejects_unknown_host(tmp_path, path):
    response = _client(tmp_path).get(path, headers={"Host": "evil.example.com"})
    assert response.status_code == 400


def test_bad_host_400_carries_security_headers(tmp_path):
    response = _client(tmp_path).get("/", headers={"Host": "evil.example.com"})
    assert response.status_code == 400
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


@pytest.mark.parametrize(
    ("host_header", "path"),
    [
        ("[::1]:8477", "/"),
        ("::1", "/"),
        ("LOCALHOST:8477", "/"),
    ],
)
def test_trusted_host_accepts_loopback_variants(tmp_path, host_header, path):
    response = _client(tmp_path).get(path, headers={"Host": host_header})
    assert response.status_code == 200


def test_static_404_carries_csp_and_no_store(tmp_path):
    response = _client(tmp_path).get("/static/does-not-exist.js")
    assert response.status_code == 404
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store"


def test_uncaught_exception_returns_inert_500_with_headers(tmp_path, monkeypatch):
    app = create_app(tmp_path)

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.artifact_store, "shaped_summary", _raise)

    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    response = client.get("/")

    assert response.status_code == 500
    assert "Traceback" not in response.text
    assert "boom" not in response.text
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


# --- Static asset serving -------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/static/..%2Fartifacts.py",
        # Not "/static/%2e%2e/": httpx's client-side RFC 3986 dot-segment
        # removal resolves that exact form to "/" before the request is
        # even sent (real browsers do the same), so it never reaches our
        # handler at all. The no-trailing-slash form below survives client
        # normalization as "/static/.." and exercises the handler's own
        # ".."-in-filename rejection instead of relying on client behavior.
        "/static/%2e%2e",
        "/static/VENDORED.md",
    ],
)
def test_static_traversal_and_disallowed_files_are_404(tmp_path, path):
    response = _client(tmp_path).get(path)
    assert response.status_code == 404


def test_static_datastar_asset_served_with_immutable_cache(tmp_path):
    filename = discover_datastar_filename()
    response = _client(tmp_path).get(f"/static/{filename}")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"

    expected = resources.files("garmin_outreach.serve").joinpath("static", filename).read_bytes()
    assert response.content == expected


def test_static_app_css_is_no_store(tmp_path):
    response = _client(tmp_path).get("/static/app.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "css" in response.headers["content-type"]


# --- Stored-XSS -------------------------------------------------------


def test_xss_in_parse_error_and_mapshare_state_render_escaped(tmp_path):
    payload = "<script>alert(1)</script>"
    _write_json(
        tmp_path / "output" / "summary.json",
        {
            "layers": {"messages": 1},
            "parse_errors": [f"{payload}.kml: XML error"],
        },
    )
    _write_json(tmp_path / "mapshare-state.json", {"last_success_utc": payload})

    response = _client(tmp_path).get("/")

    assert response.status_code == 200
    assert payload not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


# --- Messages -----------------------------------------------------------


def test_messages_empty_state_shows_hint(tmp_path):
    response = _client(tmp_path).get("/messages")
    assert response.status_code == 200
    assert "No messages found" in response.text
    assert "garmin-outreach build" in response.text
    assert "garmin-outreach mapshare" in response.text


def test_messages_renders_entries_and_escapes_xss(tmp_path):
    payload = "<script>alert(1)</script>"
    features = [
        _message_feature(
            "m1",
            timestamp_utc="2026-08-01T00:00:00Z",
            text=payload,
            event=payload,
            device_name=payload,
        ),
        _message_feature("m2"),  # undated
    ]
    _write_geojson_features(tmp_path / "output" / "geojson" / "messages.geojson", features)

    response = _client(tmp_path).get("/messages")

    assert response.status_code == 200
    assert payload not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "2026-08-01T00:00:00Z" in response.text
    assert "no timestamp" in response.text
    assert "Undated" in response.text
    assert "1 undated" in response.text


def test_messages_paging_links(tmp_path):
    base = datetime(2026, 8, 1, tzinfo=UTC)
    features = [
        _message_feature(f"m{i}", timestamp_utc=(base + timedelta(minutes=i)).isoformat())
        for i in range(60)
    ]
    _write_geojson_features(tmp_path / "output" / "geojson" / "messages.geojson", features)

    page1 = _client(tmp_path).get("/messages?page=1")
    assert page1.status_code == 200
    assert "page 1 of 2" in page1.text.lower()
    assert 'href="/messages?page=2"' in page1.text
    assert 'href="/messages?page=0"' not in page1.text

    page2 = _client(tmp_path).get("/messages?page=2")
    assert page2.status_code == 200
    assert 'href="/messages?page=1"' in page2.text
    assert 'href="/messages?page=3"' not in page2.text
    assert "page 2 of 2" in page2.text.lower()


def test_messages_unparseable_timestamps_render_under_single_undated_label(tmp_path):
    features = [
        _message_feature("dated", timestamp_utc="2026-08-01T00:00:00Z", text="dated entry"),
        _message_feature("garbage", timestamp_utc="not-a-date", text="garbage entry"),
        _message_feature("empty", timestamp_utc="", text="empty entry"),
    ]
    _write_geojson_features(tmp_path / "output" / "geojson" / "messages.geojson", features)

    response = _client(tmp_path).get("/messages")

    assert response.status_code == 200
    assert "2 undated" in response.text
    # Exactly one "Undated" section-label row, and it appears before both of
    # the entries whose timestamp failed to parse.
    assert response.text.count("Undated") == 1
    undated_index = response.text.index("Undated")
    assert response.text.index("garbage entry") > undated_index
    assert response.text.index("empty entry") > undated_index
    assert response.text.index("dated entry") < undated_index


def test_messages_non_numeric_page_defaults_to_first(tmp_path):
    features = [_message_feature("m1", timestamp_utc="2026-08-01T00:00:00Z")]
    _write_geojson_features(tmp_path / "output" / "geojson" / "messages.geojson", features)

    response = _client(tmp_path).get("/messages?page=not-a-number")

    assert response.status_code == 200
    assert "page 1 of 1" in response.text.lower()


# --- Map ------------------------------------------------------------------


def test_map_without_geojson_capability_shows_hint(tmp_path):
    response = _client(tmp_path).get("/map")
    assert response.status_code == 200
    assert "not available" in response.text
    assert 'id="map"' not in response.text


def test_map_with_geojson_capability_renders_map_and_config(tmp_path):
    payload = "</script><script>alert(1)</script>"
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}}]
    _write_geojson_features(tmp_path / "output" / "geojson" / "messages.geojson", features)
    _write_json(
        tmp_path / "output" / "summary.json",
        {"layers": {"messages": 1}, "bbox": {"messages": [1.0, 2.0, 1.0, 2.0]}},
    )

    app = create_app(tmp_path)
    # Inject an adversarial layer name into the shaped summary's
    # capabilities to exercise the |tojson escaping of the map-config
    # <script type="application/json"> block, even though the real
    # capability-detection path only ever emits the fixed layer registry.
    original = app.state.artifact_store.shaped_summary

    def _poisoned_summary():
        summary = original()
        summary["capabilities"]["layers_present"] = [payload]
        summary["layers"][payload] = 1
        summary["bbox"][payload] = [0.0, 0.0, 1.0, 1.0]
        return summary

    app.state.artifact_store.shaped_summary = _poisoned_summary
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.get("/map")

    assert response.status_code == 200
    assert 'id="map"' in response.text
    assert 'id="map-config"' in response.text
    assert "</script" not in response.text.split('id="map-config"')[1].split("</script>", 1)[0]
    assert payload not in response.text

    filename = None
    for candidate in resources.files("garmin_outreach.serve").joinpath("static").iterdir():
        if candidate.name.startswith("maplibre-gl-csp-") and "worker" not in candidate.name:
            filename = candidate.name
    assert filename is not None
    assert filename in response.text
    assert "map.js" in response.text


def test_map_config_script_contains_layer_name_and_bbox(tmp_path):
    _write_geojson_features(
        tmp_path / "output" / "geojson" / "messages.geojson",
        [{"type": "Feature", "geometry": None, "properties": {}}],
    )
    _write_json(
        tmp_path / "output" / "summary.json",
        {"layers": {"messages": 1}, "bbox": {"messages": [1.0, 2.0, 3.0, 4.0]}},
    )

    response = _client(tmp_path).get("/map")

    assert response.status_code == 200
    config_block = response.text.split('id="map-config"')[1].split("</script>", 1)[0]
    assert "messages" in config_block
    assert "1.0" in config_block or "1" in config_block


# --- Layer GeoJSON API ------------------------------------------------


def test_api_layer_geojson_filters_and_serves(tmp_path):
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
            "properties": {
                "feature_id": "m1",
                "text": "hello",
                "imei": "300434065012340",
                "source_file": r"C:\Users\alice\secrets\track42.kml",
            },
        }
    ]
    _write_geojson_features(tmp_path / "output" / "geojson" / "messages.geojson", features)

    response = _client(tmp_path).get("/api/layers/messages.geojson")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/geo+json")
    assert response.headers["cache-control"] == "no-store"
    assert "imei" not in response.text
    assert "source_file" not in response.text
    assert "300434065012340" not in response.text
    assert "hello" in response.text


def test_api_layer_geojson_unknown_layer_is_404(tmp_path):
    response = _client(tmp_path).get("/api/layers/not-a-real-layer.geojson")
    assert response.status_code == 404
    assert response.json() == {"error": "layer not available"}
    assert response.headers["cache-control"] == "no-store"


def test_api_layer_geojson_absent_file_is_404(tmp_path):
    response = _client(tmp_path).get("/api/layers/messages.geojson")
    assert response.status_code == 404
    assert response.json() == {"error": "layer not available"}


def test_api_layer_geojson_traversal_attempt_is_404(tmp_path):
    response = _client(tmp_path).get("/api/layers/..%2Fsummary.geojson")
    assert response.status_code == 404


# --- Version agreement ------------------------------------------------


def test_datastar_sdk_js_version_agreement():
    filename = discover_datastar_filename()
    js_bytes = resources.files("garmin_outreach.serve").joinpath("static", filename).read_bytes()
    first_line = js_bytes.decode("utf-8").splitlines()[0]
    assert first_line == f"// Datastar v{importlib.metadata.version('datastar-py')}"


def test_vendored_datastar_sha256_matches_vendored_md():
    filename = discover_datastar_filename()
    js_bytes = resources.files("garmin_outreach.serve").joinpath("static", filename).read_bytes()
    vendored_md = (
        resources.files("garmin_outreach.serve")
        .joinpath("static", "VENDORED.md")
        .read_text(encoding="utf-8")
    )
    match = re.search(r"SHA-256:\s*([0-9a-f]{64})", vendored_md)
    assert match, "VENDORED.md must record a SHA-256 hex digest"
    assert hashlib.sha256(js_bytes).hexdigest() == match.group(1)


def _read_static_bytes(filename: str) -> bytes:
    return resources.files("garmin_outreach.serve").joinpath("static", filename).read_bytes()


def _vendored_md_text() -> str:
    return (
        resources.files("garmin_outreach.serve")
        .joinpath("static", "VENDORED.md")
        .read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "discover", [discover_maplibre_js_filename, discover_maplibre_worker_filename]
)
def test_maplibre_bundle_version_agreement(discover):
    filename = discover()
    js_bytes = _read_static_bytes(filename)
    # The vendored CSP bundle/worker self-report their version in the
    # license-header comment's source URL, not a dedicated "// v..." line
    # (unlike Datastar) -- see the first few lines of either file.
    header = js_bytes[:500].decode("utf-8", errors="replace")
    match = re.search(r"maplibre-gl-js/blob/v([0-9.]+)/LICENSE", header)
    assert match, f"could not find a version in {filename}'s license header"
    assert match.group(1) == "5.24.0"


@pytest.mark.parametrize(
    "discover",
    [
        discover_maplibre_js_filename,
        discover_maplibre_worker_filename,
        discover_maplibre_css_filename,
    ],
)
def test_vendored_maplibre_sha256_matches_vendored_md(discover):
    filename = discover()
    asset_bytes = _read_static_bytes(filename)
    vendored_md = _vendored_md_text()
    match = re.search(re.escape(filename) + r":\s*([0-9a-f]{64})", vendored_md)
    assert match, f"VENDORED.md must record a SHA-256 hex digest for {filename}"
    assert hashlib.sha256(asset_bytes).hexdigest() == match.group(1)


# --- run() ------------------------------------------------------------


def test_run_rejects_non_loopback_host(tmp_path, monkeypatch):
    import garmin_outreach.serve.app as serve_app_module

    def fail_if_constructed(*args, **kwargs):
        pytest.fail("uvicorn.Server must not be constructed when the host is rejected")

    monkeypatch.setattr(serve_app_module.uvicorn, "Server", fail_if_constructed)

    with pytest.raises(RuntimeError, match="out of scope"):
        run(tmp_path, host="0.0.0.0", open_browser=False)


def test_run_port_in_use_raises_actionable_error(tmp_path):
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(RuntimeError) as excinfo:
            run(tmp_path, host="127.0.0.1", port=port, open_browser=False)
        assert str(port) in str(excinfo.value)
        assert "--port" in str(excinfo.value)
        assert "already in use" in str(excinfo.value)
    finally:
        occupied.close()
