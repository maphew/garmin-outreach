"""Read-only artifact adapter over the pipeline's data/ outputs.

Presentation layer only: shapes and sanitizes what `exporters.py` and
`pipeline.py` already wrote. Never parses KML/GPX itself. Outputs are not
one atomic snapshot (`_replace_directory()` in `exporters.py` leaves a
window where `output/geojson/` does not exist, and `summary.json` is
written twice per rebuild with two different shapes), so every read here
is tolerant: short retry on transient errors, every field optional,
malformed input degrades instead of raising.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path

LAYERS: tuple[str, ...] = (
    "track_points",
    "messages",
    "waypoints",
    "events",
    "tracks",
    "routes",
    "courses",
    "trips",
)

# Fields safe to expose on the HTTP surface. Never: source_file, source_kind,
# imei, extra_json, garmin_id, incident_id, map_display_name, latitude,
# longitude (geometry carries position) -- see docs/spec-serve-ui.md section 8.
# Also never: feature_id -- `Feature.stable_id()` embeds the raw garmin_id
# (e.g. "garmin:1002:0") whenever the parser supplied one, which is exactly
# the kind of identifier this allowlist otherwise forbids re-exposing.
PROPERTY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "name",
        "timestamp_utc",
        "device_name",
        "device_type",
        "event",
        "text",
        "elevation_m",
        "velocity_kmh",
        "course_deg",
        "valid_gps_fix",
        "in_emergency",
        "point_count",
        "start_time_utc",
        "end_time_utc",
        "split_reason",
        "distance_km",
    }
)

# Top-level GeoJSON members kept as-is when filtering a layer file;
# everything else (e.g. a future exporter adding "metadata") is dropped
# rather than passed through.
_TOP_LEVEL_GEOJSON_ALLOWLIST: frozenset[str] = frozenset({"type", "name", "crs", "bbox"})

# Per-feature members kept as-is. "id" is included deliberately: the
# pipeline's GeoJSON writer (pyogrio's GeoJSON driver, always called with a
# fresh unnamed index -- see exporters.write_outputs) never emits an OGC
# "id" member in real output (verified against tests/fixtures/mapshare.kml),
# so it never carries the garmin-embedding stable id that
# properties["feature_id"] would. If a future exporter change starts setting
# "id" from stable_id(), drop it from this set too.
_FEATURE_GEOJSON_ALLOWLIST: frozenset[str] = frozenset({"type", "id", "geometry", "properties"})

_RETRY_DELAY_SECONDS = 0.05
_STALE_SCAN_CACHE_SECONDS = 5.0
_PARSE_ERROR_SEPARATOR = ": "
_DEFAULT_PER_PAGE = 50
_MIN_PER_PAGE = 1
_MAX_PER_PAGE = 500


def _read_bytes(path: Path) -> bytes | None:
    for attempt in range(2):
        try:
            return path.read_bytes()
        except (FileNotFoundError, PermissionError):
            # No retry is worth attempting if the parent directory itself
            # doesn't exist yet (e.g. an empty data/ dir) -- the file can't
            # appear mid-request, so this would just pay the delay twice per
            # request for nothing.
            if attempt == 0 and path.parent.is_dir():
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            return None
        except OSError:
            return None
    return None


def _stat_mtime(path: Path) -> float | None:
    for attempt in range(2):
        try:
            return path.stat().st_mtime
        except (FileNotFoundError, PermissionError):
            if attempt == 0 and path.parent.is_dir():
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            return None
        except OSError:
            return None
    return None


def _stat_key(path: Path) -> tuple[int, int] | None:
    """Cheap cache-invalidation key: (mtime_ns, size). Not a content hash --
    good enough to detect the append-only/replace-directory rewrite patterns
    this module already tolerates elsewhere."""
    for attempt in range(2):
        try:
            stat = path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except (FileNotFoundError, PermissionError):
            if attempt == 0 and path.parent.is_dir():
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            return None
        except OSError:
            return None
    return None


# per_page is not currently reachable from HTTP -- views.messages() always
# calls ArtifactStore.messages() with the default, so this clamp only
# guards direct/future callers (phase B may add a ?per_page= query param).
def _clamp_per_page(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_PER_PAGE
    if parsed < _MIN_PER_PAGE:
        return _MIN_PER_PAGE
    if parsed > _MAX_PER_PAGE:
        return _MAX_PER_PAGE
    return parsed


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # Python 3.11+ (requires-python >=3.11) accepts a "Z" UTC suffix
        # natively, so no manual "Z" -> "+00:00" rewrite is needed here.
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Assume naive timestamps are UTC so they compare safely against
        # timezone-aware ones instead of raising.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _entry_sort_key(item: tuple[dict, datetime | None]) -> tuple[int, float, str]:
    entry, parsed_ts = item
    tiebreak = str(entry["id"])
    if parsed_ts is None:
        # Undated bucket sorts after every dated entry, ordered by id.
        return (1, 0.0, tiebreak)
    return (0, -parsed_ts.timestamp(), tiebreak)


def _basename(path_text: str) -> str:
    stripped = path_text.strip()
    if "://" in stripped:
        # A feed URL's last path segment would be the MapShare identifier;
        # treat anything URL-shaped as unparseable rather than leak it.
        return "unknown"
    tail = re.split(r"[\\/]", stripped)[-1]
    return tail or "unknown"


def _sanitize_parse_error(raw_entry: object) -> dict:
    if not isinstance(raw_entry, str):
        return {"file": "unknown", "category": "parse-error"}
    path_part, separator, _rest = raw_entry.partition(_PARSE_ERROR_SEPARATOR)
    if not separator or not path_part.strip():
        return {"file": "unknown", "category": "parse-error"}
    return {"file": _basename(path_part), "category": "parse-error"}


class ArtifactStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self._raw_scan_cache: tuple[float, float | None] | None = None
        self._layer_geojson_cache: dict[str, tuple[tuple[int, int], bytes]] = {}
        self._messages_cache: tuple[tuple[int, int], list[dict], int] | None = None

    def layer_geojson(self, name: str) -> bytes | None:
        if name not in LAYERS or "/" in name or "\\" in name:
            return None
        summary_path = self.data_dir / "output" / "summary.json"
        raw_summary, _summary_mtime, _summary_present = self._read_summary(summary_path)
        if self._formats_exclude_geojson(raw_summary):
            # The exporter leaves a previous build's `output/geojson/` dir in
            # place even when the *current* summary was built with e.g.
            # `--formats gpkg` -- without this gate a stale directory would
            # keep serving a previous build's layer data against the current
            # summary's counts. Malformed/missing summaries fall through to
            # the tolerant directory-existence behavior below (unchanged).
            return None
        geojson_dir = self.data_dir / "output" / "geojson"
        try:
            resolved_dir = geojson_dir.resolve()
            candidate = (geojson_dir / f"{name}.geojson").resolve()
        except OSError:
            return None
        if not candidate.is_relative_to(resolved_dir):
            return None
        try:
            return self._layer_geojson(name, candidate)
        except Exception:
            return None

    def _layer_geojson(self, name: str, path: Path) -> bytes | None:
        stat_key = _stat_key(path)
        if stat_key is None:
            return None
        cached = self._layer_geojson_cache.get(name)
        if cached is not None and cached[0] == stat_key:
            return cached[1]
        data = _read_bytes(path)
        if data is None:
            return None
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        filtered = self._filter_layer_geojson(parsed)
        if filtered is None:
            return None
        try:
            encoded = json.dumps(
                filtered, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except ValueError:
            # A non-finite coordinate (or other value allow_nan rejects) is a
            # data problem, not a request-handling one -- treat it the same
            # as any other unreadable/torn layer file.
            return None
        self._layer_geojson_cache[name] = (stat_key, encoded)
        return encoded

    def _filter_layer_geojson(self, parsed: object) -> dict | None:
        if not isinstance(parsed, dict):
            return None
        features = parsed.get("features")
        if not isinstance(features, list):
            return None
        filtered_features = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            filtered_feature = {
                key: value for key, value in feature.items() if key in _FEATURE_GEOJSON_ALLOWLIST
            }
            filtered_feature["properties"] = {
                key: value for key, value in properties.items() if key in PROPERTY_ALLOWLIST
            }
            filtered_features.append(filtered_feature)
        result = {
            key: value for key, value in parsed.items() if key in _TOP_LEVEL_GEOJSON_ALLOWLIST
        }
        result["features"] = filtered_features
        return result

    def messages(self, page: int, per_page: int = 50) -> dict:
        safe_per_page = _clamp_per_page(per_page)
        try:
            return self._messages(page, safe_per_page)
        except Exception:
            return {
                "total": 0,
                "undated": 0,
                "page": 1,
                "pages": 1,
                "per_page": safe_per_page,
                "entries": [],
            }

    def _messages(self, page: int, per_page: int) -> dict:
        entries, undated = self._message_entries()
        total = len(entries)
        pages = max(1, math.ceil(total / per_page))
        if page < 1:
            page = 1
        elif page > pages:
            page = pages
        start = (page - 1) * per_page
        page_entries = entries[start : start + per_page]
        return {
            "total": total,
            "undated": undated,
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "entries": page_entries,
        }

    def _message_entries(self) -> tuple[list[dict], int]:
        summary_path = self.data_dir / "output" / "summary.json"
        raw_summary, _summary_mtime, _summary_present = self._read_summary(summary_path)
        if self._formats_exclude_geojson(raw_summary):
            # Same stale-directory gate as layer_geojson(): a leftover
            # messages.geojson from a previous geojson build must not feed
            # the timeline when the current build excluded geojson.
            return [], 0
        path = self.data_dir / "output" / "geojson" / "messages.geojson"
        stat_key = _stat_key(path)
        if stat_key is None:
            return [], 0
        cached = self._messages_cache
        if cached is not None and cached[0] == stat_key:
            return cached[1], cached[2]
        data = _read_bytes(path)
        if data is None:
            return [], 0
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return [], 0
        if not isinstance(parsed, dict):
            return [], 0
        features = parsed.get("features")
        if not isinstance(features, list):
            return [], 0
        items: list[tuple[dict, datetime | None]] = []
        undated = 0
        for feature in features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            entry_id = properties.get("feature_id")
            if entry_id is None:
                entry_id = feature.get("id")
            if entry_id is None:
                entry_id = ""
            timestamp_utc = properties.get("timestamp_utc")
            if not isinstance(timestamp_utc, str):
                timestamp_utc = None
            parsed_ts = _parse_timestamp(timestamp_utc)
            # An unparseable string (garbage, empty) is undated the same as a
            # missing field -- keeping the raw string here would put it above
            # the /messages template's "Undated" label while still counting
            # toward the undated total, which the template keys off `is none`.
            if parsed_ts is None:
                timestamp_utc = None
            entry = {
                "id": entry_id,
                "text": properties.get("text") if isinstance(properties.get("text"), str) else None,
                "timestamp_utc": timestamp_utc,
                "event": properties.get("event")
                if isinstance(properties.get("event"), str)
                else None,
                "device_name": properties.get("device_name")
                if isinstance(properties.get("device_name"), str)
                else None,
            }
            if parsed_ts is None:
                undated += 1
            items.append((entry, parsed_ts))
        items.sort(key=_entry_sort_key)
        sorted_entries = [entry for entry, _ in items]
        self._messages_cache = (stat_key, sorted_entries, undated)
        return sorted_entries, undated

    def shaped_summary(self) -> dict:
        try:
            return self._shaped_summary()
        except Exception:
            return {
                "layers": {},
                "bbox": {},
                "input_files": None,
                "parse_errors": {"count": 0, "entries": []},
                "capabilities": {"geojson_available": False, "layers_present": []},
                "freshness": {"state": "freshness_unknown", "mapshare_last_success_utc": None},
            }

    def _shaped_summary(self) -> dict:
        summary_path = self.data_dir / "output" / "summary.json"
        raw_summary, summary_mtime, summary_present = self._read_summary(summary_path)
        return {
            "layers": self._shaped_layers(raw_summary),
            "bbox": self._shaped_bbox(raw_summary),
            "input_files": self._shaped_input_files(raw_summary),
            "parse_errors": self._shaped_parse_errors(raw_summary),
            "capabilities": self._capabilities(raw_summary),
            "freshness": self._freshness(raw_summary, summary_mtime, summary_present),
        }

    def _read_summary(self, path: Path) -> tuple[object | None, float | None, bool]:
        data = _read_bytes(path)
        if data is None:
            return None, None, False
        mtime = _stat_mtime(path)
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, mtime, True
        return parsed, mtime, True

    def _shaped_layers(self, raw_summary: object) -> dict[str, int]:
        if not isinstance(raw_summary, dict):
            return {}
        raw_layers = raw_summary.get("layers")
        if not isinstance(raw_layers, dict):
            return {}
        shaped: dict[str, int] = {}
        for name in LAYERS:
            value = raw_layers.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                shaped[name] = value
        return shaped

    def _shaped_bbox(self, raw_summary: object) -> dict[str, list[float]]:
        if not isinstance(raw_summary, dict):
            return {}
        raw_bbox = raw_summary.get("bbox")
        if not isinstance(raw_bbox, dict):
            return {}
        shaped: dict[str, list[float]] = {}
        for name in LAYERS:
            value = raw_bbox.get(name)
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                continue
            try:
                bounds = [float(component) for component in value]
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(component) for component in bounds):
                shaped[name] = bounds
        return shaped

    def _shaped_input_files(self, raw_summary: object) -> int | None:
        if not isinstance(raw_summary, dict):
            return None
        value = raw_summary.get("input_files")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None

    def _shaped_parse_errors(self, raw_summary: object) -> dict:
        if not isinstance(raw_summary, dict):
            return {"count": 0, "entries": []}
        raw_errors = raw_summary.get("parse_errors")
        if not isinstance(raw_errors, list):
            return {"count": 0, "entries": []}
        entries = [_sanitize_parse_error(entry) for entry in raw_errors]
        return {"count": len(raw_errors), "entries": entries}

    def _formats_exclude_geojson(self, raw_summary: object) -> bool:
        """True when the current summary explicitly names its build formats
        and geojson is not among them.

        The exporter's `_replace_directory()` leaves a previous build's
        `output/geojson/` dir in place even when the current build ran with
        e.g. `--formats gpkg` (pre-existing exporter behavior); this adapter
        must not then serve that leftover directory's data against the
        current summary's counts. A missing/malformed summary, or one with
        no usable `"formats"` list, falls through to the tolerant
        directory-existence default (`False` here) -- unchanged from before
        this gate existed.
        """
        if not isinstance(raw_summary, dict):
            return False
        formats = raw_summary.get("formats")
        if not isinstance(formats, list):
            return False
        return "geojson" not in formats

    def _capabilities(self, raw_summary: object) -> dict:
        if self._formats_exclude_geojson(raw_summary):
            return {"geojson_available": False, "layers_present": []}
        geojson_dir = self.data_dir / "output" / "geojson"
        available = geojson_dir.is_dir()
        present = []
        if available:
            for name in LAYERS:
                if (geojson_dir / f"{name}.geojson").is_file():
                    present.append(name)
        return {"geojson_available": available, "layers_present": present}

    def _freshness(
        self, raw_summary: object, summary_mtime: float | None, summary_present: bool
    ) -> dict:
        mapshare_last_success_utc = self._mapshare_last_success_utc()
        if not summary_present:
            return {
                "state": "outputs_missing",
                "mapshare_last_success_utc": mapshare_last_success_utc,
            }
        if not isinstance(raw_summary, dict):
            return {
                "state": "freshness_unknown",
                "mapshare_last_success_utc": mapshare_last_success_utc,
            }
        try:
            latest_raw_mtime = self._raw_max_mtime()
        except OSError:
            return {
                "state": "freshness_unknown",
                "mapshare_last_success_utc": mapshare_last_success_utc,
            }
        if summary_mtime is None:
            state = "freshness_unknown"
        elif latest_raw_mtime is not None and latest_raw_mtime > summary_mtime:
            state = "outputs_stale"
        else:
            state = "ok"
        return {"state": state, "mapshare_last_success_utc": mapshare_last_success_utc}

    def invalidate_raw_scan_cache(self) -> None:
        """Force the next `_raw_max_mtime()` call to rescan `raw/` from disk.

        A job-start event usually populates `_raw_scan_cache` before the job
        has acquired anything; if the job then archives new raw data but
        fails its rebuild within the ~5s cache window, a completion patch
        rendered from the stale cached value would report freshness "ok"
        instead of "outputs_stale", and nothing would ever correct it (no
        later event re-renders `#summary-live`). Callers render a terminal
        job event's summary patch after calling this so that render always
        rescans (docs/spec-serve-ui.md section 7 Phase B).
        """
        self._raw_scan_cache = None

    def _raw_max_mtime(self) -> float | None:
        now = time.monotonic()
        if self._raw_scan_cache is not None:
            cached_at, cached_value = self._raw_scan_cache
            if now - cached_at < _STALE_SCAN_CACHE_SECONDS:
                return cached_value
        value = self._scan_raw_max_mtime(self.data_dir / "raw")
        self._raw_scan_cache = (now, value)
        return value

    def _scan_raw_max_mtime(self, raw_dir: Path) -> float | None:
        if not raw_dir.is_dir():
            return None
        latest: float | None = None
        for path in raw_dir.rglob("*"):
            try:
                if not path.is_file():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if latest is None or mtime > latest:
                latest = mtime
        return latest

    def _mapshare_last_success_utc(self) -> str | None:
        data = _read_bytes(self.data_dir / "mapshare-state.json")
        if data is None:
            return None
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        value = parsed.get("last_success_utc")
        return value if isinstance(value, str) else None
