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
