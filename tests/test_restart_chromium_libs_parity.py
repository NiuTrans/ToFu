"""Legacy restart and rendered supervisord launchers share browser wiring."""

from __future__ import annotations

import configparser
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

try:
    import pwd
except ImportError:  # Windows has no POSIX account database.
    pwd = None


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _renderer_module():
    path = ROOT / "deploy" / "supervisor" / "render_config.py"
    spec = importlib.util.spec_from_file_location("browser_supervisor_renderer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_environment(tmp_path: Path, *, bundle_present: bool) -> str:
    project = tmp_path / "project"
    project.mkdir()
    (project / "server.py").touch()
    home = tmp_path / "home"
    home.mkdir()
    browser = home / "tofu-browser-libs"
    if bundle_present:
        (browser / "lib").mkdir(parents=True)
        fonts = browser / "etc" / "fonts"
        fonts.mkdir(parents=True)
        (fonts / "fonts.conf").touch()
    text = _renderer_module().render_config(
        project_root=project,
        python_executable=Path(sys.executable),
        user_name=pwd.getpwuid(os.getuid()).pw_name,
        home_directory=home,
        port=15000,
        browser_libraries_directory=browser,
    )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return parser["program:tofu"]["environment"]


def test_restart_script_exports_present_local_browser_bundle():
    source = _source("restart_15000.sh")
    assert 'BROWSER_LIBS_DIR="${TOFU_BROWSER_LIBS_DIR:-${HOME}/tofu-browser-libs}"' in source
    assert 'export CHROMIUM_EXTRA_LIB_DIRS="${BROWSER_LIBS_DIR}/lib"' in source
    assert 'export FONTCONFIG_PATH="${BROWSER_LIBS_DIR}/etc/fonts"' in source
    assert 'export FONTCONFIG_FILE="${BROWSER_LIBS_DIR}/etc/fonts/fonts.conf"' in source


@pytest.mark.skipif(pwd is None, reason="Supervisor renderer requires POSIX accounts")
def test_supervisord_renderer_emits_the_same_present_bundle(tmp_path: Path):
    environment = _render_environment(tmp_path, bundle_present=True)
    browser = tmp_path / "home" / "tofu-browser-libs"
    assert f'CHROMIUM_EXTRA_LIB_DIRS="{browser / "lib"}"' in environment
    assert f'FONTCONFIG_PATH="{browser / "etc" / "fonts"}"' in environment
    assert f'FONTCONFIG_FILE="{browser / "etc" / "fonts" / "fonts.conf"}"' in environment


@pytest.mark.skipif(pwd is None, reason="Supervisor renderer requires POSIX accounts")
def test_supervisord_renderer_does_not_advertise_an_absent_bundle(tmp_path: Path):
    environment = _render_environment(tmp_path, bundle_present=False)
    assert "CHROMIUM_EXTRA_LIB_DIRS" not in environment
    assert "FONTCONFIG_PATH" not in environment
    assert "FONTCONFIG_FILE" not in environment


@pytest.mark.skipif(sys.platform != "linux", reason="legacy restart is Linux-only")
def test_relocated_restart_with_stale_environment_marker_fails_closed(
        tmp_path: Path):
    script = tmp_path / "restart_15000.sh"
    shutil.copy2(ROOT / "restart_15000.sh", script)
    stale_python = tmp_path / "旧 host" / "missing python"
    (tmp_path / ".tofu_env.json").write_text(
        json.dumps({"python": str(stale_python)}) + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TOFU_ALLOW_LIFECYCLE_TEST": "1"},
    )

    assert result.returncode == 1
    assert ".tofu_env.json points to missing Python" in result.stderr
    assert "Rerun install.sh after moving this checkout" in result.stderr
