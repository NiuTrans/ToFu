"""Provider removal preserves independent catalog and other access resources."""

import copy

import pytest

from lib.model_routing.domain import empty_document
from lib.model_routing.managed_provider import (
    remove_provider_resources,
    remove_provider_resources_in_place,
    unreferenced_secret_references,
)

pytestmark = pytest.mark.unit


def _candidate():
    document = empty_document()
    document['creators'] = [{'creator_id': 'catalog-creator'}]
    document['models'] = [{'creator_id': 'catalog-creator', 'model_id': 'catalog-model'}]
    for provider in ('removed', 'kept'):
        document['providers'].append({'provider_id': provider})
        document['provider_accesses'].append({
            'provider_id': provider, 'provider_access_id': f'{provider}-access',
        })
        document['connections'].append({
            'provider_access_id': f'{provider}-access', 'connection_id': f'{provider}-connection',
        })
        document['offerings'].append({
            'provider_access_id': f'{provider}-access', 'offering_id': f'{provider}-offering',
        })
        document['deployments'].append({
            'deployment_id': f'{provider}-deployment', 'connection_id': f'{provider}-connection',
            'offering_id': f'{provider}-offering',
        })
    for index, reference in enumerate(('z-secret', 'a-shared-secret', 'z-secret', '')):
        document['credentials'].append({
            'credential_id': f'removed-credential-{index}',
            'provider_access_id': 'removed-access', 'secret_reference': reference,
        })
    document['credentials'].append({
        'credential_id': 'kept-credential', 'provider_access_id': 'kept-access',
        'secret_reference': 'a-shared-secret',
    })
    return document


@pytest.mark.parametrize('in_place', [False, True])
def test_provider_removal_preserves_unrelated_graph_and_shared_secret_reference(in_place):
    candidate = _candidate()
    original = copy.deepcopy(candidate)
    if in_place:
        references = remove_provider_resources_in_place(candidate, 'removed')
        result = candidate
        assert references == ['z-secret', 'a-shared-secret', 'z-secret']
    else:
        result, references = remove_provider_resources(candidate, 'removed')
        assert references == ['a-shared-secret', 'z-secret']
        assert candidate == original
    expected = copy.deepcopy(original)
    for collection in ('providers', 'provider_accesses', 'connections', 'credentials',
                       'offerings', 'deployments'):
        expected[collection] = [original[collection][-1]]
    assert result == expected
    assert remove_provider_resources_in_place(result, 'removed') == []
    assert result == expected


@pytest.mark.parametrize('in_place', [False, True])
def test_unknown_provider_removal_is_a_noop(in_place):
    candidate = _candidate()
    original = copy.deepcopy(candidate)
    if in_place:
        references = remove_provider_resources_in_place(candidate, 'unknown')
        result = candidate
    else:
        result, references = remove_provider_resources(candidate, 'unknown')
    assert references == []
    assert result == original == candidate


def test_copying_removal_keeps_caller_rows_detached():
    original = _candidate()
    result, _references = remove_provider_resources(original, 'removed')
    result['providers'][0]['name'] = 'Edited kept provider'
    result['models'][0]['model_id'] = 'edited-model'
    assert 'name' not in original['providers'][-1]
    assert original['models'][0]['model_id'] == 'catalog-model'


def test_secret_cleanup_keeps_active_references_and_candidate_order():
    candidate = _candidate()
    references = ['z-secret', '', 'orphan-z', 'a-shared-secret', 'orphan-a', 'orphan-z']
    before = copy.deepcopy(candidate)
    assert unreferenced_secret_references(candidate, iter(references)) == [
        'orphan-z', 'orphan-a', 'orphan-z',
    ]
    assert candidate == before


def test_secret_cleanup_requires_a_credential_collection():
    with pytest.raises(KeyError):
        unreferenced_secret_references({}, ['possibly-active-secret'])
