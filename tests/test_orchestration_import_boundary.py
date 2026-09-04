"""Executable contracts for request-loaded orchestration services."""

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
def test_application_container_import_keeps_execution_services_dormant():
    proc = _run_isolated(
        'import sys; from lib.orchestration.application_services import '
        'OrchestrationApplicationServices; '
        'print("ORCH-CONTAINER", OrchestrationApplicationServices.__name__, '
        '"lib.orchestration.runtime_start_service" in sys.modules, '
        '"lib.orchestration.runtime_service" in sys.modules, '
        '"lib.orchestration.runtime_mutation_service" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'ORCH-CONTAINER OrchestrationApplicationServices False False False'
        in proc.stdout
    )


@pytest.mark.unit
def test_route_registration_keeps_concrete_services_dormant():
    proc = _run_isolated(
        'import sys; import routes.api_v1.orchestrations as route; '
        'print("ORCH-ROUTE", route.orchestration_run_runtime.kind, '
        '"lib.orchestration.authoring_service" in sys.modules, '
        '"lib.orchestration.definition_service" in sys.modules, '
        '"lib.orchestration.run_service" in sys.modules, '
        '"lib.orchestration.sidecar_run_store" in sys.modules, '
        '"lib.orchestration.human_gate_service" in sys.modules, '
        '"lib.orchestration.runtime_start_service" in sys.modules, '
        '"lib.orchestration.runtime_mutation_service" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'ORCH-ROUTE orchestration-run False False False False False False False'
        in proc.stdout
    )


@pytest.mark.unit
def test_server_boot_keeps_orchestration_services_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'names = [name for name in sys.modules '
        'if name == "lib.orchestration" '
        'or name.startswith("lib.orchestration.")]; '
        'print("SERVER-ORCH", len(names), '
        '"lib.orchestration.authoring_service" in sys.modules, '
        '"lib.orchestration.definition_service" in sys.modules, '
        '"lib.orchestration.run_service" in sys.modules, '
        '"lib.orchestration.sidecar_run_store" in sys.modules, '
        '"lib.orchestration.human_gate_service" in sys.modules, '
        '"lib.orchestration.runtime_start_service" in sys.modules, '
        '"lib.orchestration.runtime_mutation_service" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    fields = proc.stdout.split('SERVER-ORCH ', 1)[1].splitlines()[0].split()
    assert int(fields[0]) < 97
    assert fields[1:] == ['False'] * 7
