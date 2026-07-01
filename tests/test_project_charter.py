"""tests/test_project_charter.py — Pillar #2 project-brain Charter.

Covers the three-stage discipline (read / propose / commit) of the project
"north star":
  • upsert + optimistic-lock ``version`` (a stale ``expected_version`` commit
    is REJECTED, never silently clobbering);
  • ``propose_amendment`` writes EXACTLY one ``proposed_decision`` event into
    the Activity Feed and NEVER touches the ``project_charter`` table
    (proposal ≠ commit) — proven by both a grep-style source check and a
    behavioral check;
  • the ``decided`` event is produced ONLY by the commit path;
  • the system-context injection seam injects the charter block when a charter
    exists and skips it when none does.

Two MANDATORY source-level negative controls (assert-FAIL-then-restore-byte-
identical):
  • NC-1: make ``propose_amendment`` write the table → the "propose never
    writes the table" test FAILS.
  • NC-2: no-op the charter branch of the injection seam → the injection test
    FAILS.
"""

from __future__ import annotations

import importlib
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CHARTER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_charter.py')
_SYSCTX_SRC = os.path.join(ROOT, 'lib', 'tasks_pkg', 'system_context.py')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_charter')
        db.execute('DELETE FROM project_events')
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _charter_rows(flask_app, project_path):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        return db.execute('SELECT * FROM project_charter WHERE project_path=?',
                          (project_path,)).fetchall()


