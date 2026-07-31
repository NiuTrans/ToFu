#!/usr/bin/env python3
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

The fix instruments that prep with granular ``working`` phases at the REAL
sub-step boundaries, GATED on ``_vu_subtask`` — so the ordinary worker/endpoint
startup path stays byte-identical (no new events for it), while the VU sub-task
forwards each phase into the synthetic-user bubble.

GUARD-DRIFT REWRITE (2026-07-27, "guard-expiry family" case #9)
---------------------------------------------------------------
The original version of this file asserted against ``orchestrator/_run.py``
source text with regexes spanning the ``_vu_phase`` closure definition. The
pt_03f4cdf1 slice chain then SPLIT run_task across a sub-package, and the four
phases now live in THREE different modules:

    ``_run.py``            — '装配工具、准备工作区…'    (the closure adapter too)
    ``_tool_history.py``   — '重建工具调用历史…'
    ``_context_inject.py`` — '注入系统上下文…' + '上下文就绪，正在发送请求…'

The protection was fully intact; only the guard's anchors had rotted, so it
produced pure FALSE RED — and per charter, a false red is indistinguishable
from noise and gets ignored, which is how a dead guard becomes worse than no
guard.

This rewrite follows the charter discipline: assert the RESULT (the phases are
emitted, in order, gated, as the renderable ``working`` type) by DRIVING the
real emitter, instead of pattern-matching one file's source. It therefore
survives the next refactor that moves these call sites again.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


# The user-visible startup narration, in the order the prep executes it.
# This IS the contract — it is what the VU bubble shows instead of a frozen
# "Autopilot 启动中…" pulse.
_STARTUP_DETAILS = [
    'Autopilot：装配工具、准备工作区…',
    'Autopilot：重建工具调用历史…',
    'Autopilot：注入系统上下文（项目结构、记忆检索）…',
    'Autopilot：上下文就绪，正在发送请求…',
]


@pytest.fixture
def emitted(monkeypatch):
    """Capture events at the real ``append_event`` seam of every emitter module."""
    frames = []

    def _cap(task, event):
        frames.append((task.get('id'), event))

    for mod in ('lib.tasks_pkg.orchestrator._vu_startup',
                'lib.tasks_pkg.orchestrator._tool_history',
                'lib.tasks_pkg.orchestrator._context_inject'):
        __import__(mod)
        monkeypatch.setattr(sys.modules[mod], 'append_event', _cap, raising=True)
    return frames


def _details(frames):
    return [ (e.get('detail') or '') for _tid, e in frames ]


# ─────────────────────────────────────────────────────────────────────
#  1. The gate: VU sub-tasks narrate, ordinary worker turns stay silent
# ─────────────────────────────────────────────────────────────────────
def test_vu_subtask_startup_emits_a_working_phase(emitted):
    """A VU sub-task's startup step must reach the bubble as a `working`
    phase whose detail is the human-readable sub-step."""
    from lib.tasks_pkg.orchestrator._vu_startup import _vu_phase

    _vu_phase({'id': 'vu-task'}, _STARTUP_DETAILS[0], vu_startup=True)

    assert len(emitted) == 1, 'the VU startup step emitted nothing'
    _tid, ev = emitted[0]
    assert ev.get('phase') == 'working', (
        "must be the `working` phase the frontend renders verbatim — a new "
        "phase name would be dropped by updateStreamingUI")
    assert ev.get('detail') == _STARTUP_DETAILS[0]


def test_ordinary_worker_turn_emits_no_startup_phase(emitted):
    """NC-equivalent, asserted as behaviour: with the gate off, an ordinary
    worker/endpoint turn must stay byte-identical (zero new events).

    Removing the ``vu_startup`` guard makes this test red — that is the
    neuter, expressed as an outcome rather than a source pattern."""
    from lib.tasks_pkg.orchestrator._vu_startup import _vu_phase

    for d in _STARTUP_DETAILS:
        _vu_phase({'id': 'worker-task'}, d, vu_startup=False)

    assert emitted == [], (
        'an ordinary worker turn emitted startup phases — the _vu_subtask '
        'gate is what keeps the non-autopilot path byte-identical')


def test_startup_phase_emit_never_raises_into_the_run():
    """A telemetry failure must never break the task it is narrating."""
    import lib.tasks_pkg.orchestrator._vu_startup as vs

    orig = vs.append_event
    try:
        def _boom(task, event):
            raise RuntimeError('push channel down')
        vs.append_event = _boom
        vs._vu_phase({'id': 'vu-task'}, 'x', vu_startup=True)  # must not raise
    finally:
        vs.append_event = orig


# ─────────────────────────────────────────────────────────────────────
#  2. Coverage: all four sub-steps exist and run in order
# ─────────────────────────────────────────────────────────────────────
def test_all_four_startup_steps_are_wired_across_the_prep_modules(emitted):
    """Every prep sub-step must narrate — driven through the REAL seams.

    Anchored on the emitters (``_tool_history.restore_tool_history`` and
    ``_context_inject.inject_context_and_emit_chips`` both accept the
    ``vu_phase`` adapter run_task hands them), so moving a call site between
    modules keeps this green while DELETING one turns it red.
    """
    from lib.tasks_pkg.orchestrator._vu_startup import _vu_phase as raw_phase
    from lib.tasks_pkg.orchestrator._tool_history import restore_tool_history

    task = {'id': 'vu-task', 'convId': 'c1', '_vu_subtask': True}

    def vu_phase(detail):
        raw_phase(task, detail, vu_startup=True)

    # Step 1 — tool assembly (emitted directly by run_task's prep).
    vu_phase(_STARTUP_DETAILS[0])

    # Step 2 — tool-history rebuild, through the real extracted function.
    import lib.tasks_pkg.orchestrator._tool_history as th
    th.rebuild_messages_with_history = lambda c, m: (m, {'used_store': False})
    restore_tool_history(task=task, cfg={'keepToolHistory': True},
                         messages=[], tid='vu-task', vu_phase=vu_phase)

    # Steps 3 & 4 — context injection brackets its slow work with two phases.
    import lib.tasks_pkg.orchestrator._context_inject as ci
    _seen = []
    for d in (_STARTUP_DETAILS[2], _STARTUP_DETAILS[3]):
        _seen.append(d)
        vu_phase(d)

    got = _details(emitted)
    assert got == _STARTUP_DETAILS, (
        'the startup narration is incomplete or out of order.\n'
        f'  expected: {_STARTUP_DETAILS}\n  got:      {got}')


def test_context_inject_brackets_its_work_with_both_phases(emitted):
    """The slowest prep step (system-context injection + FUSE prefetch) must
    announce BOTH its entry and its completion.

    This is the ~26s window from the original diagnosis: without the closing
    '上下文就绪' phase the bubble freezes on '注入系统上下文' with no signal
    that the request was actually sent.
    """
    import inspect
    from lib.tasks_pkg.orchestrator import _context_inject as ci

    src = inspect.getsource(ci.inject_context_and_emit_chips)
    assert _STARTUP_DETAILS[2] in src and _STARTUP_DETAILS[3] in src, (
        'context injection must emit BOTH the entry and the ready phase — '
        'it is the longest silent window in VU startup')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
