"""Model metadata inheritance is durable, scoped, and separate from probes."""
from copy import deepcopy

import pytest

from lib.model_routing import (InMemoryModelRoutingRepository, ModelRef,
    ModelRoutingError, NativeModelSelection, OwnerBoundary, RoutePolicy,
    compile_candidates, normalize_document)
from lib.model_routing.catalog_identity import reconcile_catalog_identities
from tests.model_routing.test_model_routing_contract import _full_document
from tests.model_routing.test_model_inheritance import pending_document

pytestmark = pytest.mark.unit


def test_saved_inheritance_tracks_model_changes_and_runtime_eligibility():
    from tests.support.model_registration import complete_registration
    doc = complete_registration(_full_document())
    doc['offerings'][0].update(capabilities=['text'], context_window=32768,
        inherited_model_fields=['capabilities', 'context_window'])
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(834)
    repo.compare_and_swap(owner, doc, expected_revision=0)
    installed = repo.get(owner).document
    assert installed['offerings'][0]['context_window'] == 200000
    def update_model(working):
        working['models'][0]['context_window'] = 500000
        working['models'][0]['capabilities'].append('vision')
        return working
    repo.mutate(owner, update_model)
    loaded = repo.get(owner).document
    inherited = next(o for o in loaded['offerings'] if o['offering_id'] == 'alpha-a')
    explicit = next(o for o in loaded['offerings'] if o['offering_id'] == 'alpha-b')
    assert inherited['context_window'] == 500000 and 'vision' in inherited['capabilities']
    assert explicit['context_window'] == 100000 and 'vision' not in explicit['capabilities']
    candidates = compile_candidates(loaded,
        NativeModelSelection(ModelRef('creator', 'alpha'), None),
        policy=RoutePolicy(required_capabilities=frozenset({'vision'}), required_context=400000))
    assert candidates and {r.offering['offering_id'] for r in candidates} == {'alpha-a'}


def test_explicit_provider_override_survives_later_public_model_change():
    doc = _full_document()
    offering = doc['offerings'][0]
    offering.update(context_window=32768, inherited_model_fields=['capabilities'])
    doc['models'][0]['context_window'] = 500000
    normalized = normalize_document(doc)
    row = next(o for o in normalized['offerings'] if o['offering_id'] == 'alpha-a')
    assert row['context_window'] == 32768
    assert row['inherited_model_fields'] == ['capabilities']


def test_pending_fallback_is_replaced_on_identity_binding_without_a_probe():
    doc = pending_document()
    row = doc['offerings'][0]
    row.update(capabilities=['text'], context_window=32768,
               inherited_model_fields=['capabilities', 'context_window'])
    assert reconcile_catalog_identities(doc) == 1
    assert row['context_window'] == 500000
    assert 'thinking' in row['capabilities']
    assert doc['deployments'][0]['probe_status'] == 'unprobed'
    normalize_document(doc)


@pytest.mark.parametrize('invalid', ['context_window', None, ['pricing'],
    ['context_window', 'context_window'], ['capabilities', 'context_window', 'pricing']])
def test_invalid_inheritance_never_commits(invalid):
    repo, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(835)
    repo.compare_and_swap(owner, _full_document(), expected_revision=0)
    before = repo.get(owner)
    doc = deepcopy(before.document)
    doc['offerings'][0]['inherited_model_fields'] = invalid
    with pytest.raises(ModelRoutingError):
        repo.compare_and_swap(owner, doc, expected_revision=before.revision)
    assert repo.get(owner).document == before.document
