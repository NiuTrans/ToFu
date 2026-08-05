# Incident anchor: born in commit 1fb1e5c6 — refactor(orchestrator): pt_03f4cdf1 slice 36 — extract continue-toolH...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity guards for pt_03f4cdf1 slice 36 — extract the
continue-toolHistory injection + the memory-prefetch eligibility drift
guard from _run.py into
lib.tasks_pkg.orchestrator._tool_history.inject_continue_tool_history().

The unit runs once per invocation, after context injection and before
resume-state hydration:

    1. ``inject_tool_history(messages, cfg, task, model)`` — restores
       interrupted tool-call context from the continue checkpoint;
       returns the injected count.
    2. On a non-zero count: ``rs.tool_call_happened = True`` AND
       ``rs.tool_round_num = <count>`` (offset so new roundNums don't
       conflict with the restored ones).
    3. Eligibility drift guard: the early memory-prefetch spawn used
       ``len(cfg['toolHistory'])`` as its eligibility input; if the
       injected count disagrees, WARN — inject_tool_history no longer
       derives its count from that key alone (the spawn's skip
       decision may silently flip).

Failing-first: written BEFORE the extraction; the signature/delegation
guards turn RED until the leaf function exists and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_tool_history.py'


# ---------------------------------------------------------------------------
# 1. leaf exposes the helper
# ---------------------------------------------------------------------------
def test_tool_history_exposes_inject_helper():
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._tool_history')
    assert hasattr(mod, 'inject_continue_tool_history') \
        and callable(mod.inject_continue_tool_history), (
            '_tool_history must export inject_continue_tool_history')


# ---------------------------------------------------------------------------
# 2. _run.py delegates; returns the injected count
# ---------------------------------------------------------------------------
def test_run_task_delegates_with_count_rebind():
    src = RUN_PY.read_text()
    assert '_injected_tool_calls = inject_continue_tool_history(' in src, (
        '_run.py must delegate with the count rebind')


def test_run_py_no_inline_inject_block():
    src = RUN_PY.read_text()
    assert 'inject_tool_history(messages, cfg, task, model)' not in src, (
        'the inject call must live in _tool_history.py')
    assert 'rs.tool_round_num = _injected_tool_calls' not in src, (
        'the roundNum offset stamp must live in _tool_history.py')
    assert 'memory-prefetch eligibility drift' not in src, (
        'the drift guard must live in _tool_history.py')


# ---------------------------------------------------------------------------
# 3. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_carries_inject_stamps_and_guard():
    src = LEAF_PY.read_text()
    assert 'inject_tool_history(' in src
    assert 'rs.tool_call_happened = True' in src
    assert 'rs.tool_round_num' in src
    assert 'memory-prefetch eligibility drift' in src
    assert 'cfg.get(\'toolHistory\')' in src or 'cfg.get("toolHistory")' in src


# ---------------------------------------------------------------------------
# 4. BEHAVIOURAL: the stamp branch + drift guard (owner directive)
# ---------------------------------------------------------------------------
def _drive(leaf, monkeypatch, count, tool_history):
    monkeypatch.setattr(leaf, 'inject_tool_history',
                        lambda messages, cfg, task, model: count)
    cfg = {'toolHistory': tool_history}

    class RS:
        tool_call_happened = False
        tool_round_num = 0
    rs = RS()
    out = leaf.inject_continue_tool_history(
        task={}, rs=rs, messages=[], cfg=cfg, model='m', tid='deadbeef')
    return out, rs


def test_behaviour_nonzero_count_stamps_rs(monkeypatch):
    import lib.tasks_pkg.orchestrator._tool_history as leaf
    out, rs = _drive(leaf, monkeypatch, 3, [1, 2, 3])
    assert out == 3, 'returns the injected count'
    assert rs.tool_call_happened is True
    assert rs.tool_round_num == 3, (
        'roundNum offsets by the injected count so new rounds do not '
        'conflict with the restored ones')


def test_behaviour_zero_count_no_stamp(monkeypatch):
    import lib.tasks_pkg.orchestrator._tool_history as leaf
    out, rs = _drive(leaf, monkeypatch, 0, [])
    assert out == 0
    assert rs.tool_call_happened is False, (
        'zero injections must NOT set tool_call_happened')
    assert rs.tool_round_num == 0


def test_behaviour_drift_guard_warns_on_mismatch(monkeypatch, caplog):
    """injected=0 but cfg['toolHistory'] non-empty (or vice versa) → WARN;
    agreement → no WARN."""
    import lib.tasks_pkg.orchestrator._tool_history as leaf
    import logging
    with caplog.at_level(logging.WARNING):
        _drive(leaf, monkeypatch, 0, [1, 2])
    assert any('eligibility drift' in r.message for r in caplog.records), (
        'a mismatch between the injected count and cfg[toolHistory] '
        'must warn (the spawn used the latter for eligibility)')
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _drive(leaf, monkeypatch, 2, [1, 2])
    assert not any('eligibility drift' in r.message for r in caplog.records), (
        'agreement must NOT warn')
