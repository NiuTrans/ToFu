#!/usr/bin/env python3
"""Wire-parity for pt_862771477a86 slice 1 — _RoundState container swap.

Scope: run_task's stream main loop historically carried 14 cross-iteration
locals as bare function locals. Slice 1 moves them onto ONE flat dataclass,
``lib/tasks_pkg/orchestrator/_round_state.py::RoundState``, as a PURE
CONTAINER SWAP (byte-identical behavior — no loop-shape change, no event
change; the while loop, its ceiling and every break path stay put).

Owner rulings baked into the shape (2026-07-27):
  * FLAT fields — no control/llm/usage/tools sub-objects.
  * ``round_num`` / ``_premature_retry_count`` NOT included (chassis-owned
    at cutover; they stay plain locals by design — their continued presence
    as locals is NOT a violation).
  * task-dict channels NOT included (owned by the task).

Failing-first / NEUTER — this test asserts:
  1. The module exists, ``RoundState`` is a dataclass exposing EXACTLY the
     14 sanctioned fields (delete or rename one → RED; this is the
     delete-field NEUTER guard).
  2. ``_run.py`` imports RoundState AND constructs it.
  3. The historical inline initializers and bare-local mutation pivots are
     GONE from ``_run.py`` (a silent revert puts them back inline).
  4. Defaults reproduce the pre-slice initializers byte-for-byte.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The 14 sanctioned flat fields (owner ruling: 16 cross-iteration locals
# minus round_num + premature_retry_count, both chassis-owned at cutover).
_FIELDS = {
    'model', 'preset', 'thinking_enabled',
    'exit_reason', 'abort_phase', 'consecutive_tool_timeouts',
    'last_checkpoint_ts',
    'assistant_msg', 'last_finish_reason', 'last_usage',
    'accumulated_usage', 'api_rounds',
    'tool_call_happened', 'tool_round_num',
}


@_unit
def test_round_state_module_exposes_exactly_the_14_fields():
    """Slice 1 NEUTER guard: RoundState is a dataclass with EXACTLY the 14
    sanctioned fields — deleting one turns this red; adding a 15th (e.g.
    smuggling round_num in) also turns it red (owner ruling: flat, 14)."""
    import dataclasses
    import importlib
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._round_state')
    assert hasattr(mod, 'RoundState'), 'missing RoundState'
    assert dataclasses.is_dataclass(mod.RoundState)
    names = {f.name for f in dataclasses.fields(mod.RoundState)}
    assert names == _FIELDS, (
        f'RoundState fields drifted: missing={sorted(_FIELDS - names)} '
        f'extra={sorted(names - _FIELDS)} — the shape is owner-ruled; '
        'change it only with a charter-level decision')


@_unit
def test_run_task_constructs_round_state():
    """Slice 1: _run.py must import RoundState and construct it in run_task."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert ('from lib.tasks_pkg.orchestrator._round_state import RoundState'
            in src), '_run.py must import RoundState after slice 1'
    assert 'rs = RoundState(' in src, (
        '_run.py must CONSTRUCT rs = RoundState(...) in run_task')


@_unit
def test_inline_loop_state_initializers_gone():
    """Slice 1: the historical inline initializers / bare-local pivots MUST
    be gone from _run.py (a silent revert reintroduces them)."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        "_loop_exit_reason = 'max_rounds_exhausted'",
        '_abort_detected_phase = None',
        '_consecutive_tool_timeouts = 0',
        '_last_checkpoint = 0.0',
        'tool_call_happened = False',
        'last_finish_reason = None',
        'last_usage = None',
        'assistant_msg = None',
        'accumulated_usage = {}',
        'api_rounds = []',
        "\n                assistant_msg = llm_result['assistant_msg']",
        "\n                model = llm_result['model']",
        '_consecutive_tool_timeouts += 1',
    ):
        assert pivot not in src, (
            f'_run.py must NOT re-carry bare loop-state pivot {pivot!r} '
            '— it lives on rs after slice 1')


@_unit
def test_round_state_defaults_match_pre_slice_initializers():
    """Slice 1: defaults reproduce the historical init values byte-for-byte."""
    import importlib
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._round_state')
    rs = mod.RoundState(model='m1', preset='p1', thinking_enabled=True)
    assert rs.model == 'm1' and rs.preset == 'p1' and rs.thinking_enabled is True
    assert rs.exit_reason == 'max_rounds_exhausted'
    assert rs.abort_phase is None
    assert rs.consecutive_tool_timeouts == 0
    assert rs.last_checkpoint_ts == 0.0
    assert rs.assistant_msg is None
    assert rs.last_finish_reason is None
    assert rs.last_usage is None
    assert rs.accumulated_usage == {} and rs.api_rounds == []
    assert rs.tool_call_happened is False
    assert rs.tool_round_num == 0
    # Mutable defaults must be per-instance (no shared-list bug).
    rs2 = mod.RoundState(model='m2', preset='p2', thinking_enabled=False)
    rs.api_rounds.append({'round': 1})
    assert rs2.api_rounds == []


if __name__ == '__main__':
    for fn in [
        test_round_state_module_exposes_exactly_the_14_fields,
        test_run_task_constructs_round_state,
        test_inline_loop_state_initializers_gone,
        test_round_state_defaults_match_pre_slice_initializers,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
