import json
import math
import os
from pathlib import Path

from garmin_outreach.exporters import write_outputs
from garmin_outreach.parsers import parse_file
from garmin_outreach.serve.artifacts import LAYERS, ArtifactStore

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, allow_nan=True), encoding="utf-8")


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def _write_geojson_features(path: Path, features: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(payload, allow_nan=True), encoding="utf-8")


def _layer_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / "output" / "geojson" / f"{name}.geojson"


def _message_feature(
    feature_id,
    timestamp_utc=None,
    text=None,
    event=None,
    device_name=None,
    extra=None,
    top_level_id=None,
) -> dict:
    properties: dict = {}
    if feature_id is not None:
        properties["feature_id"] = feature_id
    if timestamp_utc is not None:
        properties["timestamp_utc"] = timestamp_utc
    if text is not None:
        properties["text"] = text
    if event is not None:
        properties["event"] = event
    if device_name is not None:
        properties["device_name"] = device_name
    if extra:
        properties.update(extra)
    feature: dict = {"type": "Feature", "geometry": None, "properties": properties}
    if top_level_id is not None:
        feature["id"] = top_level_id
    return feature


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
    # Present-but-unparsable is distinct from absent (spec section 7):
    # a malformed summary.json is freshness_unknown, not outputs_missing.
    assert shaped["freshness"]["state"] == "freshness_unknown"
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


def test_parse_error_feed_url_path_becomes_unknown(tmp_path):
    # A MapShare feed URL's last path segment would be the share identifier;
    # `_basename()` must not treat it as a filename.
    raw_entry = "https://inreach.garmin.com/feed/share/example: fetch error"
    _write_json(
        tmp_path / "output" / "summary.json",
        {"layers": {}, "input_files": 1, "parse_errors": [raw_entry]},
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["parse_errors"]["entries"] == [{"file": "unknown", "category": "parse-error"}]


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


def test_raw_scan_cache_invalidation_forces_rescan(tmp_path):
    # The ~5s raw-scan cache can make a completion patch render freshness
    # "ok" instead of "outputs_stale" if a newer raw file appeared within
    # the cache window (e.g. a job that archived data then failed its
    # rebuild) -- `invalidate_raw_scan_cache()` is the caller's escape
    # hatch, exercised end-to-end over the SSE path in test_serve_events.py.
    summary_path = tmp_path / "output" / "summary.json"
    _write_json(summary_path, {"layers": {"messages": 1}})
    os.utime(summary_path, (1_700_000_000, 1_700_000_000))

    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["freshness"]["state"] == "ok"

    # A newer raw file appears -- still within the cache window, so a
    # second read reuses the now-stale cached scan result.
    _touch(tmp_path / "raw" / "imports" / "a.kml", 1_700_000_500)
    still_cached = store.shaped_summary()
    assert still_cached["freshness"]["state"] == "ok"

    store.invalidate_raw_scan_cache()
    after_invalidate = store.shaped_summary()
    assert after_invalidate["freshness"]["state"] == "outputs_stale"


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


def test_capabilities_and_layer_hidden_when_current_summary_formats_excludes_geojson(tmp_path):
    # The exporter leaves a previous build's `output/geojson/` dir in place
    # even when the *current* build ran with e.g. `--formats gpkg`; the
    # adapter must not serve that leftover directory's data against the
    # current summary's counts.
    geojson_dir = tmp_path / "output" / "geojson"
    geojson_dir.mkdir(parents=True)
    (geojson_dir / "messages.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    _write_json(
        tmp_path / "output" / "summary.json",
        {"layers": {"messages": 1}, "formats": ["gpkg"]},
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["capabilities"] == {"geojson_available": False, "layers_present": []}
    assert store.layer_geojson("messages") is None


def test_capabilities_and_layer_present_when_current_summary_formats_includes_geojson(tmp_path):
    geojson_dir = tmp_path / "output" / "geojson"
    geojson_dir.mkdir(parents=True)
    (geojson_dir / "messages.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    _write_json(
        tmp_path / "output" / "summary.json",
        {"layers": {"messages": 1}, "formats": ["gpkg", "geojson"]},
    )
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["capabilities"] == {"geojson_available": True, "layers_present": ["messages"]}
    assert store.layer_geojson("messages") is not None


def test_capabilities_directory_behavior_preserved_when_summary_malformed(tmp_path):
    # No usable "formats" field (missing, or the summary itself is
    # unparsable) must fall through to the tolerant directory-existence
    # default rather than hide a layer that is actually present.
    geojson_dir = tmp_path / "output" / "geojson"
    geojson_dir.mkdir(parents=True)
    (geojson_dir / "messages.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    summary_path = tmp_path / "output" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(b'{"layers": {"messages": 1')
    store = ArtifactStore(tmp_path)
    shaped = store.shaped_summary()
    assert shaped["capabilities"] == {"geojson_available": True, "layers_present": ["messages"]}
    assert store.layer_geojson("messages") is not None


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


# --- layer_geojson ---------------------------------------------------------


def test_layer_geojson_filters_forbidden_properties(tmp_path):
    scary_path = r"C:\Users\alice\secrets\raw\imports\track42.kml"
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
            "properties": {
                "feature_id": "m1",
                "text": "hello",
                "timestamp_utc": "2026-08-01T00:00:00Z",
                "device_name": "unit-1",
                "source_file": scary_path,
                "source_kind": "kml",
                "imei": "300434065012340",
                "extra_json": '{"secret": true}',
                "garmin_id": "abc123",
                "incident_id": "inc-1",
                "map_display_name": "Alice's device",
                "latitude": 2.0,
                "longitude": 1.0,
            },
        }
    ]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)
    result = store.layer_geojson("messages")
    assert result is not None
    text = result.decode("utf-8")

    for forbidden in (
        "source_file",
        "source_kind",
        "imei",
        "extra_json",
        "garmin_id",
        "incident_id",
        "map_display_name",
        "latitude",
        "longitude",
        scary_path,
        "300434065012340",
        "feature_id",
        "m1",
    ):
        assert forbidden not in text

    for allowed in ("text", "hello", "timestamp_utc", "device_name", "unit-1"):
        assert allowed in text

    parsed = json.loads(text)
    assert parsed["features"][0]["geometry"] == {"type": "Point", "coordinates": [1.0, 2.0]}


