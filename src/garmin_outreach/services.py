"""Shared job orchestration used by both the CLI and the (future) UI job runner.

Each `run_*` function is the single outermost mutating boundary for its job:
it acquires the interprocess `writer_lock` itself, so callers (CLI dispatch,
UI job runner) must never also hold it — `writer_lock` is not reentrant.

Acquisition (mapshare sync / explore export) writes raw archives to disk
before the optional rebuild runs. If the rebuild step then fails, the
archived raw data is still on disk and must not be lost from the caller's
view: `BuildFailedAfterAcquisition` carries both the acquisition result and
the rebuild failure so callers can report "acquired, but rebuild failed"
(the UI's `partial_success` state) rather than a bare exception.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .explore_http import browserless_export
from .locking import writer_lock
from .mapshare import sync_mapshare
from .pipeline import rebuild


class BuildFailedAfterAcquisition(RuntimeError):
    """Acquisition succeeded and its raw data is archived, but the rebuild failed."""

    def __init__(self, acquisition_key: str, acquisition_result: dict, cause: Exception):
        super().__init__(str(cause))
        self.acquisition_key = acquisition_key
        self.acquisition_result = acquisition_result
        self.cause = cause


def run_build(
    data_dir: Path,
    *,
    formats: tuple[str, ...],
    gap_hours: float,
    max_speed_kmh: float,
    jump_km: float,
) -> dict:
    with writer_lock(data_dir, label="build"):
        output = rebuild(
            data_dir,
            formats=formats,
            gap_hours=gap_hours,
            max_speed_kmh=max_speed_kmh,
            jump_km=jump_km,
        )
    return {"output": output}


def run_mapshare(
    identifier_or_url: str,
    data_dir: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    full: bool = False,
    chunk_days: int = 31,
    username: str = "",
    password: str | None = None,
    imei: str | None = None,
    no_build: bool = False,
    formats: tuple[str, ...],
    gap_hours: float,
    max_speed_kmh: float,
    jump_km: float,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    if not identifier_or_url:
        raise RuntimeError("Supply a MapShare identifier or set GARMIN_MAPSHARE_ID")
    result: dict = {}
    with writer_lock(data_dir, label="mapshare"):
        mapshare_result = sync_mapshare(
            identifier_or_url,
            data_dir,
            start=start,
            end=end,
            full=full,
            chunk_days=chunk_days,
            username=username,
            password=password,
            imei=imei,
            progress=progress,
        )
        result["mapshare"] = mapshare_result
        if not no_build:
            try:
                result["output"] = rebuild(
                    data_dir,
                    formats=formats,
                    gap_hours=gap_hours,
                    max_speed_kmh=max_speed_kmh,
                    jump_km=jump_km,
                )
            except Exception as error:
                raise BuildFailedAfterAcquisition("mapshare", mapshare_result, error) from error
    return result


def run_explore_http(
    data_dir: Path,
    *,
    export_formats: tuple[str, ...],
    browser: str | None = None,
    no_build: bool = False,
    formats: tuple[str, ...],
    gap_hours: float,
    max_speed_kmh: float,
    jump_km: float,
) -> dict:
    result: dict = {}
    with writer_lock(data_dir, label="explore"):
        explore_result = browserless_export(
            data_dir,
            formats=export_formats,
            browser=browser,
        )
        result["explore"] = explore_result
        if not no_build:
            try:
                result["output"] = rebuild(
                    data_dir,
                    formats=formats,
                    gap_hours=gap_hours,
                    max_speed_kmh=max_speed_kmh,
                    jump_km=jump_km,
                )
            except Exception as error:
                raise BuildFailedAfterAcquisition("explore", explore_result, error) from error
    return result
