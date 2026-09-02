"""Contract tests for the stored-definition repository composition seam."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lib.orchestration.definition_service import OrchestrationDefinitionService
from lib.orchestration.definition_store_port import (
    bind_orchestration_definition_store,
)


pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Mutation:
    entry: dict | None = None
    conflict: bool = False
    current_updated_at: int | None = None
    deleted: bool = False


class _GuardedStore:
    def list_entries(self):
        return []

    def get_entry(self, _orchestration_id):
        return None

    def get_definition(self, _orchestration_id):
        return None

    def create(self, definition):
        return {'id': 'created', 'definition': definition}

    def update_if_current(self, orchestration_id, definition, *,
                          expected_updated_at):
        return _Mutation(
            entry={
                'id': orchestration_id,
                'definition': definition,
                'updatedAt': expected_updated_at,
            },
            current_updated_at=expected_updated_at,
        )

    def delete_if_current(self, _orchestration_id, *,
                          expected_updated_at):
        return _Mutation(
            deleted=True,
            current_updated_at=expected_updated_at,
        )


def _definition(name='Guarded update'):
    return {
        'schema': 'tofu.orchestration/v1',
        'name': name,
        'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'},
            {
                'id': 'w', 'type': 'role', 'role': 'worker',
                'params': {'objective': 'Do the work'},
            },
            {'id': 'z', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 's', 'to': 'w'},
            {'from': 'w', 'to': 'z'},
        ],
    }


def test_complete_guarded_store_binds_without_wrapper():
    store = _GuardedStore()

    assert bind_orchestration_definition_store(store) is store


@pytest.mark.parametrize('missing', [
    'list_entries', 'get_entry', 'get_definition', 'create',
    'update_if_current', 'delete_if_current',
])
def test_missing_base_capability_is_rejected_during_composition(missing):
    store = _GuardedStore()
    setattr(store, missing, None)

    with pytest.raises(TypeError, match=missing):
        bind_orchestration_definition_store(store)


@pytest.mark.parametrize('guarded', [
    'update_if_current', 'delete_if_current',
])
def test_missing_mutation_capability_is_rejected_during_composition(guarded):
    store = _GuardedStore()
    setattr(store, guarded, None)

    with pytest.raises(TypeError, match=guarded):
        OrchestrationDefinitionService(store)
