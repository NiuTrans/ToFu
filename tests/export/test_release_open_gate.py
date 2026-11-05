"""release_open.sh's exported-tree gate fails when any gate command fails.

Bash suspends ``set -e`` inside a subshell whose exit status is tested by
``||``, so a gate written as ``( cmd1; cmd2 ) || fail`` reports only the last
command's status: a ruff failure followed by a passing collection printed
"gates green". These tests execute the same gate command as the release
against a fake ``uv`` and require every failing step to stop the release.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'release_open_gates.sh'


def _run_gate(tmp_path: Path, failing_word: str | None) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fail_clause = (f'case " $* " in *" {failing_word} "*) exit 1 ;; esac\n'
                   if failing_word else '')
    fake_uv = bin_dir / 'uv'
    fake_uv.write_text('#!/bin/sh\n'
                       'printf "%s|%s\\n" "$PWD" "$*" >> "$GATE_CALLS"\n'
                       + fail_clause + 'exit 0\n', encoding='utf-8')
    fake_uv.chmod(0o755)
    gated_dir = tmp_path / 'gated'
    gated_dir.mkdir()
    env = {**os.environ,
           'PATH': f'{bin_dir}{os.pathsep}{os.environ.get("PATH", "")}',
           'GATE_CALLS': str(tmp_path / 'calls')}
    return subprocess.run([shutil.which('bash') or '/bin/bash', str(SCRIPT), str(gated_dir)],
                          env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize('failing_word', ['ruff', 'make'])
def test_gate_fails_when_any_gate_command_fails(tmp_path, failing_word):
    result = _run_gate(tmp_path, failing_word)

    assert result.returncode != 0, result.stdout + result.stderr
    assert 'gates green' not in result.stdout
    assert 'gates FAILED' in result.stderr
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert len(calls) == (1 if failing_word == 'ruff' else 2)
    assert all(call.startswith(str(tmp_path / 'gated') + '|') for call in calls)


def test_gate_passes_when_every_gate_command_passes(tmp_path):
    result = _run_gate(tmp_path, None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'gates green' in result.stdout
    assert (tmp_path / 'calls').read_text().splitlines() == [
        f'{tmp_path / "gated"}|run --frozen --extra dev ruff check .',
        f'{tmp_path / "gated"}|run --frozen --extra test make test-collect',
    ]


def test_gate_rejects_a_missing_candidate_before_running_tools(tmp_path):
    result = subprocess.run(
        [shutil.which('bash') or '/bin/bash', str(SCRIPT), str(tmp_path / 'missing')],
        capture_output=True, text=True, timeout=60)

    assert result.returncode != 0
    assert 'gates FAILED' in result.stderr
    assert '[1/2]' not in result.stdout
