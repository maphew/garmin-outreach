from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from defusedxml import ElementTree as ET

from .model import Coordinate, Feature

MESSAGE_EVENTS = (
    "text message",
    "quick message",
    "quick text",
    "msg to shared map",
    "test message",
)
TRACK_EVENTS = ("tracking message",)
WAYPOINT_EVENTS = ("waypoint/navigation", "reference point")


def parse_file(path: Path) -> list[Feature]:
    suffix = path.suffix.lower()
    if suffix in {".kml", ".xml"}:
        return parse_kml(path.read_bytes(), source_file=str(path))
    if suffix == ".gpx":
        return parse_gpx(path.read_bytes(), source_file=str(path))
    raise ValueError(f"Unsupported input format: {path}")


def parse_kml(data: bytes, *, source_file: str = "<memory>") -> list[Feature]:
    root = ET.fromstring(data)
    if _local(root.tag).lower() != "kml":
        raise ValueError(f"Not a KML document: {source_file}")
    features: list[Feature] = []
    for child in root:
        if _local(child.tag) in {"Document", "Folder"}:
            _walk_kml_container(child, (), features, source_file)
        elif _local(child.tag) == "Placemark":
            features.extend(_parse_placemark(child, (), source_file))
    return features


def _walk_kml_container(node, parents: tuple[str, ...], output: list[Feature], source: str):
    node_name = _child_text(node, "name")
    is_folder = _local(node.tag) == "Folder"
    path = parents + ((node_name,) if is_folder and node_name else ())
    for child in node:
        name = _local(child.tag)
        if name in {"Document", "Folder"}:
            _walk_kml_container(child, path, output, source)
        elif name == "Placemark":
            output.extend(_parse_placemark(child, path, source))


def _parse_placemark(node, folders: tuple[str, ...], source: str) -> list[Feature]:
    name = _child_text(node, "name")
    description = _child_text(node, "description")
    timestamp = None
    for element in node.iter():
        if _local(element.tag) == "when" and element.text:
            timestamp = _iso_utc(element.text)
            break
    extended = _extended_data(node)
    props = _normalise_garmin_properties(extended)
    props.update(
        {
            "name": name or props.get("name"),
            "description": description,
            "timestamp_utc": timestamp or props.get("timestamp_utc"),
            "folder": "/".join(folders) or None,
            "source": "garmin",
            "source_file": source,
            "source_kind": "mapshare_event" if extended.get("Event") else "kml",
        }
    )
    props = {key: value for key, value in props.items() if value not in (None, "")}
    garmin_id = props.get("garmin_id")
    output: list[Feature] = []
    geometry_number = 0
    for geometry in node.iter():
        kind = _local(geometry.tag)
        if kind == "Point":
            coords = _kml_coordinates(_child_text(geometry, "coordinates"))
            if coords:
                layer = _point_layer(props.get("event"), folders)
                output.append(
                    Feature(
                        layer,
                        "Point",
                        coords[0],
                        dict(props),
                        _source_id(layer, garmin_id, source, name, coords[0], geometry_number),
                    )
                )
                geometry_number += 1
        elif kind == "LineString":
            coords = _kml_coordinates(_child_text(geometry, "coordinates"))
            if len(coords) >= 2:
                layer = _line_layer(folders, name)
                line_props = dict(props)
                line_props["point_count"] = len(coords)
                output.append(
                    Feature(
                        layer,
                        "LineString",
                        tuple(coords),
                        line_props,
                        _source_id(layer, garmin_id, source, name, coords, geometry_number),
                    )
                )
                geometry_number += 1
        elif kind == "Track":  # gx:Track
            coords = []
            whens: list[str] = []
            for child in geometry:
                child_name = _local(child.tag)
                if child_name == "coord" and child.text:
                    parts = child.text.split()
                    if len(parts) >= 2:
                        coords.append((float(parts[0]), float(parts[1])))
                elif child_name == "when" and child.text:
                    whens.append(_iso_utc(child.text) or child.text)
            if len(coords) >= 2:
                layer = _line_layer(folders, name)
                line_props = dict(props)
                line_props.update(
                    {
                        "point_count": len(coords),
                        "start_time_utc": whens[0] if whens else None,
                        "end_time_utc": whens[-1] if whens else None,
                    }
                )
                output.append(
                    Feature(
                        layer,
                        "LineString",
                        tuple(coords),
                        {k: v for k, v in line_props.items() if v is not None},
                        _source_id(layer, garmin_id, source, name, coords, geometry_number),
                    )
                )
                parent_id = output[-1].stable_id()
                for index, coordinate in enumerate(coords):
                    point_props = dict(props)
                    point_props.update(
                        {
                            "timestamp_utc": whens[index] if index < len(whens) else None,
                            "parent_id": parent_id,
                            "sequence": index,
                        }
                    )
                    output.append(
                        Feature(
                            "track_points",
                            "Point",
                            coordinate,
                            {k: v for k, v in point_props.items() if v is not None},
                            f"{parent_id}:point:{index}",
                        )
                    )
                geometry_number += 1
    return output


