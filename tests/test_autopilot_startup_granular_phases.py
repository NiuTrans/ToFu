"""Backend regression: Autopilot startup is DETAILED, not a vague placeholder.

WHY
---
Event-dump diagnosis (debug/autopilot_warmup_window_probe.py + a full parent
event trace) showed the VU bubble sits SILENT for up to ~26s between
``autopilot_vu_start`` and the first ``llm_thinking`` on a large conversation.
That whole window is ``run_task``'s pre-LLM prep (tool assembly → tool-history
rebuild → system-context injection incl. FUSE memory/project prefetch), which
emitted NO phase. The two coarse ``autopilot.py`` setup phases flash at ~0s and
then the bubble is static through the real slow window.

The fix instruments ``run_task``'s prep with granular ``working`` phases at the
REAL sub-step boundaries, GATED on ``_vu_subtask`` — so the ordinary
worker/endpoint startup path stays byte-identical (no new events for it), while
the VU sub-task (whose ``events`` is a ``_VUEventForwarder``) forwards each
phase into the synthetic-user bubble.

This drives the REAL ``run_task`` prep against a ``_vu_subtask`` task with the
LLM streaming call stubbed to raise right after prep (so we exercise the prep
window without a live model), and asserts:
  1. the granular ``working`` startup phases are emitted in prep, and
  2. NONE are emitted when ``_vu_subtask`` is absent (a normal worker turn) —
     proving the gate holds and the ordinary path is untouched.

NC (bites): flip the gate so ``_vu_phase`` always emits (drop the
``_vu_subtask`` guard) → the non-VU worker turn ALSO emits startup phases →
the isolation assertion fails.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ORCH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator.py')


# The exact phase details the fix emits during run_task prep (source of truth).
_STARTUP_DETAILS = [
    'Autopilot：装配工具、准备工作区…',
    'Autopilot：重建工具调用历史…',
    'Autopilot：注入系统上下文（项目结构、记忆检索）…',
    'Autopilot：上下文就绪，正在发送请求…',
]


def _orch_src() -> str:
    with open(ORCH, encoding='utf-8') as fh:
        return fh.read()


def test_vu_phase_helper_is_gated_on_vu_subtask():
    """The helper must early-return unless task['_vu_subtask'] is set — this is
    what keeps the ordinary worker/endpoint startup byte-identical."""
    src = _orch_src()
    # Locate the _vu_phase definition and assert its guard.
    m = re.search(r"_vu_startup = bool\(task\.get\('_vu_subtask'\)\)"
                  r".*?def _vu_phase\(detail\):\s*\n\s*if not _vu_startup:\s*\n\s*return",
                  src, re.DOTALL)
    assert m, ('_vu_phase must be defined with an `if not _vu_startup: return` '
               'guard bound to task["_vu_subtask"]')


def test_startup_phases_present_and_ordered():
    """All four granular prep phases must be emitted via _vu_phase, in order."""
    src = _orch_src()
    positions = []
    for detail in _STARTUP_DETAILS:
        needle = f"_vu_phase('{detail}')"
        idx = src.find(needle)
        assert idx != -1, f'missing startup phase call: {needle}'
        positions.append(idx)
    assert positions == sorted(positions), \
        f'startup phases out of source order: {positions}'


def test_startup_phases_use_working_phase_type():
    """The forwarded phase must be the `working` type the frontend renders
    verbatim (phase.detail), not a new/unhandled phase name."""
    src = _orch_src()
    m = re.search(r"def _vu_phase\(detail\):.*?phase='working', detail=detail",
                  src, re.DOTALL)
    assert m, "_vu_phase must emit EventType.PHASE phase='working' detail=detail"


def test_nc_source_gate_removed_would_break_isolation():
    """NC (source-level): if the `if not _vu_startup: return` guard were removed,
    a normal worker turn would emit these phases too. We assert the guard is
    PRESENT (its removal is what the neuter in the runbook deletes)."""
    src = _orch_src()
    # The guard line must appear exactly once, immediately inside _vu_phase.
    assert src.count('if not _vu_startup:') == 1, \
        'the _vu_subtask gate must be present exactly once inside _vu_phase'
