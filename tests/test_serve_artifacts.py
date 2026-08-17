import json
import math
import os
from pathlib import Path

from garmin_outreach.serve.artifacts import LAYERS, ArtifactStore


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, allow_nan=True), encoding="utf-8")


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def test_missing_data_dir_entirely(tmp_path):
    store = ArtifactStore(tmp_path / "does-not-exist")
    shaped = store.shaped_summary()
    assert shaped["layers"] == {}
    assert shaped["bbox"] == {}
    assert shaped["input_files"] is None
    assert shaped["parse_errors"] == {"count": 0, "entries": []}
    assert shaped["capabilities"] == {"geojson_available": False, "layers_present": []}
    assert shaped["freshness"]["state"] == "outputs_missing"
    assert shaped["freshness"]["mapshare_last_success_utc"] is None


def test_missing_summary_json(tmp_path):
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["state"] == "outputs_missing"
    assert shaped["layers"] == {}


def test_corrupt_summary_json_truncated(tmp_path):
    summary_path = tmp_path / "output" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_bytes(b'{"layers": {"messages": 1')
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["state"] == "outputs_missing"
    assert shaped["layers"] == {}


def test_write_outputs_shape_summary(tmp_path):
    _write_json(
        tmp_path / "output" / "summary.json",
        {
            "feature_count": 3,
            "layers": {"messages": 1, "track_points": 2},
            "formats": ["geojson"],
            "written": {"geojson": ["messages.geojson", "track_points.geojson"]},
            "bbox": {"messages": [-1.0, -1.0, 1.0, 1.0]},
        },
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["layers"] == {"messages": 1, "track_points": 2}
    assert shaped["bbox"] == {"messages": [-1.0, -1.0, 1.0, 1.0]}
    assert shaped["input_files"] is None
    assert shaped["parse_errors"] == {"count": 0, "entries": []}
    assert shaped["freshness"]["state"] == "ok"


def test_rebuild_shape_summary(tmp_path):
    _write_json(
        tmp_path / "output" / "summary.json",
        {
            "feature_count": 3,
            "layers": {"messages": 1, "track_points": 2},
            "formats": ["geojson"],
            "written": {"geojson": ["messages.geojson", "track_points.geojson"]},
            "bbox": {"messages": [-1.0, -1.0, 1.0, 1.0]},
            "input_files": 5,
            "parse_errors": [r"C:\Users\alice\data\raw\bad.kml: XML error"],
        },
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["input_files"] == 5
    assert shaped["parse_errors"]["count"] == 1
    assert shaped["parse_errors"]["entries"] == [{"file": "bad.kml", "category": "parse-error"}]


def test_summary_valid_json_but_list_not_dict(tmp_path):
    summary_path = tmp_path / "output" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["layers"] == {}
    assert shaped["bbox"] == {}
    assert shaped["input_files"] is None
    assert shaped["parse_errors"] == {"count": 0, "entries": []}
    assert shaped["freshness"]["state"] == "freshness_unknown"


def test_parse_error_sanitization_strips_absolute_path_and_exception_text(tmp_path):
    scary_path = r"C:\Users\alice\secrets\raw\imports\track42.kml"
    raw_entry = f"{scary_path}: XMLSyntaxError: token '<script>' near IMEI 300434065012340, line 12"
    _write_json(
        tmp_path / "output" / "summary.json",
        {
            "layers": {},
            "input_files": 1,
            "parse_errors": [raw_entry],
        },
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["parse_errors"]["count"] == 1
    assert shaped["parse_errors"]["entries"] == [{"file": "track42.kml", "category": "parse-error"}]
    dumped = json.dumps(shaped)
    assert scary_path not in dumped
    assert "alice" not in dumped
    assert "300434065012340" not in dumped


def test_parse_error_entry_not_matching_pattern_becomes_unknown(tmp_path):
    _write_json(
        tmp_path / "output" / "summary.json",
        {"layers": {}, "input_files": 1, "parse_errors": ["no separator here", 42, None]},
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["parse_errors"]["count"] == 3
    assert shaped["parse_errors"]["entries"] == [
        {"file": "unknown", "category": "parse-error"},
        {"file": "unknown", "category": "parse-error"},
        {"file": "unknown", "category": "parse-error"},
    ]


def test_staleness_stale_when_raw_file_newer_than_summary(tmp_path):
    summary_path = tmp_path / "output" / "summary.json"
    _write_json(summary_path, {"layers": {"messages": 1}})
    os.utime(summary_path, (1_700_000_000, 1_700_000_000))
    _touch(tmp_path / "raw" / "imports" / "a.kml", 1_700_000_500)

    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["state"] == "outputs_stale"


def test_staleness_ok_when_raw_file_older_than_summary(tmp_path):
    summary_path = tmp_path / "output" / "summary.json"
    _write_json(summary_path, {"layers": {"messages": 1}})
    os.utime(summary_path, (1_700_000_500, 1_700_000_500))
    _touch(tmp_path / "raw" / "imports" / "a.kml", 1_700_000_000)

    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["state"] == "ok"


def test_staleness_ok_when_raw_dir_absent(tmp_path):
    _write_json(tmp_path / "output" / "summary.json", {"layers": {"messages": 1}})
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["state"] == "ok"


def test_mapshare_last_success_utc_present_and_independent_of_state(tmp_path):
    _write_json(
        tmp_path / "mapshare-state.json",
        {
            "feed_url": "https://inreach.garmin.com/feed/share/example",
            "last_success_utc": "2026-08-01T12:00:00Z",
            "updated_utc": "2026-08-01T12:00:05Z",
        },
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["state"] == "outputs_missing"
    assert shaped["freshness"]["mapshare_last_success_utc"] == "2026-08-01T12:00:00Z"


def test_mapshare_state_malformed_yields_none_state_unaffected(tmp_path):
    _write_json(tmp_path / "output" / "summary.json", {"layers": {"messages": 1}})
    state_path = tmp_path / "mapshare-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(b"{not valid json")
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["mapshare_last_success_utc"] is None
    assert shaped["freshness"]["state"] == "ok"


def test_capabilities_with_geojson_dir_ignores_stray_files(tmp_path):
    geojson_dir = tmp_path / "output" / "geojson"
    geojson_dir.mkdir(parents=True)
    (geojson_dir / "track_points.geojson").write_text("{}", encoding="utf-8")
    (geojson_dir / "messages.geojson").write_text("{}", encoding="utf-8")
    (geojson_dir / "bogus.geojson").write_text("{}", encoding="utf-8")
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["capabilities"]["geojson_available"] is True
    assert shaped["capabilities"]["layers_present"] == ["track_points", "messages"]


def test_capabilities_without_geojson_dir(tmp_path):
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["capabilities"] == {"geojson_available": False, "layers_present": []}


def test_unknown_layer_names_dropped_from_shaped_layers(tmp_path):
    _write_json(
        tmp_path / "output" / "summary.json",
        {"layers": {"messages": 3, "bogus_layer": 5}},
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["layers"] == {"messages": 3}


def test_bbox_filtering_drops_non_finite_and_non_registry_entries(tmp_path):
    _write_json(
        tmp_path / "output" / "summary.json",
        {
            "layers": {},
            "bbox": {
                "messages": [-1.0, -2.0, 1.0, 2.0],
                "track_points": [math.nan, 0.0, 1.0, 1.0],
                "waypoints": [math.inf, 0.0, 1.0, 1.0],
                "bogus_layer": [0.0, 0.0, 1.0, 1.0],
                "events": [0.0, 0.0, 1.0],
            },
        },
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["bbox"] == {"messages": [-1.0, -2.0, 1.0, 2.0]}


def test_layer_geojson_known_present_returns_exact_bytes(tmp_path):
    geojson_dir = tmp_path / "output" / "geojson"
    geojson_dir.mkdir(parents=True)
    content = b'{"type": "FeatureCollection", "features": []}'
    (geojson_dir / "messages.geojson").write_bytes(content)
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("messages") == content


def test_layer_geojson_absent_layer_returns_none(tmp_path):
    geojson_dir = tmp_path / "output" / "geojson"
    geojson_dir.mkdir(parents=True)
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("track_points") is None


def test_layer_geojson_unknown_name_returns_none(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("nope") is None


def test_layer_geojson_traversal_attempts_return_none(tmp_path):
    geojson_dir = tmp_path / "output" / "geojson"
    geojson_dir.mkdir(parents=True)
    (geojson_dir / "messages.geojson").write_bytes(b"{}")
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("..%2F") is None
    assert store.layer_geojson("../etc") is None
    assert store.layer_geojson("../output/geojson/messages") is None


def test_registry_contains_expected_layers():
    assert LAYERS == (
        "track_points",
        "messages",
        "waypoints",
        "events",
        "tracks",
        "routes",
        "courses",
        "trips",
    )
