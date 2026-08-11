from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .archive import archive_file
from .explore import capture_explore
from .mapshare import sync_mapshare
from .pipeline import rebuild


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="garmin-outreach",
        description="Liberate Garmin inReach/Explore history into standard GIS layers.",
    )
    root.add_argument("--data-dir", type=Path, default=Path("data"))
    root.add_argument(
        "--formats",
        default="gpkg,geojson,shp",
        help="Comma-separated output formats: gpkg, geojson, shp (default: all)",
    )
    sub = root.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Archive GPX/KML exports and rebuild GIS outputs")
    ingest.add_argument("files", nargs="+", type=Path)

    sub.add_parser("build", help="Rebuild outputs from the raw archive")

    mapshare = sub.add_parser(
        "mapshare", help="Incrementally sync the documented MapShare KML feed"
    )
    mapshare.add_argument(
        "identifier",
        nargs="?",
        default=os.environ.get("GARMIN_MAPSHARE_ID"),
        help="MapShare identifier or Raw KML Feed URL (or GARMIN_MAPSHARE_ID)",
    )
    mapshare.add_argument("--start", type=_date, help="UTC start date/time for this run")
    mapshare.add_argument("--end", type=_date, help="UTC end date/time for this run")
    mapshare.add_argument(
        "--full", action="store_true", help="Rescan history from 2010 (deduplicated)"
    )
    mapshare.add_argument("--chunk-days", type=int, default=31)
    mapshare.add_argument("--imei")
    mapshare.add_argument("--username", default=os.environ.get("GARMIN_MAPSHARE_USERNAME", ""))
    mapshare.add_argument(
        "--ask-password",
        action="store_true",
        help="Prompt for a MapShare password; otherwise use GARMIN_MAPSHARE_PASSWORD",
    )

    explore = sub.add_parser(
        "explore", help="Capture the consumer Explore export through a persistent signed-in browser"
    )
    explore.add_argument("--export-formats", default="kml", choices=("kml", "gpx", "both"))
    explore.add_argument("--profile-dir", type=Path)
    explore.add_argument("--headless", action="store_true")
    explore.add_argument("--login-timeout", type=int, default=600)

    for command in (ingest, mapshare, explore):
        command.add_argument(
            "--no-build", action="store_true", help="Archive only; do not rebuild outputs"
        )

    for command in (ingest, mapshare, explore):
        _add_cleanup_options(command)
    build = next(action for action in sub.choices.values() if action.prog.endswith(" build"))
    _add_cleanup_options(build)
    return root


def _add_cleanup_options(command):
    command.add_argument("--trip-gap-hours", type=float, default=6.0)
    command.add_argument("--max-speed-kmh", type=float, default=200.0)
    command.add_argument("--jump-km", type=float, default=10.0)


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    formats = tuple(item.strip() for item in args.formats.split(",") if item.strip())
    invalid = set(formats) - {"gpkg", "geojson", "shp"}
    if invalid:
        raise SystemExit(f"Unsupported output format(s): {', '.join(sorted(invalid))}")
    try:
        result: dict = {}
        if args.command == "ingest":
            archived = []
            for path in args.files:
                if not path.is_file():
                    raise RuntimeError(f"Input does not exist: {path}")
                if path.suffix.lower() not in {".kml", ".gpx"}:
                    raise RuntimeError(f"Input must be KML or GPX: {path}")
                destination, created = archive_file(path, args.data_dir / "raw" / "imports")
                archived.append({"path": str(destination), "created": created})
            result["archive"] = archived
        elif args.command == "mapshare":
            if not args.identifier:
                raise RuntimeError("Supply a MapShare identifier or set GARMIN_MAPSHARE_ID")
            password = (
                getpass.getpass("MapShare password: ")
                if args.ask_password
                else os.environ.get("GARMIN_MAPSHARE_PASSWORD")
            )
            result["mapshare"] = sync_mapshare(
                args.identifier,
                args.data_dir,
                start=args.start,
                end=args.end,
                full=args.full,
                chunk_days=args.chunk_days,
                username=args.username,
                password=password,
                imei=args.imei,
            )
        elif args.command == "explore":
            export_formats = (
                ("kml", "gpx") if args.export_formats == "both" else (args.export_formats,)
            )
            result["explore"] = capture_explore(
                args.data_dir,
                formats=export_formats,
                profile_dir=args.profile_dir,
                headless=args.headless,
                login_timeout_seconds=args.login_timeout,
            )
        if args.command == "build" or not getattr(args, "no_build", False):
            result["output"] = rebuild(
                args.data_dir,
                formats=formats,
                gap_hours=args.trip_gap_hours,
                max_speed_kmh=args.max_speed_kmh,
                jump_km=args.jump_km,
            )
        print(json.dumps(result, indent=2))
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


def _date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "use ISO 8601, e.g. 2024-01-01 or 2024-01-01T12:00Z"
        ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
