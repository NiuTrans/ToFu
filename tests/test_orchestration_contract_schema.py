"""Reusable OpenAPI projection for backend-owned contract metadata."""

from __future__ import annotations

import pytest

from lib.orchestration.contract_schema import contract_snapshot_schema


pytestmark = pytest.mark.unit


def test_contract_snapshot_schema_preserves_policy_and_open_registry_paths():
    snapshot = {
        'format': 'example/v1',
        'names': ['one', 'two'],
        'limits': {'maxLength': 8},
        'flags': {'safe': True},
    }

    schema = contract_snapshot_schema(
        snapshot, open_object_paths=[('flags',)])

    assert schema['required'] == list(snapshot)
    assert schema['additionalProperties'] is False
    assert schema['properties']['format']['enum'] == ['example/v1']
    assert schema['properties']['names'] == {
        'type': 'array',
        'minItems': 2,
        'maxItems': 2,
        'items': {'type': 'string', 'enum': ['one', 'two']},
        'uniqueItems': True,
    }
    assert schema['properties']['limits']['properties']['maxLength'] == {
        'type': 'integer', 'const': 8, 'minimum': 1,
    }
    assert schema['properties']['flags']['additionalProperties'] is True


def test_contract_snapshot_schema_returns_detached_documents():
    snapshot = {'values': ['one']}
    schema = contract_snapshot_schema(snapshot)
    schema['properties']['values']['items']['enum'].append('mutated')

    assert snapshot == {'values': ['one']}
    assert contract_snapshot_schema(snapshot)['properties']['values'][
        'items']['enum'] == ['one']
