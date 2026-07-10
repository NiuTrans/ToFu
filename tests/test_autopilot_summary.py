"""tests/test_autopilot_summary.py — Autopilot run close-out summary reporter.

Covers the "fold the run + summary report at the end" feature:
  • ``run_summary_reporter`` is READ-ONLY — its sub-task config strips every
    tool-enabling feature so the reporter only synthesises the existing
    transcript (no new state changes, cheap). This is the negative-control:
    reverting the read-only stripping makes ``test_reporter_subconfig_is_read_only``
    fail.
  • the reporter prompt encodes the objective-anchored debrief structure
    (outcome verdict + what was done + verification + gaps) and is honest.
  • ``REPORTER_PROMPT_VERSION`` is content-derived (stale-process detection),
    mirroring ``VU_PROMPT_VERSION``.
  • ``_store_run_record`` persists ONE authoritative per-run record in the
    conversation SIDECAR (``settings.autopilotSummaries[runId]``) carrying BOTH
    the terminal ``status='concluded'`` + ``reason`` AND the optional close-out
    ``content`` — a human-only record, NOT a ``role='assistant'`` chat message.
    ``_store_run_summary`` is the clean-close-out wrapper (reason=task_done).
    The negative control here re-appends it as a message and asserts that fails
    the "not a message" contract.
  • the ``autopilot_run_concluded`` event is registered in the streaming
    contract and carries the sidecar ``record`` (both the concluded status and
    the optional report) — the single BACKEND-AUTHORITATIVE run-end fact.

No live LLM / orchestrator — ``_run_single_turn`` is stubbed.
"""

import hashlib
import json

import pytest


# ── reporter prompt: structure + honesty + version marker ──────────────

def test_reporter_prompt_has_debrief_structure_and_honesty():
    from lib.tasks_pkg.autopilot import _REPORTER_ROLE_PROMPT as p
    low = p.lower()
    # Objective-anchored debrief sections.
    assert 'objective' in low
    assert 'outcome' in low
    assert 'verification' in low
    assert 'gaps' in low or 'risks' in low
    # Honest debrief, not a victory lap; read-only (no new tools).
    assert 'not a victory lap' in low or 'honest' in low
    assert 'do not run any tools' in low or 'do not start new work' in low
    # Readability contract: the report must be self-contained for a reader
    # who did NOT watch the run, and must translate the transcript into plain
    # language rather than replaying tool calls / internal jargon verbatim.
    assert 'self-contained' in low
    assert 'did not watch' in low
    assert 'translate' in low and 'transcribe' in low
    assert 'tool-call' in low or 'tool call' in low


def test_reporter_prompt_version_is_content_derived():
    import lib.tasks_pkg.autopilot as ap
    expected = hashlib.sha256(
        ap._REPORTER_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]
    assert ap.REPORTER_PROMPT_VERSION == expected
    assert len(ap.REPORTER_PROMPT_VERSION) == 8


# ── run_summary_reporter: read-only sub-config (the negative control) ──

def _summary_task():
    return {
        'id': 'task-sum-test-0001',
        'convId': 'conv-sum-test',
        'config': {
            'model': 'm',
            'autopilot': True,
            # Deliberately turn things ON to prove the reporter strips them.
            'searchMode': 'multi',
            'fetchEnabled': True,
            'projectPath': '/some/project',
            'codeExecEnabled': True,
            'browserEnabled': True,
            'memoryEnabled': True,
            'swarmEnabled': True,
        },
        'messages': [
            {'role': 'user', 'content': 'Ship a working feature.'},
            {'role': 'assistant', 'content': 'Done — I built X and tests pass.'},
            {'role': 'user', 'content': 'keep going', '_isVirtualUser': True},
            {'role': 'assistant', 'content': 'Refined X, verified the edge case.'},
        ],
    }


def test_reporter_subconfig_is_read_only(monkeypatch):
    """The reporter sub-task must run with EVERY tool-enabling feature OFF.

    NEGATIVE CONTROL: this is the assertion that the read-only stripping in
    ``run_summary_reporter`` exists. Reverting that stripping (leaving the
    parent's searchMode/projectPath/etc. on the sub-config) makes this fail.
    """
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    captured = {}

    def _fake_turn(sub_task):
        captured['cfg'] = dict(sub_task.get('config') or {})
        captured['messages'] = sub_task.get('messages') or []
        sub_task['toolRounds'] = []
        return {'content': 'Objective: ship X.\nOutcome: met.\nDone: built X.'}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Ship a working feature.')

    report = ap.run_summary_reporter(_summary_task())
    assert report is not None
    assert 'Outcome' in report['text']

    cfg = captured['cfg']
    assert cfg.get('searchMode') == 'off'
    assert cfg.get('fetchEnabled') is False
    assert cfg.get('projectPath') == ''
    assert cfg.get('codeExecEnabled') is False
    assert cfg.get('browserEnabled') is False
    assert cfg.get('memoryEnabled') is False
    assert cfg.get('swarmEnabled') is False
    assert cfg.get('autopilot') is False
    assert cfg.get('endpointMode') is False

    # The directive carries the reporter role + objective anchor + version.
    directive = captured['messages'][-1]
    assert directive.get('_isReporterDirective') is True
    assert directive.get('_reporterPromptVersion') == ap.REPORTER_PROMPT_VERSION
    assert 'ORIGINAL OBJECTIVE' in directive['content']
    assert 'Ship a working feature.' in directive['content']


