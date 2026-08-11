from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import datetime

from .model import Feature


def split_trips(
    features: list[Feature],
    *,
    gap_hours: float = 6.0,
    max_speed_kmh: float = 200.0,
    jump_km: float = 10.0,
) -> list[Feature]:
    """Add conservative derived trip lines without deleting original points.

    A new trip starts after a long time gap or an implausibly fast, substantial
    jump. The original track points and tracks always remain untouched.
    """

    groups: dict[str, list[Feature]] = defaultdict(list)
    passthrough: list[Feature] = []
    for feature in features:
        if feature.layer != "track_points" or not feature.properties.get("timestamp_utc"):
            passthrough.append(feature)
            continue
        key = str(
            feature.properties.get("parent_id")
            or feature.properties.get("imei")
            or feature.properties.get("device_name")
            or feature.properties.get("source_file")
            or "unknown"
        )
        groups[key].append(feature)

    annotated: list[Feature] = []
    trips: list[Feature] = []
    for group_key, points in sorted(groups.items()):
        points.sort(
            key=lambda item: (_parse_time(item.properties["timestamp_utc"]), item.stable_id())
        )
        segments: list[tuple[list[Feature], str]] = []
        current: list[Feature] = []
        reason = "first_point"
        for point in points:
            split_reason = None
            if current:
                previous = current[-1]
                elapsed_hours = (
                    _parse_time(point.properties["timestamp_utc"])
                    - _parse_time(previous.properties["timestamp_utc"])
                ).total_seconds() / 3600
                distance_km = _haversine(previous.coordinates, point.coordinates)
                speed = distance_km / elapsed_hours if elapsed_hours > 0 else math.inf
                if elapsed_hours > gap_hours:
                    split_reason = "time_gap"
                elif distance_km >= jump_km and speed > max_speed_kmh:
                    split_reason = "implausible_jump"
                elif elapsed_hours < 0:
                    split_reason = "time_reversal"
            if split_reason:
                segments.append((current, reason))
                current = []
                reason = split_reason
            current.append(point)
        if current:
            segments.append((current, reason))

        for segment_index, (segment, split_reason) in enumerate(segments):
            trip_id = _trip_id(group_key, segment)
            annotated.extend(point.with_properties(trip_id=trip_id) for point in segment)
            if len(segment) < 2:
                continue
            coords = tuple(point.coordinates for point in segment)
            distance = sum(_haversine(a, b) for a, b in zip(coords, coords[1:], strict=False))
            props = {
                "trip_id": trip_id,
                "group_key": group_key,
                "sequence": segment_index,
                "split_reason": split_reason,
                "start_time_utc": segment[0].properties["timestamp_utc"],
                "end_time_utc": segment[-1].properties["timestamp_utc"],
                "point_count": len(segment),
                "distance_km": round(distance, 6),
                "source": "garmin-outreach",
                "source_kind": "derived_trip",
            }
            trips.append(Feature("trips", "LineString", coords, props, trip_id))
    return passthrough + annotated + trips


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _trip_id(group_key: str, segment: list[Feature]) -> str:
    content = "|".join(
        (group_key, segment[0].stable_id(), segment[-1].stable_id(), str(len(segment)))
    )
    return "trip:" + hashlib.sha256(content.encode()).hexdigest()[:20]


def _haversine(a, b) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(h))
