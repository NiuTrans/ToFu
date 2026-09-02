"""Dependency-direction tests for orchestration application-service ports."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from lib.orchestration.application_result_ports import (
    OrchestrationMutationResultPort,
)
from lib.orchestration.application_service_ports import (
    DefinitionServicePort,
    RunServicePort,
)
from lib.orchestration.runtime_ports import (
    OrchestrationDurableRunPort,
    OrchestrationRunTransitionPort,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_application_run_ports_extend_runtime_capabilities_once():
    assert OrchestrationDurableRunPort in RunServicePort.__mro__
    assert OrchestrationRunTransitionPort in \
        OrchestrationMutationResultPort.__mro__
    assert 'create_new' not in RunServicePort.__dict__
    assert 'append_event' not in RunServicePort.__dict__
    assert 'project_event' not in RunServicePort.__dict__
    assert 'transition_status' in RunServicePort.__dict__


def test_definition_resolver_port_describes_every_shared_selection_axis():
    parameters = inspect.signature(
        DefinitionServicePort.resolve).parameters

    assert list(parameters) == [
        'self', 'inline', 'builtin', 'stored_id', 'require_inline_nodes',
    ]


def test_production_http_adapters_import_focused_application_ports():
    provider_consumers = [
        'orchestration_authoring_routes.py',
        'orchestration_definition_request_http.py',
        'orchestration_definition_routes.py',
        'orchestration_mutation_routes.py',
        'orchestration_run_http.py',
        'orchestration_runtime_start_http.py',
        'orchestration_runtime_routes.py',
        'orchestration_task_routes.py',
    ]
    result_consumers = [
        'orchestration_authoring_http.py',
        'orchestration_definition_http.py',
        'orchestration_definition_request_http.py',
        'orchestration_definition_service_http.py',
        'orchestration_mutation_http.py',
        'orchestration_task_http.py',
    ]
    route_dir = ROOT / 'routes/api_v1'

    for name in provider_consumers:
        source = (route_dir / name).read_text()
        assert 'from lib.orchestration.application_provider_ports import' \
            in source
        assert 'orchestration_route_ports' not in source
    for name in result_consumers:
        source = (route_dir / name).read_text()
        assert 'from lib.orchestration.application_result_ports import' \
            in source
        assert 'orchestration_route_ports' not in source

    for path in route_dir.glob('orchestration_*.py'):
        source = path.read_text()
        assert 'orchestration_route_ports' not in source, path.name
        assert 'from lib.orchestration.application_ports import' not in source