def parse_gpx(data: bytes, *, source_file: str = "<memory>") -> list[Feature]:
    root = ET.fromstring(data)
    if _local(root.tag).lower() != "gpx":
        raise ValueError(f"Not a GPX document: {source_file}")
    output: list[Feature] = []
    for wpt in _direct_children(root, "wpt"):
        coordinate = _gpx_coordinate(wpt)
        props = _gpx_point_properties(wpt, source_file)
        layer = _gpx_point_layer(props)
        output.append(Feature(layer, "Point", coordinate, props))

    for route_index, route in enumerate(_direct_children(root, "rte")):
        points = _direct_children(route, "rtept")
        coords = [_gpx_coordinate(point) for point in points]
        if len(coords) < 2:
            continue
        name = _child_text(route, "name") or f"Route {route_index + 1}"
        props = _gpx_container_properties(route, source_file)
        props.update({"name": name, "point_count": len(coords), "source_kind": "gpx_route"})
        output.append(Feature("routes", "LineString", tuple(coords), props))

    for track_index, track in enumerate(_direct_children(root, "trk")):
        track_name = _child_text(track, "name") or f"Track {track_index + 1}"
        segments = _direct_children(track, "trkseg")
        for segment_index, segment in enumerate(segments):
            points = _direct_children(segment, "trkpt")
            coords = [_gpx_coordinate(point) for point in points]
            if not coords:
                continue
            line_props = _gpx_container_properties(track, source_file)
            line_props.update(
                {
                    "name": track_name,
                    "segment": segment_index,
                    "point_count": len(coords),
                    "source_kind": "gpx_track",
                }
            )
            if len(coords) >= 2:
                line = Feature("tracks", "LineString", tuple(coords), line_props)
                output.append(line)
                parent_id = line.stable_id()
            else:
                parent_id = f"gpx:{source_file}:{track_index}:{segment_index}"
            for point_index, (point, coordinate) in enumerate(zip(points, coords, strict=True)):
                props = _gpx_point_properties(point, source_file)
                props.update(
                    {
                        "name": track_name,
                        "parent_id": parent_id,
                        "segment": segment_index,
                        "sequence": point_index,
                        "source_kind": "gpx_trackpoint",
                    }
                )
                output.append(
                    Feature(
                        "track_points",
                        "Point",
                        coordinate,
                        props,
                        f"{parent_id}:point:{point_index}",
                    )
                )
    return output