def test_layer_geojson_drops_unlisted_top_level_and_feature_keys(tmp_path):
    path = _layer_path(tmp_path, "waypoints")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "FeatureCollection",
        "metadata": {"generator": "some-future-exporter"},
        "features": [
            {
                "type": "Feature",
                "id": "keep-me",
                "provenance": {"source": "leaky"},
                "geometry": None,
                "properties": {"name": "wp1"},
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = ArtifactStore(tmp_path)
    result = store.layer_geojson("waypoints")
    assert result is not None
    text = result.decode("utf-8")
    assert "metadata" not in text
    assert "generator" not in text
    assert "provenance" not in text
    assert "leaky" not in text

    parsed = json.loads(text)
    assert set(parsed.keys()) == {"type", "features"}
    feature = parsed["features"][0]
    assert set(feature.keys()) == {"type", "id", "geometry", "properties"}
    assert feature["id"] == "keep-me"


def test_layer_geojson_missing_properties_tolerated(tmp_path):
    _write_geojson_features(_layer_path(tmp_path, "trips"), [{"type": "Feature", "geometry": None}])
    store = ArtifactStore(tmp_path)
    result = store.layer_geojson("trips")
    assert result is not None
    parsed = json.loads(result)
    assert parsed["features"][0]["properties"] == {}


def test_layer_geojson_deterministic_bytes(tmp_path):
    _write_geojson_features(
        _layer_path(tmp_path, "waypoints"),
        [{"type": "Feature", "geometry": None, "properties": {"name": "wp1"}}],
    )
    store = ArtifactStore(tmp_path)
    first = store.layer_geojson("waypoints")
    second = store.layer_geojson("waypoints")
    assert first is not None
    assert first == second


def test_layer_geojson_cache_invalidates_on_rewrite(tmp_path):
    path = _layer_path(tmp_path, "events")
    _write_geojson_features(
        path, [{"type": "Feature", "geometry": None, "properties": {"name": "first"}}]
    )
    store = ArtifactStore(tmp_path)
    first = store.layer_geojson("events")
    assert first is not None
    assert b"first" in first

    _write_geojson_features(
        path, [{"type": "Feature", "geometry": None, "properties": {"name": "second"}}]
    )
    stat = path.stat()
    os.utime(path, (stat.st_mtime + 5, stat.st_mtime + 5))

    second = store.layer_geojson("events")
    assert second is not None
    assert b"second" in second
    assert b"first" not in second


def test_layer_geojson_torn_json_returns_none(tmp_path):
    path = _layer_path(tmp_path, "routes")
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"type": "FeatureCollection", "features": [')
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("routes") is None


def test_layer_geojson_real_pipeline_output_never_leaks_garmin_id(tmp_path):
    # End-to-end regression for the feature_id leak (docs/spec-serve-ui.md
    # section 8): drive the real parser/exporter over a real fixture, then
    # confirm the raw garmin_id embedded in Feature.stable_id() (e.g.
    # "garmin:1002:0") never survives the HTTP-facing filter.
    features = parse_file(_FIXTURES_DIR / "mapshare.kml")
    output_dir = tmp_path / "output"
    write_outputs(features, output_dir, formats=("geojson",))

    store = ArtifactStore(tmp_path)

    messages_body = store.layer_geojson("messages")
    assert messages_body is not None
    assert b"garmin:" not in messages_body
    assert b"Everything is fine." in messages_body

    track_points_body = store.layer_geojson("track_points")
    assert track_points_body is not None
    assert b"garmin:" not in track_points_body
    assert b"Test User" in track_points_body


def test_layer_geojson_unknown_name_returns_none(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("bogus") is None


def test_layer_geojson_traversal_name_returns_none(tmp_path):
    (tmp_path / "output").mkdir(parents=True)
    (tmp_path / "output" / "secret.geojson").write_text("{}", encoding="utf-8")
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("../secret") is None
    assert store.layer_geojson("messages/../../secret") is None
    assert store.layer_geojson("messages\\..\\..\\secret") is None


def test_layer_geojson_non_finite_coordinate_returns_none(tmp_path):
    _write_geojson_features(
        _layer_path(tmp_path, "track_points"),
        [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float("nan"), 1.0]},
                "properties": {},
            }
        ],
    )
    store = ArtifactStore(tmp_path)
    assert store.layer_geojson("track_points") is None


# --- messages ----------------------------------------------------------------


def test_messages_ordering_desc_with_tiebreak(tmp_path):
    features = [
        _message_feature("a", "2026-08-01T00:00:00Z"),
        _message_feature("b", "2026-08-02T00:00:00Z"),
        _message_feature("c", "2026-08-02T00:00:00Z"),
    ]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)
    result = store.messages(page=1)
    ids = [entry["id"] for entry in result["entries"]]
    assert ids == ["b", "c", "a"]


