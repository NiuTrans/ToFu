"""Executable contracts for dormant paper engine import boundaries."""

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
def test_server_boot_keeps_dormant_paper_engines_unloaded():
    proc = _run_isolated(
        'import sys; import server; '
        'print("PAPER", '
        '"lib.paper.deepen_runtime" in sys.modules, '
        '"lib.paper.deepen_engine" in sys.modules, '
        '"lib.paper.qa_engine" in sys.modules, '
        '"lib.paper.translate_engine" in sys.modules, '
        '"lib.paper.recommend_engine._events" in sys.modules, '
        '"lib.paper.recommend_task" in sys.modules, '
        '"lib.paper.report_engine.worker" in sys.modules, '
        '"lib.tools.registry" in sys.modules, '
        '"lib.paper.podcast_engine.worker" in sys.modules, '
        '"lib.paper.podcast_engine._script" in sys.modules, '
        '"lib.paper.podcast_engine._audio" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'PAPER True False False False False False False False False False False' \
        in proc.stdout


@pytest.mark.unit
def test_background_podcast_sweep_import_does_not_load_generation_stages():
    proc = _run_isolated(
        'import sys; import lib.paper.podcast_engine.worker; '
        'print("PODCAST", '
        '"lib.paper.podcast_engine._script" in sys.modules, '
        '"lib.paper.podcast_engine._audio" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'PODCAST False False' in proc.stdout
