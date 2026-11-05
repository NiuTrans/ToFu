"""One request authority owns primary/fallback selection and slot lifetimes."""

import copy
from types import SimpleNamespace

import pytest

from lib.llm_dispatch import ephemeral, factory
from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.model_routing import ModelRef, NativeModelSelection, OwnerBoundary
from lib.model_routing import dispatch_adapter as adapter, routing, selection_resolution
from tests.model_routing.test_model_routing_contract import _full_document
from tests.support.model_registration import complete_registration

pytestmark = pytest.mark.unit


@pytest.fixture
def authority(monkeypatch):
    dispatcher = LLMDispatcher()
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(ephemeral, '_handles', {})
    monkeypatch.setattr(ephemeral, 'audit_log', lambda *a, **kw: None)
    monkeypatch.setenv('TOFU_EPHEMERAL_PREFLIGHT', '0')
    document = complete_registration(_full_document())
    for connection in document['connections']:
        connection['base_url'] = 'http://127.0.0.1:1/v1'
    reads = []
    def get(boundary):
        reads.append(boundary)
        return SimpleNamespace(document=document, revision=3)
    repository = SimpleNamespace(get=get, resolve_secret=lambda *a: pytest.fail('local credentials need no secrets'))
    yield SimpleNamespace(repository=repository, document=document, reads=reads, dispatcher=dispatcher)
    assert ephemeral.count_ephemeral_slots() == 0 and dispatcher.slots == []


def selection():
    return NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a')


def identities(candidates):
    return [(c.model['model_id'], c.provider_id, c.deployment['deployment_id'],
             c.credential['credential_id']) for c in candidates]


def test_request_validates_one_snapshot_for_primary_and_fallback(authority, monkeypatch):
    snapshot = copy.deepcopy(authority.document)
    primary = routing.compile_candidates(snapshot, selection())
    fallback = routing.compile_model_fallback_candidates(snapshot, selection(), original_provider_id='provider-a')
    normalized = []
    original = selection_resolution.normalize_document
    def normalize(document):
        normalized.append(document)
        return original(document)
    monkeypatch.setattr(selection_resolution, 'normalize_document', normalize)
    group = adapter.mint_routed_slot_group(authority.repository, OwnerBoundary.create(23), selection())
    try:
        assert identities(group.candidates) == identities(primary + fallback)
        assert [h.slot.route_rank for h in group.handles] == list(range(len(group.handles)))
        assert len(authority.reads) == len(normalized) == 1
        assert authority.document == snapshot
    finally:
        adapter.dispose_routed_slot_group(group)


def test_reused_compiler_matches_standalone_fallbacks_without_mutating_source(authority):
    original = copy.deepcopy(authority.document)
    compiler = routing.RouteCandidateCompiler(authority.document)
    for limit in (None, 1, 5):
        assert compiler.compile(selection(), max_candidates=limit) == routing.compile_candidates(
            original, selection(), max_candidates=limit)
        assert compiler.compile_fallbacks(selection(), original_provider_id='provider-a',
            max_candidates=limit) == routing.compile_model_fallback_candidates(
                original, selection(), original_provider_id='provider-a', max_candidates=limit)
    assert authority.document == original


def test_new_request_observes_revocation_without_changing_existing_request(authority):
    first = adapter.mint_routed_slot_group(authority.repository, OwnerBoundary.create(23), selection())
    before = identities(first.candidates)
    second = None
    try:
        authority.document['credentials'][0]['enabled'] = False
        second = adapter.mint_routed_slot_group(authority.repository, OwnerBoundary.create(24), selection())
        assert [c.provider_id for c in second.candidates] == ['provider-b']
        assert identities(first.candidates) == before
        assert first.candidates[0].credential['enabled'] is True
        assert [b.owner_user_id for b in authority.reads] == [23, 24]
        assert all(h.slot.routing_owner_user_id == 23 for h in first.handles)
        assert all(h.slot.routing_owner_user_id == 24 for h in second.handles)
    finally:
        adapter.dispose_routed_slot_group(second)
        adapter.dispose_routed_slot_group(first)
