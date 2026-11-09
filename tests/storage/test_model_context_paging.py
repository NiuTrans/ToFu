"""Executor context reads recover snapshot drift without combining stale pages."""
from copy import deepcopy

import pytest

from lib.storage.errors import StorageError
from lib.storage.model_continuation import ModelContinuationStore
from lib.storage.turn_projection_pages import stale_turn_projection_page, turn_projection_page

pytestmark = pytest.mark.unit


def page(ids, *, digest='a', covered=0, continuation=None, eligible=True, cursor=None):
    return {'supported': True, 'covered': covered,
            'turns': [{'turnId': key, 'projection': {'content': digest + key}} for key in ids],
            'historyProof': {'version': 1, 'count': 2, 'digest': digest * 64},
            'continuation': continuation, 'proofEligible': eligible, 'nextTurnId': cursor}


class ScriptedClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def query(self, operation, payload, **kwargs):
        assert operation == 'turn.context.read'
        self.calls.append(dict(payload))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def read(client):
    return ModelContinuationStore(client=client).read_context(
        user_id=11, conversation_id='conv', turn_id='target', replay=False)


@pytest.mark.parametrize('change', ['proof', 'covered', 'continuation', 'cursor'])
def test_snapshot_drift_restarts_from_first_page_and_discards_partial_history(change):
    stale = page(['old'], eligible=False, cursor='old')
    changed = page(['target'])
    if change == 'proof':
        changed['historyProof']['digest'] = 'b' * 64
    elif change == 'covered':
        changed['covered'] = 1
    elif change == 'continuation':
        changed['continuation'] = {'messages': [{'role': 'assistant', 'content': 'new cache'}]}
    else:
        changed = StorageError('database_protocol_error',
                               'context page cursor is outside the selected suffix')
    client = ScriptedClient([stale, changed,
                             page(['fresh'], digest='b', cursor='fresh'),
                             page(['target'], digest='b')])
    result = read(client)
    assert [row['turnId'] for row in result['turns']] == ['fresh', 'target']
    assert all(row['projection']['content'].startswith('b') for row in result['turns'])
    assert result['historyProof']['digest'] == 'b' * 64
    assert result['proofEligible'] is True
    assert result['nextTurnId'] is None
    assert [call.get('after_turn_id') for call in client.calls] == [None, 'old', None, 'fresh']
    assert all(call['user_id'] == 11 and call['conversation_id'] == 'conv'
               and call['turn_id'] == 'target' and call['replay'] is False
               for call in client.calls)


def test_continuation_and_eligibility_are_reselected_after_restart():
    cache = {'messages': [{'role': 'assistant', 'content': 'new cache'}]}
    client = ScriptedClient([page(['old'], cursor='old'),
                             page(['target'], covered=1, continuation=cache),
                             page(['fresh'], covered=1, continuation=cache, cursor='fresh'),
                             page(['target'], covered=1, continuation=cache, eligible=False)])
    result = read(client)
    assert result['continuation'] == cache and result['covered'] == 1
    assert result['proofEligible'] is False
    assert [row['turnId'] for row in result['turns']] == ['fresh', 'target']


def test_continuous_revision_churn_stops_after_three_assemblies():
    client = ScriptedClient([page(['old'], cursor='old'),
                             page(['target'], digest='b')] * 3)
    with pytest.raises(StorageError, match='changed during paging') as caught:
        read(client)
    assert caught.value.code == 'database_conflict'
    assert len(client.calls) == 6


@pytest.mark.parametrize('code,message', [
    ('database_timeout', 'read timed out'),
    ('database_conflict', 'unrelated authority conflict'),
    ('database_protocol_error', 'invalid context page cursor'),
    ('database_unavailable', 'authority stopped'),
])
def test_unrelated_storage_errors_propagate_without_restart(code, message):
    error = StorageError(code, message)
    client = ScriptedClient([page(['old'], cursor='old'), error])
    with pytest.raises(StorageError) as caught:
        read(client)
    assert caught.value is error
    assert len(client.calls) == 2