def test_reporter_empty_output_returns_none(monkeypatch):
    """An empty report produces None (nothing to fold/show)."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    monkeypatch.setattr(orch, '_run_single_turn',
                        lambda st: (st.__setitem__('toolRounds', []),
                                    {'content': '   '})[1])
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Obj.')
    assert ap.run_summary_reporter(_summary_task()) is None


def test_reporter_error_returns_none(monkeypatch):
    """A sub-task error skips the report (non-fatal)."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    monkeypatch.setattr(orch, '_run_single_turn',
                        lambda st: {'error': 'llm down'})
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Obj.')
    assert ap.run_summary_reporter(_summary_task()) is None


# ── event contract ────────────────────────────────────────────────────

def test_autopilot_run_concluded_event_registered():
    from lib.agent_core.events import EventType, is_registered, get_event_spec
    # The unified BACKEND-AUTHORITATIVE run-end fact (renamed from the old
    # summary-only 'autopilot_summary'), fired on BOTH close-out paths.
    assert EventType.AUTOPILOT_RUN_CONCLUDED == 'autopilot_run_concluded'
    assert is_registered(EventType.AUTOPILOT_RUN_CONCLUDED)
    assert not hasattr(EventType, 'AUTOPILOT_SUMMARY')
    spec = get_event_spec(EventType.AUTOPILOT_RUN_CONCLUDED)
    assert 'runId' in spec.fields
    # The payload carries the SIDECAR record (`record`) — both the concluded
    # status/reason AND the optional report — not a chat message.
    assert 'record' in spec.fields
    assert 'summary' not in spec.fields
    assert 'summaryMessage' not in spec.fields


# ── _store_run_summary: SIDECAR persistence, NOT a chat message ────────

def test_store_run_summary_writes_sidecar_not_messages(monkeypatch):
    """The summary must land in settings.autopilotSummaries[runId] as a
    human-only record — never as a role='assistant' row in `messages`.

    NEGATIVE CONTROL: re-pointing _store_run_summary at the message list
    (appending a role='assistant' summary row) makes the "messages untouched"
    and "no role/_msgId" assertions fail.
    """
    import lib.tasks_pkg.autopilot as ap

    # In-memory fake DB capturing the settings/messages UPDATE.
    state = {'messages': '[{"role":"user","content":"hi"}]', 'settings': '{}'}

    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def __init__(self, row):
                    self._row = row
                def fetchone(self):
                    return self._row
            if 'SELECT settings' in sql:
                return _R((state['settings'],))
            if 'SELECT messages' in sql:
                return _R((state['messages'],))
            return _R(None)

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql:
            state['settings'] = params[0]
        elif 'messages=' in sql:
            # The migration must NOT touch messages — capture if it does.
            state['messages'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)

    rec = ap._store_run_summary('conv-x', 'ar-1', 'Outcome: shipped X.',
                                translated='结果：已交付 X。')
    assert rec is not None
    # The record is NOT a message: no role, no _msgId.
    assert 'role' not in rec
    assert '_msgId' not in rec
    assert rec['runId'] == 'ar-1'
    assert rec['content'] == 'Outcome: shipped X.'
    assert rec['translatedContent'] == '结果：已交付 X。'
    # ★ The record now carries the terminal fold-fact too (clean close-out).
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'task_done'

    # It landed in the sidecar, keyed by runId.
    settings = json.loads(state['settings'])
    assert 'autopilotSummaries' in settings
    assert settings['autopilotSummaries']['ar-1']['content'] == 'Outcome: shipped X.'
    assert settings['autopilotSummaries']['ar-1']['status'] == 'concluded'

    # The messages column was NOT touched (the summary is not a chat message).
    msgs = json.loads(state['messages'])
    assert len(msgs) == 1
    assert msgs[0]['content'] == 'hi'
    for m in msgs:
        assert not m.get('_isAutopilotSummary')


def test_store_run_summary_multiple_runs_keyed_by_runid(monkeypatch):
    """Two runs in one conversation store side-by-side under their runIds."""
    import lib.tasks_pkg.autopilot as ap

    state = {'settings': '{}'}

    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def fetchone(_self):
                    return (state['settings'],)
            return _R()

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql:
            state['settings'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)

    ap._store_run_summary('conv-y', 'ar-1', 'Report A')
    ap._store_run_summary('conv-y', 'ar-2', 'Report B')
    settings = json.loads(state['settings'])
    summaries = settings['autopilotSummaries']
    assert summaries['ar-1']['content'] == 'Report A'
    assert summaries['ar-2']['content'] == 'Report B'


