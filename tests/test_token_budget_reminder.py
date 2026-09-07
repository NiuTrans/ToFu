"""tests/test_token_budget_reminder.py — Token-budget reminder unit tests.

Covers the Codex-inspired additions (borrow list batch 1):

  1. ``_maybe_inject_token_budget_reminder`` fires exactly once per context
     window at the configured threshold, appends at the END of the message
     list (cached-prefix safe), and marks the reminder ``_isMeta``.
  2. ``_find_turn_boundary`` treats ``_isMeta`` user messages as transparent:
     a trailing meta reminder must NOT become a turn boundary, or the real
     current turn would lose its "always preserved whole" invariant.

The token-estimate seam is monkeypatched on the ``_pipeline`` module
namespace so the tests are deterministic and DB-free.
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit

# Boot the Flask→Quart shim BEFORE any lib.* imports (see test_hook_taxonomy).
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location(
    'server_for_shim_budget_test', 'server.py')
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
del _spec, _mod, _importlib_util

import lib.tasks_pkg.compaction._pipeline as _pipeline
from lib.tasks_pkg.compaction._constants import _TOKEN_BUDGET_REMINDER_RATIO
from lib.tasks_pkg.compaction._layer2._anchor import _find_turn_boundary

_USABLE = 1000


def _task():
    return {'id': 'tb-001', 'convId': 'cb-001', 'config': {}}


class _EstimateSandbox(unittest.TestCase):
    """Pin the pipeline's token-estimate seam to deterministic values."""

    def setUp(self):
        self._orig = (_pipeline._estimate_total_tokens,
                      _pipeline._usable_context,
                      _pipeline._get_context_limit)
        self._used = 0
        _pipeline._estimate_total_tokens = lambda msgs: self._used
        _pipeline._usable_context = lambda limit: limit
        _pipeline._get_context_limit = lambda task=None: _USABLE

    def tearDown(self):
        (_pipeline._estimate_total_tokens,
         _pipeline._usable_context,
         _pipeline._get_context_limit) = self._orig

    def _msgs(self):
        return [{'role': 'user', 'content': 'do the thing'}]


class TestTokenBudgetReminder(_EstimateSandbox):

    def test_below_threshold_no_injection(self):
        self._used = int(_USABLE * _TOKEN_BUDGET_REMINDER_RATIO) - 1
        task = _task()
        msgs = self._msgs()
        _pipeline._maybe_inject_token_budget_reminder(msgs, task)
        self.assertEqual(len(msgs), 1)
        self.assertNotIn('_tokenBudgetReminderFired', task)

    def test_fires_once_at_threshold(self):
        self._used = int(_USABLE * _TOKEN_BUDGET_REMINDER_RATIO)
        task = _task()
        msgs = self._msgs()
        _pipeline._maybe_inject_token_budget_reminder(msgs, task)
        self.assertEqual(len(msgs), 2)
        reminder = msgs[-1]
        self.assertEqual(reminder['role'], 'user')
        self.assertTrue(reminder.get('_isMeta'),
                        'reminder must be marked _isMeta so compaction '
                        'treats it as a context carrier, not a human turn')
        self.assertIn('<token_budget>', reminder['content'])
        self.assertTrue(task['_tokenBudgetReminderFired'])
        # Second call: claim blocks a duplicate.
        _pipeline._maybe_inject_token_budget_reminder(msgs, task)
        self.assertEqual(len(msgs), 2)

    def test_reset_allows_one_fresh_reminder_per_window(self):
        self._used = _USABLE - 50
        task = _task()
        msgs = self._msgs()
        _pipeline._maybe_inject_token_budget_reminder(msgs, task)
        self.assertEqual(len(msgs), 2)
        # The pipeline resets the claim after a successful compaction.
        task['_tokenBudgetReminderFired'] = False
        _pipeline._maybe_inject_token_budget_reminder(msgs, task)
        self.assertEqual(len(msgs), 3)

    def test_no_task_is_noop(self):
        self._used = _USABLE
        msgs = self._msgs()
        _pipeline._maybe_inject_token_budget_reminder(msgs, None)
        self.assertEqual(len(msgs), 1)

    def test_over_limit_no_injection(self):
        # remaining <= 0 → the L2 force path owns this situation, not the
        # advisory reminder.
        self._used = _USABLE + 10
        task = _task()
        msgs = self._msgs()
        _pipeline._maybe_inject_token_budget_reminder(msgs, task)
        self.assertEqual(len(msgs), 1)
        self.assertNotIn('_tokenBudgetReminderFired', task)


class TestMetaTurnBoundaryTransparency(unittest.TestCase):
    """A trailing ``_isMeta`` user message must not become a turn boundary."""

    def test_meta_reminder_is_not_a_turn_start(self):
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'real request'},
            {'role': 'assistant', 'content': 'working…'},
            {'role': 'user', 'content': '<token_budget>…</token_budget>',
             '_isMeta': True},
        ]
        boundary = _find_turn_boundary(msgs, budget_tokens=10 ** 9)
        self.assertEqual(boundary, 1,
                         'meta reminder at index 3 must be transparent; the '
                         'current turn still starts at the REAL user message')

    def test_real_user_messages_still_bound_turns(self):
        msgs = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'turn one'},
            {'role': 'assistant', 'content': 'answer one'},
            {'role': 'user', 'content': 'turn two'},
            {'role': 'assistant', 'content': 'working…'},
        ]
        boundary = _find_turn_boundary(msgs, budget_tokens=10 ** 9)
        self.assertEqual(boundary, 1)


def test_pipeline_reuses_l2_measurement_for_noop_reminder(monkeypatch):
    """The common no-compaction round performs no second transcript scan."""
    messages = [{'role': 'user', 'content': 'continue'}]
    task = {'id': 'tb-reuse', 'convId': 'cb-reuse',
            '_userId': 1, 'config': {'model': 'gpt-4'}}

    monkeypatch.setattr(_pipeline, 'micro_compact', lambda *a, **k: 0)

    def force(_messages, **kwargs):
        kwargs['_measurement_out'].update({
            'message_tokens': 123,
            'message_count': len(_messages),
            'gate_tokens': 123,
            'method': 'test',
        })
        return False

    monkeypatch.setattr(_pipeline, 'force_compact_if_needed', force)
    monkeypatch.setattr(
        _pipeline, '_estimate_total_tokens',
        lambda _messages: pytest.fail('pipeline rescanned unchanged messages'))

    _pipeline.run_compaction_pipeline(messages, current_round=1, task=task)

    assert messages == [{'role': 'user', 'content': 'continue'}]


if __name__ == '__main__':
    unittest.main()
