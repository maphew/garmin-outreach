import json
from pathlib import Path

import geopandas as gpd

from garmin_outreach.cleanup import split_trips
from garmin_outreach.exporters import write_outputs
from garmin_outreach.parsers import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_writes_geopackage_geojson_and_shapefiles(tmp_path):
    features = split_trips(parse_file(FIXTURES / "mapshare.kml"))
    summary = write_outputs(features, tmp_path)
    assert summary["layers"]["messages"] == 1
    assert (tmp_path / "garmin-outreach.gpkg").exists()
    assert (tmp_path / "geojson" / "messages.geojson").exists()
    assert (tmp_path / "shapefile" / "track_points.shp").exists()
    messages = gpd.read_file(tmp_path / "garmin-outreach.gpkg", layer="messages")
    assert messages.iloc[0]["text"] == "Everything is fine."


def test_summary_bbox_covers_each_layer(tmp_path):
    features = split_trips(parse_file(FIXTURES / "mapshare.kml"))
    summary = write_outputs(features, tmp_path)

    assert set(summary["bbox"].keys()) == set(summary["layers"].keys())
    for bounds in summary["bbox"].values():
        assert len(bounds) == 4
        assert all(isinstance(value, float) for value in bounds)
        minx, miny, maxx, maxy = bounds
        assert minx <= maxx
        assert miny <= maxy

    # "messages" has a single fixture point at (-135.001, 60.001).
    assert summary["bbox"]["messages"] == [-135.001, 60.001, -135.001, 60.001]


def test_summary_bbox_round_trips_through_summary_json(tmp_path):
    features = split_trips(parse_file(FIXTURES / "mapshare.kml"))
    summary = write_outputs(features, tmp_path)

    written_summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert written_summary["bbox"] == summary["bbox"]
