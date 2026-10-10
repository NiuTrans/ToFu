"""A broken credential cannot suppress an independent authorized route."""
from types import SimpleNamespace
import pytest

from lib.model_routing import ModelRef, NativeModelSelection, OwnerBoundary
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing import dispatch_adapter as adapter
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit


def _prepare(monkeypatch, secrets):
    document = _full_document()
    for index, credential in enumerate(document['credentials']):
        credential['kind'] = 'api_key'
        credential['secret_reference'] = f'secret-{index}'
    minted, disposed = [], []
    def resolve(_owner, reference):
        value = secrets[int(reference.split('-')[1])]
        if isinstance(value, Exception):
            raise value
        return value
    repository = SimpleNamespace(
        get=lambda owner: SimpleNamespace(document=document), resolve_secret=resolve)
    def mint(**kwargs):
        handle = SimpleNamespace(slot=SimpleNamespace(), api_key=kwargs['api_key'])
        minted.append(handle)
        return handle
    monkeypatch.setattr(adapter, 'mint_ephemeral_slot', mint)
    monkeypatch.setattr(adapter, 'dispose_ephemeral_slot', lambda handle: disposed.append(handle))
    return repository, minted, disposed


def _mint(repository):
    return adapter.mint_routed_slot_group(
        repository, OwnerBoundary.create(23),
        NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a'))


@pytest.mark.parametrize('broken', [
    ModelRoutingError('credential unavailable', kind='credential_secret_missing'),
    '{"format":"tofu.credential-secret/v1","extra_headers":["invalid"]}',
])
def test_invalid_first_credential_does_not_mask_healthy_sibling(monkeypatch, broken):
    repository, minted, _ = _prepare(monkeypatch, [broken, 'working-key'])
    group = _mint(repository)
    assert len(group.handles) == 1
    assert minted[0].api_key == 'working-key'
    assert group.candidates[0].provider_id == 'provider-b'


def test_no_usable_credential_retains_typed_failure(monkeypatch):
    failure = ModelRoutingError('credential unavailable', kind='credential_secret_missing')
    repository, minted, _ = _prepare(monkeypatch, [failure, failure])
    with pytest.raises(ModelRoutingError) as captured:
        _mint(repository)
    assert captured.value.kind == 'credential_secret_missing'
    assert not minted


@pytest.mark.parametrize('failure', [
    RuntimeError('storage unavailable'),
    ModelRoutingError('owner denied', kind='model_routing_forbidden'),
])
def test_infrastructure_or_authority_failure_stops_and_cleans_existing_handles(monkeypatch, failure):
    repository, minted, disposed = _prepare(monkeypatch, ['working-key', failure])
    with pytest.raises(type(failure), match=str(failure)):
        _mint(repository)
    assert len(minted) == 1
    assert disposed == minted


def test_shared_credential_is_resolved_once_per_group_not_across_requests(monkeypatch):
    repository, minted, _ = _prepare(monkeypatch, ['working-key', 'other-key'])
    selection = NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a')
    candidate = adapter.compile_candidates(repository.get(None).document, selection)[0]
    monkeypatch.setattr(adapter, 'compile_candidates', lambda *a, **k: [candidate] * 12)
    monkeypatch.setattr(adapter, 'compile_model_fallback_candidates', lambda *a, **k: [])
    calls = []

    def resolve(owner, reference):
        calls.append((owner.owner_user_id, reference))
        return f'key-{owner.owner_user_id}-{len(calls)}'

    repository.resolve_secret = resolve
    first = _mint(repository)
    assert len(first.handles) == 12
    assert len(calls) == 1
    assert {handle.api_key for handle in first.handles} == {'key-23-1'}
    second = _mint(repository)
    assert len(calls) == 2
    assert {handle.api_key for handle in second.handles} == {'key-23-2'}
    third = adapter.mint_routed_slot_group(repository, OwnerBoundary.create(24), selection)
    assert len(calls) == 3
    assert {handle.api_key for handle in third.handles} == {'key-24-3'}


def test_shared_missing_credential_is_not_reloaded_for_each_deployment(monkeypatch):
    failure = ModelRoutingError('credential unavailable', kind='credential_secret_missing')
    repository, minted, _ = _prepare(monkeypatch, [failure, 'working-key'])
    selection = NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a')
    candidates = adapter.compile_candidates(repository.get(None).document, selection)
    assert len(candidates) == 2
    monkeypatch.setattr(adapter, 'compile_candidates',
                        lambda *a, **k: [candidates[0]] * 12 + [candidates[1]])
    monkeypatch.setattr(adapter, 'compile_model_fallback_candidates', lambda *a, **k: [])
    original = repository.resolve_secret
    calls = []

    def resolve(owner, reference):
        calls.append(reference)
        return original(owner, reference)

    repository.resolve_secret = resolve
    group = _mint(repository)
    assert len(group.handles) == 1
    assert len(calls) == 2
    assert minted[0].api_key == 'working-key'
