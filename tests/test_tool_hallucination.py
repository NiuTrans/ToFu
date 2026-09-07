"""Unified hallucinated-tool rejection — backend behavior contract.

Covers the single rejection path added 2026-06:
  * lib/tool_input_repair.classify_tool_call / suggest_tool_names /
    build_rejection_message — pure classification + message helpers.
  * lib/tasks_pkg/tool_dispatch.parse_tool_calls — a tool name that is neither
    a real session tool nor an aliasable synonym is classified as a
    hallucination: the round is stamped status='rejected' + _rejected, a
    standardized rejection message is set as the parse-error (so the call is
    NEVER dispatched), and execute_tool_pipeline keeps the 'rejected' status.

Run directly (the conda env's pytest is broken — see
tool-name-alias-repair-layer memory):

    python3 tests/test_tool_hallucination.py
"""

import os
import sys
import threading
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tool_input_repair import (
    HALLUCINATION_ABORT_THRESHOLD, REJECTION_ESCALATE_THRESHOLD,
    build_rejection_message, classify_tool_call, clear_rejection,
    record_rejection, suggest_tool_names,
)
from lib.tasks_pkg.tool_dispatch.api import (
    execute_tool_pipeline,
    parse_tool_calls,
)
from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names
import lib.tool_input_repair as _tir


pytestmark = pytest.mark.unit


_KNOWN = {'web_search', 'fetch_url', 'read_files', 'grep_search', 'find_files'}


def _schema(names):
    return [{'type': 'function', 'function': {'name': n, 'parameters': {}}}
            for n in names]


def _make_task(tool_names):
    return {
        'id': 'task_hallu_' + 'x' * 8,
        'convId': 'convhallu',
        '_userId': 1,
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        'toolRounds': [],
        'aborted': False,
        '_tool_schema': _schema(tool_names),
    }


def _assistant(tool_calls):
    return {'content': '', 'tool_calls': tool_calls}


def _tc(name, args='{}', tc_id=None):
    return {'id': tc_id or ('call_' + name), 'type': 'function',
            'function': {'name': name, 'arguments': args}}


class TestClassifier(unittest.TestCase):
    def test_real_tool_not_flagged(self):
        self.assertIsNone(classify_tool_call('web_search', _KNOWN))

    def test_fake_tool_flagged_with_suggestion(self):
        d = classify_tool_call('search_web', _KNOWN)
        self.assertIsNotNone(d)
        self.assertEqual(d['kind'], 'hallucinated')
        self.assertEqual(d['attempted'], 'search_web')
        self.assertIn('web_search', d['suggestions'])

    def test_nonsense_no_false_suggestion(self):
        d = classify_tool_call('zqxwhatever_nope', _KNOWN)
        self.assertEqual(d['suggestions'], [])

    def test_suggest_respects_threshold(self):
        self.assertEqual(suggest_tool_names('totally_unrelated_xyz', _KNOWN), [])
        self.assertIn('read_files', suggest_tool_names('read_file', _KNOWN))

    def test_message_mentions_suggestions_and_not_executed(self):
        msg = build_rejection_message(classify_tool_call('search_web', _KNOWN))
        self.assertIn('not a real tool', msg)
        self.assertIn('NOT executed', msg)
        self.assertIn('web_search', msg)


class TestKnownToolNames(unittest.TestCase):
    def test_uses_schema_snapshot(self):
        task = _make_task(['web_search', 'mcp__foo__bar'])
        names = _known_tool_names(task)
        self.assertIn('web_search', names)
        self.assertIn('mcp__foo__bar', names)

    def test_falls_back_to_registry_when_no_schema(self):
        # No _tool_schema → registry harvest. Built-ins must be present.
        names = _known_tool_names({'id': 'x'})
        self.assertIn('read_files', names)


class TestParseRejectsHallucination(unittest.TestCase):
    def test_fake_tool_rejected_not_dispatched(self):
        task = _make_task(['web_search', 'read_files'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('search_web', '{"query": "x"}')]),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        self.assertEqual(len(parsed), 1)
        tc, fn_name, tc_id, fn_args, rn, round_entry, parse_err = parsed[0]
        # A parse-error short-circuits execution in execute_tool_pipeline.
        self.assertTrue(parse_err)
        self.assertIn('not a real tool', parse_err)
        # The round is stamped rejected with the descriptor.
        self.assertEqual(round_entry['status'], 'rejected')
        self.assertEqual(round_entry['_rejected']['attempted'], 'search_web')
        self.assertIn('web_search', round_entry['_rejected']['suggestions'])

    def test_real_tool_not_rejected(self):
        task = _make_task(['web_search', 'read_files'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('web_search', '{"query": "x"}')]),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        _, fn_name, _, _, _, round_entry, parse_err = parsed[0]
        self.assertEqual(fn_name, 'web_search')
        self.assertIsNone(parse_err)
        self.assertNotEqual(round_entry.get('status'), 'rejected')
        self.assertNotIn('_rejected', round_entry)

    def test_aliasable_name_is_repaired_not_rejected(self):
        # read_file → read_files via the alias table; must NOT be rejected.
        task = _make_task(['read_files', 'web_search'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('read_file', '{"path": "x"}')]),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        _, fn_name, _, _, _, round_entry, parse_err = parsed[0]
        self.assertEqual(fn_name, 'read_files')
        self.assertIsNone(parse_err)
        self.assertNotEqual(round_entry.get('status'), 'rejected')

    def test_mcp_tool_in_schema_not_rejected(self):
        # An MCP tool present in the live schema must be recognised even
        # though it isn't a built-in.
        task = _make_task(['mcp__tavily__search', 'read_files'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('mcp__tavily__search', '{"q": "x"}')]),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        _, fn_name, _, _, _, round_entry, parse_err = parsed[0]
        self.assertEqual(fn_name, 'mcp__tavily__search')
        self.assertIsNone(parse_err)
        self.assertNotEqual(round_entry.get('status'), 'rejected')


