"""Chassis guard: consecutive-identical-round circuit breaker.

WHY
---
2026-07-27 incident: ONE swarm sub-agent (`agent-researcher-6b62ec57`) spun
26,683,114 rounds in 3.5h and wrote 53,366,229 lines = 9.1 GB into
logs/app.log (96% of the whole file). Every round looked like:

    Round N LLM done in 0.0s — stop=tool_calls content_len=0 ... total_tokens=2N

i.e. the model asked for the SAME tool call every round, forever, and the
the loop had no semantic no-progress guard, while the optional wall-clock
guard was disabled.

MEASURED NON-SIGNATURE (do not "fix" this by watching empty content):
across the 07-24..07-26 logs, EXCLUDING the runaway, `content_len == 0`
accounts for **866 of 1723 rounds (50.3%)** — an empty-content round is the
NORMAL shape of a pure tool-calling turn. Halting on it would kill half of
all real agents. The distinguishing property of a wedged loop is
REPETITION, not silence: the same tool-call fingerprint recurring with no
intervening progress.

So the chassis counts CONSECUTIVE rounds whose tool-call fingerprint is
identical to the previous round's and halts at the threshold with
`exit_reason='no_progress'` — structurally the same shape as the existing
`max_consecutive_tool_timeouts` breaker (consecutive counter → halted=True
+ exit_reason), per the charter's "fix the chassis, not the caller" rule.

NEUTER evidence expected: deleting the fingerprint comparison (always
treating a round as progress) must make `test_identical_rounds_halt` red.
"""

from __future__ import annotations

import os
import sys
import unittest

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _tc(name='web_search', args='{}', _id='t1'):
    return {'id': _id, 'function': {'name': name, 'arguments': args}}


def _tool_turn(calls):
    return {'role': 'assistant', 'content': '', 'tool_calls': calls}, \
        'tool_calls', {'total_tokens': 2}


def _final_turn(text='done'):
    return {'role': 'assistant', 'content': text}, 'stop', {'total_tokens': 2}


