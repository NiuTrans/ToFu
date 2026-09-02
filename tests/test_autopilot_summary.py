"""tests/test_autopilot_summary.py — Autopilot run CLOSE-OUT (fold) fact.

The autopilot close-out REPORT layer (the LLM reporter turn, on-demand
``summarize_run``, the report content/translation, and the async summary
daemon) was REMOVED — a clean [VU: TASK_DONE] and the budget-guard cutoff now
conclude the run report-free. What remains, and is covered here, is the B-layer
fold machinery both close-out paths still need:

  • ``_store_run_record`` persists ONE authoritative per-run record in the
    conversation SIDECAR (``settings.autopilotSummaries[runId]``) carrying the
    terminal ``status='concluded'`` + ``reason`` (NO report ``content``) — a
    human-only record, NOT a ``role='assistant'`` chat message.
  • ``conclude_run`` is the manual-stop close-out seam (resolve run id → write
    concluded(stopped) record → clear the run pin).
  • ``_emit_run_concluded_event`` is the report-free close-out helper the clean
    TASK_DONE + budget-guard paths route through (store record + feed pulse +
    ``autopilot_run_concluded`` SSE).
  • the ``autopilot_run_concluded`` event is registered in the streaming
    contract and carries the sidecar ``record`` — the single
    BACKEND-AUTHORITATIVE run-end fact.

No live LLM / orchestrator.
"""

import json
import threading

import pytest


# ── event contract ────────────────────────────────────────────────────

def test_autopilot_run_concluded_event_registered():
    from lib.agent_core.events import EventType, is_registered, get_event_spec
    # The BACKEND-AUTHORITATIVE run-end fact, fired on BOTH close-out paths.
    assert EventType.AUTOPILOT_RUN_CONCLUDED == 'autopilot_run_concluded'
    assert is_registered(EventType.AUTOPILOT_RUN_CONCLUDED)
    assert not hasattr(EventType, 'AUTOPILOT_SUMMARY')
    spec = get_event_spec(EventType.AUTOPILOT_RUN_CONCLUDED)
    assert 'runId' in spec.fields
    # The payload carries the SIDECAR record (`record`) — the concluded
    # status/reason — not a chat message.
    assert 'record' in spec.fields
    assert 'summary' not in spec.fields
    assert 'summaryMessage' not in spec.fields


# ── _store_run_record: the concluded (fold) fact, no report content ────

def _fake_settings_db(monkeypatch, state):
    """Back settings reads and writes with the domain authority seam."""
    from tests._conversation_authority import install_conversation_state
    install_conversation_state(monkeypatch, state)


def test_store_run_record_manual_stop_is_concluded_without_report(monkeypatch):
    """A manual stop writes a concluded record with a reason but NO content.

    The crux of the symmetric-conclude fix: the manual-stop path produces a
    BACKEND-AUTHORITATIVE fold-fact (status=concluded, reason=stopped) that the
    frontend keys on — instead of inferring run-end from stream/task absence.
    """
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    rec = ap._store_run_record('conv-z', 'ar-stop', reason='stopped', user_id=1)
    assert rec is not None
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'stopped'
    assert 'content' not in rec           # a manual stop has no report
    assert 'role' not in rec and '_msgId' not in rec  # not a chat message

    stored = json.loads(state['settings'])['autopilotSummaries']['ar-stop']
    assert stored['status'] == 'concluded'
    assert stored['reason'] == 'stopped'
    assert 'content' not in stored


def test_store_run_record_task_done_reason_is_sticky(monkeypatch):
    """A later bare `stopped` conclude must NOT downgrade an earlier clean
    `task_done` record's verdict (they can race on close-out)."""
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    ap._store_run_record('conv-race', 'ar-r', reason='stopped', user_id=1)
    ap._store_run_record('conv-race', 'ar-r', reason='task_done', user_id=1)
    ap._store_run_record('conv-race', 'ar-r', reason='stopped', user_id=1)  # racing stop

    rec = json.loads(state['settings'])['autopilotSummaries']['ar-r']
    assert rec['reason'] == 'task_done'       # never downgraded
    assert rec['status'] == 'concluded'


