"""Tests for the Pillar #7 human↔brain status lane (lib/conversations/project_status.py).

Proves:
  • collect_pillar_state reads LIVE state across all six pillars into the
    evidence dict (board/charter/pending/presence/feed/digest).
  • the synthesis PROMPT actually carries the live pillar values (north-star +
    epic titles + counts) — NEUTER: stub collect_pillar_state to empty and the
    live values vanish from the prompt, proving the synthesis is NOT a stub and
    genuinely reads live pillar state.
  • the staleness gate elides the LLM when the pillar-state fingerprint is
    unchanged, and re-synthesizes when it moves.
  • snapshots are APPEND-ONLY with a monotonic per-project seq + bounded
    retention; the history trail is newest-first.
  • answer_status_question is READ-ONLY (appends NO snapshot).
  • the status memory is HUMAN-FACING ONLY — it is not on the system-context
    injection path.

DB-free where possible; persistence uses a real in-memory sqlite3 connection
(the module uses `?` placeholders + db.commit(), so a plain sqlite3 conn with
row_factory=Row is a faithful stand-in). Pillar reads are lazily imported, so
we monkeypatch the SOURCE modules.
"""

import sqlite3

import pytest

import lib.conversations.project_status as pstat

pytestmark = pytest.mark.unit


# ── A real in-memory DB for the append-only snapshot store ──────────────

def _make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE project_status_snapshots ('
        ' project_path TEXT NOT NULL, seq INTEGER NOT NULL, '
        ' snapshot_id TEXT NOT NULL DEFAULT \'\', '
        ' narrative TEXT NOT NULL DEFAULT \'\', '
        ' pillar_state TEXT NOT NULL DEFAULT \'{}\', '
        ' trigger TEXT NOT NULL DEFAULT \'manual\', '
        ' ts INTEGER NOT NULL DEFAULT 0, '
        ' PRIMARY KEY (project_path, seq))')
    conn.commit()
    return conn


@pytest.fixture
def db(monkeypatch):
    conn = _make_db()
    monkeypatch.setattr(pstat, 'get_thread_db', lambda *a, **k: conn)
    yield conn
    conn.close()


# ── Fake LIVE pillar state (what the six pillar reads return) ───────────

_NORTH_STAR = 'Ship the append-only status lane so the human can see project drift.'
_EPIC_TITLE = 'Refactor the parser subsystem'


def _wire_pillars(monkeypatch, *, board=None, charter=None, pending=0,
                  peers=None, feed=None, digest=None):
    """Monkeypatch the SOURCE modules collect_pillar_state lazily imports."""
    import lib.conversations.project_board as pb
    import lib.conversations.project_charter as pc
    import lib.presence.registry as pr
    import lib.conversations.project_feed as pf
    import lib.conversations.project_summary as ps

    _board = board if board is not None else {
        'tasks': [{'title': _EPIC_TITLE, 'status': 'claimed',
                   'owner_conv_id': 'convA', 'kind': 'epic'}],
        'open': 2, 'claimed': 1, 'done': 5, 'blocked': 0,
    }
    _charter = charter if charter is not None else {
        'exists': True, 'version': 8, 'content': _NORTH_STAR,
        'decisions': [{'text': 'No fan-out verb — the lane is 1:1.'}],
    }
    monkeypatch.setattr(pb, 'read_board', lambda p: _board)
    monkeypatch.setattr(pc, 'read_charter', lambda p: _charter)
    monkeypatch.setattr(pc, 'pending_proposals', lambda p: [{}] * pending)
    monkeypatch.setattr(pr, 'snapshot',
                        lambda p: {'peers': peers if peers is not None
                                   else [{'convId': 'convA'}, {'convId': 'convB'}]})
    monkeypatch.setattr(pf, 'read_project_feed',
                        lambda p, **k: feed if feed is not None
                        else {'events': []})
    monkeypatch.setattr(ps, 'project_digest_entries',
                        lambda p, **k: digest if digest is not None else [])


class _DispatchSpy:
    """Records the messages passed to dispatch_chat; returns a canned answer."""

    def __init__(self, answer='All tracking. No drift.'):
        self.calls = []
        self.answer = answer

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return (self.answer, {})


