"""Failure-attribution receipts: every durable terminal failure links to the
round / tool / phase that produced it, and whole-turn auto-retries persist a
cross-attempt chain instead of orphaning the discarded attempt's error.
"""

from __future__ import annotations

import logging

import pytest

from lib.agent_loop import AbortSignal
from lib.error_envelope import make_envelope
pytestmark = pytest.mark.unit

from lib.tasks_pkg.failure_attribution import (
    ATTRIBUTION_KEY,
    append_turn_retry_receipt,
    attach_failure_attribution,
    build_failure_attribution,
    ensure_failure_attribution,
    record_active_round,
    turn_retry_chain,
)


def test_record_active_round_stamps_ints_only():
    task: dict = {}
    record_active_round(task, 3)
    assert task['_activeRoundNum'] == 3
    record_active_round(task, 'final')
    record_active_round(task, None)
    record_active_round(task, True)
    assert task['_activeRoundNum'] == 3


def test_attach_adds_round_phase_and_tool_without_overriding():
    task = {'_activeRoundNum': 7}
    envelope = make_envelope('tool_loop', detail='same op failed')
    attach_failure_attribution(
        envelope, task, phase='nonretryable_tool_failure',
        tool_name='bash', tool_call_id='call_1')
    assert envelope[ATTRIBUTION_KEY] == {
        'roundNum': 7,
        'phase': 'nonretryable_tool_failure',
        'toolName': 'bash',
        'toolCallId': 'call_1',
    }
    # A second attach never overwrites the first seam's evidence.
    attach_failure_attribution(envelope, task, phase='terminal')
    assert envelope[ATTRIBUTION_KEY]['phase'] == 'nonretryable_tool_failure'


def test_attribution_derives_tool_from_last_failed_projection_round():
    task = {'_activeRoundNum': 2}
    projection = {'toolRounds': [
        {'toolName': 'read_file', 'toolCallId': 'ok-1', 'status': 'done'},
        {'toolName': 'bash', 'toolCallId': 'bad-9', 'status': 'error'},
    ]}
    attribution = build_failure_attribution(task, projection=projection)
    assert attribution['roundNum'] == 2
    assert attribution['toolName'] == 'bash'
    assert attribution['toolCallId'] == 'bad-9'


def test_retry_receipt_links_attempts_and_stays_bounded():
    task: dict = {'_activeRoundNum': 4}
    envelope = attach_failure_attribution(
        make_envelope('ratelimit', detail='HTTP 429'), task, phase='dispatch')
    for attempt in range(1, 12):
        append_turn_retry_receipt(
            task, attempt=attempt, kind='ratelimit',
            backoff_s=1.5, error=envelope)
    chain = turn_retry_chain(task)
    assert len(chain) == 8  # bounded
    assert chain[-1]['attempt'] == 11
    assert chain[0]['kind'] == 'ratelimit'
    assert chain[0]['backoff_s'] == 1.5
    assert chain[0][ATTRIBUTION_KEY]['roundNum'] == 4
    assert chain[0]['detail'] == 'HTTP 429'
    # The returned chain is a copy — mutating it cannot corrupt the task.
    chain[0]['attempt'] = 999
    assert task['_turnRetryChain'][0]['attempt'] == 4  # attempts 4..11


def test_failed_settlement_carries_attribution_backstop_and_retry_chain():
    from lib.turn_lifecycle import _settlement

    task = {
        'status': 'error',
        'error': make_envelope('generic', detail='executor exploded'),
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
        '_activeRoundNum': 5,
        '_turnRetryChain': [{
            'attempt': 1, 'kind': 'ratelimit', 'backoff_s': 1.0, 'at': 1.0,
        }],
    }
    projection = {'content': '', 'toolRounds': [
        {'toolName': 'bash', 'toolCallId': 'call-x', 'status': 'timeout'},
    ]}
    status, settlement = _settlement(task, {'type': 'error'}, projection)
    assert status == 'failed'
    attribution = settlement['error'][ATTRIBUTION_KEY]
    assert attribution['roundNum'] == 5
    assert attribution['toolName'] == 'bash'
    assert attribution['toolCallId'] == 'call-x'
    assert settlement['retryChain'] == [
        {'attempt': 1, 'kind': 'ratelimit', 'backoff_s': 1.0, 'at': 1.0},
    ]


def test_failed_settlement_omits_attribution_when_no_context_known():
    from lib.turn_lifecycle import _settlement

    task = {
        'status': 'error',
        'error': make_envelope('generic', detail='boom'),
        'model': 'gpt-4o',
        'config': {'model': 'gpt-4o'},
    }
    status, settlement = _settlement(
        task, {'type': 'error'}, {'content': '', 'toolRounds': []})
    assert status == 'failed'
    assert settlement['error'][ATTRIBUTION_KEY] == {'phase': 'terminal'}
    assert 'retryChain' not in settlement