def test_conclude_run_writes_authoritative_stopped_record(monkeypatch):
    """conclude_run() is the manual-stop close-out seam: it resolves the run id,
    writes the BACKEND-AUTHORITATIVE concluded(stopped) record, and clears the
    run pin so the next run is fresh."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.autopilot_run_lifecycle as lifecycle
    # Seed a conv whose settings pin a live run id + carry a VU turn stamp.
    state = {'settings': json.dumps({'autopilotRunId': 'ar-live'}),
             'messages': json.dumps([
                 {'role': 'user', 'content': 'obj'},
                 {'role': 'user', 'content': 'vu', '_isVirtualUser': True,
                  '_autopilotRunId': 'ar-live'}])}

    _fake_settings_db(monkeypatch, state)
    # Isolate the repository-backed resolver; its behavior is covered by the
    # focused resolver tests, while this test owns close-out persistence.
    monkeypatch.setattr(lifecycle, '_resolve_recent_run_id',
                        lambda conv_id, *, user_id: 'ar-live')

    rec = ap.conclude_run('conv-live', reason='stopped', user_id=1)
    assert rec is not None
    assert rec['runId'] == 'ar-live'
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'stopped'
    assert 'content' not in rec  # a manual stop has no report

    settings = json.loads(state['settings'])
    assert settings['autopilotSummaries']['ar-live']['status'] == 'concluded'
    # The run pin was cleared so the next run mints a fresh id.
    assert 'autopilotRunId' not in settings


# ── _emit_run_concluded_event: the report-free close-out helper ────────

def test_emit_run_concluded_event_stores_and_emits(monkeypatch):
    """The helper persists the concluded record, fires the feed pulse, and
    emits the autopilot_run_concluded SSE — with NO report content."""
    import lib.tasks_pkg.autopilot as ap
    # Post-slice-3 (pt_00459503): ``_emit_run_concluded_event`` lives in
    # ``lib.tasks_pkg.autopilot_run_lifecycle`` (a leaf module). Its
    # internal calls to ``_store_run_record`` and ``_emit_run_concluded``
    # resolve from that module's OWN globals, not the facade — so we
    # patch the origin bindings there. The facade re-export identity
    # (ap._store_run_record IS leaf._store_run_record) is separately
    # guarded by test_autopilot_markers_lazy_import_contract.py.
    import lib.tasks_pkg.autopilot_run_lifecycle as apl

    stored = {}
    monkeypatch.setattr(apl, '_store_run_record',
                        lambda conv_id, run_id, *, user_id, reason='task_done':
                        stored.update(conv_id=conv_id, run_id=run_id,
                                      user_id=user_id, reason=reason)
                        or {'runId': run_id, 'status': 'concluded', 'reason': reason})
    concluded = []
    monkeypatch.setattr(apl, '_emit_run_concluded',
                        lambda conv_id, run_id, text, cfg, *, user_id:
                        concluded.append((text, user_id)))
    events = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: events.append(ev))

    task = {'id': 'task-rc-1', 'convId': 'conv-rc', 'config': {}, '_userId': 1}
    rec = ap._emit_run_concluded_event(task, 'conv-rc', 'ar-rc', reason='task_done')

    assert rec is not None and rec['status'] == 'concluded'
    assert stored == {
        'conv_id': 'conv-rc', 'run_id': 'ar-rc', 'user_id': 1,
        'reason': 'task_done',
    }
    # Feed pulse fired with EMPTY report text (report layer removed).
    assert concluded == [('', 1)]
    # The SSE run-concluded event was emitted with the record.
    assert any(ev.get('type') == 'autopilot_run_concluded'
               and ev.get('runId') == 'ar-rc' for ev in events)


def test_emit_run_concluded_event_none_record_short_circuits(monkeypatch):
    """A persist failure (None record) short-circuits — no pulse, no SSE."""
    import lib.tasks_pkg.autopilot as ap
    # Post-slice-3: patch the origin binding on autopilot_run_lifecycle.
    import lib.tasks_pkg.autopilot_run_lifecycle as apl
    monkeypatch.setattr(apl, '_store_run_record', lambda *a, **k: None)
    fired = []
    monkeypatch.setattr(apl, '_emit_run_concluded',
                        lambda *a, **k: fired.append('pulse'))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: fired.append('sse'))
    task = {'id': 't', 'convId': 'c', 'config': {}, '_userId': 1}
    assert ap._emit_run_concluded_event(task, 'c', 'ar-x') is None
    assert fired == []


# ── maybe_run_autopilot TASK_DONE: report-free synchronous conclude ────
#  With the report layer removed there is no async summary daemon: a clean
#  [VU: TASK_DONE] concludes the run report-free ON the calling thread
#  (_emit_run_concluded_event) then settles the turn (clear marker + run pin).

def _task_done_task():
    return {
        'id': 'task-done-0001',
        'convId': 'conv-td',
        '_userId': 1,
        'config': {'model': 'm', 'autopilot': True},
        'messages': [
            {'role': 'user', 'content': 'Ship it.'},
            {'role': 'assistant', 'content': 'Done.'},
        ],
        '_autopilot_deciding': True,
    }


def test_task_done_concludes_report_free_and_settles(monkeypatch):
    """On [VU: TASK_DONE], maybe_run_autopilot must conclude the run via
    _emit_run_concluded_event (report-free) ON the calling thread, then clear
    the marker + run pin and return None (no follow-up baton)."""
    import lib.tasks_pkg.autopilot as ap

    main_thread = threading.current_thread()
    seen = {'thread': None, 'run_id': None, 'reason': None}

    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(
        ap, '_get_or_persist_run_id',
        lambda conv_id, *, user_id: 'ar-td')
    monkeypatch.setattr(
        ap, '_has_pending_real_message',
        lambda conv_id, *, user_id: False)
    monkeypatch.setattr(ap, '_successor_already_running',
                        lambda task, conv_id: False)

    def _fake_vu(task, vu_msg_id=None):
        task['_vu_emitted_done'] = True
        return None
    monkeypatch.setattr(ap, 'run_virtual_user', _fake_vu)

    def _obs_conclude(task, conv_id, run_id, reason='task_done'):
        seen.update(thread=threading.current_thread(), run_id=run_id, reason=reason)
        return {'runId': run_id, 'status': 'concluded', 'reason': reason}
    monkeypatch.setattr(ap, '_emit_run_concluded_event', _obs_conclude)

    cleared = {'marker': [], 'run_pin': []}
    import lib.message_queue as _mq
    monkeypatch.setattr(_mq, 'clear_autopilot_marker',
                        lambda cid, *, user_id:
                        cleared['marker'].append((cid, user_id)))
    monkeypatch.setattr(ap, '_clear_run_id',
                        lambda cid, *, user_id:
                        cleared['run_pin'].append((cid, user_id)))
    events = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: events.append(ev))

    result = ap.maybe_run_autopilot(_task_done_task())

    assert result is None
    # Conclude ran SYNCHRONOUSLY on the calling thread (no async daemon).
    assert seen['thread'] is main_thread
    assert seen['run_id'] == 'ar-td'
    assert seen['reason'] == 'task_done'
    # The turn settled: marker + run pin cleared, vu_cancel emitted.
    assert cleared['marker'] == [('conv-td', 1)]
    assert cleared['run_pin'] == [('conv-td', 1)]
    assert any(ev.get('type') == 'autopilot_vu_cancel' for ev in events)


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
