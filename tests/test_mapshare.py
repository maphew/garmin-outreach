from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import garmin_outreach.mapshare as mapshare
from garmin_outreach.mapshare import feed_url_for, sync_mapshare

FIXTURES = Path(__file__).parent / "fixtures"


def test_identifier_becomes_documented_feed_url():
    assert feed_url_for("My Map") == "https://inreach.garmin.com/feed/share/My%20Map"


def test_credentials_cannot_be_redirected_to_non_garmin_host():
    with pytest.raises(ValueError, match="hosted by Garmin"):
        feed_url_for("https://example.com/feed/share/me")


def test_zero_length_windows_are_rejected():
    with pytest.raises(ValueError, match="greater than zero"):
        sync_mapshare("sample", Path("unused"), chunk_days=0)


def test_mapshare_sync_archives_only_new_events(tmp_path, monkeypatch):
    content = (FIXTURES / "mapshare.kml").read_bytes()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, params):
            request = httpx.Request("GET", url, params=params)
            return httpx.Response(200, content=content, request=request)

    monkeypatch.setattr(mapshare.httpx, "Client", FakeClient)
    start = datetime(2024, 6, 1, tzinfo=UTC)
    end = datetime(2024, 6, 2, tzinfo=UTC)

    first = sync_mapshare("sample", tmp_path, start=start, end=end)
    second = sync_mapshare("sample", tmp_path, start=start, end=end)

    assert first["new_archives"] == 1
    assert first["new_features"] == 3
    assert second["new_archives"] == 0
    assert second["new_features"] == 0
    assert len(list((tmp_path / "raw" / "mapshare").glob("*.kml"))) == 1
