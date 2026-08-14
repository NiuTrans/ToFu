"""Unified late-bound orchestration application-service composition."""

from __future__ import annotations

import pytest

from lib.orchestration import service as service_facade
from lib.orchestration.application_services import (
    OrchestrationApplicationServices,
)


pytestmark = pytest.mark.unit


class _Definitions:
    def __init__(self, marker: str):
        self.marker = marker

    def resolve(self, *, inline=None, builtin='', stored_id='',
                require_inline_nodes=False):
        return {
            'marker': self.marker,
            'inline': inline,
            'builtin': builtin,
            'stored_id': stored_id,
            'require_inline_nodes': require_inline_nodes,
        }


class _Runtime:
    pass


def test_application_services_are_late_bound_and_share_one_runtime_start_seam():
    current = {'definitions': _Definitions('first'), 'runs': object()}
    authoring = object()
    gates = object()
    services = OrchestrationApplicationServices(
        runtime=_Runtime(),
        definition_service=lambda: current['definitions'],
        run_service=lambda: current['runs'],
        authoring_service=lambda: authoring,
        human_gate_service=lambda: gates,
    )

    assert services.definitions().marker == 'first'
    assert services.runs() is current['runs']
    assert services.authoring() is authoring
    assert services.human_gates() is gates
    assert services.resolve_definition({
        'definition': {'name': 'Inline'}, 'id': 'stored-id',
    }) == {
        'marker': 'first', 'inline': {'name': 'Inline'},
        'builtin': '', 'stored_id': 'stored-id',
        'require_inline_nodes': False,
    }

    replacement_definitions = _Definitions('second')
    replacement_runs = object()
    current.update(
        definitions=replacement_definitions,
        runs=replacement_runs,
    )
    starts = services.runtime_starts()
    mutations = services.runtime_mutations()
    assert services.definitions() is replacement_definitions
    assert services.runs() is replacement_runs
    assert starts._definition_service() is replacement_definitions
    assert starts._run_service() is replacement_runs
    assert mutations._runtime is services.runtime


def test_application_services_are_exported_by_the_compatibility_facade():
    assert service_facade.OrchestrationApplicationServices is \
        OrchestrationApplicationServices
    assert hasattr(service_facade, 'OrchestrationRuntimeMutationService')
