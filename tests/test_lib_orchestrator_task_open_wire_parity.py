"""Wire-parity guards for pt_03f4cdf1 slice 35 — extract the task-open
cluster from _run.py's preamble into
lib.tasks_pkg.orchestrator._task_open (three helpers):

    check_autopilot_kick(task) -> bool
        A carrier task that runs ONLY the virtual-user hook (no worker
        LLM turn). The conversation already ended and the last message
        is the agent's reply, so the simulated user answers it directly
        (lib.tasks_pkg.autopilot._run_autopilot_kick). True → the
        caller returns immediately.

    snapshot_turn_input(task)
        Pristine turn-input snapshot for turn-level auto-retry. run_task
        mutates a LOCAL copy of messages (system-context injection,
        tool-history rebuild, completed tool rounds) and writes it back
        on exit — a transient-error re-run must restore the ORIGINAL
        input first, or it would double-inject system blocks and replay
        a half-finished round. Captured ONCE (never overwritten),
        skipped for _flow_managed tasks.

    log_task_open(task, tid) -> float
        queue_wait timing log (create→run_task, when _t_created is set)
        + the ▶ START bracket (FULL task id for grep-correlation with
        the cost popover). Returns _t_run_start for the caller's later
        _t_prep_done anchor.

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest


pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_task_open.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes all three helpers
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_helpers():
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._task_open')
    for name in ('check_autopilot_kick', 'snapshot_turn_input',
                 'log_task_open'):
        assert hasattr(mod, name) and callable(getattr(mod, name)), (
            f'_task_open must export {name}')


# ---------------------------------------------------------------------------
# 2. _run.py imports and delegates
# ---------------------------------------------------------------------------
def test_run_py_imports_task_open():
    src = RUN_PY.read_text()
    assert 'from lib.tasks_pkg.orchestrator._task_open import' in src, (
        '_run.py must import from _task_open at module scope')


def test_run_task_delegates_all_three():
    src = RUN_PY.read_text()
    assert 'check_autopilot_kick(task)' in src
    assert 'snapshot_turn_input(task)' in src
    assert 'log_task_open(task, tid)' in src


# ---------------------------------------------------------------------------
# 3. inline bodies are gone from _run.py
# ---------------------------------------------------------------------------
def test_run_py_no_inline_open_bodies():
    src = RUN_PY.read_text()
    assert '_run_autopilot_kick(task)' not in src, (
        'the autopilot kick call must live in _task_open.py')
    assert "task['_turn_input_messages']" not in src, (
        'the turn-input snapshot must live in _task_open.py')
    assert 'queue_wait=' not in src, (
        'the queue_wait timing log must live in _task_open.py')


# ---------------------------------------------------------------------------
# 4. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_carries_kick_snapshot_and_logs():
    src = LEAF_PY.read_text()
    assert '_run_autopilot_kick(task)' in src
    assert "_turn_input_messages" in src
    assert '_flow_managed' in src, (
        'the snapshot must skip Flow-managed tasks')
    assert 'queue_wait=' in src
    assert '▶ START' in src, (
        'the START bracket must keep the FULL task id form')


# ---------------------------------------------------------------------------
# 5. BEHAVIOURAL: the branches (owner directive)
# ---------------------------------------------------------------------------
def test_behaviour_kick_branch(monkeypatch):
    """_autopilot_kick set → helper runs the kick and returns True;
    unset → returns False and never imports the autopilot path."""
    import lib.tasks_pkg.orchestrator._task_open as leaf
    calls = []
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.setattr(ap, '_run_autopilot_kick',
                        lambda t: calls.append(t['id']))
    assert leaf.check_autopilot_kick({'id': 'x', '_autopilot_kick': True}) is True
    assert calls == ['x']
    assert leaf.check_autopilot_kick({'id': 'y'}) is False
    assert calls == ['x'], 'no second kick for a plain task'


def test_behaviour_snapshot_once_and_flow_skip():
    """Snapshot captured ONCE (a second call must NOT overwrite), and
    Flow-managed tasks are skipped entirely."""
    from lib.tasks_pkg.orchestrator._task_open import snapshot_turn_input
    task = {'messages': [{'role': 'user', 'content': 'hi'}]}
    snapshot_turn_input(task)
    assert task['_turn_input_messages'] == [{'role': 'user', 'content': 'hi'}]
    task['messages'].append({'role': 'assistant', 'content': 'mutated'})
    snapshot_turn_input(task)
    assert task['_turn_input_messages'] == [{'role': 'user', 'content': 'hi'}], (
        'the pristine snapshot must survive a second call (retry path)')
    flow_task = {'_flow_managed': True, 'messages': [{'role': 'user'}]}
    snapshot_turn_input(flow_task)
    assert '_turn_input_messages' not in flow_task, (
        'Flow-managed tasks never get a snapshot')


def test_behaviour_log_open_bracket_and_timing(caplog):
    """Both log lines fire; queue_wait only when _t_created is set; the
    return value is a float timestamp."""
    import lib.tasks_pkg.orchestrator._task_open as leaf
    with caplog.at_level('INFO'):
        out = leaf.log_task_open(
            {'id': 'deadbeefcafebabe', 'convId': 'c1', 'messages': [1, 2],
             '_t_created': 1000.0}, 'deadbeef')
    assert isinstance(out, float)
    assert any('▶ START' in r.message and 'deadbeefcafebabe' in r.message
               for r in caplog.records), (
        'the START bracket must carry the FULL task id')
    assert any('queue_wait=' in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level('INFO'):
        leaf.log_task_open(
            {'id': 'deadbeefcafebabe', 'messages': []}, 'deadbeef')
    assert not any('queue_wait=' in r.message for r in caplog.records), (
        'no _t_created → no queue_wait line')
