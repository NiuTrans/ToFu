"""Concrete definition result and resolution ownership contracts."""

from __future__ import annotations

import pytest

import lib.orchestration.definition_resolution as resolution
import lib.orchestration.definition_results as results
import lib.orchestration.definition_service as service


pytestmark = pytest.mark.unit


def test_definition_service_facade_preserves_result_and_resolver_identity():
    assert service.ResolvedDefinition is results.ResolvedDefinition
    assert service.DefinitionWriteResult is results.DefinitionWriteResult
    assert service.DefinitionDeleteResult is results.DefinitionDeleteResult
    assert service.resolve_definition is resolution.resolve_definition


def test_definition_resolution_detaches_inline_and_stored_documents():
    inline = {'name': 'Inline', 'nodes': []}
    stored = {'name': 'Stored', 'nodes': []}

    resolved_inline = resolution.resolve_definition(inline=inline)
    resolved_stored = resolution.resolve_definition(
        stored_id='flow-1',
        load_stored=lambda _flow_id: stored,
    )

    resolved_inline.definition['name'] = 'changed'
    resolved_stored.definition['name'] = 'changed'
    assert inline['name'] == 'Inline'
    assert stored['name'] == 'Stored'
