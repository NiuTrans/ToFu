"""Budget diagnostics never enter model context; legacy meta carriers stay transparent.

The token-estimate seam is pinned for deterministic, DB-free tests.
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
from lib.tasks_pkg.compaction._constants import _CONTEXT_BUDGET_DIAGNOSTIC_RATIO
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


class TestContextBudgetDiagnostics(_EstimateSandbox):

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        self.audit = patch.object(_pipeline, 'audit_log').start()
        self.addCleanup(patch.stopall)

    def test_below_threshold_no_diagnostic(self):
        self._used = int(_USABLE * _CONTEXT_BUDGET_DIAGNOSTIC_RATIO) - 1
        task, msgs = _task(), self._msgs()
        _pipeline._maybe_record_context_budget(msgs, task)
        self.audit.assert_not_called()
        self.assertNotIn('_contextBudgetDiagnosticRecorded', task)
        self.assertEqual(msgs, self._msgs())

    def test_threshold_is_content_free_and_bounded(self):
        self._used = int(_USABLE * _CONTEXT_BUDGET_DIAGNOSTIC_RATIO)
        task, msgs = _task(), self._msgs()
        for current_round in range(4, 1004):
            _pipeline._maybe_record_context_budget(
                msgs, task, current_round=current_round)
        self.audit.assert_called_once_with(
            'context_budget', round=4, context_limit_tokens=1000,
            usable_tokens=1000, used_tokens=700, remaining_tokens=300,
            over_budget_tokens=0, remaining_usable_pct=30,
            diagnostic_ratio=0.70, token_count_kind='estimated',
            measurement_scope='working_messages',
            capacity_owner='automatic_compaction')
        self.assertEqual(msgs, self._msgs())
        self.assertNotIn('_injected_notes', task)
        self.assertEqual(set(task) - set(_task()),
                         {'_contextBudgetDiagnosticRecorded', '_contextManifest'})
        row = task['_contextManifest'][0]
        self.assertEqual(row['registryId'], 'context_budget')
        self.assertFalse(row['injected'])
        self.assertEqual(row['tokens'], 0)
        self.assertEqual(row['metrics']['usableTokens'], 1000)

    def test_reset_allows_one_fresh_diagnostic_per_window(self):
        self._used = _USABLE - 50
        task, msgs = _task(), self._msgs()
        _pipeline._maybe_record_context_budget(msgs, task)
        task['_contextBudgetDiagnosticRecorded'] = False
        _pipeline._maybe_record_context_budget(msgs, task)
        self.assertEqual(self.audit.call_count, 2)
        self.assertEqual(msgs, self._msgs())

    def test_no_task_is_noop(self):
        msgs = self._msgs()
        _pipeline._maybe_record_context_budget(msgs, None)
        self.audit.assert_not_called()
        self.assertEqual(msgs, self._msgs())

    def test_over_limit_is_diagnostic_not_instruction(self):
        self._used = _USABLE + 10
        task, msgs = _task(), self._msgs()
        _pipeline._maybe_record_context_budget(msgs, task)
        self.assertEqual(self.audit.call_args.kwargs['remaining_tokens'], 0)
        self.assertEqual(self.audit.call_args.kwargs['over_budget_tokens'], 10)
        self.assertEqual(msgs, self._msgs())

    def test_telemetry_failure_does_not_affect_request_or_retry_unboundedly(self):
        self._used = 900
        self.audit.side_effect = RuntimeError('offline')
        task, msgs = _task(), self._msgs()
        _pipeline._maybe_record_context_budget(msgs, task)
        _pipeline._maybe_record_context_budget(msgs, task)
        self.audit.assert_called_once()
        self.assertEqual(msgs, self._msgs())

    def test_precomputed_measurement_is_reused(self):
        task, msgs = _task(), self._msgs()
        _pipeline._maybe_record_context_budget(msgs, task, used_tokens=800)
        self.assertEqual(self.audit.call_args.kwargs['used_tokens'], 800)


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


def test_pipeline_reuses_l2_measurement_for_noop_diagnostic(monkeypatch):
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