# ── _store_run_record: the manual-stop (bare, no report) concluded fact ──

def _fake_settings_db(monkeypatch, state):
    """Wire a monkeypatched DB whose only column is settings JSON.

    ``_store_run_record`` / ``conclude_run`` route settings writes through
    ``lib.conversations.settings_store.update_conversation_settings``, which
    binds ``get_thread_db`` / ``db_execute_with_retry`` in the settings_store
    namespace at import — so those are the names that must be patched (patching
    ``lib.database.*`` alone would not intercept the settings write). We patch
    BOTH namespaces so the message-list path (``_append_vu_message_to_conv``,
    still on ``lib.database``) and the settings path both hit the fake.
    """
    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def fetchone(_self):
                    return (state['settings'],)
            return _R()

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql or 'settings=' in sql:
            state['settings'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)


def test_store_run_record_manual_stop_is_concluded_without_report(monkeypatch):
    """A manual stop writes a concluded record with a reason but NO content.

    This is the crux of the symmetric-conclude fix: the manual-stop path now
    produces a BACKEND-AUTHORITATIVE fold-fact (status=concluded, reason=
    stopped) that the frontend keys on — instead of the frontend having to
    infer run-end from stream/task absence.
    """
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    rec = ap._store_run_record('conv-z', 'ar-stop', reason='stopped')
    assert rec is not None
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'stopped'
    assert 'content' not in rec           # a manual stop has no report
    assert 'role' not in rec and '_msgId' not in rec  # not a chat message

    stored = json.loads(state['settings'])['autopilotSummaries']['ar-stop']
    assert stored['status'] == 'concluded'
    assert stored['reason'] == 'stopped'
    assert 'content' not in stored


def test_conclude_run_writes_authoritative_stopped_record(monkeypatch):
    """conclude_run() is the manual-stop close-out seam: it resolves the run id,
    writes the BACKEND-AUTHORITATIVE concluded(stopped) record, and clears the
    run pin so the next run is fresh.

    NEGATIVE CONTROL: making conclude_run a no-op (returning None without
    writing the record) makes this test fail — proving the manual-stop fold
    depends on the backend fact, not on any frontend inference.
    """
    import lib.tasks_pkg.autopilot as ap
    # Seed a conv whose settings pin a live run id + carry a VU turn stamp.
    state = {'settings': json.dumps({'autopilotRunId': 'ar-live'}),
             'messages': json.dumps([
                 {'role': 'user', 'content': 'obj'},
                 {'role': 'user', 'content': 'vu', '_isVirtualUser': True,
                  '_autopilotRunId': 'ar-live'}])}

    class _FakeDB:
        def execute(self, sql, params=None):
            class _R:
                def __init__(self, row):
                    self._row = row
                def fetchone(self):
                    return self._row
            if 'SELECT settings, messages' in sql:
                return _R((state['settings'], state['messages']))
            if 'SELECT settings' in sql:
                return _R((state['settings'],))
            return _R(None)

    def _fake_retry(db, sql, params):
        if 'SET settings' in sql:
            state['settings'] = params[0]

    import lib.conversations.settings_store as _ss
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_db, 'db_execute_with_retry', _fake_retry)
    monkeypatch.setattr(_ss, 'get_thread_db', lambda domain: _FakeDB())
    monkeypatch.setattr(_ss, 'db_execute_with_retry', _fake_retry)

    rec = ap.conclude_run('conv-live', reason='stopped')
    assert rec is not None
    assert rec['runId'] == 'ar-live'
    assert rec['status'] == 'concluded'
    assert rec['reason'] == 'stopped'
    assert 'content' not in rec  # a manual stop has no report

    settings = json.loads(state['settings'])
    # The concluded record landed in the sidecar under the resolved run id.
    assert settings['autopilotSummaries']['ar-live']['status'] == 'concluded'
    # The run pin was cleared so the next run mints a fresh id.
    assert 'autopilotRunId' not in settings


def test_store_run_record_task_done_reason_is_sticky(monkeypatch):
    """A later bare `stopped` conclude must NOT downgrade an earlier clean
    `task_done` record's verdict OR erase its report (they can race)."""
    import lib.tasks_pkg.autopilot as ap
    state = {'settings': '{}'}
    _fake_settings_db(monkeypatch, state)

    ap._store_run_record('conv-race', 'ar-r', reason='stopped')
    ap._store_run_record('conv-race', 'ar-r', reason='task_done',
                         text='Outcome: shipped.')
    ap._store_run_record('conv-race', 'ar-r', reason='stopped')  # racing stop

    rec = json.loads(state['settings'])['autopilotSummaries']['ar-r']
    assert rec['reason'] == 'task_done'       # never downgraded
    assert rec['content'] == 'Outcome: shipped.'  # report preserved
    assert rec['status'] == 'concluded'