def _wire_llm(monkeypatch, spy):
    import lib.llm_dispatch as ld
    monkeypatch.setattr(ld, 'dispatch_chat', spy)


# ════════════════════════════════════════════════════════════════════
#  1. collect_pillar_state reads all six pillars into the evidence dict
# ════════════════════════════════════════════════════════════════════

def test_collect_pillar_state_reads_all_pillars(monkeypatch):
    _wire_pillars(monkeypatch, pending=3)
    state = pstat.collect_pillar_state('/proj/x')
    assert state['epicsOpen'] == 2
    assert state['epicsClaimed'] == 1
    assert state['epicsDone'] == 5
    assert state['pendingDecisions'] == 3
    assert state['charterExists'] is True
    assert state['charterVersion'] == 8
    assert state['northStar'] == _NORTH_STAR
    assert state['activePeers'] == 2
    # The claimed epic is surfaced as in-flight with its owner.
    assert any(e['title'] == _EPIC_TITLE and e['owner'] == 'convA'
               for e in state['epicsInFlight'])


def test_collect_pillar_state_degrades_when_a_pillar_raises(monkeypatch):
    import lib.conversations.project_board as pb
    _wire_pillars(monkeypatch)

    def _boom(_p):
        raise RuntimeError('board down')
    monkeypatch.setattr(pb, 'read_board', _boom)
    # Never raises; board fields degrade to defaults, other pillars still read.
    state = pstat.collect_pillar_state('/proj/x')
    assert state['epicsOpen'] == 0
    assert state['charterVersion'] == 8   # charter pillar still read


# ════════════════════════════════════════════════════════════════════
#  2. The synthesis PROMPT carries live pillar values (+ NEUTER)
# ════════════════════════════════════════════════════════════════════

