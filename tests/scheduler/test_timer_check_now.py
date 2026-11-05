"""Executable contract for the operator's "check now" on a timer.

Owner report (2026-11-02): the panel's trigger button was described as
running one check, but it delivered the continuation without checking. The
continuation then asserted that a batch had finished while it was still
running. Checking is its own action with exactly a background poll's
semantics, and delivering without a check stays a separate action.
"""

from __future__ import annotations

import pytest

from lib.scheduler.timer import _loop

pytestmark = pytest.mark.unit

TIMER_ID = 'tmr_check'


def _verdict(ready, reason, *, parse_error=False, model='predicate', raw=''):
    # poll_timer's tuple: ready, reason, tokens, skipped, parse_error,
    # cmd_output, model, tool_trace, raw_content
    return (ready, reason, 0, False, parse_error, '[exit=1]', model, [], raw)


def _install(monkeypatch, verdicts, **row_overrides):
    state = {
        'row': {
            'id': TIMER_ID, 'user_id': 7, 'status': 'active',
            'poll_count': 3, 'max_polls': 80, 'last_poll_decision': 'wait',
            'last_poll_reason': 'not yet', **row_overrides,
        },
        'polls': [], 'records': [], 'delivered': [], 'exhausted': [],
    }

    def get_row(timer_id, *, user_id):
        assert user_id == 7
        return dict(state['row'])

    def poll(timer_id, *, user_id):
        state['polls'].append(timer_id)
        verdict = verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    def record(timer_id, decision, reason, tokens_used, check_output='',
               model='', poll_id='', raw_output='', *, user_id):
        state['records'].append({'decision': decision, 'reason': reason,
                                 'poll_id': poll_id, 'raw_output': raw_output})
        state['row'].update(poll_count=state['row']['poll_count'] + 1,
                            last_poll_decision=decision,
                            last_poll_reason=reason)

    def deliver(timer):
        state['delivered'].append(timer['id'])
        state['row']['status'] = 'triggered'
        return _loop.TimerContinuationOutcome('steered')

    def exhaust(timer_id, *, user_id):
        state['exhausted'].append(timer_id)
        return True

    monkeypatch.setattr(_loop, '_get_timer_row', get_row)
    monkeypatch.setattr(_loop, 'poll_timer', poll)
    monkeypatch.setattr(_loop, '_record_poll', record)
    monkeypatch.setattr(_loop, '_execute_continuation', deliver)
    monkeypatch.setattr(_loop, '_mark_exhausted', exhaust)
    return state


def test_unmet_condition_records_one_poll_and_delivers_nothing(monkeypatch):
    state = _install(monkeypatch, [_verdict(False, 'predicate no match (exit=1)')])

    outcome = _loop.check_timer_now(TIMER_ID, user_id=7)

    assert outcome.decision == 'wait'
    assert outcome.reason == 'predicate no match (exit=1)'
    assert outcome.delivery is None
    assert state['delivered'] == []
    # It is the next poll of the same ledger and budget, not a side channel.
    assert [r['poll_id'] for r in state['records']] == [f'{TIMER_ID}.p4']
    assert state['row']['status'] == 'active'


def test_met_condition_delivers_like_a_background_poll(monkeypatch):
    state = _install(monkeypatch, [_verdict(True, 'summary.json present')])

    outcome = _loop.check_timer_now(TIMER_ID, user_id=7)

    assert outcome.decision == 'ready'
    assert outcome.delivery.kind == 'steered'
    assert state['records'][0]['decision'] == 'ready'
    assert state['delivered'] == [TIMER_ID]


def test_committed_ready_verdict_is_delivered_without_a_new_check(monkeypatch):
    state = _install(monkeypatch, [], last_poll_decision='ready',
                     last_poll_reason='done')

    outcome = _loop.check_timer_now(TIMER_ID, user_id=7)

    assert state['polls'] == []
    assert outcome.decision == 'ready' and outcome.reason == 'done'
    assert state['delivered'] == [TIMER_ID]


def test_spent_poll_budget_retires_the_watcher_without_polling(monkeypatch):
    state = _install(monkeypatch, [], poll_count=80, max_polls=80)

    assert _loop.check_timer_now(TIMER_ID, user_id=7) is None
    assert state['polls'] == [] and state['exhausted'] == [TIMER_ID]


def test_settled_timer_is_not_checked(monkeypatch):
    state = _install(monkeypatch, [], status='triggered')

    assert _loop.check_timer_now(TIMER_ID, user_id=7) is None
    assert state['polls'] == [] and state['delivered'] == []


def test_failed_check_is_recorded_as_an_error(monkeypatch):
    state = _install(monkeypatch, [RuntimeError('predicate spawn failed')])

    outcome = _loop.check_timer_now(TIMER_ID, user_id=7)

    assert outcome.decision == 'error'
    assert 'predicate spawn failed' in outcome.reason
    assert state['records'][0]['raw_output'] == 'predicate spawn failed'
    assert state['delivered'] == []


def test_ambiguous_predicate_is_an_error_not_a_wait(monkeypatch):
    _install(monkeypatch, [_verdict(False, 'predicate errored (exit=127)',
                                    parse_error=True)])
    assert _loop.check_timer_now(TIMER_ID, user_id=7).decision == 'error'


def test_committed_row_outranks_this_calls_verdict(monkeypatch):
    """The poll number is one command identity. When a background poll
    committed it first, this call's different payload is a command conflict,
    and the durable verdict decides instead of a server error."""
    from lib.storage import StorageError

    state = _install(monkeypatch, [_verdict(True, 'looked ready')])

    def concurrent(timer_id, decision, reason, tokens_used, check_output='',
                   model='', poll_id='', raw_output='', *, user_id):
        state['row'].update(last_poll_decision='wait',
                            last_poll_reason='background poll: not yet')
        raise StorageError('database_conflict',
                           'command_id was reused for a different request')

    monkeypatch.setattr(_loop, '_record_poll', concurrent)

    outcome = _loop.check_timer_now(TIMER_ID, user_id=7)

    assert outcome.decision == 'wait'
    assert outcome.reason == 'background poll: not yet'
    assert state['delivered'] == []


def test_other_storage_failures_still_fail_the_check(monkeypatch):
    from lib.storage import StorageError

    state = _install(monkeypatch, [_verdict(True, 'looked ready')])

    def unavailable(*_args, **_kwargs):
        raise StorageError('database_unavailable', 'authority restarting')

    monkeypatch.setattr(_loop, '_record_poll', unavailable)

    with pytest.raises(StorageError):
        _loop.check_timer_now(TIMER_ID, user_id=7)
    assert state['delivered'] == []
