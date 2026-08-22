import argparse
import contextlib
import json
import sys
from pathlib import Path

import pytest

import garmin_outreach.cli as cli
import garmin_outreach.services as services


def _fake_rebuild(monkeypatch, calls, *, module=cli):
    def rebuild(data_dir, *, formats, gap_hours, max_speed_kmh, jump_km):
        calls.append(data_dir)
        return {"rebuilt": True}

    monkeypatch.setattr(module, "rebuild", rebuild)


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
    _fake_rebuild(monkeypatch, calls, module=services)
    monkeypatch.setattr(services, "sync_mapshare", lambda *args, **kwargs: {"new_features": 0})

    cli.main(["--data-dir", str(tmp_path), "mapshare", "sample", "--no-build"])

    assert calls == []


def test_explore_no_build_does_not_rebuild(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls, module=services)
    monkeypatch.setattr(services, "browserless_export", lambda *args, **kwargs: {"exported": []})

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
    _fake_rebuild(monkeypatch, calls, module=services)
    monkeypatch.setattr(services, "sync_mapshare", lambda *args, **kwargs: {"new_features": 0})

    cli.main(["--data-dir", str(tmp_path), "mapshare", "sample"])

    assert calls == [tmp_path]


def test_explore_without_no_build_rebuilds(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls, module=services)
    monkeypatch.setattr(services, "browserless_export", lambda *args, **kwargs: {"exported": []})

    cli.main(["--data-dir", str(tmp_path), "explore"])

    assert calls == [tmp_path]


def test_explore_browser_transport_uses_inline_lock_and_rebuild(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls)
    monkeypatch.setattr(cli, "capture_explore", lambda *args, **kwargs: {"exported": []})

    cli.main(["--data-dir", str(tmp_path), "explore", "--transport", "browser"])

    assert calls == [tmp_path]


def test_build_always_rebuilds(tmp_path, monkeypatch):
    calls = []
    _fake_rebuild(monkeypatch, calls, module=services)

    cli.main(["--data-dir", str(tmp_path), "build"])

    assert calls == [tmp_path]


def test_unknown_command_never_reaches_rebuild(monkeypatch, capsys):
    """Regression for the dispatch fall-through: an unrecognized
    args.command with no no_build/cleanup attributes must not call
    rebuild(). Uses a synthetic command name (not a real subcommand) so this
    stays independent of any actual command's dispatch branch, including
    the now-real `serve`."""
    calls = []
    _fake_rebuild(monkeypatch, calls)
    _fake_rebuild(monkeypatch, calls, module=services)

    def fake_parser():
        root = argparse.ArgumentParser(prog="garmin-outreach")
        root.add_argument("--data-dir", type=Path, default=Path("data"))
        root.add_argument("--formats", default="gpkg,geojson,shp")
        sub = root.add_subparsers(dest="command", required=True)
        sub.add_parser("frobnicate")
        return root

    monkeypatch.setattr(cli, "parser", fake_parser)

    cli.main(["frobnicate"])

    assert calls == []
    captured = capsys.readouterr()
    # Pins today's placeholder behavior (an unrecognized command prints an
    # empty JSON object and exits 0).
    assert json.loads(captured.out) == {}


def test_serve_help_discloses_optional_data_jobs():
    help_text = cli.parser().format_help()

    assert "serve" in help_text
    assert "optional data jobs" in help_text
    assert "read-only web UI" not in help_text


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


def test_mapshare_stdout_contract_matches_service_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        services, "sync_mapshare", lambda *args, **kwargs: {"new_features": 3, "requests": 1}
    )
    monkeypatch.setattr(services, "rebuild", lambda *args, **kwargs: {"rebuilt": True})

    cli.main(["--data-dir", str(tmp_path), "mapshare", "sample"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "mapshare": {"new_features": 3, "requests": 1},
        "output": {"rebuilt": True},
    }


def test_mapshare_build_failure_after_acquisition_exits_2_with_cause_message(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        services, "sync_mapshare", lambda *args, **kwargs: {"new_features": 3, "requests": 1}
    )

    def failing_rebuild(*args, **kwargs):
        raise RuntimeError("disk is full")

    monkeypatch.setattr(services, "rebuild", failing_rebuild)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--data-dir", str(tmp_path), "mapshare", "sample"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: disk is full"


def test_mapshare_build_failure_with_programming_error_cause_reraises(tmp_path, monkeypatch):
    """A `BuildFailedAfterAcquisition` whose `.cause` is not a
    RuntimeError/ValueError is a programming error, not an expected user
    failure -- it must surface as a real traceback, not a clean `error: ...`
    exit 2 that would hide the bug."""
    monkeypatch.setattr(
        services, "sync_mapshare", lambda *args, **kwargs: {"new_features": 3, "requests": 1}
    )

    def failing_rebuild(*args, **kwargs):
        raise TypeError("unexpected argument shape")

    monkeypatch.setattr(services, "rebuild", failing_rebuild)

    with pytest.raises(TypeError, match="unexpected argument shape"):
        cli.main(["--data-dir", str(tmp_path), "mapshare", "sample"])


def test_mapshare_build_failure_with_runtime_error_cause_still_exits_2(
    tmp_path, monkeypatch, capsys
):
    """A RuntimeError cause is the expected/handled shape and keeps the
    clean one-line CLI failure behavior (regression guard alongside the
    TypeError case above)."""
    monkeypatch.setattr(
        services, "sync_mapshare", lambda *args, **kwargs: {"new_features": 3, "requests": 1}
    )

    def failing_rebuild(*args, **kwargs):
        raise RuntimeError("disk is full")

    monkeypatch.setattr(services, "rebuild", failing_rebuild)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--data-dir", str(tmp_path), "mapshare", "sample"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: disk is full"


def test_mapshare_missing_identifier_with_ask_password_does_not_prompt(tmp_path, monkeypatch):
    """The identifier pre-check must run before getpass.getpass() -- a
    missing identifier must fail immediately rather than blocking on a
    terminal prompt for a request that was always going to fail."""

    def _fail_if_called(*args, **kwargs):
        pytest.fail("getpass.getpass() must not be called when the identifier is missing")

    monkeypatch.setattr(cli.getpass, "getpass", _fail_if_called)
    monkeypatch.delenv("GARMIN_MAPSHARE_ID", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--data-dir", str(tmp_path), "mapshare", "--ask-password"])

    assert excinfo.value.code == 2


def test_serve_dispatches_read_only_with_clean_stdout(tmp_path, monkeypatch, capsys):
    pytest.importorskip("starlette")
    calls = []
    _fake_rebuild(monkeypatch, calls)

    run_calls = []

    def fake_run(data_dir, *, host, port, open_browser):
        run_calls.append((data_dir, host, port, open_browser))

    import garmin_outreach.serve.app as serve_app_module

    monkeypatch.setattr(serve_app_module, "run", fake_run)

    @contextlib.contextmanager
    def fail_writer_lock(*args, **kwargs):
        pytest.fail("writer_lock should not be entered for serve")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(cli, "writer_lock", fail_writer_lock)

    cli.main(
        ["--data-dir", str(tmp_path), "serve", "--host", "127.0.0.1", "--port", "9999", "--no-open"]
    )

    assert run_calls == [(tmp_path, "127.0.0.1", 9999, False)]
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == ""


def test_serve_missing_ui_extra_raises_actionable_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "garmin_outreach.serve.app", None)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--data-dir", str(tmp_path), "serve"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "uv sync --extra ui" in captured.err
    assert "pip install -e .[ui]" in captured.err
