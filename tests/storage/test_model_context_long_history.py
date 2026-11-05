"""Model-context proofs and paging have no Turn-count ceiling.

The former 10,000-header bound refused proofs (`supported: false`), checkpoint
publication and client paging on long-lived lanes. These specifications cross it
on the SQL authority and the backend-neutral client; Tofu-DB's native walk is
specified in packages/tofu-db/src/turn/model_context/tests.rs.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

import pytest

from lib.agent_core.model_context_limits import MAX_JSON_SAFE_INTEGER, MAX_TURN_WITNESSES
from lib.storage.model_continuation import (
    ModelContinuationStore, encode_projection, settled_witness,
)

pytestmark = pytest.mark.unit

_HISTORY = 10_050
_OWNER = 11


def _proof_projection(count, turns=None):
    return {'messages': [{'role': 'assistant', 'content': 'retained'}],
            'turns': turns or [['turn', '0' * 64]],
            'historyProof': {'version': 2, 'count': count, 'digest': 'a' * 64}}


@pytest.mark.parametrize('count', [0, 9_999, 10_000, 20_000, 1 << 40, MAX_JSON_SAFE_INTEGER])
def test_proof_count_is_a_plain_counter(count):
    assert encode_projection(_proof_projection(count)) is not None


@pytest.mark.parametrize('count', [MAX_JSON_SAFE_INTEGER + 1, 2**64, -1, 1.0, True, '1', None])
def test_proof_count_outside_exact_json_integers_is_refused(count):
    assert encode_projection(_proof_projection(count)) is None


def test_witness_bound_applies_only_to_legacy_lists():
    witness = ['turn', '0' * 64]
    assert encode_projection(_proof_projection(1, [witness, witness])) is None
    legacy = {'messages': [{'role': 'assistant', 'content': 'retained'}]}
    assert encode_projection(legacy | {'turns': [witness] * MAX_TURN_WITNESSES}) is not None
    assert encode_projection(legacy | {'turns': [witness] * (MAX_TURN_WITNESSES + 1)}) is None


class _PagedClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.cursors = []

    def query(self, operation, payload, **kwargs):
        assert operation == 'turn.context.read'
        self.cursors.append(payload.get('after_turn_id'))
        return self.pages.pop(0)


def _client_page(start, stop, *, last):
    return {'supported': True, 'covered': 0,
            'turns': [{'turnId': f't{index}'} for index in range(start, stop)],
            'historyProof': {'version': 2, 'count': last, 'digest': 'c' * 64},
            'continuation': None, 'proofEligible': True,
            'nextTurnId': None if stop == last + 1 else f't{stop - 1}'}


def test_client_assembles_pages_past_ten_thousand_turns():
    last = 4 * 4_000 - 1
    client = _PagedClient([_client_page(start, start + 4_000, last=last)
                           for start in (0, 4_000, 8_000, 12_000)])
    context = ModelContinuationStore(client=client).read_context(
        user_id=_OWNER, conversation_id='conv', turn_id=f't{last}', replay=False)
    assert [row['turnId'] for row in context['turns']] == [f't{index}' for index in range(last + 1)]
    assert client.cursors == [None, 't3999', 't7999', 't11999']
    assert context['nextTurnId'] is None


def test_client_restarts_when_a_page_repeats_any_assembled_turn():
    repeated = _client_page(1, 3, last=3) | {'turns': [{'turnId': 't0'}, {'turnId': 't2'}]}
    client = _PagedClient([_client_page(0, 2, last=3), repeated,
                           _client_page(0, 2, last=3), _client_page(2, 4, last=3)])
    context = ModelContinuationStore(client=client).read_context(
        user_id=_OWNER, conversation_id='conv', turn_id='t3', replay=False)
    assert [row['turnId'] for row in context['turns']] == ['t0', 't1', 't2', 't3']
    assert client.cursors == [None, 't1', None, 't1']


@pytest.fixture
def sql_authority(tmp_path):
    from benchmarks.tofudb_context_controls import InProcessSQLite

    engine = InProcessSQLite(json_boundary=False, directory=tmp_path)
    engine.start()
    try:
        yield engine
    finally:
        engine.stop()


def _seed(engine, conversation, count):
    """Real settled appends, several per writer transaction for fixture speed."""
    from lib.storage_authority_sql.operation_registry import build_registry

    append_settled = build_registry()['turn.append_settled'].handler

    engine.execute('conversation.create', {'conv_id': conversation, 'user_id': _OWNER}, True)
    for start in range(0, count, 500):
        def append(session, start=start):
            for index in range(start, min(start + 500, count)):
                append_settled(session, {
                    'conversation_id': conversation, 'user_id': _OWNER,
                    'turn_id': f'{conversation}-{index}', 'command_id': uuid.uuid4().hex,
                    'actor': 'assistant' if index % 2 else 'human',
                    'projection': {'content': f'content-{index}'}})
        engine.backend.command('test.context.seed', '', None, 'maintenance', append,
                               time.monotonic() + 60, receipt_required=False)


def _read(engine, conversation, index, *, replay, after=None):
    payload = {'conversation_id': conversation, 'user_id': _OWNER,
               'turn_id': f'{conversation}-{index}', 'replay': replay}
    if after is not None:
        payload['after_turn_id'] = f'{conversation}-{after}'
    return engine.execute('turn.context.read', payload, False)


def _independent_proof(rows):
    digest = hashlib.sha256()
    for row in rows[:-1]:
        digest.update((json.dumps([row['turnId'], row['parentTurnId'], row['status'],
            row['currentAttemptId'], row.get('modelContextRevision', row['projectionRevision'])],
            ensure_ascii=False, separators=(',', ':')) + '\n').encode())
    return {'version': 2, 'count': len(rows) - 1, 'digest': digest.hexdigest()}


def test_sql_authority_streams_proofs_past_ten_thousand_headers(sql_authority):
    conversation = 'long-' + uuid.uuid4().hex
    _seed(sql_authority, conversation, _HISTORY)
    cold = _read(sql_authority, conversation, _HISTORY - 1, replay=False)
    assert cold['supported'] is True and cold['nextTurnId'] is None
    assert len(cold['turns']) == _HISTORY
    assert cold['historyProof'] == _independent_proof(cold['turns'])
    checkpoint = {'turns': [settled_witness(cold['turns'][-1])],
                  'historyProof': cold['historyProof'], 'policy': 'long-lane',
                  'messages': [{'role': 'assistant', 'content': 'compacted long history'}]}
    assert cold['historyProof']['count'] >= 10_000
    assert sql_authority.execute('model_continuation.put', {
        'conversation_id': conversation, 'user_id': _OWNER, 'lane_id': 'main',
        'projection': checkpoint}, True) is True
    for index in (_HISTORY, _HISTORY + 1):
        sql_authority.execute('turn.append_settled', {
            'conversation_id': conversation, 'user_id': _OWNER,
            'turn_id': f'{conversation}-{index}', 'command_id': uuid.uuid4().hex,
            'actor': 'human', 'projection': {'content': f'content-{index}'}}, True)

    warm = _read(sql_authority, conversation, _HISTORY + 1, replay=True)
    assert warm['covered'] == _HISTORY and warm['continuationStatus'] == 'available'
    assert warm['continuation'] == checkpoint
    assert [row['turnId'] for row in warm['turns']] == [
        f'{conversation}-{_HISTORY}', f'{conversation}-{_HISTORY + 1}']
    full = _read(sql_authority, conversation, _HISTORY + 1, replay=False)
    assert warm['historyProof'] == full['historyProof'] == _independent_proof(full['turns'])
    after = _read(sql_authority, conversation, _HISTORY + 1, replay=True, after=_HISTORY)
    assert after['covered'] == _HISTORY
    assert [row['turnId'] for row in after['turns']] == [f'{conversation}-{_HISTORY + 1}']

    old = sql_authority.execute('turn.get', {'conversation_id': conversation, 'user_id': _OWNER,
                                             'turn_id': f'{conversation}-3'}, False)
    sql_authority.execute('turn.projection.update', {
        'conversation_id': conversation, 'user_id': _OWNER, 'turn_id': f'{conversation}-3',
        'projection': {'content': 'edited long ago'},
        'expected_projection_revision': old['projectionRevision']}, True)
    changed = _read(sql_authority, conversation, _HISTORY + 1, replay=True)
    assert changed['covered'] == 0 and changed['continuation'] is None
    assert changed['continuationStatus'] == 'history_changed'
    assert len(changed['turns']) == _HISTORY + 2
    assert changed['turns'][3]['projection']['content'] == 'edited long ago'
    assert changed['historyProof'] == _independent_proof(changed['turns'])


def test_sql_keyset_page_size_never_changes_a_response(sql_authority, monkeypatch):
    from lib.storage_authority_sql.operations_pkg import _model_context

    conversation = 'pages-' + uuid.uuid4().hex
    _seed(sql_authority, conversation, 10)
    cold = _read(sql_authority, conversation, 5, replay=False)
    assert sql_authority.execute('model_continuation.put', {
        'conversation_id': conversation, 'user_id': _OWNER, 'lane_id': 'main',
        'projection': {'turns': [settled_witness(cold['turns'][-1])],
                       'historyProof': cold['historyProof'], 'policy': 'pages',
                       'messages': [{'role': 'assistant', 'content': 'kept'}]}}, True)

    def outcome(*args, **kwargs):
        try:
            return _read(sql_authority, conversation, *args, **kwargs)
        except Exception as error:  # noqa: BLE001 -- the error itself is the compared outcome
            return type(error).__name__, str(error)

    reads = [(target, replay, after) for target in (5, 6, 9) for replay in (True, False)
             for after in (None, 2, 5, 6, 8, 9)]
    expected = [outcome(target, replay=replay, after=after) for target, replay, after in reads]
    assert {'covered': 6, 'continuationStatus': 'available'}.items() <= expected[
        reads.index((9, True, None))].items()
    for rows in (1, 2, 3, 7):
        monkeypatch.setattr(_model_context, '_HEADER_PAGE_ROWS', rows)
        assert [outcome(target, replay=replay, after=after)
                for target, replay, after in reads] == expected