def test_synthesis_prompt_carries_live_pillar_state(monkeypatch):
    """generate_narrative must feed the LIVE north-star + epic title into the
    LLM prompt — proving the synthesis genuinely reads live pillar state."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)

    state = pstat.collect_pillar_state('/proj/x')
    narrative = pstat.generate_narrative(state)
    assert narrative == 'All tracking. No drift.'
    assert len(spy.calls) == 1
    prompt = spy.calls[0][-1]['content']   # the user message
    assert _NORTH_STAR in prompt, 'live north-star missing from synthesis prompt'
    assert _EPIC_TITLE in prompt, 'live in-flight epic missing from synthesis prompt'


def test_NC_synthesis_on_empty_state_loses_live_values(monkeypatch):
    """NEUTER: if the pillar state is EMPTY (as a stub would produce), the live
    values are absent from the prompt. This is the mirror of the positive test:
    it proves the positive assertion is load-bearing on real pillar reads, not
    a coincidence — an empty/stubbed collect can never carry the live values."""
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    empty_state = {
        'epicsOpen': 0, 'epicsClaimed': 0, 'epicsDone': 0, 'epicsBlocked': 0,
        'epicsInFlight': [], 'pendingDecisions': 0, 'charterExists': False,
        'charterVersion': 0, 'northStar': '', 'decisions': [],
        'activePeers': 0, 'recentBlocks': [], 'siblings': [],
    }
    pstat.generate_narrative(empty_state)
    prompt = spy.calls[0][-1]['content']
    assert _NORTH_STAR not in prompt
    assert _EPIC_TITLE not in prompt
    # The empty state renders the explicit "no charter" marker instead.
    assert 'no charter committed yet' in prompt


# ════════════════════════════════════════════════════════════════════
#  3. Staleness gate: no LLM when fingerprint unchanged; re-synth when moved
# ════════════════════════════════════════════════════════════════════

def test_staleness_gate_elides_llm_when_unchanged(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)

    snap1 = pstat.build_status_snapshot('/proj/x', trigger='manual')
    assert snap1 is not None and snap1['seq'] == 1
    assert len(spy.calls) == 1

    # Second build, nothing changed → cached, NO second LLM call, no new row.
    snap2 = pstat.build_status_snapshot('/proj/x', trigger='on_open')
    assert len(spy.calls) == 1, 'LLM was called despite unchanged pillar state'
    assert snap2['seq'] == 1
    assert pstat.read_status_history('/proj/x')['maxSeq'] == 1


def test_staleness_gate_resynthesizes_when_state_moves(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)

    pstat.build_status_snapshot('/proj/x', trigger='manual')
    assert len(spy.calls) == 1

    # An epic completes → done count moves → fingerprint changes → re-synth.
    _wire_pillars(monkeypatch, board={
        'tasks': [], 'open': 2, 'claimed': 0, 'done': 6, 'blocked': 0})
    snap2 = pstat.build_status_snapshot('/proj/x', trigger='epic_completed')
    assert len(spy.calls) == 2, 'LLM not re-called after pillar state moved'
    assert snap2['seq'] == 2


def test_force_bypasses_staleness_gate(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    pstat.build_status_snapshot('/proj/x', trigger='manual')
    pstat.build_status_snapshot('/proj/x', trigger='manual', force=True)
    assert len(spy.calls) == 2


# ════════════════════════════════════════════════════════════════════
#  4. Append-only history + monotonic seq + retention
# ════════════════════════════════════════════════════════════════════

def test_history_is_append_only_and_newest_first(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)

    for done in (5, 6, 7):
        _wire_pillars(monkeypatch, board={
            'tasks': [], 'open': 1, 'claimed': 0, 'done': done, 'blocked': 0})
        pstat.build_status_snapshot('/proj/x', trigger='epic_completed')

    hist = pstat.read_status_history('/proj/x')
    seqs = [s['seq'] for s in hist['snapshots']]
    assert seqs == [3, 2, 1], f'history not newest-first / monotonic: {seqs}'
    assert hist['maxSeq'] == 3


def test_retention_prunes_beyond_keep(db, monkeypatch):
    monkeypatch.setattr(pstat, '_SNAPSHOTS_KEEP', 3)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    for done in range(10):
        _wire_pillars(monkeypatch, board={
            'tasks': [], 'open': 1, 'claimed': 0, 'done': done, 'blocked': 0})
        pstat.build_status_snapshot('/proj/x', trigger='epic_completed')
    hist = pstat.read_status_history('/proj/x', limit=200)
    # Only the last _SNAPSHOTS_KEEP rows survive; seq keeps climbing.
    assert len(hist['snapshots']) == 3
    assert [s['seq'] for s in hist['snapshots']] == [10, 9, 8]


# ════════════════════════════════════════════════════════════════════
#  4b. get_status_view is NON-BLOCKING (the "stuck on Synthesizing" fix)
# ════════════════════════════════════════════════════════════════════

def test_get_status_view_returns_cached_and_delegates_nonblocking_warm(db, monkeypatch):
    """The tab-open path must return the CACHED snapshot instantly and hand the
    (possibly LLM) re-synth to a NON-BLOCKING warm — never synthesize on the
    calling thread. This is the fix for the tab hanging on 'Synthesizing…'."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)

    # Seed one snapshot (one real synthesis).
    seeded = pstat.build_status_snapshot('/proj/x', trigger='manual')
    assert seeded['seq'] == 1 and len(spy.calls) == 1

    # Now the project moves (an epic finishes) → the view is stale.
    _wire_pillars(monkeypatch, board={
        'tasks': [], 'open': 2, 'claimed': 0, 'done': 6, 'blocked': 0})

    # Spy the warm so no background thread runs; capture how it was invoked.
    warm_calls = []
    monkeypatch.setattr(pstat, 'build_status_snapshot',
                        lambda p, **kw: warm_calls.append(kw))

    view = pstat.get_status_view('/proj/x', limit=30)

    # Returned the CACHED latest INSTANTLY (still seq 1) with the flag set.
    assert view['latest']['seq'] == 1
    assert view['maxSeq'] == 1
    assert view['refreshing'] is True
    # NO synchronous LLM call happened on the calling thread.
    assert len(spy.calls) == 1, 'get_status_view synthesized synchronously (blocked)'
    # The warm was delegated NON-BLOCKING.
    assert len(warm_calls) == 1 and warm_calls[0].get('blocking') is False


