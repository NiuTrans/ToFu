"""CLI contract for the Python-facade-only server installer."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'scripts' / 'install_on_server.sh'


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', str(INSTALLER), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_help_and_unknown_arguments_never_invoke_python(tmp_path):
    env = dict(os.environ, PY=str(tmp_path / 'missing python'))

    help_result = _run('--help', env=env)
    assert help_result.returncode == 0
    assert 'only Tofu\'s in-process Python facade' in help_result.stdout
    assert 'does not install or start the Tofu web app' in help_result.stdout

    bad_result = _run('--typo', env=env)
    assert bad_result.returncode == 2
    assert 'accepts no command-line options' in bad_result.stderr
    assert 'not executable' not in bad_result.stderr


def test_installer_treats_python_path_as_one_executable(tmp_path):
    calls = tmp_path / 'calls.txt'
    fake_python = tmp_path / 'fake env' / 'python'
    fake_python.parent.mkdir()
    fake_python.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s\\n" "$*" >> "$CALLS_FILE"\n'
        'if [ "${1:-}" = "-c" ]; then printf "fake-python\\n"; fi\n',
        encoding='utf-8',
    )
    fake_python.chmod(0o755)
    env = dict(
        os.environ,
        PY=str(fake_python),
        CALLS_FILE=str(calls),
        PIP_ARGS='--disable-pip-version-check --no-cache-dir',
        WITH_PLAYWRIGHT='0',
    )

    result = _run(env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    invocations = calls.read_text(encoding='utf-8').splitlines()
    assert any(line.startswith('-m pip install --upgrade ') for line in invocations)
    assert any('--disable-pip-version-check --no-cache-dir' in line
               for line in invocations)
    assert any(line == '-' for line in invocations)


@pytest.mark.parametrize('value', ['', 'yes', '2'])
def test_invalid_playwright_switch_fails_before_python(tmp_path, value):
    env = dict(
        os.environ,
        PY=str(tmp_path / 'missing python'),
        WITH_PLAYWRIGHT=value,
    )

    result = _run(env=env)

    assert result.returncode == 2
    assert 'WITH_PLAYWRIGHT must be exactly 0 or 1' in result.stderr
    assert 'target Python is not executable' not in result.stderr
