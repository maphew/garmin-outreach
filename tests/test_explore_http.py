"""Unit tests for the browserless Explore export contract.

These cover the pure request-building logic reverse-engineered from map.js. The
live HTTP path needs a signed-in Garmin session and cannot run in CI, so it is
exercised separately with a real account.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from garmin_outreach.explore_http import (
    build_export_body,
    extract_kml_if_kmz,
    parse_users,
)


def test_parse_users_bare_list():
    payload = [
        {"Id": 11, "GroupID": 2, "Checked": True},
        {"Id": 12, "GroupID": 2, "Checked": False},
    ]
    users = parse_users(payload)
    assert [u["id"] for u in users] == ["11", "12"]
    assert users[0]["group_id"] == 2
    assert users[1]["checked"] is False


def test_parse_users_live_envelope():
    # The real GetUsersSimplified response is a {success, result} envelope; the
    # user rows live under "result".
    payload = {"success": True, "result": [{"Id": 7, "GroupID": 5, "Checked": True}]}
    users = parse_users(payload)
    assert users == [{"id": "7", "group_id": 5, "checked": True}]


def test_parse_users_envelope_and_key_casing():
    # Parsing must also tolerate a "Users" envelope and mixed key casing.
    payload = {"Users": [{"id": 7, "groupId": 5}], "Groups": [{"Id": 5}]}
    users = parse_users(payload)
    assert users == [{"id": "7", "group_id": 5, "checked": True}]


def test_parse_users_drops_rows_without_id():
    payload = [{"GroupID": 1}, {"Id": 9}]
    assert [u["id"] for u in parse_users(payload)] == ["9"]


def test_parse_users_handles_non_list():
    assert parse_users(None) == []
    assert parse_users({"Users": None}) == []


def test_build_export_body_exports_everything():
    users = [{"id": "11", "group_id": 2, "checked": True}, {"id": "12", "group_id": 2}]
    body = build_export_body(users)
    assert body["visibleUserIds"] == "11,12"
    assert body["chosenGroup"] == ""
    assert body["deviceMenuItem"] == "Active"
    assert body["serviceTypes"] == "5,3,6,7,9,10"
    assert body["filter"] == "null"
    # Blank exclusion lists mean "exclude nothing" -> every waypoint and route.
    assert body["waypointsNotVisibleSyncIds"] == ""
    assert body["invisibleRoutesSyncIds"] == ""
    assert body["fromDate"] == "" and body["toDate"] == ""


def test_build_export_body_empty_users():
    body = build_export_body([])
    assert body["visibleUserIds"] == ""


def _kmz(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_extract_kml_if_kmz_unwraps_zip():
    # Garmin delivers a "KML" export as a KMZ wrapping mapdata.kml.
    inner = b'<?xml version="1.0"?><kml><Document/></kml>'
    assert extract_kml_if_kmz(_kmz({"mapdata.kml": inner})) == inner


def test_extract_kml_if_kmz_passes_raw_kml_through():
    raw = b"<kml><Document/></kml>"
    assert extract_kml_if_kmz(raw) == raw


def test_extract_kml_if_kmz_rejects_kmz_without_kml():
    with pytest.raises(RuntimeError, match="no KML"):
        extract_kml_if_kmz(_kmz({"overlay.png": b"\x89PNG"}))
