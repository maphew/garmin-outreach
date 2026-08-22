from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from .model import Feature

SHAPEFILE_ALIASES = {
    "source_file": "src_file",
    "source_kind": "src_kind",
    "timestamp_utc": "time_utc",
    "device_name": "device",
    "map_display_name": "map_name",
    "device_type": "dev_type",
    "incident_id": "inc_id",
    "elevation_m": "elev_m",
    "velocity_kmh": "speed_kmh",
    "course_deg": "course_deg",
    "valid_gps_fix": "gps_fix",
    "in_emergency": "emergency",
    "description": "descr",
    "point_count": "n_points",
    "start_time_utc": "start_utc",
    "end_time_utc": "end_utc",
    "split_reason": "splitwhy",
    "distance_km": "dist_km",
    "feature_id": "feature_id",
}


def write_outputs(
    features: list[Feature],
    output_dir: Path,
    *,
    formats: tuple[str, ...] = ("gpkg", "geojson", "shp"),
    basename: str = "garmin-outreach",
) -> dict:
    try:
        import geopandas as gpd
        from shapely.geometry import LineString, Point
    except ImportError as error:
        raise RuntimeError(
            "GIS dependencies are missing. Install the project with: pip install -e ."
        ) from error

    output_dir.mkdir(parents=True, exist_ok=True)
    layers: dict[str, list[Feature]] = defaultdict(list)
    for feature in features:
        layers[feature.layer].append(feature)

    frames = {}
    for layer, layer_features in sorted(layers.items()):
        rows = []
        geometries = []
        for feature in layer_features:
            row = {"feature_id": feature.stable_id(), **_serializable(feature.properties)}
            rows.append(row)
            geometries.append(
                Point(feature.coordinates)
                if feature.geometry_type == "Point"
                else LineString(feature.coordinates)
            )
        frames[layer] = gpd.GeoDataFrame(rows, geometry=geometries, crs="EPSG:4326")

    written: dict[str, list[str] | str] = {}
    if "gpkg" in formats and frames:
        destination = output_dir / f"{basename}.gpkg"
        fd, temp_name = tempfile.mkstemp(suffix=".gpkg", dir=output_dir)
        os.close(fd)
        Path(temp_name).unlink()
        try:
            first = True
            for layer, frame in frames.items():
                frame.to_file(
                    temp_name,
                    layer=layer,
                    driver="GPKG",
                    engine="pyogrio",
                    mode="w" if first else "a",
                )
                first = False
            os.replace(temp_name, destination)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        written["gpkg"] = str(destination)

    if "geojson" in formats:
        target = output_dir / "geojson"
        _replace_directory(target, lambda temp: _write_geojson(frames, temp))
        written["geojson"] = [str(target / f"{layer}.geojson") for layer in frames]

    if "shp" in formats:
        target = output_dir / "shapefile"
        _replace_directory(target, lambda temp: _write_shapefiles(frames, temp))
        written["shp"] = [str(target / f"{layer}.shp") for layer in frames]

    # Non-finite coordinates (e.g. a stray "nan"/"inf" token in an input
    # file) would otherwise leak a literal NaN/Infinity into summary.json,
    # which json.dumps writes by default but browsers cannot parse. Omit
    # the layer's bbox entirely rather than publish a bad bounding box.
    bbox = {}
    for layer, frame in frames.items():
        bounds = [float(value) for value in frame.total_bounds]
        if all(math.isfinite(value) for value in bounds):
            bbox[layer] = bounds

    summary = {
        "feature_count": len(features),
        "layers": dict(sorted(Counter(feature.layer for feature in features).items())),
        "formats": list(formats),
        "written": written,
        "bbox": bbox,
    }
    _atomic_text(output_dir / "summary.json", json.dumps(summary, indent=2, allow_nan=False) + "\n")
    return summary


def _write_geojson(frames, target: Path):
    for layer, frame in frames.items():
        frame.to_file(target / f"{layer}.geojson", driver="GeoJSON", engine="pyogrio")


def _write_shapefiles(frames, target: Path):
    field_map: dict[str, dict[str, str]] = {}
    for layer, frame in frames.items():
        renamed = _shapefile_names([column for column in frame.columns if column != "geometry"])
        field_map[layer] = renamed
        output = frame.rename(columns=renamed)
        output.to_file(
            target / f"{layer}.shp",
            driver="ESRI Shapefile",
            engine="pyogrio",
            encoding="UTF-8",
        )
    _atomic_text(target / "fields.json", json.dumps(field_map, indent=2, sort_keys=True) + "\n")


def _replace_directory(destination: Path, writer):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        writer(temp)
        old = destination.with_name(f".{destination.name}-old")
        if old.exists():
            shutil.rmtree(old)
        if destination.exists():
            destination.rename(old)
        temp.rename(destination)
        if old.exists():
            shutil.rmtree(old)
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def _shapefile_names(names: list[str]) -> dict[str, str]:
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for name in names:
        preferred = SHAPEFILE_ALIASES.get(name, name)
        candidate = preferred[:10]
        counter = 1
        while candidate.lower() in used:
            suffix = str(counter)
            candidate = preferred[: 10 - len(suffix)] + suffix
            counter += 1
        used.add(candidate.lower())
        mapping[name] = candidate
    return mapping


def _serializable(properties: dict) -> dict:
    values = {}
    for key, value in properties.items():
        if isinstance(value, (dict, list, tuple)):
            values[key] = json.dumps(value, sort_keys=True, ensure_ascii=False)
        else:
            values[key] = value
    return values


def _atomic_text(path: Path, content: str):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
