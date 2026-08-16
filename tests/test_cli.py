import argparse
import json
from pathlib import Path

import pytest

import garmin_outreach.cli as cli


def _fake_rebuild(monkeypatch, calls):
    def rebuild(data_dir, *, formats, gap_hours, max_speed_kmh, jump_km):
        calls.append(data_dir)
        return {"rebuilt": True}

    monkeypatch.setattr(cli, "rebuild", rebuild)


def test_ingest_no_build_does_not_rebuild(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)
    monkeypatch.setattr(cli, "archive_file", lambda path, directory: (directory / path.name, True))
    gpx = tmp_path / "track.gpx"
    gpx.write_text("<gpx/>")

    cli.main(["--data-dir", str(tmp_path), "ingest", str(gpx), "--no-build"])

    assert calls == []


def test_mapshare_no_build_does_not_rebuild(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)
    monkeypatch.setattr(cli, "sync_mapshare", lambda *args, **kwargs: {"new_features": 0})

    cli.main(["--data-dir", str(tmp_path), "mapshare", "sample", "--no-build"])

    assert calls == []


def test_explore_no_build_does_not_rebuild(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)
    monkeypatch.setattr(cli, "browserless_export", lambda *args, **kwargs: {"exported": []})

    cli.main(["--data-dir", str(tmp_path), "explore", "--no-build"])

    assert calls == []


def test_ingest_without_no_build_rebuilds(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)
    monkeypatch.setattr(cli, "archive_file", lambda path, directory: (directory / path.name, True))
    gpx = tmp_path / "track.gpx"
    gpx.write_text("<gpx/>")

    cli.main(["--data-dir", str(tmp_path), "ingest", str(gpx)])

    assert calls == [tmp_path]


def test_mapshare_without_no_build_rebuilds(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)
    monkeypatch.setattr(cli, "sync_mapshare", lambda *args, **kwargs: {"new_features": 0})

    cli.main(["--data-dir", str(tmp_path), "mapshare", "sample"])

    assert calls == [tmp_path]


def test_explore_without_no_build_rebuilds(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)
    monkeypatch.setattr(cli, "browserless_export", lambda *args, **kwargs: {"exported": []})

    cli.main(["--data-dir", str(tmp_path), "explore"])

    assert calls == [tmp_path]


def test_build_always_rebuilds(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)

    cli.main(["--data-dir", str(tmp_path), "build"])

    assert calls == [tmp_path]


def test_unknown_command_never_reaches_rebuild(monkeypatch, capsys):
    """Regression for the fall-through that would crash a future `serve`
    command: an unrecognized args.command with no no_build/cleanup
    attributes must not call rebuild()."""
    calls = []
    _fake_rebuild(monkeypatch, calls)

    def fake_parser():
        root = argparse.ArgumentParser(prog="garmin-outreach")
        root.add_argument("--data-dir", type=Path, default=Path("data"))
        root.add_argument("--formats", default="gpkg,geojson,shp")
        sub = root.add_subparsers(dest="command", required=True)
        sub.add_parser("serve")
        return root

    monkeypatch.setattr(cli, "parser", fake_parser)

    cli.main(["serve"])

    assert calls == []
    captured = capsys.readouterr()
    # Pins today's placeholder behavior (an unrecognized command prints an
    # empty JSON object and exits 0); this will change once a real `serve`
    # command lands, since the spec says `serve` keeps stdout clean.
    assert json.loads(captured.out) == {}


def test_invalid_formats_exits(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--data-dir", str(tmp_path), "--formats", "bogus", "build"])


def test_ingest_no_build_stdout_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "rebuild", lambda *args, **kwargs: pytest.fail("rebuild called"))
    destination = tmp_path / "raw" / "imports" / "track.gpx"
    monkeypatch.setattr(cli, "archive_file", lambda path, directory: (destination, True))
    gpx = tmp_path / "track.gpx"
    gpx.write_text("<gpx/>")

    cli.main(["--data-dir", str(tmp_path), "ingest", str(gpx), "--no-build"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"archive": [{"path": str(destination), "created": True}]}
