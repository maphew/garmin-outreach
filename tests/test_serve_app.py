import hashlib
import importlib.metadata
import importlib.resources as resources
import json
import re
import socket
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("uvicorn")
pytest.importorskip("jinja2")
pytest.importorskip("datastar_py")

from starlette.testclient import TestClient

from garmin_outreach.serve.app import create_app, run
from garmin_outreach.serve.security import CONTENT_SECURITY_POLICY
from garmin_outreach.serve.views import discover_datastar_filename


def _client(data_dir: Path, **kwargs) -> TestClient:
    # base_url must be a loopback name the host-allowlist middleware
    # accepts; TestClient's default "http://testserver" would be rejected.
    return TestClient(create_app(data_dir), base_url="http://127.0.0.1", **kwargs)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


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


@pytest.mark.parametrize("path", ["/", "/api/summary"])
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


@pytest.mark.parametrize("path", ["/", "/api/summary", "/static/app.css"])
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
