"""tests/tasks_pkg/test_autopilot_summary.py — Autopilot run CLOSE-OUT (fold) fact.

The autopilot close-out REPORT layer (the dedicated LLM reporter turn,
on-demand ``summarize_run``, the report translation, and the async summary
daemon) was REMOVED. The fold's report ``content`` is now sourced from the
run's OWN terminal evidence: the last virtual-user verdict (machine tokens
stripped), forwarded by the Flow lifecycle boundary as ``report=`` — so a
concluded run always carries its own explanation of WHY it ended. What
remains, and is covered here, is the B-layer fold machinery both close-out
paths still need:

  • ``_store_run_record`` persists ONE authoritative per-run record in the
    conversation SIDECAR (``settings.autopilotSummaries[runId]``) carrying the
    terminal ``status='concluded'`` + ``reason`` (+ ``content`` when a report
    is supplied) — a human-only record, NOT a ``role='assistant'`` chat
    message.
  • ``conclude_run`` is the manual-stop close-out seam (resolve run id → write
    concluded(stopped) record → clear the run pin). A manual stop has no VU
    verdict, so it stores NO ``content``.
  • ``_emit_run_concluded_event`` is the close-out helper the Flow TASK_DONE +
    safety-cap paths route through (store record + feed pulse +
    ``autopilot_run_concluded`` SSE), forwarding ``report=`` into the record.
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
    import lib.tasks_pkg.autopilot_run_lifecycle as _apl
    import lib.tasks_pkg.autopilot_state as _aps
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    rec = _apl._store_run_record('conv-z', 'ar-stop', reason='stopped', user_id=1)
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
    import lib.tasks_pkg.autopilot_run_lifecycle as _apl
    import lib.tasks_pkg.autopilot_state as _aps
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    _apl._store_run_record('conv-race', 'ar-r', reason='stopped', user_id=1)
    _apl._store_run_record('conv-race', 'ar-r', reason='task_done', user_id=1)
    _apl._store_run_record('conv-race', 'ar-r', reason='stopped', user_id=1)  # racing stop

    rec = json.loads(state['settings'])['autopilotSummaries']['ar-r']
    assert rec['reason'] == 'task_done'       # never downgraded
    assert rec['status'] == 'concluded'


def test_conclude_run_writes_authoritative_stopped_record(monkeypatch):
    """conclude_run() is the manual-stop close-out seam: it resolves the run id,
    writes the BACKEND-AUTHORITATIVE concluded(stopped) record, and clears the
    run pin so the next run is fresh."""
    import lib.tasks_pkg.autopilot_run_lifecycle as _apl
    import lib.tasks_pkg.autopilot_state as _aps
    import lib.goal_runs.closeout as lifecycle
    # Seed a conv whose settings pin a live run id + carry a VU turn stamp.
    state = {'settings': json.dumps({'autopilotRunId': 'ar-live'}),
             'messages': json.dumps([
                 {'role': 'user', 'content': 'obj'},
                 {'role': 'user', 'content': 'vu', '_isVirtualUser': True,
                  '_autopilotRunId': 'ar-live'}])}

    _fake_settings_db(monkeypatch, state)
    # Isolate the repository-backed resolver; its behavior is covered by the
    # focused resolver tests, while this test owns close-out persistence.
    monkeypatch.setattr(lifecycle, 'resolve_recent_run_id',
                        lambda conv_id, *, user_id: 'ar-live')

    rec = _apl.conclude_run('conv-live', reason='stopped', user_id=1)
    assert rec is not None
    assert rec['runId'] == 'ar-live'
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'stopped'
    assert 'content' not in rec  # a manual stop has no report

    settings = json.loads(state['settings'])
    assert settings['autopilotSummaries']['ar-live']['status'] == 'concluded'
    # The run pin was cleared so the next run mints a fresh id.
    assert 'autopilotRunId' not in settings


# ── _emit_run_concluded_event: the close-out helper ──────────────────

def test_emit_run_concluded_event_stores_and_emits(monkeypatch):
    """The helper persists the concluded record, fires the feed pulse, and
    emits the autopilot_run_concluded SSE. With no ``report=`` the record
    stays content-free (the manual-stop shape)."""
    import lib.tasks_pkg.autopilot_run_lifecycle as _apl
    import lib.tasks_pkg.autopilot_state as _aps
    # Post-slice-3 (pt_00459503): ``_emit_run_concluded_event`` lives in
    # ``lib.tasks_pkg.autopilot_run_lifecycle`` (a leaf module). Its
    # internal calls to ``_store_run_record`` and ``_emit_run_concluded``
    # resolve from that module's OWN globals, not the facade — so we
    # patch the origin bindings there. The facade re-export identity
    # (_apl._store_run_record IS leaf._store_run_record) is separately
    # guarded by test_autopilot_markers_lazy_import_contract.py.
    import lib.goal_runs.closeout as apl

    stored = {}
    monkeypatch.setattr(
        apl, 'store_run_record',
        lambda conv_id, run_id, *, user_id, reason='task_done', text='',
             has_full_report=False:
        stored.update(conv_id=conv_id, run_id=run_id,
                      user_id=user_id, reason=reason, text=text)
        or {'runId': run_id, 'status': 'concluded', 'reason': reason})
    concluded = []
    monkeypatch.setattr(apl, 'emit_run_concluded',
                        lambda conv_id, run_id, text, cfg, *, user_id:
                        concluded.append((text, user_id)))
    events = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: events.append(ev))

    task = {'id': 'task-rc-1', 'convId': 'conv-rc', 'config': {}, '_userId': 1}
    rec = _apl._emit_run_concluded_event(task, 'conv-rc', 'ar-rc', reason='task_done')

    assert rec is not None and rec['status'] == 'concluded'
    assert stored == {
        'conv_id': 'conv-rc', 'run_id': 'ar-rc', 'user_id': 1,
        'reason': 'task_done', 'text': '',
    }
    # Feed pulse fired with EMPTY report text (no report supplied).
    assert concluded == [('', 1)]
    # The SSE run-concluded event was emitted with the record.
    assert any(ev.get('type') == 'autopilot_run_concluded'
               and ev.get('runId') == 'ar-rc' for ev in events)


def test_emit_run_concluded_event_forwards_report_into_record(monkeypatch):
    """``report=`` (the terminal VU verdict) lands in the record AND the feed
    pulse — a concluded run is never explanation-free again."""
    import lib.tasks_pkg.autopilot_run_lifecycle as _apl
    import lib.tasks_pkg.autopilot_state as _aps
    import lib.goal_runs.closeout as apl

    stored = {}
    monkeypatch.setattr(
        apl, 'store_run_record',
        lambda conv_id, run_id, *, user_id, reason='task_done', text='',
             has_full_report=False:
        stored.update(reason=reason, text=text,
                      has_full_report=has_full_report)
        or {'runId': run_id, 'status': 'concluded', 'reason': reason,
            'content': text})
    concluded = []
    monkeypatch.setattr(apl, 'emit_run_concluded',
                        lambda conv_id, run_id, text, cfg, *, user_id:
                        concluded.append(text))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: None)

    task = {'id': 'task-rc-2', 'convId': 'conv-rc2', 'config': {},
            '_userId': 1}
    rec = _apl._emit_run_concluded_event(
        task, 'conv-rc2', 'ar-rc2', reason='no_progress',
        report='Last gap: the payout path was never reconciled.')

    assert stored == {
        'reason': 'no_progress',
        'text': 'Last gap: the payout path was never reconciled.',
        'has_full_report': False,
    }
    assert concluded == ['Last gap: the payout path was never reconciled.']
    assert rec['content'] == 'Last gap: the payout path was never reconciled.'


def test_emit_run_concluded_event_none_record_short_circuits(monkeypatch):
    """A persist failure (None record) short-circuits — no pulse, no SSE."""
    import lib.tasks_pkg.autopilot_run_lifecycle as _apl
    import lib.tasks_pkg.autopilot_state as _aps
    # Post-slice-3: patch the origin binding on autopilot_run_lifecycle.
    import lib.goal_runs.closeout as apl
    monkeypatch.setattr(apl, 'store_run_record', lambda *a, **k: None)
    fired = []
    monkeypatch.setattr(apl, 'emit_run_concluded',
                        lambda *a, **k: fired.append('pulse'))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: fired.append('sse'))
    task = {'id': 't', 'convId': 'c', 'config': {}, '_userId': 1}
    assert _apl._emit_run_concluded_event(task, 'c', 'ar-x') is None
    assert fired == []


def test_store_run_record_marks_full_report_availability(monkeypatch):
    """The fold record keeps the bounded excerpt plus hasFullReport; the flag
    is sticky across a later bare re-conclude while the excerpt is unchanged."""
    import lib.tasks_pkg.autopilot_run_lifecycle as _apl
    import lib.tasks_pkg.autopilot_state as _aps
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    rec = _apl._store_run_record(
        'conv-full', 'ar-full', reason='task_done', user_id=1,
        text='excerpt of the verdict',
        has_full_report=True)
    assert rec['hasFullReport'] is True
    assert rec['content'] == 'excerpt of the verdict'

    again = _apl._store_run_record(
        'conv-full', 'ar-full', reason='task_done', user_id=1,
        text='excerpt of the verdict')
    assert again['hasFullReport'] is True  # sticky while the excerpt matches

    stored = json.loads(state['settings'])['autopilotSummaries']['ar-full']
    assert stored['hasFullReport'] is True
