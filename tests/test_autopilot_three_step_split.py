#!/usr/bin/env python3
"""tests/test_autopilot_three_step_split.py — the parent ``done`` ships BEFORE
the VU LLM call, and the VU carrier registers (claims the conv→latest index)
BEFORE the parent ``done`` (HB-1, pt_8dc03017).

WHY
---
The prior inline design ran ``maybe_run_autopilot`` → ``run_virtual_user`` →
``_run_single_turn`` (the full VU LLM, 12–52s) on the finalize thread BEFORE
``append_event(done_evt)``, so the terminal frame was delayed by a whole VU
round-trip.  The three-step split:

  1. REGISTER the VU carrier + claim the conv→latest successor index (cheap,
     NO LLM) — ``register_autopilot_turn``, called before ``append_event``.
  2. emit the parent ``done`` (with ``latestLiveTaskId`` = the VU carrier).
  3. run the VU LLM decision AFTER done — ``maybe_run_autopilot`` detects the
     armed carrier and runs only the decision + follow-up spawn.

Pinned here:
  * the SOURCE ordering in ``_finalize_and_emit_done`` (register before
    append_event, maybe_run_autopilot after append_event), and
  * the BEHAVIOUR: registration does NOT call ``_run_single_turn`` while it
    DOES advance the supersede index; the armed ``maybe_run_autopilot`` runs
    ``_run_single_turn`` exactly once afterwards (no re-registration).
"""

from __future__ import annotations

import os
import threading

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
FINALIZE_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── Source-order contract ────────────────────────────────────────────

def test_finalize_registers_before_done_and_runs_vu_after_done():
    """_finalize_and_emit_done must call register_autopilot_turn BEFORE
    append_event(done_evt), and maybe_run_autopilot AFTER append_event(done_evt)
    — otherwise the done frame is either missing the successor stamp (HB-1) or
    delayed by the VU LLM round-trip."""
    src = _read(FINALIZE_PATH)
    arm_pos = src.index('register_autopilot_turn(task)')
    done_pos = src.index('append_event(task, done_evt)')
    run_pos = src.index('maybe_run_autopilot(task)')
    assert arm_pos < done_pos, (
        '_finalize.py: the VU carrier must be registered (claiming the '
        'conv→latest index) BEFORE the parent done ships — HB-1 happens-before')
    assert done_pos < run_pos, (
        '_finalize.py: the VU LLM decision (maybe_run_autopilot) must run '
        'AFTER append_event(done_evt) so the terminal frame is not delayed by '
        'a VU round-trip')


# ── Behavioural: registration is cheap, LLM runs later ───────────────

def _mk_task(conv_id='conv-split'):
    return {
        'id': 'parent-split',
        'convId': conv_id,
        '_userId': 1,
        'status': 'done',
        'aborted': False,
        'config': {},
        'messages': [
            {'role': 'user', 'content': 'please continue'},
            {'role': 'assistant', 'content': 'done'},
        ],
        'events': [],
        'events_lock': threading.Lock(),
    }


def _wire(monkeypatch):
    """Stub every collaborator so register_autopilot_turn / maybe_run_autopilot
    run deterministically without a DB or a real LLM."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.manager.runtime as manager_runtime

    record = {'create_task': 0, 'run_single_turn': 0, 'record_latest': []}

    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda cid, *, user_id: False)
    monkeypatch.setattr(ap, '_successor_already_running', lambda task, cid: False)
    monkeypatch.setattr(ap, '_get_or_persist_run_id', lambda cid, *, user_id: 'run-split')
    monkeypatch.setattr(ap, '_get_or_persist_objective', lambda cid, msgs, *, user_id: '')
    monkeypatch.setattr(ap, '_classify_verdict',
                        lambda text, verifier_role=None: {'phase': 'stop'})
    monkeypatch.setattr(ap, '_emit_vu_lifecycle_frame', lambda task, evt: None)
    monkeypatch.setattr(ap, '_emit_run_concluded_event',
                        lambda *a, **k: None)
    monkeypatch.setattr(ap, '_clear_run_id', lambda cid, *, user_id: None)
    monkeypatch.setattr(ap, '_emit_vu_setup_phase', lambda *a, **k: None)
    monkeypatch.setattr(ap, '_install_vu_carrier_contract',
                        lambda parent, sub, vu_msg_id: None)

    monkeypatch.setattr(mgr, 'append_event', lambda task, evt: None)
    monkeypatch.setattr(mgr, 'discard_task', lambda tid, conv_id=None: None)
    monkeypatch.setattr(mgr, 'write_carrier_terminal_row', lambda task, st: None)

    def _fake_create_task(
        conv_id, messages, config, *, user_id, supersede=True,
    ):
        record['create_task'] += 1
        sub = {
            'id': 'vu-split', 'convId': conv_id, '_userId': user_id,
            'status': 'running',
            'aborted': False, 'config': config, 'toolRounds': [],
            'events': [], 'events_lock': threading.Lock(),
        }
        return sub

    def _fake_record_latest(conv_id, task_id):
        record['record_latest'].append((conv_id, task_id))

    def _fake_run_single_turn(sub_task, **k):
        record['run_single_turn'] += 1
        return {'content': '', 'error': None, 'thinking': '', 'usage': {},
                'messages': []}

    monkeypatch.setattr(
        'lib.tasks_pkg.manager.create_task', _fake_create_task)
    monkeypatch.setattr(
        manager_runtime, '_record_latest_task', _fake_record_latest)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._turn._run_single_turn',
                        _fake_run_single_turn)
    return record


def test_register_claims_index_without_running_vu(monkeypatch):
    """Step 1 registers the carrier (advances the supersede index) and MUST NOT
    run the VU LLM — the LLM is step 3, after the parent done."""
    import lib.tasks_pkg.autopilot as ap

    record = _wire(monkeypatch)
    task = _mk_task()

    out = ap.register_autopilot_turn(task)

    assert out is not None and out.get('armed') is True
    assert record['create_task'] == 1, 'the VU carrier must be created once'
    assert record['run_single_turn'] == 0, (
        'registration must be cheap — the VU LLM (_run_single_turn) must NOT '
        'run before the parent done')
    assert ('conv-split', 'vu-split') in record['record_latest'], (
        'registration must advance the conv→latest index to the VU carrier '
        '(HB-1: the successor is claimable before done ships)')
    assert task.get('_autopilot_arm_ctx') is not None, (
        'registration must stash the ctx so maybe_run_autopilot can resume '
        'the decision phase without re-registering')


def test_armed_maybe_run_autopilot_runs_vu_llm_once(monkeypatch):
    """Step 3 (maybe_run_autopilot after done) runs the VU LLM exactly once and
    does NOT re-register the carrier."""
    import lib.tasks_pkg.autopilot as ap

    record = _wire(monkeypatch)
    task = _mk_task()

    assert ap.register_autopilot_turn(task) is not None
    result = ap.maybe_run_autopilot(task)

    assert result is None  # TASK_DONE stub → no follow-up baton
    assert record['run_single_turn'] == 1, (
        'the armed maybe_run_autopilot must run the VU LLM exactly once')
    assert record['create_task'] == 1, (
        'the armed maybe_run_autopilot must NOT re-register the carrier — '
        'registration happened in step 1')
    assert '_autopilot_arm_ctx' not in task, (
        'the arm ctx must be consumed (popped) by the decision phase')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