def test_messages_undated_entries_at_end_and_counted(tmp_path):
    features = [
        _message_feature("dated1", "2026-08-01T00:00:00Z"),
        _message_feature("undated2"),
        _message_feature("undated1"),
    ]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)
    result = store.messages(page=1)
    ids = [entry["id"] for entry in result["entries"]]
    assert ids == ["dated1", "undated1", "undated2"]
    assert result["undated"] == 2
    assert result["total"] == 3


def test_messages_paging_math(tmp_path):
    features = [_message_feature(f"m{i}", f"2026-08-{i:02d}T00:00:00Z") for i in range(1, 8)]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)

    page1 = store.messages(page=1, per_page=3)
    assert page1["total"] == 7
    assert page1["pages"] == 3
    assert page1["page"] == 1
    assert len(page1["entries"]) == 3

    page3 = store.messages(page=3, per_page=3)
    assert page3["page"] == 3
    assert len(page3["entries"]) == 1

    page_high = store.messages(page=99, per_page=3)
    assert page_high["page"] == 3
    assert len(page_high["entries"]) == 1

    page_low = store.messages(page=0, per_page=3)
    assert page_low["page"] == 1
    assert len(page_low["entries"]) == 3


def test_messages_per_page_clamping(tmp_path):
    features = [_message_feature(f"m{i}", "2026-08-01T00:00:00Z") for i in range(3)]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)

    result_low = store.messages(page=1, per_page=0)
    assert result_low["per_page"] == 1

    result_high = store.messages(page=1, per_page=10_000)
    assert result_high["per_page"] == 500


