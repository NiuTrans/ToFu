"""Tests for the Pillar #7 WATCH lane (lib/conversations/project_watch.py).

Owner's non-negotiable coverage:
  (a) NEUTER proving each item's response genuinely reads LIVE pillar state
      (not a stub) — mirrors the empty-state neuter from the status lane.
  (b) the human-facing-only SOURCE guard extended to the watch module.
  (c) "promote to charter goal" actually calls the charter-commit path and does
      NOT write into the agent prompt path.
  (d) append-only response trail + bounded retention + monotonic ordering.

DB-free where possible; the store uses a real in-memory sqlite3 connection
(the module uses `?` placeholders + db.commit()). Pillar reads are lazily
imported, so we monkeypatch the SOURCE modules.
"""

import sqlite3

import pytest

import lib.conversations.project_watch as pw

pytestmark = pytest.mark.unit


# ── Real in-memory DB for the two watch tables ─────────────────────────

def _make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        'CREATE TABLE project_watch_items ('
        ' item_id TEXT PRIMARY KEY, project_path TEXT NOT NULL DEFAULT \'\','
        ' kind TEXT NOT NULL DEFAULT \'concern\', text TEXT NOT NULL DEFAULT \'\','
        ' status TEXT NOT NULL DEFAULT \'open\', promoted INTEGER NOT NULL DEFAULT 0,'
        ' response_fingerprint TEXT NOT NULL DEFAULT \'\','
        ' created_by_conv TEXT NOT NULL DEFAULT \'\','
        ' created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)')
    conn.execute(
        'CREATE TABLE project_watch_responses ('
        ' item_id TEXT NOT NULL, seq INTEGER NOT NULL,'
        ' project_path TEXT NOT NULL DEFAULT \'\', response TEXT NOT NULL DEFAULT \'\','
        ' pillar_state TEXT NOT NULL DEFAULT \'{}\', trigger TEXT NOT NULL DEFAULT \'manual\','
        ' ts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (item_id, seq))')
    conn.commit()
    return conn


@pytest.fixture
def db(monkeypatch):
    conn = _make_db()
    monkeypatch.setattr(pw, 'get_thread_db', lambda *a, **k: conn)
    yield conn
    conn.close()


_NORTH_STAR = 'Ship the watch lane so the human sees drift over time.'
_EPIC_TITLE = 'Refactor the evaluation harness'


def _wire_pillars(monkeypatch, *, done=5):
    """Monkeypatch the SOURCE modules collect_pillar_state lazily imports."""
    import lib.conversations.project_board as pb
    import lib.conversations.project_charter as pc
    import lib.presence.registry as pr
    import lib.conversations.project_feed as pf
    import lib.conversations.project_summary as ps
    monkeypatch.setattr(pb, 'read_board', lambda p: {
        'tasks': [{'title': _EPIC_TITLE, 'status': 'claimed',
                   'owner_conv_id': 'convA', 'kind': 'epic'}],
        'open': 2, 'claimed': 1, 'done': done, 'blocked': 0})
    monkeypatch.setattr(pc, 'read_charter', lambda p: {
        'exists': True, 'version': 8, 'content': _NORTH_STAR,
        'decisions': [{'text': 'No fan-out verb.'}]})
    monkeypatch.setattr(pc, 'pending_proposals', lambda p: [])
    monkeypatch.setattr(pr, 'snapshot', lambda p: {'peers': [{'convId': 'convA'}]})
    monkeypatch.setattr(pf, 'read_project_feed', lambda p, **k: {'events': []})
    monkeypatch.setattr(ps, 'project_digest_entries', lambda p, **k: [])


class _DispatchSpy:
    def __init__(self, answer='On track; no drift.'):
        self.calls = []
        self.answer = answer

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return (self.answer, {})


def _wire_llm(monkeypatch, spy):
    import lib.llm_dispatch as ld
    monkeypatch.setattr(ld, 'dispatch_chat', spy)


# ════════════════════════════════════════════════════════════════════
#  Human CRUD
# ════════════════════════════════════════════════════════════════════