class TestPipelinePreservesRejected(unittest.TestCase):
    def test_pipeline_keeps_rejected_status_and_returns_message(self):
        task = _make_task(['web_search'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('search_web', '{"query": "x"}')]),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        timed_out = execute_tool_pipeline(
            task, parsed, cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=None, messages=[],
            all_search_results_text=[], round_num=0, model='test-model',
        )
        self.assertFalse(timed_out)
        round_entry = parsed[0][5]
        # Status must stay 'rejected' (NOT flipped to 'done' by finalize).
        self.assertEqual(round_entry['status'], 'rejected')
        # Result meta carries the rejected descriptor.
        meta = (round_entry.get('results') or [])[0]
        self.assertIsNotNone(meta)
        self.assertIn('rejected', meta)
        self.assertEqual(meta['rejected']['attempted'], 'search_web')


class TestRepeatRejectionEscalation(unittest.TestCase):
    """The circuit breaker for a no-suggestion phantom looped N× (the
    screenshot's module_buffer_manager ×7)."""

    def setUp(self):
        # The repeat counter is process-global; isolate each test.
        _tir._REJECT_COUNTS.clear()

    def tearDown(self):
        _tir._REJECT_COUNTS.clear()

    def test_first_rejection_is_generic_no_tool_list(self):
        d = classify_tool_call('module_buffer_manager', _KNOWN)
        msg = build_rejection_message(d, repeat_count=1, known_tools=_KNOWN)
        self.assertIn('not a real tool', msg)
        # First strike: NO enumerated tool list, no "STOP calling".
        self.assertNotIn('ONLY tools you may call', msg)
        self.assertNotIn('`web_search`', msg)

    def test_repeat_rejection_injects_real_tool_list(self):
        d = classify_tool_call('module_buffer_manager', _KNOWN)
        msg = build_rejection_message(
            d, repeat_count=REJECTION_ESCALATE_THRESHOLD, known_tools=_KNOWN)
        # Escalated: enumerates the real tools so the model has a target.
        self.assertIn('ONLY tools you may call', msg)
        self.assertIn('STOP calling', msg)
        self.assertIn('`web_search`', msg)
        self.assertIn('`read_files`', msg)

    def test_escalation_only_when_no_suggestions(self):
        # A name WITH a near suggestion never gets the tool-list dump even on
        # repeat — it already has a concrete correction direction.
        d = classify_tool_call('search_web', _KNOWN)
        self.assertTrue(d['suggestions'])
        msg = build_rejection_message(
            d, repeat_count=REJECTION_ESCALATE_THRESHOLD + 2, known_tools=_KNOWN)
        self.assertIn('web_search', msg)
        self.assertNotIn('ONLY tools you may call', msg)

    def test_record_and_clear_rejection_counts(self):
        self.assertEqual(record_rejection('c1', 'foo'), 1)
        self.assertEqual(record_rejection('c1', 'foo'), 2)
        # Distinct conv → independent streak.
        self.assertEqual(record_rejection('c2', 'foo'), 1)
        clear_rejection('c1', 'foo')
        self.assertEqual(record_rejection('c1', 'foo'), 1)

    def test_dispatch_injects_tool_list_on_repeat(self):
        """The screenshot scenario: same phantom across consecutive rounds —
        the >=Nth rejection message carries the real tool list."""
        task = _make_task(['web_search', 'read_files', 'grep_search'])
        last_err = None
        for _ in range(REJECTION_ESCALATE_THRESHOLD):
            parsed, _ = parse_tool_calls(
                _assistant([_tc('module_buffer_manager', '{}')]),
                task, round_num=0, tool_round_num=0, project_enabled=False,
            )
            last_err = parsed[0][6]
        self.assertIsNotNone(last_err)
        self.assertIn('ONLY tools you may call', last_err)
        self.assertIn('`read_files`', last_err)

    def test_real_tool_call_resets_streak(self):
        task = _make_task(['web_search', 'read_files'])
        # Two phantom rejections build a streak.
        for _ in range(2):
            parse_tool_calls(
                _assistant([_tc('phantomzz', '{}')]),
                task, round_num=0, tool_round_num=0, project_enabled=False,
            )
        self.assertEqual(_tir._REJECT_COUNTS.get(('convhallu', 'phantomzz')), 2)
        # Now a REAL tool by that very name would reset — simulate by calling
        # the same string as a registered tool.
        task2 = _make_task(['phantomzz', 'read_files'])
        task2['convId'] = 'convhallu'
        parse_tool_calls(
            _assistant([_tc('phantomzz', '{}')]),
            task2, round_num=0, tool_round_num=0, project_enabled=False,
        )
        self.assertNotIn(('convhallu', 'phantomzz'), _tir._REJECT_COUNTS)