def _feed_kinds(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        feed = read_project_feed(project_path, limit=500)
    return [e['kind'] for e in feed['events']]


# ════════════════════════════════════════════════════════════════════
#  commit / read / optimistic lock
# ════════════════════════════════════════════════════════════════════

def test_commit_then_read(flask_app):
    from lib.conversations.project_charter import commit_charter, read_charter
    with flask_app.app_context():
        r = commit_charter('/p/c', content='Ship Pillar 2.',
                           add_decision='Use soft leases.', updated_by_conv='cA')
        assert r['ok'] and r['version'] == 1
        rec = read_charter('/p/c')
    assert rec['content'] == 'Ship Pillar 2.'
    assert rec['version'] == 1
    assert any(d['text'] == 'Use soft leases.' for d in rec['decisions'])
    assert rec['exists'] is True


def test_empty_read(flask_app):
    from lib.conversations.project_charter import read_charter
    with flask_app.app_context():
        rec = read_charter('/p/empty')
    assert rec['exists'] is False and rec['version'] == 0 and rec['content'] == ''


def test_optimistic_lock_rejects_stale_commit(flask_app):
    from lib.conversations.project_charter import commit_charter
    with flask_app.app_context():
        assert commit_charter('/p/lock', content='v1', updated_by_conv='cA')['ok']
        # version is now 1. A commit expecting version 0 must be REJECTED.
        stale = commit_charter('/p/lock', content='clobber',
                               expected_version=0, updated_by_conv='cB')
        assert stale['ok'] is False
        assert stale['error'] == 'version_conflict'
        assert stale['current_version'] == 1
        # A commit with the CORRECT expected_version succeeds.
        ok = commit_charter('/p/lock', content='v2',
                            expected_version=1, updated_by_conv='cB')
        assert ok['ok'] and ok['version'] == 2
        from lib.conversations.project_charter import read_charter
        assert read_charter('/p/lock')['content'] == 'v2'


# ════════════════════════════════════════════════════════════════════
#  propose: feed-only, NEVER the table
# ════════════════════════════════════════════════════════════════════

def test_propose_writes_feed_not_table(flask_app):
    from lib.conversations.project_charter import propose_amendment
    with flask_app.app_context():
        res = propose_amendment('/p/prop', 'cA', 'We should adopt X.')
    assert res['ok']
    # The charter table must have NO row for this project — proposal ≠ commit.
    assert _charter_rows(flask_app, '/p/prop') == []
    # Exactly one proposed_decision event in the feed.
    kinds = _feed_kinds(flask_app, '/p/prop')
    assert kinds.count('proposed_decision') == 1
    assert 'decided' not in kinds


def test_propose_source_never_touches_charter_table():
    """Source-level: propose_amendment must not contain any write to the
    project_charter table (INSERT/UPDATE/DELETE/upsert)."""
    with open(_CHARTER_SRC, encoding='utf-8') as f:
        src = f.read()
    # Isolate the propose_amendment function body.
    start = src.index('def propose_amendment(')
    end = src.index('def commit_charter(')
    body = src[start:end]
    low = body.lower()
    for forbidden in ('insert into project_charter', 'update project_charter',
                      'delete from project_charter', 'on conflict'):
        assert forbidden not in low, \
            f'propose_amendment must not write the charter table (found: {forbidden})'


def test_decided_only_from_commit(flask_app):
    """The 'decided' event is produced ONLY by commit, never by propose/read."""
    from lib.conversations.project_charter import (
        commit_charter, propose_amendment, read_charter,
    )
    with flask_app.app_context():
        propose_amendment('/p/d', 'cA', 'proposal one')
        read_charter('/p/d')
        assert 'decided' not in _feed_kinds(flask_app, '/p/d')
        commit_charter('/p/d', add_decision='committed one', updated_by_conv='cA')
    kinds = _feed_kinds(flask_app, '/p/d')
    assert kinds.count('decided') == 1


# ════════════════════════════════════════════════════════════════════
#  render block + injection seam
# ════════════════════════════════════════════════════════════════════

def test_render_block_present_and_absent(flask_app):
    from lib.conversations.project_charter import commit_charter, render_charter_block
    with flask_app.app_context():
        assert render_charter_block('/p/none') == ''
        commit_charter('/p/has', content='North star here.',
                       add_decision='Decision A.', updated_by_conv='cA')
        block = render_charter_block('/p/has')
    assert '[PROJECT CHARTER]' in block
    assert 'North star here.' in block
    assert 'Decision A.' in block


def _run_inject(flask_app, project_path, has_charter):
    """Drive the system_context charter-injection seam and return the assembled
    system text. Builds a minimal messages list + calls the public injector."""
    from lib.conversations.project_charter import commit_charter
    from lib.tasks_pkg import system_context as sc
    with flask_app.app_context():
        if has_charter:
            commit_charter(project_path, content='Injected north star.',
                           add_decision='Inj decision.', updated_by_conv='cA')
        messages = [{'role': 'user', 'content': 'hello'}]
        sc._inject_system_contexts(
            messages, project_path, True,   # project_path, project_enabled
            False, False, False, True,      # memory, search, swarm, has_real_tools
            conv_id='cTest', task=None)
    # The charter lands as a separate cache-stable block (system message or a
    # <system-reminder>-wrapped user tail). Flatten EVERY message's content
    # (string or multimodal list) so the scan can't miss it.
    parts = []
    for m in messages:
        c = m.get('content', '')
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for seg in c:
                if isinstance(seg, dict):
                    parts.append(seg.get('text', '') or '')
                elif isinstance(seg, str):
                    parts.append(seg)
    return '\n'.join(parts)


def test_injection_present_when_charter_exists(flask_app):
    out = _run_inject(flask_app, '/p/inj', has_charter=True)
    assert '[PROJECT CHARTER]' in out
    assert 'Injected north star.' in out


def test_injection_absent_when_no_charter(flask_app):
    out = _run_inject(flask_app, '/p/noinj', has_charter=False)
    assert '[PROJECT CHARTER]' not in out


# ════════════════════════════════════════════════════════════════════
#  Routes: GET /charter, POST /charter/commit
# ════════════════════════════════════════════════════════════════════

def test_route_charter_read_and_commit(flask_app, flask_client):
    import json as _json
    # commit via the human-gated route
    r = flask_client.post('/api/v1/project/charter/commit', json={
        'path': '/p/route-c', 'content': 'North star.',
        'add_decision': 'Decision via route.'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = _json.loads(r.get_data(as_text=True))
    assert body.get('version') == 1
    # read it back
    r2 = flask_client.get('/api/v1/project/charter?path=/p/route-c')
    data = _json.loads(r2.get_data(as_text=True))
    assert data['content'] == 'North star.'
    assert any(d['text'] == 'Decision via route.' for d in data['decisions'])


def test_route_charter_commit_version_conflict_409(flask_app, flask_client):
    import json as _json
    flask_client.post('/api/v1/project/charter/commit',
                      json={'path': '/p/route-409', 'content': 'v1'})
    # stale expected_version → 409
    r = flask_client.post('/api/v1/project/charter/commit', json={
        'path': '/p/route-409', 'content': 'clobber', 'expected_version': 0})
    assert r.status_code == 409
    assert _json.loads(r.get_data(as_text=True)).get('error') == 'version_conflict'


def test_route_charter_requires_path(flask_client):
    assert flask_client.get('/api/v1/project/charter').status_code == 400
    assert flask_client.post('/api/v1/project/charter/commit',
                             json={'content': 'x'}).status_code == 400


# ════════════════════════════════════════════════════════════════════
#  pending_proposals — the "awaiting you" count decrements on commit/dismiss
# ════════════════════════════════════════════════════════════════════

def test_pending_excludes_committed_proposal(flask_app):
    """A proposal resolved by a matching commit (by proposalId) is NO LONGER
    pending — the root-cause fix for the over-counting collab-bar headline."""
    from lib.conversations.project_charter import (
        commit_charter, pending_proposals, propose_amendment,
    )
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/p/pending-commit')
    with flask_app.app_context():
        res = propose_amendment(p, 'cA', 'Adopt the soft-lease board')
        pid = res['proposalId']
        assert len(pending_proposals(p)) == 1
        assert build_brain_summary(p)['pendingDecisions'] == 1
        # Commit resolving THIS proposal → it drops out of pending.
        commit_charter(p, add_decision='Adopt the soft-lease board',
                       updated_by_conv='human', resolves_proposal=pid)
        assert pending_proposals(p) == [], 'committed proposal must not stay pending'
        assert build_brain_summary(p)['pendingDecisions'] == 0, \
            'the collab-bar count must decrement after commit'


def test_pending_excludes_dismissed_proposal(flask_app):
    """A durably-dismissed (rejected) proposal drops out of pending for
    everyone — not a local DOM dismiss that evaporates on reload."""
    from lib.conversations.project_charter import (
        dismiss_proposal, pending_proposals, propose_amendment,
    )
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/p/pending-dismiss')
    with flask_app.app_context():
        res = propose_amendment(p, 'cA', 'A proposal to reject')
        pid = res['proposalId']
        assert len(pending_proposals(p)) == 1
        dismiss_proposal(p, 'human', pid)
        assert pending_proposals(p) == [], 'dismissed proposal must not stay pending'
        assert build_brain_summary(p)['pendingDecisions'] == 0


def test_pending_keeps_unrelated_proposal(flask_app):
    """Committing proposal A must NOT resolve an unrelated pending proposal B
    (the id match is per-proposal, not a blanket clear)."""
    from lib.conversations.project_charter import (
        commit_charter, pending_proposals, propose_amendment,
    )
    p = os.path.abspath('/p/pending-two')
    with flask_app.app_context():
        a = propose_amendment(p, 'cA', 'Proposal A')['proposalId']
        propose_amendment(p, 'cB', 'Proposal B')  # B stays pending
        commit_charter(p, add_decision='Proposal A', updated_by_conv='human',
                       resolves_proposal=a)
        pend = pending_proposals(p)
    texts = [x['summary'] for x in pend]
    assert len(pend) == 1 and 'Proposal B' in texts[0], \
        'only the committed proposal drops; the unrelated one remains pending'


def test_route_charter_pending_and_dismiss(flask_app, flask_client):
    import json as _json
    from lib.conversations.project_charter import propose_amendment
    p = os.path.abspath('/p/route-pending')
    with flask_app.app_context():
        pid = propose_amendment(p, 'cA', 'Route proposal')['proposalId']
    r = flask_client.get('/api/v1/project/charter/pending?path=' + p)
    assert r.status_code == 200
    data = _json.loads(r.get_data(as_text=True))
    assert len(data['pending']) == 1 and data['pending'][0]['proposalId'] == pid
    # Durable dismiss via the route → pending empties.
    rd = flask_client.post('/api/v1/project/charter/dismiss',
                           json={'path': p, 'proposalId': pid})
    assert rd.status_code == 200
    r2 = flask_client.get('/api/v1/project/charter/pending?path=' + p)
    assert _json.loads(r2.get_data(as_text=True))['pending'] == []


def test_NC3_no_exclude_filter_overcounts(flask_app):
    """THE decisive NC: no-op the 'exclude resolved' filter in
    pending_proposals → a committed proposal WRONGLY stays pending (count
    returns 1 instead of 0) — reproducing the over-count bug. Byte-identical
    restore."""
    def run():
        import lib.conversations.project_charter as pc
        importlib.reload(pc)
        p = os.path.abspath('/p/nc3')
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute("DELETE FROM project_events WHERE project_path=?", (p,))
            db.execute("DELETE FROM project_charter WHERE project_path=?", (p,))
            db.commit()
            pid = pc.propose_amendment(p, 'cA', 'Proposal X')['proposalId']
            pc.commit_charter(p, add_decision='Proposal X',
                              updated_by_conv='human', resolves_proposal=pid)
            pend = pc.pending_proposals(p)
        # With the exclude-filter disabled, the committed proposal WRONGLY
        # remains pending → the over-count bug.
        assert len(pend) == 1, \
            'NC-3: without the exclude filter, a committed proposal wrongly stays pending'

    _patch_restore(
        _CHARTER_SRC,
        "        if pid and pid in resolved:\n            continue",
        "        if False and pid and pid in resolved:  # NC-3\n            continue",
        run,
    )
    import lib.conversations.project_charter as pc
    importlib.reload(pc)


# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROLS
# ════════════════════════════════════════════════════════════════════

def _patch_restore(path, old, new, run):
    with open(path, encoding='utf-8') as f:
        original = f.read()
    assert old in original, f'anchor not found in {path}'
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original.replace(old, new, 1))
        run()
    finally:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(original)
    with open(path, encoding='utf-8') as f:
        assert f.read() == original, 'source not restored byte-identical'


def test_NC1_propose_writing_table_breaks_isolation(flask_app):
    """NC-1: make propose_amendment ALSO write the charter table → the
    'propose never writes the table' behavioral test FAILS."""
    def run():
        import lib.conversations.project_charter as pc
        importlib.reload(pc)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute("DELETE FROM project_charter WHERE project_path='/nc1'")
            get_thread_db(DOMAIN_CHAT).commit()
            pc.propose_amendment('/nc1', 'cA', 'leaky proposal')
            rows = get_thread_db(DOMAIN_CHAT).execute(
                "SELECT * FROM project_charter WHERE project_path='/nc1'").fetchall()
        # With the NC patch, propose wrote a row → isolation broken.
        assert len(rows) > 0, 'NC-1 should have caused propose to write the table'

    _patch_restore(
        _CHARTER_SRC,
        "    audit_log('charter_proposed', project_path=project_path,",
        ("    get_thread_db(DOMAIN_CHAT).execute(\n"
         "        'INSERT INTO project_charter (project_path, content, decisions, "
         "updated_by_conv, updated_at, version) VALUES (?, ?, ?, ?, ?, ?) "
         "ON CONFLICT(project_path) DO UPDATE SET content=excluded.content',\n"
         "        (project_path, proposal, '[]', conv_id or '', 0, 0))  # NC-1\n"
         "    get_thread_db(DOMAIN_CHAT).commit()  # NC-1\n"
         "    audit_log('charter_proposed', project_path=project_path,"),
        run,
    )
    import lib.conversations.project_charter as pc
    importlib.reload(pc)


def test_NC2_injection_noop_breaks_injection(flask_app):
    """NC-2: no-op the charter branch of the injection seam → the injection
    test FAILS (the charter is no longer injected)."""
    def run():
        from lib.tasks_pkg import system_context as sc
        importlib.reload(sc)
        out = _run_inject(flask_app, '/nc2', has_charter=True)
        assert '[PROJECT CHARTER]' not in out, \
            'NC-2: with the injection branch no-opped, the charter must NOT appear'

    _patch_restore(
        _SYSCTX_SRC,
        "        if _charter_block:\n"
        "            _charter_spliced = _wrap_system_reminder(_charter_block)",
        "        if False and _charter_block:  # NC-2\n"
        "            _charter_spliced = _wrap_system_reminder(_charter_block)",
        run,
    )
    from lib.tasks_pkg import system_context as sc
    importlib.reload(sc)
