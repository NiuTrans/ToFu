"""Executable contract for on-demand skill catalog networking."""

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
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.unit
def test_server_skill_route_registration_does_not_load_http_transport():
    proc = _run_isolated(
        'import sys; import server; '
        'print("ROUTES-HTTP", "routes.api_v1.skills" in sys.modules, '
        '"routes.api_v1.webhooks" in sys.modules, '
        '"routes.paper_pkg._arxiv" in sys.modules, '
        '"lib.paper.arxiv" in sys.modules, '
        '"lib.http_client" in sys.modules, "requests" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'ROUTES-HTTP True True True False False False' in proc.stdout