def test_add_lists_and_validates(db):
    r = pw.add_watch_item('/proj/x', 'goal', 'Reach 95% rater agreement')
    assert r['ok'] and r['item']['kind'] == 'goal'
    assert pw.add_watch_item('/proj/x', 'nonsense', 'x')['error'] == 'invalid kind'
    assert pw.add_watch_item('/proj/x', 'goal', '   ')['error'] == 'empty text'
    items = pw.list_watch_items('/proj/x')['items']
    assert len(items) == 1 and items[0]['text'] == 'Reach 95% rater agreement'
    assert items[0]['responses'] == []


def test_edit_resolve_reopen_delete(db):
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Artifacts may desync')['item']['item_id']
    assert pw.edit_watch_item(item_id, text='Artifacts desync under load')['ok']
    assert pw.set_watch_status(item_id, 'resolved')['ok']
    assert pw.list_watch_items('/proj/x')['items'][0]['status'] == 'resolved'
    assert pw.set_watch_status(item_id, 'open')['ok']
    assert pw.set_watch_status(item_id, 'bogus')['error'] == 'invalid status'
    assert pw.delete_watch_item(item_id)['ok']
    assert pw.list_watch_items('/proj/x')['items'] == []


# ════════════════════════════════════════════════════════════════════
#  (a) NEUTER — the response reads LIVE pillar state
# ════════════════════════════════════════════════════════════════════

def test_response_prompt_carries_live_pillar_state(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship the watch lane')['item']['item_id']
    resp = pw.address_watch_item(item_id, trigger='manual', force=True)
    assert resp and resp['response'] == 'On track; no drift.'
    prompt = spy.calls[0][-1]['content']
    assert _NORTH_STAR in prompt, 'live north-star missing from watch-item prompt'
    assert _EPIC_TITLE in prompt, 'live in-flight epic missing from watch-item prompt'
    # The human's item text also rides into the prompt.
    assert 'Ship the watch lane' in prompt


def test_NC_response_on_empty_state_loses_live_values(db, monkeypatch):
    """NEUTER: stub the shared synthesis-source builder to ignore live state
    (as a stub generator would). The live north-star + epic then vanish from
    the prompt — proving the positive assertion is load-bearing on real pillar
    reads, not coincidence."""
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    import lib.conversations.project_status as pstat
    monkeypatch.setattr(pstat, '_build_synthesis_source',
                        lambda ps: 'STUBBED — no live state')
    item_id = pw.add_watch_item('/proj/x', 'goal', 'Ship the watch lane')['item']['item_id']
    pw.address_watch_item(item_id, trigger='manual', force=True)
    prompt = spy.calls[0][-1]['content']
    assert _NORTH_STAR not in prompt
    assert _EPIC_TITLE not in prompt


# ════════════════════════════════════════════════════════════════════
#  (d) Append-only trail + retention + monotonic ordering + staleness gate
# ════════════════════════════════════════════════════════════════════

def test_staleness_gate_and_append_only_trail(db, monkeypatch):
    _wire_pillars(monkeypatch, done=5)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Watch drift')['item']['item_id']

    r1 = pw.address_watch_item(item_id, trigger='manual')
    assert r1['seq'] == 1 and len(spy.calls) == 1

    # Nothing changed → cached, NO second LLM call, no new response row.
    r2 = pw.address_watch_item(item_id, trigger='on_open')
    assert r2['seq'] == 1 and len(spy.calls) == 1

    # Pillar state moves (an epic completes) → fresh response appended.
    _wire_pillars(monkeypatch, done=6)
    r3 = pw.address_watch_item(item_id, trigger='epic_completed')
    assert r3['seq'] == 2 and len(spy.calls) == 2

    trail = pw.list_watch_items('/proj/x')['items'][0]['responses']
    assert [t['seq'] for t in trail] == [2, 1], 'trail not newest-first / monotonic'


def test_response_retention_prunes(db, monkeypatch):
    monkeypatch.setattr(pw, '_RESPONSES_KEEP', 3)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'concern', 'Retention')['item']['item_id']
    for done in range(6):
        _wire_pillars(monkeypatch, done=done)
        pw.address_watch_item(item_id, trigger='epic_completed')
    trail = pw.list_watch_items('/proj/x', resp_limit=100)['items'][0]['responses']
    assert len(trail) == 3
    assert [t['seq'] for t in trail] == [6, 5, 4]


