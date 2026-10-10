"""Shared chassis recovery keeps budgets, correlation and semantic breakers."""
from copy import deepcopy

import pytest

from lib.agent_loop import AbortSignal, run_agent_loop, validate_agent_loop_checkpoint

pytestmark = pytest.mark.unit


class Interrupted(Exception):
    pass


def turn(rnd, tools):
    return {'role': 'assistant', 'tool_calls': [{'id': 'recycled',
        'function': {'name': 'experiment', 'arguments': '{}'}}]}, 'tool_calls', {}


def checkpoint_after(rounds=2, **kwargs):
    saved = []; effects = []
    def checkpoint(value):
        saved.append(deepcopy(value))
        if len(saved) == rounds: raise Interrupted()
    with pytest.raises(Interrupted):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
            execute_tool=lambda rnd, call: effects.append((rnd, call['id'])) or 'same',
            on_checkpoint=checkpoint, **kwargs)
    return saved[-1], effects


def test_valid_halted_state_is_inspectable_but_never_continuable():
    checkpoint, _ = checkpoint_after(rounds=2, max_consecutive_no_progress_rounds=3)
    checkpoint['progress']['no_progress_streak'] = 3
    ledger = validate_agent_loop_checkpoint(checkpoint, require_continuable=False)
    assert ledger.no_progress_streak == 3
    with pytest.raises(ValueError, match='already halted'):
        validate_agent_loop_checkpoint(checkpoint)


def test_no_progress_history_and_round_identity_survive_split_execution():
    checkpoint, effects = checkpoint_after(max_consecutive_no_progress_rounds=3)
    resumed = []
    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
        execute_tool=lambda rnd, call: resumed.append((rnd, call['id'])) or 'same',
        max_consecutive_no_progress_rounds=3, resume_checkpoint=checkpoint)
    assert outcome.halted and outcome.exit_reason == 'no_progress'
    assert [rnd for rnd, _ in effects + resumed] == [0, 1, 2, 3]
    assert len({identity for _, identity in effects + resumed}) == 4
    assert outcome.consecutive_no_progress_rounds == 3
    assert checkpoint['progress']['no_progress_streak'] == 1


def test_retry_credit_is_not_reset_after_resume():
    saved = []
    def dispatch(rnd, tools):
        return ({'content': 'closed'}, 'length', {}) if rnd == 0 else turn(rnd, tools)
    def checkpoint(value):
        saved.append(deepcopy(value)); raise Interrupted()
    with pytest.raises(Interrupted):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=dispatch,
            execute_tool=lambda *args: 'result', retry_bonus=lambda *args: True,
            max_retry_bonus=1, on_checkpoint=checkpoint)
    assert saved[0]['retry_bonus_used'] == 1 and saved[0]['next_round'] == 2
    dispatched = []
    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[],
        dispatch=lambda rnd, tools: dispatched.append(rnd) or ({'content': 'done'}, 'stop', {}),
        retry_bonus=lambda *args: True, max_retry_bonus=1, resume_checkpoint=saved[0])
    assert dispatched == [2] and outcome.retry_bonus_used == 1 and outcome.completed


def test_timeout_streak_survives_and_halted_round_is_not_checkpointed():
    saved = []
    def checkpoint(value): saved.append(deepcopy(value)); raise Interrupted()
    with pytest.raises(Interrupted):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
            execute_tools=lambda *args: {'timed_out': True},
            max_consecutive_tool_timeouts=2, on_checkpoint=checkpoint)
    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
        execute_tools=lambda *args: {'timed_out': True}, max_consecutive_tool_timeouts=2,
        resume_checkpoint=saved[0], on_checkpoint=lambda value: pytest.fail('halted checkpoint'))
    assert outcome.exit_reason == 'tool_timeout' and outcome.consecutive_tool_timeouts == 2


def test_terminal_failure_signature_survives_split():
    saved = []
    def checkpoint(value): saved.append(deepcopy(value)); raise Interrupted()
    args = dict(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
        execute_tools=lambda *args: {'nonretryable_failure_signatures': ['experiment:denied']},
        max_consecutive_nonretryable_failure_rounds=2)
    with pytest.raises(Interrupted): run_agent_loop(**args, on_checkpoint=checkpoint)
    outcome = run_agent_loop(**args, resume_checkpoint=saved[0])
    assert outcome.exit_reason == 'nonretryable_tool_failure'
    assert outcome.consecutive_nonretryable_failure_rounds == 2


@pytest.mark.parametrize('field,value', [('schema_version', True), ('next_round', True),
    ('next_round', 0), ('retry_bonus_used', 3), ('progress', {}),
    ('claimed_tool_call_ids', ['same', 'same']), ('limits', {})])
def test_malformed_checkpoint_is_refused_before_dispatch(field, value):
    checkpoint, _ = checkpoint_after(max_consecutive_no_progress_rounds=3)
    checkpoint[field] = value
    with pytest.raises(ValueError):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[],
            dispatch=lambda *args: pytest.fail('dispatch before validation'),
            max_consecutive_no_progress_rounds=3, resume_checkpoint=checkpoint)


def test_changing_breaker_limits_cannot_reset_history():
    checkpoint, _ = checkpoint_after(max_consecutive_no_progress_rounds=3)
    with pytest.raises(ValueError, match='incompatible'):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
            max_consecutive_no_progress_rounds=0, resume_checkpoint=checkpoint)


def test_round_end_refusal_prevents_checkpoint_and_next_dispatch():
    dispatched = []
    def refuse(rnd): raise Interrupted()
    with pytest.raises(Interrupted):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[],
            dispatch=lambda rnd, tools: dispatched.append(rnd) or turn(rnd, tools),
            execute_tool=lambda *args: 'result', on_round_end=refuse,
            on_checkpoint=lambda *args: pytest.fail('unsettled checkpoint'))
    assert dispatched == [0]


@pytest.mark.parametrize('field,value', [('maximum_evidence_ids', 2049),
    ('last_call_signature', 'broken'), ('observed_evidence_ids', ['same', 'same']),
    ('no_progress_streak', True), ('last_evidence_signature', '')])
def test_corrupt_progress_history_is_refused(field, value):
    checkpoint, _ = checkpoint_after(max_consecutive_no_progress_rounds=3)
    checkpoint['progress'][field] = value
    with pytest.raises(ValueError):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[],
            dispatch=lambda *args: pytest.fail('dispatch before validation'),
            max_consecutive_no_progress_rounds=3, resume_checkpoint=checkpoint)


def test_sink_mutation_cannot_reset_live_progress():
    def mutate(value):
        value['progress']['no_progress_streak'] = 0
        value['claimed_tool_call_ids'].clear()
        value['limits'].clear()
    outcome = run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=turn,
        execute_tool=lambda *args: 'same', max_consecutive_no_progress_rounds=2,
        on_checkpoint=mutate)
    assert outcome.exit_reason == 'no_progress' and outcome.rounds == 3