def test_cursor_that_does_not_advance_fails_without_restart():
    client = ScriptedClient([page(['old'], cursor='wrong')])
    with pytest.raises(StorageError) as caught:
        read(client)
    assert caught.value.code == 'database_protocol_error'
    assert len(client.calls) == 1


def _paged_turn(*, revision=4, content='answer', status='completed'):
    """A Turn whose tool rounds span more than one turn.projection.page page."""
    return {'turnId': 'paged', 'projectionRevision': revision, 'status': status,
            'projection': {'model': 'm', 'content': content,
                           'toolRounds': [{'toolContent': f'{index}:' + 'x' * 1_000_000}
                                          for index in range(9)]}}


def paged_context(turn, *, digest='a'):
    rows = [{'turnId': 'request', 'projection': {'content': 'ask'}}]
    if turn is not None:
        rows.append({'pagedTurn': turn_projection_page(turn, 0)['turn']})
    rows.append({'turnId': 'target', 'projection': {}})
    return page([], digest=digest) | {'turns': rows}


class PagedAuthority:
    """Scripted context reads; turn.projection.page serves the Turn it holds now."""

    def __init__(self, contexts, turn, *, on_page=None):
        self.contexts = iter(contexts)
        self.turn = turn
        self.on_page = on_page
        self.calls = []

    def query(self, operation, payload, **kwargs):
        self.calls.append((operation, dict(payload)))
        if operation == 'turn.context.read':
            return deepcopy(next(self.contexts))
        assert operation == 'turn.projection.page'
        if self.on_page is not None:
            self.on_page(self, payload)
        if self.turn is None:
            return None
        revision = self.turn['projectionRevision']
        if payload.get('projection_revision') not in (None, revision):
            return stale_turn_projection_page(revision)
        return deepcopy(turn_projection_page(self.turn, payload['page']))


def test_a_paged_row_becomes_its_turn_read_by_page_at_the_head_revision():
    turn = _paged_turn()
    authority = PagedAuthority([paged_context(turn)], turn)
    result = read(authority)
    assert [row['turnId'] for row in result['turns']] == ['request', 'paged', 'target']
    assert result['turns'][1] == turn
    count = turn_projection_page(turn, 0)['pageCount']
    assert count > 1
    assert [payload for operation, payload in authority.calls
            if operation == 'turn.projection.page'] == [
        {'user_id': 11, 'conversation_id': 'conv', 'turn_id': 'paged', 'page': index,
         'projection_revision': 4} for index in range(count)]


@pytest.mark.parametrize('change', ['moved', 'deleted', 'head'])
def test_a_paged_turn_that_changed_restarts_the_whole_read(change):
    old = _paged_turn()
    new = {'moved': _paged_turn(revision=5, content='edited'), 'deleted': None,
           # A public Turn can change without a projection revision (native
           # execution-epoch presentation); its pages must carry the read head.
           'head': _paged_turn(status='interrupted')}[change]

    def change_once(authority, payload):
        if authority.turn is old and (payload['page'] == 1 or change != 'moved'):
            authority.turn = new

    authority = PagedAuthority([paged_context(old), paged_context(new, digest='b')], old,
                               on_page=change_once)
    result = read(authority)
    assert result['historyProof']['digest'] == 'b' * 64
    expected = ['request', 'target'] if new is None else ['request', 'paged', 'target']
    assert [row['turnId'] for row in result['turns']] == expected
    if new is not None:
        assert result['turns'][1] == new
    assert sum(operation == 'turn.context.read' for operation, _ in authority.calls) == 2


def test_a_paged_turn_that_keeps_moving_exhausts_the_bounded_restarts():
    turn = _paged_turn()

    def move(authority, payload):
        authority.turn = authority.turn | {
            'projectionRevision': authority.turn['projectionRevision'] + 1}

    authority = PagedAuthority([paged_context(turn)] * 3, turn, on_page=move)
    with pytest.raises(StorageError, match='changed during paging') as caught:
        read(authority)
    assert caught.value.code == 'database_conflict'
    assert sum(operation == 'turn.context.read' for operation, _ in authority.calls) == 3
