"""Whole-turn recovery shares failure policy and retains paid attempt evidence."""
from copy import deepcopy

import pytest

from lib.error_envelope import from_exception, make_envelope
from lib.llm import RateLimitError, RetryableAPIError
from lib.tasks_pkg.turn_retry import should_auto_retry_turn
from tests._registered_chat_task import registered_chat_task
from tests.turn_lifecycle.test_turn_auto_retry import _fresh_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('error', [
    RetryableAPIError('Provider temporarily unavailable', status_code=503),
    RetryableAPIError('Gateway temporarily unavailable', status_code=502),
    RateLimitError('Vendor temporarily unavailable', is_gateway=True, status_code=403),
])
def test_classified_upstream_failure_can_recover_without_manual_retry(error):
    envelope = from_exception(error, source='llm-stream')
    assert envelope['kind'] == 'upstream_error' and envelope['retryable'] is True
    retry, wait = should_auto_retry_turn(envelope, attempt=0)
    assert retry and wait > 0


_BLOCKERS = [
    {'retryable': False},
    {'autoRetryExhausted': True},
    {'retryPolicy': {'mode': 'after_condition_change', 'automatic': False}},
    {'context': 'request-prepare', 'detail': 'protocol_conversion:invalid history'},
]


@pytest.mark.parametrize('kind', ['upstream_error', 'network'])
@pytest.mark.parametrize('blocker', _BLOCKERS)
def test_automatic_recovery_respects_the_shared_failure_policy(kind, blocker):
    envelope = {**make_envelope(kind), **deepcopy(blocker)}
    before = deepcopy(envelope)
    assert should_auto_retry_turn(envelope, attempt=0) == (False, 0.0)
    assert envelope == before


def test_upstream_recovery_reaches_the_existing_invocation_and_keeps_paid_usage(monkeypatch):
    from lib.tasks_pkg.orchestrator import _finalize, _ports

    calls = []
    monkeypatch.setattr(_ports, 'rerun_task', lambda task: calls.append(task))
    monkeypatch.setattr('lib.tasks_pkg.turn_retry.auto_turn_backoff_seconds', lambda _: 0)
    task = _fresh_task('upstream_error')
    task['usage'] = {'prompt_tokens': 17, 'completion_tokens': 3, 'total_tokens': 20}
    task['apiRounds'] = [{'round': 1, 'usage': dict(task['usage'])}]
    paid_usage, paid_rounds = deepcopy(task['usage']), deepcopy(task['apiRounds'])

    with registered_chat_task(task):
        assert _finalize._maybe_auto_retry_turn(task, cfg={}) is True

    assert calls == [task]
    assert task['_auto_turn_retry_count'] == 1
    assert task['_checkpointUsage'] == paid_usage
    assert task['_checkpointApiRounds'] == paid_rounds
    assert task['usage'] == {} and task['apiRounds'] == []
    assert task['messages'] == task['_turn_input_messages']
    assert task['error'] is None and task['status'] == 'running'
    assert sum(event['type'] == 'retry_reset' for event in task['events']) == 1


@pytest.mark.parametrize('blocker', _BLOCKERS)
def test_blocked_recovery_does_not_reset_content_or_spend_an_attempt(monkeypatch, blocker):
    from lib.tasks_pkg.orchestrator import _finalize, _ports

    calls = []
    monkeypatch.setattr(_ports, 'rerun_task', lambda task: calls.append(task))
    monkeypatch.setattr('lib.tasks_pkg.turn_retry.auto_turn_backoff_seconds', lambda _: 0)
    task = _fresh_task('network')
    task['error'].update(deepcopy(blocker))
    before = {key: deepcopy(task[key]) for key in ('error', 'messages', 'content', 'thinking')}
    with registered_chat_task(task):
        assert _finalize._maybe_auto_retry_turn(task, cfg={}) is False
    assert not calls and not task['events']
    assert not task.get('_auto_turn_retry_count')
    assert {key: task[key] for key in before} == before


@pytest.mark.parametrize('change', [
    {'toolCallCount': 1},
    {'toolRounds': [{'status': 'running'}]},
    {'toolRounds': [{'status': 'done'}]},
    {'toolRounds': [{'status': 'error'}]},
    {'_suppress_whole_turn_retry_to_preserve_partial': True},
])
def test_new_transient_kind_cannot_replay_started_tools_or_discard_partial_reply(monkeypatch, change):
    from lib.tasks_pkg.orchestrator import _finalize, _ports

    calls = []
    monkeypatch.setattr(_ports, 'rerun_task', lambda task: calls.append(task))
    task = _fresh_task('upstream_error')
    task.update(deepcopy(change))
    with registered_chat_task(task):
        assert _finalize._maybe_auto_retry_turn(task, cfg={}) is False
    assert not calls and not task['events']
    assert task['error']['kind'] == 'upstream_error'


@pytest.mark.parametrize('config, attempt', [
    ({'disableAutoTurnRetry': True}, 0),
    ({'autoTurnRetryMax': 0}, 0),
    ({'autoTurnRetryMax': 1}, 1),
])
def test_upstream_failure_keeps_explicit_opt_out_and_attempt_cap(config, attempt):
    assert should_auto_retry_turn(make_envelope('upstream_error'), attempt, config) == (False, 0.0)
