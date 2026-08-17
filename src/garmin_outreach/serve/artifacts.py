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

_RETRY_DELAY_SECONDS = 0.05
_STALE_SCAN_CACHE_SECONDS = 5.0
_PARSE_ERROR_SEPARATOR = ": "


def _read_bytes(path: Path) -> bytes | None:
    for attempt in range(2):
        try:
            return path.read_bytes()
        except (FileNotFoundError, PermissionError):
            if attempt == 0:
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
            if attempt == 0:
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            return None
        except OSError:
            return None
    return None


def _basename(path_text: str) -> str:
    tail = re.split(r"[\\/]", path_text.strip())[-1]
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
        raw_summary, summary_mtime = self._read_summary(summary_path)
        return {
            "layers": self._shaped_layers(raw_summary),
            "bbox": self._shaped_bbox(raw_summary),
            "input_files": self._shaped_input_files(raw_summary),
            "parse_errors": self._shaped_parse_errors(raw_summary),
            "capabilities": self._capabilities(),
            "freshness": self._freshness(raw_summary, summary_mtime),
        }

    def _read_summary(self, path: Path) -> tuple[object | None, float | None]:
        data = _read_bytes(path)
        if data is None:
            return None, None
        mtime = _stat_mtime(path)
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, mtime
        return parsed, mtime

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

    def _capabilities(self) -> dict:
        geojson_dir = self.data_dir / "output" / "geojson"
        available = geojson_dir.is_dir()
        present = []
        if available:
            for name in LAYERS:
                if (geojson_dir / f"{name}.geojson").is_file():
                    present.append(name)
        return {"geojson_available": available, "layers_present": present}

    def _freshness(self, raw_summary: object, summary_mtime: float | None) -> dict:
        mapshare_last_success_utc = self._mapshare_last_success_utc()
        if raw_summary is None:
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

    def layer_geojson(self, name: str) -> bytes | None:
        if name not in LAYERS or "/" in name or "\\" in name:
            return None
        geojson_dir = self.data_dir / "output" / "geojson"
        try:
            resolved_dir = geojson_dir.resolve()
            candidate = (geojson_dir / f"{name}.geojson").resolve()
        except OSError:
            return None
        if not candidate.is_relative_to(resolved_dir):
            return None
        return _read_bytes(candidate)
