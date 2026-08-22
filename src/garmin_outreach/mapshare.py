from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from .archive import archive_bytes
from .parsers import parse_kml

ALLOWED_FEED_HOSTS = {
    "inreach.garmin.com",
    "share.garmin.com",
    "explore.garmin.com",
}


def sync_mapshare(
    identifier_or_url: str,
    data_dir: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    full: bool = False,
    chunk_days: int = 31,
    overlap_minutes: int = 10,
    username: str = "",
    password: str | None = None,
    imei: str | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    if chunk_days <= 0:
        raise ValueError("chunk_days must be greater than zero")
    feed_url = feed_url_for(identifier_or_url)
    state_path = data_dir / "mapshare-state.json"
    state = _read_state(state_path)
    now = (end or datetime.now(UTC)).astimezone(UTC)
    if start is not None:
        cursor = start.astimezone(UTC)
    elif not full and state.get("last_success_utc"):
        cursor = _parse_datetime(state["last_success_utc"]) - timedelta(minutes=overlap_minutes)
    else:
        cursor = datetime(2010, 1, 1, tzinfo=UTC)
    if cursor >= now:
        return {"requests": 0, "new_archives": 0, "new_features": 0, "feed_url": feed_url}

    raw_dir = data_dir / "raw" / "mapshare"
    existing_ids = _existing_ids(raw_dir)
    requests = new_archives = new_features = 0
    auth = httpx.BasicAuth(username, password) if password is not None else None
    with httpx.Client(follow_redirects=True, timeout=120, auth=auth) as client:
        while cursor < now:
            window_end = min(cursor + timedelta(days=chunk_days), now)
            params = {"d1": _garmin_time(cursor), "d2": _garmin_time(window_end)}
            if imei:
                params["imei"] = imei
            response = _request_with_retries(client, feed_url, params)
            requests += 1
            _validate_kml_response(response)
            parsed = parse_kml(response.content, source_file=feed_url)
            ids = {(feature.layer, feature.stable_id()) for feature in parsed}
            unseen = ids - existing_ids
            if unseen:
                label = f"{cursor:%Y%m%dT%H%M%SZ}-{window_end:%Y%m%dT%H%M%SZ}"
                _, created = archive_bytes(response.content, raw_dir, label, ".kml")
                new_archives += int(created)
                new_features += len(unseen)
                existing_ids.update(ids)
            _write_state(
                state_path,
                {
                    "feed_url": feed_url,
                    "last_success_utc": _iso(window_end),
                    "updated_utc": _iso(datetime.now(UTC)),
                },
            )
            if progress is not None:
                with contextlib.suppress(Exception):
                    progress(
                        {
                            "stage": "window",
                            "window_start_utc": _iso(cursor),
                            "window_end_utc": _iso(window_end),
                            "new_features": len(unseen),
                        }
                    )
            cursor = window_end
    return {
        "requests": requests,
        "new_archives": new_archives,
        "new_features": new_features,
        "feed_url": feed_url,
    }


def feed_url_for(value: str) -> str:
    if "://" not in value:
        if not value.strip():
            raise ValueError("MapShare identifier cannot be empty")
        return "https://inreach.garmin.com/feed/share/" + quote(value.strip(), safe="")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    allowed = (
        host in ALLOWED_FEED_HOSTS
        or host.endswith(".inreach.garmin.com")
        or host.endswith("-share.explore.garmin.com")
    )
    if parsed.scheme != "https" or not allowed:
        raise ValueError("Feed URL must be HTTPS and hosted by Garmin")
    return value


def _request_with_retries(client, url, params):
    for attempt in range(4):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as error:
            if attempt == 3:
                raise RuntimeError(f"Could not reach the Garmin MapShare feed: {error}") from error
            time.sleep(2**attempt)
            continue
        if response.status_code not in {429, 500, 502, 503, 504}:
            if response.status_code == 401:
                raise RuntimeError("MapShare authentication failed")
            if response.is_error:
                raise RuntimeError(f"Garmin MapShare returned HTTP {response.status_code}")
            return response
        if attempt == 3:
            raise RuntimeError(
                f"Garmin MapShare returned HTTP {response.status_code} after retries"
            )
        retry_after = response.headers.get("retry-after")
        delay = (
            min(float(retry_after), 30.0) if retry_after and retry_after.isdigit() else 2**attempt
        )
        time.sleep(delay)
    raise AssertionError("unreachable")


def _validate_kml_response(response):
    sample = response.content[:1000].lower()
    if b"<kml" not in sample:
        raise RuntimeError(
            "Garmin did not return KML. Check the MapShare identifier, "
            "visibility settings, and password."
        )


def _existing_ids(raw_dir: Path) -> set[tuple[str, str]]:
    ids: set[tuple[str, str]] = set()
    if not raw_dir.exists():
        return ids
    for path in raw_dir.glob("*.kml"):
        try:
            ids.update(
                (feature.layer, feature.stable_id())
                for feature in parse_kml(path.read_bytes(), source_file=str(path))
            )
        except Exception:
            continue
    return ids


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _garmin_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
