"""Executor context reads recover snapshot drift without combining stale pages."""
from copy import deepcopy

import pytest

from lib.storage.errors import StorageError
from lib.storage.model_continuation import ModelContinuationStore

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