def test_get_status_view_not_refreshing_when_quiescent(db, monkeypatch):
    """A settled project (fingerprint unchanged) reports refreshing=False and
    does NOT trigger any warm — repeated tab-opens are free."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    pstat.build_status_snapshot('/proj/x', trigger='manual')

    warm_calls = []
    monkeypatch.setattr(pstat, 'build_status_snapshot',
                        lambda p, **kw: warm_calls.append(kw))
    view = pstat.get_status_view('/proj/x')
    assert view['latest']['seq'] == 1
    assert view['refreshing'] is False
    assert warm_calls == []


def test_get_status_view_first_open_flags_refreshing_no_snapshot(db, monkeypatch):
    """First-ever open (no snapshot yet) → latest None but refreshing=True so
    the client shows a skeleton and polls, instead of a permanent empty state."""
    _wire_pillars(monkeypatch)
    warm_calls = []
    monkeypatch.setattr(pstat, 'build_status_snapshot',
                        lambda p, **kw: warm_calls.append(kw))
    view = pstat.get_status_view('/proj/x')
    assert view['latest'] is None and view['maxSeq'] == 0
    assert view['refreshing'] is True
    assert len(warm_calls) == 1 and warm_calls[0].get('blocking') is False


def test_get_status_view_force_warms_even_when_quiescent(db, monkeypatch):
    """refresh=1 (force) warms a fresh snapshot even if the fingerprint is
    unchanged — the manual Refresh button."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    pstat.build_status_snapshot('/proj/x', trigger='manual')

    warm_calls = []
    monkeypatch.setattr(pstat, 'build_status_snapshot',
                        lambda p, **kw: warm_calls.append(kw))
    view = pstat.get_status_view('/proj/x', force=True)
    assert view['refreshing'] is True
    assert len(warm_calls) == 1
    assert warm_calls[0].get('force') is True and warm_calls[0].get('blocking') is False


# ════════════════════════════════════════════════════════════════════
#  5. answer_status_question is READ-ONLY (writes no snapshot)
# ════════════════════════════════════════════════════════════════════

def test_ask_is_read_only(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy(answer='Nothing is blocked right now.')
    _wire_llm(monkeypatch, spy)

    res = pstat.answer_status_question('/proj/x', 'What is blocked?')
    assert res['ok'] is True
    assert res['answer'] == 'Nothing is blocked right now.'
    # The question rode into the prompt.
    assert 'What is blocked?' in spy.calls[0][-1]['content']
    # CRITICAL: no snapshot was appended by an ask.
    assert pstat.read_status_history('/proj/x')['maxSeq'] == 0


def test_ask_rejects_empty(db, monkeypatch):
    _wire_pillars(monkeypatch)
    res = pstat.answer_status_question('/proj/x', '   ')
    assert res['ok'] is False and res['error'] == 'empty question'


# ════════════════════════════════════════════════════════════════════
#  6. status_line = first sentence of the latest narrative
# ════════════════════════════════════════════════════════════════════

def test_status_line_is_first_sentence(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy(answer='We are on track. Two epics remain open. No drift.')
    _wire_llm(monkeypatch, spy)
    pstat.build_status_snapshot('/proj/x', trigger='manual')
    line = pstat.status_line('/proj/x')
    assert line == 'We are on track.'


def test_status_line_empty_when_no_snapshot(db):
    assert pstat.status_line('/proj/x') == ''


# ════════════════════════════════════════════════════════════════════
#  7. HUMAN-FACING ONLY — not on the system-context injection path
# ════════════════════════════════════════════════════════════════════

def test_status_memory_not_in_system_context_source():
    """The status lane must never be injected into sibling agent prompts. Guard
    at the source: lib/tasks_pkg/system_context.py must not reference the status
    module / its synthesis entry points."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'lib', 'tasks_pkg', 'system_context.py'),
              encoding='utf-8') as f:
        src = f.read()
    for banned in ('project_status', 'build_status_snapshot', 'status_line',
                   'collect_pillar_state', 'read_status_history'):
        assert banned not in src, (
            f'system_context.py references {banned!r} — the human-facing status '
            f'lane must NOT be on the ambient prompt-injection path')
