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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tool_input_repair import (
    build_rejection_message, classify_tool_call, suggest_tool_names,
)
from lib.tasks_pkg.tool_dispatch import (
    _known_tool_names, parse_tool_calls, execute_tool_pipeline,
)


_KNOWN = {'web_search', 'fetch_url', 'read_files', 'grep_search', 'find_files'}


def _schema(names):
    return [{'type': 'function', 'function': {'name': n, 'parameters': {}}}
            for n in names]


def _make_task(tool_names):
    return {
        'id': 'task_hallu_' + 'x' * 8,
        'convId': 'convhallu',
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
