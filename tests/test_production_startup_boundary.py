"""Executable contracts for focused production-runtime startup imports."""

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
def test_motion_package_import_keeps_render_pipeline_dormant():
    proc = _run_isolated(
        'import sys; import lib.motion_video as motion; '
        'print("MOTION-PACKAGE", len(motion.__all__), '
        'set(motion.__all__) == set(motion._EXPORT_MODULES), '
        '"lib.motion_video.runtime" in sys.modules, '
        '"lib.motion_video.engine" in sys.modules, '
        '"lib.motion_video._env" in sys.modules, '
        '"lib.motion_video._render" in sys.modules, '
        '"lib.motion_video._audio" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'MOTION-PACKAGE 74 True False False False False False' in proc.stdout


@pytest.mark.unit
def test_production_package_import_keeps_recipe_owners_dormant():
    proc = _run_isolated(
        'import sys; import lib.production as production; '
        'print("PRODUCTION-PACKAGE", len(production.__all__), '
        'set(production.__all__) == set(production._EXPORT_MODULES), '
        '"lib.production.runtime" in sys.modules, '
        '"lib.production.stages" in sys.modules, '
        '"lib.production.contracts" in sys.modules, '
        '"lib.production.research" in sys.modules, '
        '"lib.production.jobs" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'PRODUCTION-PACKAGE 40 True False False False False False'
        in proc.stdout
    )


@pytest.mark.unit
def test_motion_runtime_import_loads_only_shared_task_authority():
    proc = _run_isolated(
        'import sys; from lib.motion_video.runtime import _motion_runtime; '
        'print("MOTION-RUNTIME", _motion_runtime.kind, '
        '"lib.motion_video.runtime" in sys.modules, '
        '"lib.motion_video.engine" in sys.modules, '
        '"lib.motion_video._gates" in sys.modules, '
        '"lib.motion_video._audio" in sys.modules, '
        '"lib.production.runtime" in sys.modules, '
        '"lib.production.stages" in sys.modules, '
        '"lib.production.research" in sys.modules, '
        '"lib.production.jobs" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'MOTION-RUNTIME motion-video True False False False True False False False'
        in proc.stdout
    )


@pytest.mark.unit
def test_server_boot_keeps_motion_recipes_and_production_stages_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-PRODUCTION", '
        '"lib.motion_video.runtime" in sys.modules, '
        '"lib.motion_video.engine" in sys.modules, '
        '"lib.motion_video._env" in sys.modules, '
        '"lib.motion_video._render" in sys.modules, '
        '"lib.motion_video._audio" in sys.modules, '
        '"lib.production.runtime" in sys.modules, '
        '"lib.production.stages" in sys.modules, '
        '"lib.production.contracts" in sys.modules, '
        '"lib.production.research" in sys.modules, '
        '"lib.production.jobs" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'SERVER-PRODUCTION True False False False False True False False False False'
        in proc.stdout
    )
