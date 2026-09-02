"""Executable contracts for the lazy swarm package facade."""

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
def test_registry_child_import_does_not_initialize_swarm_execution():
    proc = _run_isolated(
        'import sys; from lib.swarm.registry import AGENT_ROLES; '
        'print("REGISTRY", bool(AGENT_ROLES), '
        '"lib.swarm.agent" in sys.modules, '
        '"lib.swarm.integration" in sys.modules, '
        '"lib.tasks_pkg.manager" in sys.modules, '
        '"lib.project_mod" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'REGISTRY True False False False False' in proc.stdout


@pytest.mark.unit
def test_package_exports_and_child_module_import_remain_compatible():
    proc = _run_isolated(
        'import sys; import lib.swarm as swarm; '
        'before = "lib.swarm.registry" in sys.modules; '
        'roles = swarm.AGENT_ROLES; '
        'from lib.swarm import persistence; '
        'print("FACADE", before, bool(roles), persistence.__name__, '
        '"lib.swarm.registry" in sys.modules, '
        '"lib.swarm.agent" in sys.modules, '
        '"lib.swarm.integration" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'FACADE False True lib.swarm.persistence True False False' \
        in proc.stdout


@pytest.mark.unit
def test_server_schema_boot_keeps_swarm_execution_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-SWARM", "lib.swarm.registry" in sys.modules, '
        '"lib.swarm.agent" in sys.modules, '
        '"lib.swarm.integration" in sys.modules, '
        '"lib.project_mod" in sys.modules)')
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-SWARM True False False False' in proc.stdout
