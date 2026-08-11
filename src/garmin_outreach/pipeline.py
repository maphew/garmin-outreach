from __future__ import annotations

import json
import os
from pathlib import Path

from .cleanup import split_trips
from .exporters import write_outputs
from .model import deduplicate
from .parsers import parse_file


def rebuild(
    data_dir: Path,
    *,
    formats: tuple[str, ...] = ("gpkg", "geojson", "shp"),
    gap_hours: float = 6.0,
    max_speed_kmh: float = 200.0,
    jump_km: float = 10.0,
) -> dict:
    raw_dir = data_dir / "raw"
    inputs = sorted(
        path
        for path in raw_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".kml", ".gpx"}
    )
    if not inputs:
        raise RuntimeError(f"No KML or GPX files found beneath {raw_dir}")
    features = []
    errors = []
    for path in inputs:
        try:
            features.extend(parse_file(path))
        except Exception as error:  # keep one corrupt batch from hiding usable history
            errors.append(f"{path}: {error}")
    if not features:
        details = "\n".join(errors)
        raise RuntimeError(f"No GIS features could be parsed.\n{details}")
    features = deduplicate(features)
    features = deduplicate(
        split_trips(
            features,
            gap_hours=gap_hours,
            max_speed_kmh=max_speed_kmh,
            jump_km=jump_km,
        )
    )
    summary = write_outputs(features, data_dir / "output", formats=formats)
    summary["input_files"] = len(inputs)
    summary["parse_errors"] = errors
    summary_path = data_dir / "output" / "summary.json"
    temporary = summary_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, summary_path)
    return summary
