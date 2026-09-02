"""Executable contracts for request-loaded optimizer analysis."""

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
def test_package_import_keeps_optimizer_pipeline_dormant():
    proc = _run_isolated(
        'import sys; import lib.optimizer as optimizer; '
        'print("OPTIMIZER-PACKAGE", optimizer.__all__, '
        'set(optimizer.__all__) == set(optimizer._EXPORT_MODULES), '
        '"lib.optimizer.storage" in sys.modules, '
        '"lib.optimizer.analyzer" in sys.modules, '
        '"lib.optimizer.orchestrator" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert "OPTIMIZER-PACKAGE ['run_once'] True False False False" in proc.stdout


@pytest.mark.unit
def test_storage_child_import_does_not_initialize_pipeline():
    proc = _run_isolated(
        'import sys; from lib.optimizer import storage; '
        'print("OPTIMIZER-STORAGE", storage.__name__, '
        '"lib.optimizer.storage" in sys.modules, '
        '"lib.optimizer.analyzer" in sys.modules, '
        '"lib.optimizer.proposer" in sys.modules, '
        '"lib.optimizer.applier" in sys.modules, '
        '"lib.optimizer.orchestrator" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'OPTIMIZER-STORAGE lib.optimizer.storage True False False False False'
        in proc.stdout
    )


@pytest.mark.unit
def test_server_boot_keeps_optimizer_pipeline_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-OPTIMIZER", "routes.api_v1.optimizer" in sys.modules, '
        '"lib.optimizer.storage" in sys.modules, '
        '"lib.optimizer.actions" in sys.modules, '
        '"lib.optimizer.analyzer" in sys.modules, '
        '"lib.optimizer.proposer" in sys.modules, '
        '"lib.optimizer.applier" in sys.modules, '
        '"lib.optimizer.orchestrator" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-OPTIMIZER True True True False False False False' in proc.stdout