class TestAutopilotLoopBreaker(unittest.TestCase):
    """Under autopilot, a repeated no-suggestion phantom aborts the task so the
    follow-up loop ends instead of burning rounds.

    CRITICAL decoupling (2026-06-30): the tool-list INJECTION threshold
    (REJECTION_ESCALATE_THRESHOLD=2) and the hard-ABORT threshold
    (HALLUCINATION_ABORT_THRESHOLD=4) MUST differ — otherwise the task would
    abort in the SAME round the list is injected and the model would never get
    a turn to USE it (graceful recovery = dead code under autopilot)."""

    def setUp(self):
        _tir._REJECT_COUNTS.clear()

    def tearDown(self):
        _tir._REJECT_COUNTS.clear()

    def _autopilot_task(self, tool_names):
        t = _make_task(tool_names)
        t['config'] = {'autopilot': True}
        return t

    def _reject_once(self, task):
        parsed, _ = parse_tool_calls(
            _assistant([_tc('module_buffer_manager', '{}')]),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        return parsed[0][6]  # the rejection message (_args_parse_error)

    def test_decoupled_injection_lives_before_abort(self):
        """The decoupling proof: rounds 2 & 3 inject the real tool list AND keep
        the task ALIVE; only round 4 (HALLUCINATION_ABORT_THRESHOLD) aborts."""
        # Sanity: the thresholds must actually be decoupled for this to mean
        # anything (a coupled config would make this test vacuous).
        self.assertGreater(HALLUCINATION_ABORT_THRESHOLD,
                           REJECTION_ESCALATE_THRESHOLD)
        task = self._autopilot_task(['web_search', 'read_files', 'grep_search'])
        # Rounds 2..(abort-1): tool list injected, task still alive.
        for n in range(REJECTION_ESCALATE_THRESHOLD,
                       HALLUCINATION_ABORT_THRESHOLD):
            # Replay so the streak reaches exactly n this iteration.
            _tir._REJECT_COUNTS[('convhallu', 'module_buffer_manager')] = n - 1
            msg = self._reject_once(task)
            self.assertIn('ONLY tools you may call', msg,
                          f'round {n}: expected the injected tool list')
            self.assertIn('`read_files`', msg)
            self.assertFalse(task.get('aborted'),
                             f'round {n}: task must stay ALIVE so the model can '
                             f'use the injected list (abort at {HALLUCINATION_ABORT_THRESHOLD})')
        # The abort round: streak hits HALLUCINATION_ABORT_THRESHOLD → abort.
        _tir._REJECT_COUNTS[('convhallu', 'module_buffer_manager')] = \
            HALLUCINATION_ABORT_THRESHOLD - 1
        self._reject_once(task)
        self.assertTrue(task.get('aborted'))
        self.assertEqual(task.get('_abort_reason'), 'hallucination_loop')

    def test_no_abort_at_escalate_threshold(self):
        """At exactly the escalate threshold (2), the task must NOT abort —
        this is the regression guard for the original coupled bug."""
        task = self._autopilot_task(['web_search', 'read_files'])
        for _ in range(REJECTION_ESCALATE_THRESHOLD):
            self._reject_once(task)
        self.assertFalse(task.get('aborted'))

    def test_autopilot_aborts_at_abort_threshold(self):
        task = self._autopilot_task(['web_search', 'read_files'])
        for _ in range(HALLUCINATION_ABORT_THRESHOLD):
            self._reject_once(task)
        self.assertTrue(task.get('aborted'))
        self.assertEqual(task.get('_abort_reason'), 'hallucination_loop')

    def test_non_autopilot_never_aborts(self):
        task = _make_task(['web_search', 'read_files'])  # no autopilot config
        for _ in range(HALLUCINATION_ABORT_THRESHOLD + 2):
            self._reject_once(task)
        self.assertFalse(task.get('aborted'))

    def test_first_strike_does_not_abort(self):
        task = self._autopilot_task(['web_search'])
        self._reject_once(task)
        self.assertFalse(task.get('aborted'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
