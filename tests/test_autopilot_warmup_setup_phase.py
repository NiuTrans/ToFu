"""Backend regression: Autopilot names the pre-stream ("warm-up") window.

WHY
---
Diagnosis (debug/autopilot_warmup_window_probe.py, 12 real runs): between
``autopilot_vu_start`` and the VU sub-task's first orchestrator phase
(``llm_thinking`` / ``waiting_model``) there is a genuinely SILENT window —
measured 2.5–26.7s — during which ``run_virtual_user`` resolves the objective
(DB read), assembles the message list and builds the sub-task. Nothing was
emitted, so the VU bubble sat on the bare "Autopilot…" placeholder with no
attribution of what was blocking.

The fix emits ``working`` phases wrapped as ``autopilot_vu_event`` (the SAME
envelope ``_VUEventForwarder`` uses) at those pre-stream steps, so the bubble
names the step and the frontend's existing ``working`` phase branch renders it.

This drives the REAL ``run_virtual_user`` with the LLM turn stubbed and asserts:
  1. two attributed ``working`` setup phases are emitted, carrying vuMsgId, and
  2. BOTH land BEFORE the sub-task's ``_run_single_turn`` is invoked (i.e. they
     fill the silent window, not after streaming starts).

NC (bites): neuter ``_emit_vu_setup_phase`` to a no-op (the pre-fix state) and
assert NO setup phases are captured — proving the assertions discriminate.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg import autopilot as ap


def _wire(monkeypatch, captured):
    """Patch the collaborators so run_virtual_user is hermetic + fast."""
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.orchestrator._turn as orch

    # Capture every event the pre-stream path emits (via the lazy import inside
    # _emit_vu_setup_phase: `from lib.tasks_pkg.manager import append_event`).
    monkeypatch.setattr(mgr, 'append_event', lambda task, event: captured.append(event))
    # No DB read for the objective.
    monkeypatch.setattr(ap, '_get_or_persist_objective', lambda cid, msgs, *, user_id: '')

    def _fake_create_task(conv_id, messages, config, **kw):
        return {
            'id': 'subtask0',
            'convId': conv_id,
            '_userId': kw['user_id'],
            'toolRounds': [],
            'aborted': False,
        }

    monkeypatch.setattr(mgr, 'create_task', _fake_create_task)

    def _fake_turn(sub_task, messages_override=None):
        # Marker so ordering (setup-phases-BEFORE-stream) is verifiable.
        captured.append({'type': '_RUN_SINGLE_TURN'})
        return {'content': 'The build still fails, please keep going.',
                'error': None}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)


def _make_task():
    return {
        'id': 'parent-abc123',
        'convId': 'conv1',
        '_userId': 1,
        'messages': [{'role': 'user', 'content': 'hi'},
                     {'role': 'assistant', 'content': 'done'}],
        'config': {},
        'events': [],
        'events_lock': threading.Lock(),
        'aborted': False,
    }


def _setup_phases(captured):
    return [e for e in captured
            if e.get('type') == 'autopilot_vu_event'
            and (e.get('inner') or {}).get('phase') == 'working']


def test_setup_phases_emitted_in_prestream_window(monkeypatch):
    captured: list = []
    _wire(monkeypatch, captured)

    res = ap.run_virtual_user(_make_task(), vu_msg_id='vu-xyz')
    assert res is not None  # 'keep going' reply → non-None

    phases = _setup_phases(captured)
    # Two attributed setup steps: objective-resolution + context-assembly.
    assert len(phases) == 2, f'expected 2 working setup phases, got {captured}'
    assert [ev['inner'].get('detailKey') for ev in phases] == [
        'stream.phase.vuVerifyAssistant',
        'stream.phase.vuAssembleContext',
    ]
    for ev in phases:
        assert ev['vuMsgId'] == 'vu-xyz'
        detail = ev['inner'].get('detail') or ''
        # Headless clients keep a meaningful English fallback while the web
        # client resolves detailKey through its current locale.
        assert detail.startswith('Autopilot ') and len(detail) > len('Autopilot ')

    # DECISIVE: both setup phases fill the SILENT window — they precede the
    # sub-task's first stream turn, not follow it.
    turn_idx = next(i for i, e in enumerate(captured)
                    if e.get('type') == '_RUN_SINGLE_TURN')
    phase_idxs = [i for i, e in enumerate(captured)
                  if e.get('type') == 'autopilot_vu_event'
                  and (e.get('inner') or {}).get('phase') == 'working']
    assert phase_idxs and max(phase_idxs) < turn_idx, \
        f'setup phases must precede _run_single_turn; order={captured}'


def test_nc_neutered_helper_emits_no_setup_phase(monkeypatch):
    """NC: with _emit_vu_setup_phase neutered to a no-op (pre-fix state), the
    pre-stream window is silent again — no working setup phases. Proves the
    positive test's assertions bite."""
    captured: list = []
    _wire(monkeypatch, captured)
    monkeypatch.setattr(ap, '_emit_vu_setup_phase',
                        lambda task, vu_msg_id, detail, **fields: None)

    res = ap.run_virtual_user(_make_task(), vu_msg_id='vu-xyz')
    assert res is not None
    assert _setup_phases(captured) == [], \
        f'neutered helper must emit no setup phase; got {captured}'