def test_editing_text_forces_readdress(db, monkeypatch):
    _wire_pillars(monkeypatch)
    spy = _DispatchSpy()
    _wire_llm(monkeypatch, spy)
    item_id = pw.add_watch_item('/proj/x', 'question', 'Q1?')['item']['item_id']
    pw.address_watch_item(item_id)
    assert len(spy.calls) == 1
    # Edit the text → fingerprint cleared → next address re-synthesizes even
    # though pillar state is unchanged.
    pw.edit_watch_item(item_id, text='Q1 revised?')
    pw.address_watch_item(item_id)
    assert len(spy.calls) == 2


# ════════════════════════════════════════════════════════════════════
#  (c) Promote-to-charter bridge: calls charter commit, NOT the agent path
# ════════════════════════════════════════════════════════════════════

def test_promote_calls_charter_commit_not_agent_prompt(db, monkeypatch):
    calls = {}

    def _fake_commit(project_path, *, add_decision=None, updated_by_conv='',
                     expected_version=None):
        calls['project_path'] = project_path
        calls['add_decision'] = add_decision
        return {'ok': True, 'version': 9}

    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter', _fake_commit)

    item_id = pw.add_watch_item('/proj/x', 'goal', 'Reach 95% agreement')['item']['item_id']
    res = pw.promote_watch_item(item_id, updated_by_conv='convH')
    assert res['ok'] and res['version'] == 9
    # The bridge routed through the charter-commit path with the item text.
    assert calls['project_path'] == '/proj/x'
    assert 'Reach 95% agreement' in calls['add_decision']
    assert calls['add_decision'].startswith('[Goal')
    # The item is flagged promoted.
    assert pw.list_watch_items('/proj/x')['items'][0]['promoted'] is True


def test_promote_propagates_charter_version_conflict(db, monkeypatch):
    import lib.conversations.project_charter as pc
    monkeypatch.setattr(pc, 'commit_charter',
                        lambda *a, **k: {'ok': False, 'error': 'version_conflict',
                                         'current_version': 12})
    item_id = pw.add_watch_item('/proj/x', 'concern', 'c')['item']['item_id']
    res = pw.promote_watch_item(item_id, expected_version=5)
    assert res['ok'] is False and res['error'] == 'version_conflict'
    # A failed commit must NOT flag the item promoted.
    assert pw.list_watch_items('/proj/x')['items'][0]['promoted'] is False


# ════════════════════════════════════════════════════════════════════
#  (b) HUMAN-FACING ONLY — not on the system-context injection path
# ════════════════════════════════════════════════════════════════════

def _system_context_package_sources():
    """Return {relpath: source} for EVERY .py under the system_context package.

    system_context was split from a single module into a package (commit
    86567af); the injection logic now spans several files. This guard scans the
    whole package dir so it keeps holding if the injection logic moves between
    package members. Fails loudly if the dir is absent (never a silent no-op)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = os.path.join(root, 'lib', 'tasks_pkg', 'system_context')
    assert os.path.isdir(pkg), (
        f'system_context package dir not found at {pkg!r} — the source guard '
        f'cannot verify the injection path; update this test to the new layout')
    sources = {}
    for dirpath, _dirs, files in os.walk(pkg):
        if '__pycache__' in dirpath:
            continue
        for fn in files:
            if fn.endswith('.py'):
                p = os.path.join(dirpath, fn)
                with open(p, encoding='utf-8') as f:
                    sources[os.path.relpath(p, root)] = f.read()
    assert sources, f'no .py sources found under {pkg!r}'
    return sources


def test_watch_not_in_system_context_source():
    sources = _system_context_package_sources()
    for banned in ('project_watch', 'address_watch_item', 'list_watch_items',
                   'add_watch_item', 'generate_item_response'):
        for rel, src in sources.items():
            assert banned not in src, (
                f'{rel} references {banned!r} — the watch lane must NOT '
                f'be on the ambient prompt-injection path')
