"""Executable contracts for request-loaded log projection policies."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    return subprocess.run(
        [sys.executable, '-c', source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.mark.unit
def test_log_route_import_keeps_projection_policies_dormant():
    proc = _run_isolated(
        'import sys; import routes.api_v1.logs as route; '
        'print("LOG-ROUTE", callable(route.detect_log_noise), '
        'callable(route.extract_file_changes_dicts), '
        '"lib.log_clean" in sys.modules, '
        '"lib.tool_changes" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'LOG-ROUTE True True False False' in proc.stdout


@pytest.mark.unit
def test_server_boot_keeps_log_projection_policies_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-LOG-ROUTE", '
        '"routes.api_v1.logs" in sys.modules, '
        '"lib.log_clean" in sys.modules, '
        '"lib.log_clean._detect" in sys.modules, '
        '"lib.tool_changes" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-LOG-ROUTE True False False False' in proc.stdout
