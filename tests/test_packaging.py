# NOTE: this module builds a real wheel and installs it into a scratch venv.
# It exercises the actual packaging pipeline (as opposed to an editable
# checkout, where missing `package-data` never surfaces) and takes roughly
# 10-30s to run.
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED_MD = REPO_ROOT / "src" / "garmin_outreach" / "serve" / "static" / "VENDORED.md"
HEX64_RE = re.compile(r"\b[0-9a-f]{64}\b")

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv executable not found on PATH"
)

_ASSET_SCRIPT = """
import hashlib
import json
from importlib.resources import files

static = files("garmin_outreach.serve") / "static"
report = {}
for entry in static.iterdir():
    if entry.is_file():
        report[entry.name] = hashlib.sha256(entry.read_bytes()).hexdigest()

templates = files("garmin_outreach.serve") / "templates"
template_names = sorted(entry.name for entry in templates.iterdir() if entry.is_file())

print(json.dumps({"static": report, "templates": template_names}))
"""


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def test_wheel_ships_all_vendored_static_assets(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, (
        f"uv build failed (exit {build.returncode})\n"
        f"stdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )

    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    wheel = wheels[0]

    venv_dir = tmp_path / "venv"
    venv_create = subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", f"{sys.version_info[0]}.{sys.version_info[1]}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert venv_create.returncode == 0, (
        f"uv venv failed (exit {venv_create.returncode})\n"
        f"stdout:\n{venv_create.stdout}\nstderr:\n{venv_create.stderr}"
    )

    venv_python = _venv_python(venv_dir)
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), "--no-deps", str(wheel)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert install.returncode == 0, (
        f"uv pip install failed (exit {install.returncode})\n"
        f"stdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    )

    script_path = tmp_path / "_asset_report.py"
    script_path.write_text(_ASSET_SCRIPT, encoding="utf-8")
    report_run = subprocess.run(
        [str(venv_python), str(script_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert report_run.returncode == 0, (
        f"asset report script failed (exit {report_run.returncode})\n"
        f"stdout:\n{report_run.stdout}\nstderr:\n{report_run.stderr}"
    )

    report = json.loads(report_run.stdout)
    static_report = report["static"]
    template_names = report["templates"]

    expected_patterns = [
        r"^datastar-.+\.js$",
        r"^maplibre-gl-csp-.+\.js$",
        r"^maplibre-gl-csp-worker-.+\.js$",
        r"^maplibre-gl-.+\.css$",
        r"^app\.css$",
        r"^map\.js$",
        r"^VENDORED\.md$",
        r"^LICENSE-datastar\.md$",
        r"^LICENSE-maplibre\.txt$",
    ]
    for pattern in expected_patterns:
        matches = [name for name in static_report if re.match(pattern, name)]
        assert matches, (
            f"no static asset matching {pattern!r} in installed wheel; "
            f"reported assets: {sorted(static_report)}"
        )

    vendored_text = VENDORED_MD.read_text(encoding="utf-8")
    vendored_hashes = set(HEX64_RE.findall(vendored_text))
    assert vendored_hashes, "no SHA-256 hashes found in VENDORED.md"

    reported_hashes = set(static_report.values())
    missing_hashes = vendored_hashes - reported_hashes
    assert not missing_hashes, (
        f"VENDORED.md records hashes not present in the installed wheel's static assets: "
        f"{missing_hashes}; reported assets: {static_report}"
    )

    expected_templates = {"base.html", "dashboard.html", "messages.html", "map.html"}
    missing_templates = expected_templates - set(template_names)
    assert not missing_templates, (
        f"installed wheel is missing templates: {missing_templates}; "
        f"reported templates: {template_names}"
    )