class TestNoProgressBreaker(unittest.TestCase):

    def test_identical_rounds_halt(self):
        """The incident shape: same tool call every round → halt, not 2**30."""
        from lib.agent_loop import AbortSignal, run_agent_loop

        calls = {'n': 0}

        def dispatch(rnd, tools):
            calls['n'] += 1
            return _tool_turn([_tc()])          # byte-identical forever

        outcome = run_agent_loop(
            abort=AbortSignal.never(),
            round_tools=None,
            dispatch=dispatch,
            execute_tools=lambda rnd, tcs: {
                'progress_evidence_ids': ['same-visible-result'],
                'result_evidence_complete': True,
            },
            max_consecutive_no_progress_rounds=5,
        )

        self.assertTrue(outcome.halted, 'breaker must halt the loop')
        self.assertEqual(outcome.exit_reason, 'no_progress')
        self.assertFalse(outcome.completed)
        self.assertFalse(outcome.aborted)
        # Bounded: the 6th identical round trips it (1 baseline + 5 repeats).
        self.assertEqual(calls['n'], 6, 'must stop at the threshold, not spin')

    def test_empty_content_alone_is_not_no_progress(self):
        """MEASURED 50.3% of real rounds have content_len==0 — a pure
        tool-calling turn with VARYING calls must never trip the breaker."""
        from lib.agent_loop import AbortSignal, run_agent_loop

        seq = [
            _tool_turn([_tc('web_search', '{"q":"a"}')]),
            _tool_turn([_tc('web_search', '{"q":"b"}')]),
            _tool_turn([_tc('fetch_url', '{"u":"x"}')]),
            _tool_turn([_tc('read_files', '{"p":"y"}')]),
            _final_turn('the substantive answer'),
        ]
        calls = {'n': 0}

        def dispatch(rnd, tools):
            m = seq[calls['n']]
            calls['n'] += 1
            return m

        outcome = run_agent_loop(
            abort=AbortSignal.never(),
            round_tools=None,
            dispatch=dispatch,
            execute_tools=lambda rnd, tcs: None,
            max_consecutive_no_progress_rounds=2,
        )

        self.assertTrue(outcome.completed,
                        'varying tool calls with empty content are PROGRESS')
        self.assertEqual(outcome.exit_reason, 'completed')
        self.assertFalse(outcome.halted)
        self.assertEqual(calls['n'], 5)

    def test_repeat_streak_resets_on_progress(self):
        """A repeat that is broken by a different call must reset the counter,
        so a legitimately retrying agent is not killed by an old streak."""
        from lib.agent_loop import AbortSignal, run_agent_loop

        seq = [
            _tool_turn([_tc('web_search', '{"q":"a"}')]),
            _tool_turn([_tc('web_search', '{"q":"a"}')]),   # repeat 1
            _tool_turn([_tc('web_search', '{"q":"a"}')]),   # repeat 2
            _tool_turn([_tc('fetch_url', '{"u":"x"}')]),    # RESET
            _tool_turn([_tc('fetch_url', '{"u":"x"}')]),    # repeat 1 again
            _final_turn('answer'),
        ]
        calls = {'n': 0}

        def dispatch(rnd, tools):
            m = seq[calls['n']]
            calls['n'] += 1
            return m

        outcome = run_agent_loop(
            abort=AbortSignal.never(),
            round_tools=None,
            dispatch=dispatch,
            execute_tools=lambda rnd, tcs: {
                'progress_evidence_ids': ['same-visible-result'],
                'result_evidence_complete': True,
            },
            max_consecutive_no_progress_rounds=3,
        )

        self.assertTrue(outcome.completed, outcome.exit_reason)
        self.assertEqual(calls['n'], 6)

    def test_identical_poll_with_changing_visible_result_is_productive(self):
        """Same call is not a loop when the model receives advancing state."""
        from lib.agent_loop import AbortSignal, run_agent_loop

        calls = {'n': 0}

        def dispatch(rnd, tools):
            calls['n'] += 1
            if calls['n'] == 7:
                return _final_turn('external job completed')
            return _tool_turn([_tc('fetch_url', '{"u":"status"}')])

        outcome = run_agent_loop(
            abort=AbortSignal.never(),
            round_tools=None,
            dispatch=dispatch,
            execute_tools=lambda rnd, tcs: {
                'progress_evidence_ids': [f'status-{rnd}'],
                'result_evidence_complete': True,
            },
            max_consecutive_no_progress_rounds=2,
        )

        self.assertTrue(outcome.completed, outcome.exit_reason)
        self.assertEqual(outcome.exit_reason, 'completed')
        self.assertEqual(calls['n'], 7)

    def test_missing_result_evidence_fails_open(self):
        """Call equality alone cannot prove that repeated execution stalled."""
        from lib.agent_loop import AbortSignal, run_agent_loop

        calls = {'n': 0}

        def dispatch(rnd, tools):
            calls['n'] += 1
            if calls['n'] == 7:
                return _final_turn('finished after opaque tools')
            return _tool_turn([_tc()])

        outcome = run_agent_loop(
            abort=AbortSignal.never(),
            round_tools=None,
            dispatch=dispatch,
            execute_tools=lambda rnd, tcs: None,
            max_consecutive_no_progress_rounds=2,
        )

        self.assertTrue(outcome.completed, outcome.exit_reason)
        self.assertEqual(calls['n'], 7)

    def test_breaker_off_by_default(self):
        """Backwards compatibility: 0 (default) disables the breaker, so every
        existing adopter keeps byte-identical behaviour."""
        from lib.agent_loop import AbortSignal, run_agent_loop

        calls = {'n': 0}

        def dispatch(rnd, tools):
            calls['n'] += 1
            if calls['n'] > 4:
                return _final_turn('stop now')
            return _tool_turn([_tc()])          # identical repeats

        outcome = run_agent_loop(
            abort=AbortSignal.never(),
            round_tools=None,
            dispatch=dispatch,
            execute_tools=lambda rnd, tcs: None,
        )

        self.assertTrue(outcome.completed)
        self.assertFalse(outcome.halted)
        self.assertEqual(calls['n'], 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
