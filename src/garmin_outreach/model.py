from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

GeometryType = Literal["Point", "LineString"]
Coordinate = tuple[float, float]


@dataclass(slots=True)
class Feature:
    """A small, dependency-free intermediate GIS feature."""

    layer: str
    geometry_type: GeometryType
    coordinates: Coordinate | tuple[Coordinate, ...]
    properties: dict[str, Any] = field(default_factory=dict)
    feature_id: str | None = None

    def stable_id(self) -> str:
        if self.feature_id:
            return self.feature_id
        payload = {
            "layer": self.layer,
            "geometry_type": self.geometry_type,
            "coordinates": self.coordinates,
            "properties": self.properties,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()[:24]

    def with_properties(self, **properties: Any) -> Feature:
        values = dict(self.properties)
        values.update(properties)
        return replace(self, properties=values)


def deduplicate(features: list[Feature]) -> list[Feature]:
    """Deduplicate deterministically, preferring the richer duplicate."""

    selected: dict[tuple[str, str], Feature] = {}
    for feature in features:
        key = (feature.layer, feature.stable_id())
        old = selected.get(key)
        if old is None or _richness(feature) > _richness(old):
            selected[key] = feature
    return sorted(selected.values(), key=lambda item: (item.layer, item.stable_id()))


def _richness(feature: Feature) -> tuple[int, int]:
    populated = sum(value not in (None, "", [], {}) for value in feature.properties.values())
    return populated, len(json.dumps(feature.properties, default=str))
