"""Contract tests for the stored-definition repository composition seam."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lib.orchestration.definition_service import (
    DefinitionServiceError,
    OrchestrationDefinitionService,
)
from lib.orchestration.definition_store_port import (
    DefinitionStoreConcurrencyError,
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
                          expected_updated_at=None):
        return _Mutation(
            entry={
                'id': orchestration_id,
                'definition': definition,
                'updatedAt': expected_updated_at,
            },
            current_updated_at=expected_updated_at,
        )

    def delete_if_current(self, _orchestration_id, *,
                          expected_updated_at=None):
        return _Mutation(
            deleted=True,
            current_updated_at=expected_updated_at,
        )


def _definition(name='Legacy update'):
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
])
def test_missing_base_capability_is_rejected_during_composition(missing):
    store = _GuardedStore()
    setattr(store, missing, None)

    with pytest.raises(TypeError, match=missing):
        bind_orchestration_definition_store(store)


@pytest.mark.parametrize('guarded,legacy', [
    ('update_if_current', 'update'),
    ('delete_if_current', 'delete'),
])
def test_missing_mutation_capability_is_rejected_during_composition(
        guarded, legacy):
    store = _GuardedStore()
    setattr(store, guarded, None)
    assert not hasattr(store, legacy)

    with pytest.raises(TypeError, match=guarded):
        OrchestrationDefinitionService(store)


def test_legacy_mutations_are_adapted_only_at_composition_boundary():
    class LegacyStore:
        def __init__(self):
            self.calls = []

        def list_entries(self):
            return []

        def get_entry(self, _orchestration_id):
            return None

        def get_definition(self, _orchestration_id):
            return None

        def create(self, definition):
            return {'id': 'created', 'definition': definition}

        def update(self, orchestration_id, definition):
            self.calls.append(('update', orchestration_id))
            return {
                'id': orchestration_id,
                'definition': definition,
                'updatedAt': 42,
            }

        def delete(self, orchestration_id):
            self.calls.append(('delete', orchestration_id))
            return True

    store = LegacyStore()
    service = OrchestrationDefinitionService(store)

    updated = service.update('legacy-flow', _definition())
    deleted = service.delete_if_current('legacy-flow')

    assert updated.valid is True
    assert updated.conflict is False
    assert updated.current_updated_at == 42
    assert deleted.deleted is True
    assert deleted.conflict is False
    assert store.calls == [
        ('update', 'legacy-flow'),
        ('delete', 'legacy-flow'),
    ]

    with pytest.raises(DefinitionServiceError) as update_error:
        service.update(
            'legacy-flow', _definition(), expected_updated_at=7)
    assert isinstance(
        update_error.value.__cause__, DefinitionStoreConcurrencyError)

    with pytest.raises(DefinitionServiceError) as delete_error:
        service.delete_if_current(
            'legacy-flow', expected_updated_at=42)
    assert isinstance(
        delete_error.value.__cause__, DefinitionStoreConcurrencyError)
    assert store.calls == [
        ('update', 'legacy-flow'),
        ('delete', 'legacy-flow'),
    ]


def test_mixed_guarded_and_legacy_mutations_share_one_adapter():
    class MixedStore(_GuardedStore):
        delete_if_current = None

        def __init__(self):
            self.expected = None

        def update_if_current(self, orchestration_id, definition, *,
                              expected_updated_at=None):
            self.expected = expected_updated_at
            return super().update_if_current(
                orchestration_id,
                definition,
                expected_updated_at=expected_updated_at,
            )

        def delete(self, _orchestration_id):
            return True

    store = MixedStore()
    service = OrchestrationDefinitionService(store)

    updated = service.update('mixed-flow', _definition(),
                             expected_updated_at=19)
    with pytest.raises(DefinitionServiceError) as caught:
        service.delete_if_current('mixed-flow', expected_updated_at=20)
    deleted = service.delete_if_current('mixed-flow')

    assert updated.valid is True
    assert store.expected == 19
    assert deleted.deleted is True
    assert isinstance(caught.value.__cause__, DefinitionStoreConcurrencyError)