def _normalise_garmin_properties(values: dict[str, str]) -> dict:
    known = {
        "Id": "garmin_id",
        "ID": "garmin_id",
        "Name": "device_name",
        "Map Display Name": "map_display_name",
        "Device Type": "device_type",
        "IMEI": "imei",
        "Incident Id": "incident_id",
        "Incident ID": "incident_id",
        "Valid GPS Fix": "valid_gps_fix",
        "In Emergency": "in_emergency",
        "Text": "text",
        "Event": "event",
    }
    props: dict = {}
    unknown: dict[str, str] = {}
    for key, value in values.items():
        if key in known:
            props[known[key]] = _bool(value) if key in {"Valid GPS Fix", "In Emergency"} else value
        elif key == "Time UTC":
            props["timestamp_utc"] = _us_utc(value)
        elif key == "Latitude":
            props["latitude"] = _number(value)
        elif key == "Longitude":
            props["longitude"] = _number(value)
        elif key == "Elevation":
            props["elevation_m"] = _number(value)
        elif key == "Velocity":
            props["velocity_kmh"] = _number(value)
        elif key == "Course":
            props["course_deg"] = _number(value)
        elif key != "Time":
            unknown[key] = value
    if unknown:
        props["extra_json"] = json.dumps(unknown, sort_keys=True, ensure_ascii=False)
    return props


def _extended_data(node) -> dict[str, str]:
    output: dict[str, str] = {}
    for element in node.iter():
        local = _local(element.tag)
        if local == "Data" and element.get("name"):
            value = next((child.text for child in element if _local(child.tag) == "value"), None)
            output[element.get("name")] = value or ""
        elif local == "SimpleData" and element.get("name"):
            output[element.get("name")] = element.text or ""
    return output


def _point_layer(event: str | None, folders: Iterable[str]) -> str:
    event_lower = (event or "").lower().rstrip(".")
    folder_lower = "/".join(folders).lower()
    if any(text in event_lower for text in TRACK_EVENTS) or "track point" in folder_lower:
        return "track_points"
    if any(text in event_lower for text in MESSAGE_EVENTS) or "message" in folder_lower:
        return "messages"
    if any(text in event_lower for text in WAYPOINT_EVENTS) or "waypoint" in folder_lower:
        return "waypoints"
    if event:
        return "events"
    return "waypoints"


def _line_layer(folders: Iterable[str], name: str | None) -> str:
    context = ("/".join(folders) + "/" + (name or "")).lower()
    if "course" in context:
        return "courses"
    if "route" in context:
        return "routes"
    return "tracks"


def _gpx_point_layer(properties: dict) -> str:
    context = " ".join(str(properties.get(key, "")) for key in ("type", "symbol", "name")).lower()
    if "message" in context:
        return "messages"
    if "track" in context and "point" in context:
        return "track_points"
    return "waypoints"


def _gpx_coordinate(node) -> Coordinate:
    return float(node.attrib["lon"]), float(node.attrib["lat"])


def _gpx_point_properties(node, source: str) -> dict:
    props = _gpx_container_properties(node, source)
    props["timestamp_utc"] = _iso_utc(_child_text(node, "time"))
    elevation = _child_text(node, "ele")
    props["elevation_m"] = _number(elevation) if elevation else None
    return {key: value for key, value in props.items() if value not in (None, "")}


def _gpx_container_properties(node, source: str) -> dict:
    props = {
        "name": _child_text(node, "name"),
        "description": _child_text(node, "desc"),
        "comment": _child_text(node, "cmt"),
        "symbol": _child_text(node, "sym"),
        "type": _child_text(node, "type"),
        "source": "garmin",
        "source_file": source,
        "source_kind": "gpx",
    }
    return {key: value for key, value in props.items() if value not in (None, "")}


def _kml_coordinates(value: str | None) -> list[Coordinate]:
    if not value:
        return []
    output: list[Coordinate] = []
    for token in value.replace("\n", " ").split():
        parts = token.split(",")
        if len(parts) >= 2:
            output.append((float(parts[0]), float(parts[1])))
    return output


def _source_id(layer, garmin_id, source, name, coords, index) -> str | None:
    if garmin_id:
        return f"garmin:{garmin_id}:{index}"
    return None


def _child_text(node, local_name: str) -> str | None:
    for child in node:
        if _local(child.tag) == local_name:
            return child.text.strip() if child.text else None
    return None


def _direct_children(node, local_name: str):
    return [child for child in node if _local(child.tag) == local_name]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group()) if match else None


def _bool(value: str):
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return None


def _iso_utc(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return value.strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _us_utc(value: str) -> str:
    for pattern in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(value.strip(), pattern).replace(tzinfo=UTC)
            return parsed.isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return value
