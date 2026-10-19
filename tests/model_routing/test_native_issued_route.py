"""Native transitions retain the task's admitted route or refuse before secrets."""
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch
import pytest
from lib.model_routing.dispatch_adapter import (
    RoutedSlotGroup, issued_route_identity, mint_routed_slot_group,
)
from lib.model_routing.native_route import compile_native_route, NativeRouteError
from lib.model_routing.routing import compile_candidates
from lib.tasks_pkg.native_execution import compile_native_application_route
from tests.model_routing.test_native_route import Repository, principal, selection

pytestmark = pytest.mark.unit


def group(repository, index=0):
    candidates = compile_candidates(repository.document, selection())
    chosen = candidates[index]
    return RoutedSlotGroup('issued-pin', [SimpleNamespace(slot=SimpleNamespace())], [chosen],
        route_identity=MappingProxyType(issued_route_identity(chosen, repository.boundary, 3)))


def test_native_route_keeps_issued_candidate_even_when_another_ranks_first():
    repo = Repository()
    issued = group(repo, 1)
    with patch('lib.model_routing.native_route.compile_candidates', wraps=compile_candidates) as compile:
        route = compile_native_application_route(repo, principal(), selection(), issued_group=issued)
    public, _ = route.consume(principal())
    assert public['provider_id'] == issued.candidates[0].provider_id == 'provider-b'
    assert compile.call_args.kwargs['max_candidates'] is None
    assert not issued.disposed


@pytest.mark.parametrize('change', ['revision', 'disabled', 'identity', 'selection'])
def test_changed_issued_route_refuses_before_credentials(change):
    repo = Repository()
    issued = group(repo)
    chosen_selection = selection()
    if change == 'revision':
        issued.route_identity = dict(issued.route_identity, revision=2)
    elif change == 'disabled':
        repo.document['connections'][0]['enabled'] = False
    elif change == 'identity':
        issued.route_identity = dict(issued.route_identity, connectionId='other')
    else:
        from lib.model_routing.domain import ModelRef, NativeModelSelection
        chosen_selection = NativeModelSelection(ModelRef('creator', 'beta'), None, '')
    with pytest.raises(NativeRouteError) as error:
        compile_native_application_route(repo, principal(), chosen_selection, issued_group=issued)
    assert error.value.kind == 'native_route_changed'
    assert repo.secret_calls == []


@pytest.mark.parametrize('change', ['owner', 'tenant', 'disposed', 'missing', 'empty', 'malformed'])
def test_unowned_or_released_group_refuses_before_repository_read(change):
    repo = Repository()
    issued = group(repo)
    if change in {'owner', 'tenant'}:
        issued.route_identity = dict(issued.route_identity, **(
            {'owner_user_id': 8} if change == 'owner' else {'tenant_id': 'other'}))
    elif change == 'disposed': issued.disposed = True
    elif change == 'missing': issued.route_identity = None
    elif change == 'malformed': issued.route_identity = []
    else: issued.handles.clear()
    with patch.object(repo, 'get', side_effect=AssertionError('authority must not be read')):
        with pytest.raises(NativeRouteError) as error:
            compile_native_application_route(repo, principal(), selection(), issued_group=issued)
    assert error.value.kind == 'native_route_owner'
    assert repo.secret_calls == []


def test_minted_witness_is_immutable_and_names_first_usable_candidate():
    repo = Repository()
    def mint(**kwargs): return SimpleNamespace(slot=SimpleNamespace())
    with patch('lib.model_routing.dispatch_adapter.mint_ephemeral_slot', side_effect=mint):
        issued = mint_routed_slot_group(repo, repo.boundary, selection())
    assert dict(issued.route_identity) == issued_route_identity(issued.candidates[0], repo.boundary, 3)
    with pytest.raises(TypeError): issued.route_identity['revision'] = 4
    assert 'secret_reference' not in issued.route_identity
    assert 'private-fixture-token' not in str(issued.route_identity)


def test_stale_revision_does_not_recompile_candidates():
    repo = Repository()
    witness = dict(group(repo).route_identity, revision=2)
    with patch('lib.model_routing.native_route.compile_candidates', side_effect=AssertionError('no reroute')):
        with pytest.raises(NativeRouteError):
            compile_native_route(repo, repo.boundary, selection(), expected_route_identity=witness)
    assert repo.secret_calls == []
