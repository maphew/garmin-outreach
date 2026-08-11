from pathlib import Path

from garmin_outreach.cleanup import split_trips
from garmin_outreach.model import deduplicate
from garmin_outreach.parsers import parse_file, parse_kml

FIXTURES = Path(__file__).parent / "fixtures"


def test_mapshare_events_are_separated():
    features = parse_file(FIXTURES / "mapshare.kml")
    layers = [feature.layer for feature in features]
    assert layers.count("track_points") == 2
    assert layers.count("messages") == 1
    point = next(feature for feature in features if feature.feature_id == "garmin:1001:0")
    assert point.coordinates == (-135.0, 60.0)
    assert point.properties["elevation_m"] == 700.5
    assert point.properties["valid_gps_fix"] is True


def test_gpx_layers_and_track_points():
    features = parse_file(FIXTURES / "explore.gpx")
    layers = [feature.layer for feature in features]
    assert layers.count("waypoints") == 1
    assert layers.count("routes") == 1
    assert layers.count("tracks") == 1
    assert layers.count("track_points") == 2


def test_trip_derivation_preserves_points():
    features = parse_file(FIXTURES / "mapshare.kml")
    result = split_trips(deduplicate(features))
    assert sum(feature.layer == "track_points" for feature in result) == 2
    trip = next(feature for feature in result if feature.layer == "trips")
    assert trip.properties["point_count"] == 2
    assert trip.properties["distance_km"] > 0


def test_duplicate_garmin_events_are_idempotent():
    data = (FIXTURES / "mapshare.kml").read_bytes()
    once = parse_kml(data)
    assert len(deduplicate(once + once)) == len(once)


def test_kml_folder_names_keep_line_types_separate():
    data = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Folder><name>Routes</name><Placemark><name>Route A</name>
        <LineString><coordinates>-135,60 -136,61</coordinates></LineString>
      </Placemark></Folder>
      <Folder><name>Courses</name><Placemark><name>Course A</name>
        <LineString><coordinates>-137,62 -138,63</coordinates></LineString>
      </Placemark></Folder>
    </Document></kml>"""
    features = parse_kml(data)
    assert [feature.layer for feature in features] == ["routes", "courses"]