def test_nonfailed_settlement_untouched():
    assert ensure_failure_attribution(None, {}) is None
    assert attach_failure_attribution('plain-string-error', {}) == (
        'plain-string-error')


def test_abort_predicate_failure_is_loud_once_then_throttled(caplog):
    calls = {'n': 0}

    def broken() -> bool:
        calls['n'] += 1
        raise RuntimeError('predicate exploded')

    signal = AbortSignal(broken)
    with caplog.at_level(logging.DEBUG):
        assert signal.aborted is False
        assert signal.aborted is False
        assert signal.aborted is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert 'abort predicate raised' in errors[0].getMessage()
    assert getattr(errors[0], 'tofu_fingerprint', '') == (
        'agent-loop.abort-predicate')
    assert calls['n'] == 3  # the loop keeps polling; only the log is throttled


def test_abort_predicate_recovers_after_failure(caplog):
    state = {'fail': True}

    def flaky() -> bool:
        if state['fail']:
            raise RuntimeError('transient')
        return True

    signal = AbortSignal(flaky)
    assert signal.aborted is False
    state['fail'] = False
    assert signal.aborted is True


@pytest.mark.parametrize('schema', ['TurnSettlement', 'ConversationSyncSnapshot', 'AttemptEvent'])
def test_retry_settlement_decodes_live_and_after_reload(schema):
    import copy
    import json
    from pathlib import Path

    from lib.conversation_sync.validation import decode
    from lib.turn_lifecycle import _settlement

    task = {'status': 'error', '_activeRoundNum': 2,
            'error': make_envelope('ratelimit', detail='retry budget exhausted')}
    error = attach_failure_attribution(task['error'], task, phase='dispatch')
    for attempt in range(1, 12):
        append_turn_retry_receipt(task, attempt=attempt, kind='ratelimit',
                                  backoff_s=1.5, error=error)
    status, settlement = _settlement(task, {'type': 'error'}, {'content': '', 'toolRounds': []})
    assert len(settlement['retryChain']) == 8
    assert settlement['retryChain'][0]['attempt'] == 4
    assert settlement['retryChain'][-1]['failureAttribution']['roundNum'] == 2
    if schema == 'TurnSettlement':
        payload = settlement
    elif schema == 'ConversationSyncSnapshot':
        fixture = (Path(__file__).resolve().parents[2] / 'contracts' / 'fixtures'
                   / 'sync_v3' / 'ConversationSyncSnapshot.json')
        payload = json.loads(fixture.read_text())
        payload['turns'][0]['settlement'] = settlement
        payload['turns'][0]['status'] = status
    else:
        from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
        from scripts.gen_wire_fixtures import _sample_openapi

        payload = _sample_openapi(OPENAPI_SCHEMAS['AttemptEvent'], ('AttemptEvent',))
        payload['type'] = 'terminal_settlement'
        payload['payload']['settlement'] = settlement
        payload['payload']['turnState']['settlement'] = copy.deepcopy(settlement)
        payload['payload']['turnState']['status'] = status
    expected = copy.deepcopy(payload)
    assert decode(schema, payload) == expected
    assert payload == expected  # Validation must not strip the durable receipts.


@pytest.mark.parametrize('invalid', [
    {'retryChain': [{'attempt': 1, 'kind': 'generic', 'backoff_s': 0, 'at': 1}] * 9},
    {'retryChain': [{'attempt': 1, 'kind': 'generic', 'backoff_s': 0, 'at': 1, 'unknown': True}]},
    {'retryChain': [{'attempt': 1, 'kind': 'generic', 'backoff_s': 0, 'at': 1, 'detail': 'x' * 301}]},
    {'retryChain': [{'attempt': '1', 'kind': 'generic', 'backoff_s': 0, 'at': 1}]},
    {'retryChain': [{'attempt': 1, 'kind': 'generic', 'at': 1}]},
    {'retryChain': [], 'unknown': True},
])
def test_retry_wire_contract_remains_bounded_and_strict(invalid):
    from lib.conversation_sync.validation import ContractViolation, decode

    with pytest.raises(ContractViolation):
        decode('TurnSettlement', invalid)


def test_task_start_failure_does_not_blame_replayed_tool():
    from lib.tasks_pkg.failure_attribution import ensure_failure_attribution
    error = {'kind': 'task_start_failed', 'context': 'task-start'}
    projection = {'toolRounds': [{'status': 'error', 'toolName': 'edit_file',
                                  'toolCallId': 'previous-attempt-call'}]}
    ensure_failure_attribution(error, {'_activeRoundNum': 70}, projection=projection)
    assert error['failureAttribution'] == {'phase': 'task-start'}
