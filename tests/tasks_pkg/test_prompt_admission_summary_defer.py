"""Summary failure at the admission guard defers when the prompt still fits.

TB2.1 v10 (2026-09-17): six trials died at ~128K of a 200K window when a
transient ``transport_error`` hit the L2 summary call under endpoint load —
the admission guard treated any summary failure as task-fatal even though
prompt + max_tokens still physically fit the window. The guard must now
proceed uncompacted (compaction retries on later rounds) whenever the
prompt physically fits, and keep the fatal path only for genuine overflow.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _big_messages(target_tokens: int) -> list[dict]:
    # ~4 chars per token of filler across a realistic shape
    filler = 'x' * (target_tokens * 4 - 200)
    return [
        {'role': 'system', 'content': 'You are a coding agent.'},
        {'role': 'user', 'content': 'Fix the bug.'},
        {'role': 'assistant', 'content': filler},
        {'role': 'user', 'content': 'continue'},
    ]


def _task() -> dict:
    return {
        'id': 'admission-defer-1',
        'convId': 'admission-defer',
        '_userId': 1,
        'config': {'model': 'glm-5.3-m17', 'maxTokens': 65536,
                   'compaction': {}},
    }


def _patch_common(monkeypatch, admission, context_limit):
    def resolve(task, *, round_num, input_tokens=None):
        # Simulate an advisory rewrite target below a physical safety ceiling.
        # Deferral cannot borrow raw output or bypass that safety boundary.
        task['_contextBudgetDecision'] = {
            'contextWindowTokens': context_limit,
            'windowSafetyTokens': 140_000,
            'resolvedOutputTokens': 65_536,
        }
        return 140_000, 60_000
    monkeypatch.setattr(admission, '_resolved_ceiling', resolve)
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: False)
    monkeypatch.setattr(
        admission, 'restore_checklist_context', lambda *a, **k: None)
    monkeypatch.setattr(
        admission, '_measure',
        lambda messages, tools, task, **kw: {
            'totalTokens': 128_000, 'toolSchemaTokens': 0})
    import lib.tasks_pkg.compaction._tokens as tokens_mod
    monkeypatch.setattr(
        tokens_mod, '_get_context_limit', lambda task: context_limit)


def test_summary_failure_defers_when_prompt_physically_fits(monkeypatch):
    from lib.tasks_pkg.compaction import _prompt_admission as admission

    _patch_common(monkeypatch, admission, 200_000)
    task = _task()
    task['_summaryBudgetFailure'] = 'summary_budget_exhausted'
    measurement = admission.enforce_dispatch_prompt_limit(
        _big_messages(128_000), [], task, round_num=5, model='glm-5.3-m17')
    # 128K + 65536 < 200K → proceed, not fatal.
    assert measurement['totalTokens'] == 128_000


def test_summary_failure_stays_fatal_when_prompt_no_longer_fits(monkeypatch):
    from lib.tasks_pkg.compaction import _prompt_admission as admission

    _patch_common(monkeypatch, admission, 130_000)
    task = _task()
    task['_summaryBudgetFailure'] = 'summary_dispatch_failed'
    with pytest.raises(admission.ContextCompactionError):
        admission.enforce_dispatch_prompt_limit(
            _big_messages(128_000), [], task, round_num=5,
            model='glm-5.3-m17')