def test_messages_missing_file_returns_empty_shape(tmp_path):
    store = ArtifactStore(tmp_path)
    result = store.messages(page=1, per_page=25)
    assert result == {
        "total": 0,
        "undated": 0,
        "page": 1,
        "pages": 1,
        "per_page": 25,
        "entries": [],
    }


def test_messages_entry_shape_exact_and_forbidden_fields_absent(tmp_path):
    feature = _message_feature(
        "m1",
        "2026-08-01T00:00:00Z",
        text="hello",
        event="checkin",
        device_name="unit-1",
        extra={"imei": "300434065012340", "source_file": r"C:\Users\alice\raw.kml"},
    )
    _write_geojson_features(_layer_path(tmp_path, "messages"), [feature])
    store = ArtifactStore(tmp_path)
    result = store.messages(page=1)
    entry = result["entries"][0]
    assert entry == {
        "id": "m1",
        "text": "hello",
        "timestamp_utc": "2026-08-01T00:00:00Z",
        "event": "checkin",
        "device_name": "unit-1",
    }
    assert set(entry.keys()) == {"id", "text", "timestamp_utc", "event", "device_name"}


def test_messages_entry_id_fallback_chain(tmp_path):
    features = [
        _message_feature(None, "2026-08-01T00:00:00Z", top_level_id="top-id-1"),
        _message_feature(None, "2026-08-02T00:00:00Z"),
    ]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)
    result = store.messages(page=1)
    ids = [entry["id"] for entry in result["entries"]]
    assert ids == ["", "top-id-1"]


def test_messages_mixed_timestamp_offset_formats_order_correctly(tmp_path):
    features = [
        _message_feature("z_suffix", "2026-08-01T12:00:00Z"),
        _message_feature("explicit_offset", "2026-08-01T13:00:00+00:00"),
        _message_feature("naive", "2026-08-01T11:00:00"),
    ]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)
    result = store.messages(page=1)
    ids = [entry["id"] for entry in result["entries"]]
    assert ids == ["explicit_offset", "z_suffix", "naive"]


def test_messages_unparseable_timestamp_normalized_to_none(tmp_path):
    features = [
        _message_feature("dated", "2026-08-01T00:00:00Z"),
        _message_feature("garbage", "not-a-date"),
        _message_feature("empty", ""),
    ]
    _write_geojson_features(_layer_path(tmp_path, "messages"), features)
    store = ArtifactStore(tmp_path)
    result = store.messages(page=1)
    entries_by_id = {entry["id"]: entry for entry in result["entries"]}
    assert entries_by_id["garbage"]["timestamp_utc"] is None
    assert entries_by_id["empty"]["timestamp_utc"] is None
    assert entries_by_id["dated"]["timestamp_utc"] == "2026-08-01T00:00:00Z"
    assert result["undated"] == 2


def test_messages_cache_invalidates_on_rewrite(tmp_path):
    path = _layer_path(tmp_path, "messages")
    _write_geojson_features(path, [_message_feature("first", "2026-08-01T00:00:00Z")])
    store = ArtifactStore(tmp_path)
    first_result = store.messages(page=1)
    assert [entry["id"] for entry in first_result["entries"]] == ["first"]

    _write_geojson_features(path, [_message_feature("second", "2026-08-02T00:00:00Z")])
    stat = path.stat()
    os.utime(path, (stat.st_mtime + 5, stat.st_mtime + 5))

    second_result = store.messages(page=1)
    assert [entry["id"] for entry in second_result["entries"]] == ["second"]
