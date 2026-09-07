"""Wire-parity guards for pt_03f4cdf1 slice 32 — extract the per-round
open (ROUND_START event + phase emit) and the streaming-accumulator
construction from _run.py's stream loop into
lib.tasks_pkg.orchestrator._round_open (two helpers:

    emit_round_open(task, rs, round_num)
        RENDER_CONTRACT Phase 3: append ROUND_START(roundNum) at the TOP
        of every round the model actually runs (INCLUDING prose-only
        rounds, which previously had no client-keyable signal), then
        _emit_tool_round_phase with {} for round 0 and rs.assistant_msg
        for round > 0 (the one branch — behavioural-pinned).

    build_stream_accumulator(task, rs, cfg, round_num, project_enabled)
        StreamingToolAccumulator construction. NOTE: the project path
        comes from ``cfg.get('projectPath')`` — NOT the resolved
        ``project_path`` local (the original inline code read cfg
        directly; preserved byte-exactly).

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
ROOT_LOOP_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_root_agent_loop.py')
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_round_open.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes both helpers
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_helpers():
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._round_open')
    for name in ('emit_round_open', 'build_stream_accumulator'):
        assert hasattr(mod, name) and callable(getattr(mod, name)), (
            f'lib.tasks_pkg.orchestrator._round_open must export {name}')


# ---------------------------------------------------------------------------
# 2. signatures
# ---------------------------------------------------------------------------
def test_helper_signatures():
    import inspect
    from lib.tasks_pkg.orchestrator._round_open import (
        build_stream_accumulator, emit_round_open)
    p1 = inspect.signature(emit_round_open).parameters
    assert list(p1) == ['task', 'rs', 'round_num'], (
        f'emit_round_open takes (task, rs, round_num), got {list(p1)}')
    p2 = inspect.signature(build_stream_accumulator).parameters
    for name in ('task', 'rs', 'cfg', 'round_num', 'project_enabled'):
        assert name in p2, f'build_stream_accumulator missing {name}'


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates
# ---------------------------------------------------------------------------
def test_run_py_imports_round_open():
    src = ROOT_LOOP_PY.read_text()
    assert ('from lib.tasks_pkg.orchestrator._round_open import' in src), (
        '_run.py must import from _round_open at module scope')


def test_run_task_delegates_round_open_and_accumulator():
    src = ROOT_LOOP_PY.read_text()
    assert 'emit_round_open(' in src, (
        'the root adapter must delegate the round-open emit')
    assert 'stream_accumulator = build_stream_accumulator(' in src, (
        'the root adapter must delegate the accumulator construction')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py
# ---------------------------------------------------------------------------
def test_run_py_no_inline_round_start_event():
    src = RUN_PY.read_text()
    assert 'build_event(EventType.ROUND_START' not in src, (
        'the ROUND_START event construction must live in _round_open.py')


def test_run_py_no_inline_accumulator_construction():
    src = RUN_PY.read_text()
    assert 'StreamingToolAccumulator(' not in src, (
        'StreamingToolAccumulator must be constructed in _round_open.py, '
        'not inline in _run.py')


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_round_start_and_phase_branch():
    src = LEAF_PY.read_text()
    assert 'build_event(EventType.ROUND_START' in src
    assert '_emit_tool_round_phase(' in src
    assert 'rs.assistant_msg if round_num > 0 else {}' in src, (
        'the phase emit must keep the round-0 empty-dict branch')


def test_leaf_accumulator_reads_cfg_projectpath():
    src = LEAF_PY.read_text()
    assert "cfg.get('projectPath')" in src, (
        "the accumulator's project path must come from "
        "cfg.get('projectPath') — NOT the resolved project_path local "
        '(byte-exact preservation of the inline original)')


# ---------------------------------------------------------------------------
# 6. BEHAVIOURAL: the round-0 phase branch (owner directive)
# ---------------------------------------------------------------------------
def test_behaviour_phase_branch_round0_vs_later(monkeypatch):
    """round_num=0 → phase emit receives {}; round_num>0 → receives
    rs.assistant_msg. ROUND_START carries the exact roundNum."""
    import lib.tasks_pkg.orchestrator._round_open as leaf
    events, phases = [], []
    monkeypatch.setattr(leaf, 'append_event',
                        lambda task, ev: events.append(ev))
    monkeypatch.setattr(leaf, 'build_event',
                        lambda et, **kw: ('EV', et, kw))
    monkeypatch.setattr(leaf, '_emit_tool_round_phase',
                        lambda task, anchor, rn: phases.append((anchor, rn)))

    class RS:
        assistant_msg = {'role': 'assistant', 'content': 'prior'}
    leaf.emit_round_open({'id': 'x'}, RS(), 0)
    leaf.emit_round_open({'id': 'x'}, RS(), 2)

    assert events[0][2] == {'roundNum': 0} and events[1][2] == {'roundNum': 2}, (
        'ROUND_START must carry the exact roundNum per round')
    assert phases[0] == ({}, 0), (
        'round 0 must emit the phase with an EMPTY anchor dict')
    assert phases[1] == ({'role': 'assistant', 'content': 'prior'}, 2), (
        'round >0 must emit the phase with rs.assistant_msg')
