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

import os

import pytest

from tests._nc_harness import patch_restore as _patch_restore

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CHARTER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_charter.py')
_CONTEXT_PROVIDERS_SRC = os.path.join(
    ROOT, 'lib', 'tasks_pkg', 'context_composer', '_providers.py')


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


@pytest.fixture(autouse=True)
def _stub_derived_background_lanes(monkeypatch):
    """Charter tests must not launch real status/watch LLM workers."""
    import lib.conversations.project_status as project_status
    import lib.conversations.project_watch as project_watch
    monkeypatch.setattr(
        project_status, 'build_status_snapshot', lambda *a, **k: None)
    monkeypatch.setattr(project_watch, 'address_open_items', lambda *a, **k: None)


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
        # content and add_decision are mutually exclusive (one call = one
        # operation), so the north star and the decision are two commits.
        assert commit_charter('/p/c', content='Ship Pillar 2.',
                              updated_by_conv='cA')['ok']
        r = commit_charter('/p/c', add_decision='Use soft leases.',
                           summary='Use soft leases.', updated_by_conv='cA')
        assert r['ok'] and r['version'] == 2
        rec = read_charter('/p/c')
    assert rec['content'] == 'Ship Pillar 2.'
    assert rec['version'] == 2
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
        commit_charter('/p/has', content='North star here.', updated_by_conv='cA')
        commit_charter('/p/has', add_decision='Decision A.',
                       summary='Decision A.', updated_by_conv='cA')
        block = render_charter_block('/p/has')
    assert '[PROJECT CHARTER]' in block
    assert 'North star here.' in block
    assert 'Decision A.' in block


def _run_inject(flask_app, project_path, has_charter):
    """Drive the system_context charter-injection seam and return the assembled
    system text. Builds a minimal messages list + calls the public injector."""
    from lib.conversations.project_charter import commit_charter
    # Resolve the injector through the _inject SUBMODULE (not the package
    # re-export): the NC neuter swaps sys.modules[...system_context._inject]
    # for a patched copy, but the package __init__'s re-exported
    # _inject_system_contexts name still points at the ORIGINAL function, so
    # calling it via the package would bypass the neuter. Look it up on the
    # submodule at call time so the swapped (neutered) copy is what runs.
    import importlib
    sc_inject = importlib.import_module('lib.tasks_pkg.system_context._inject')
    with flask_app.app_context():
        if has_charter:
            commit_charter(project_path, content='Injected north star.',
                           updated_by_conv='cA')
            commit_charter(project_path, add_decision='Inj decision.',
                           summary='Inj decision.', updated_by_conv='cA')
        messages = [{'role': 'user', 'content': 'hello'}]
        sc_inject._inject_system_contexts(
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
    # One call = one operation (content and add_decision are mutually
    # exclusive), so the north star and the decision are two requests.
    r0 = flask_client.post('/api/v1/project/charter/commit', json={
        'path': '/p/route-c', 'content': 'North star.'})
    assert r0.status_code == 200, r0.get_data(as_text=True)
    r = flask_client.post('/api/v1/project/charter/commit', json={
        'path': '/p/route-c',
        'add_decision': 'Decision via route.',
        'summary': 'Decision via route.'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = _json.loads(r.get_data(as_text=True))
    assert body.get('version') == 2
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


# ════════════════════════════════════════════════════════════════════
#  Truncation fix: a decision carries its FULL text (not the 280-char
#  feed-row summary), and the cap matches the proposal ceiling.
# ════════════════════════════════════════════════════════════════════

# A proposal longer than the feed-row cap (280) AND longer than the OLD
# decision cap (600), so it discriminates BOTH truncation points at once.
_LONG_PROPOSAL = ('SCALE-OUT ARCHITECTURE decision body. ' + ('x' * 900) +
                  ' MIDDLE-SENTINEL ' + ('y' * 900) + ' END-SENTINEL-TAIL.')


def test_pending_returns_full_proposal_not_feed_summary(flask_app):
    """pending_proposals must hand back the FULL payload.proposal text (the
    commit source), NOT the 280-char feed-row summary — the root cause of the
    truncated-decision bug."""
    from lib.conversations.project_charter import (
        pending_proposals, propose_amendment,
    )
    p = os.path.abspath('/p/full-pending')
    with flask_app.app_context():
        propose_amendment(p, 'cA', _LONG_PROPOSAL)
        pend = pending_proposals(p)
    assert len(pend) == 1
    # The full text survives end-to-end (both sentinels + full length), not the
    # 280-char feed summary.
    assert pend[0]['summary'] == _LONG_PROPOSAL[:2400]
    assert len(pend[0]['summary']) > 280
    assert 'END-SENTINEL-TAIL.' in pend[0]['summary']


def test_commit_from_full_pending_stores_full_decision(flask_app):
    """The whole chain: propose(long) → pending(full) → commit(full) stores a
    decision that is NOT clipped at 280 and survives into read + the injected
    [PROJECT CHARTER] block."""
    from lib.conversations.project_charter import (
        commit_charter, pending_proposals, propose_amendment, read_charter,
        render_charter_block,
    )
    p = os.path.abspath('/p/full-commit')
    with flask_app.app_context():
        pid = propose_amendment(p, 'cA', _LONG_PROPOSAL)['proposalId']
        full = pending_proposals(p)[0]['summary']
        commit_charter(p, add_decision=full, updated_by_conv='human',
                       resolves_proposal=pid)
        rec = read_charter(p)
        block = render_charter_block(p)
    stored = rec['decisions'][0]['text']
    assert len(stored) > 280 and len(stored) == len(_LONG_PROPOSAL[:2400])
    assert 'END-SENTINEL-TAIL.' in stored
    # And the prompt-injected block carries the full text end-to-end.
    assert 'END-SENTINEL-TAIL.' in block


def test_decision_cap_matches_proposal_ceiling(flask_app):
    """The committed-decision cap MUST equal the proposal ceiling so a commit
    never re-clips a full proposal (both are the single _DECISION_MAX_CHARS)."""
    import lib.conversations.project_charter as pc
    assert pc._DECISION_MAX_CHARS >= 2400, \
        'the decision cap must be raised to the proposal ceiling (was 600)'


def test_repair_resources_truncated_decision_from_feed(flask_app):
    """repair_truncated_decisions re-sources a decision that was stored
    truncated (a strict prefix of a longer proposal payload) back to its full
    text. Idempotent — a second run repairs nothing."""
    from lib.conversations.project_charter import (
        propose_amendment, read_charter, repair_truncated_decisions,
    )
    from lib.database import DOMAIN_CHAT, get_thread_db
    import json as _json
    import time as _time
    p = os.path.abspath('/p/repair')
    with flask_app.app_context():
        # Seed a feed proposal with the FULL text (payload.proposal).
        propose_amendment(p, 'cA', _LONG_PROPOSAL)
        # Simulate the OLD bug: a committed decision stored as the 280-char
        # PREFIX of that proposal (exactly what the feed-summary commit did).
        truncated = _LONG_PROPOSAL[:280]
        db = get_thread_db(DOMAIN_CHAT)
        decisions = [{'text': truncated, 'by_conv': 'human',
                      'ts': int(_time.time() * 1000)}]
        db.execute(
            'INSERT INTO project_charter (project_path, content, decisions, '
            'updated_by_conv, updated_at, version) VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(project_path) DO UPDATE SET decisions=excluded.decisions, '
            'version=excluded.version',
            (p, '', _json.dumps(decisions), 'human', 1, 1))
        db.commit()
        assert len(read_charter(p)['decisions'][0]['text']) == 280

        res = repair_truncated_decisions(p)
        assert res['ok'] and res['repaired'] == 1
        rec = read_charter(p)
        fixed = rec['decisions'][0]['text']
        assert len(fixed) == len(_LONG_PROPOSAL[:2400]) > 280
        assert 'END-SENTINEL-TAIL.' in fixed
        assert rec['version'] == 2  # bumped once

        # Idempotent: nothing left to repair.
        res2 = repair_truncated_decisions(p)
        assert res2['ok'] and res2['repaired'] == 0


def test_NC_pending_summary_first_reintroduces_truncation(flask_app):
    """NC: revert pending_proposals to summary-FIRST (the old bug) → a long
    proposal comes back as the 280-char feed summary, so
    test_pending_returns_full_proposal_not_feed_summary's length assertion
    FAILS. Byte-identical restore."""
    def run():
        import lib.conversations.project_charter as pc
        p = os.path.abspath('/p/nc-trunc')
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_events WHERE project_path=?', (p,))
            db.commit()
            pc.propose_amendment(p, 'cA', _LONG_PROPOSAL)
            pend = pc.pending_proposals(p)
        # With summary-first restored, the returned text is the 280-char cap.
        assert len(pend[0]['summary']) == 280, \
            'NC: summary-first must reintroduce the 280-char truncation'
        assert 'END-SENTINEL-TAIL.' not in pend[0]['summary']

    _patch_restore(
        _CHARTER_SRC,
        "            'summary': payload.get('proposal', '') or e.get('summary', ''),",
        "            'summary': e.get('summary', '') or payload.get('proposal', ''),  # NC",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  EDIT / DELETE a committed decision + DELETE the whole charter
#  (human-gated, optimistic-locked, feed-audited)
# ════════════════════════════════════════════════════════════════════

def test_update_decision_edits_in_place(flask_app):
    from lib.conversations.project_charter import (
        commit_charter, read_charter, update_decision,
    )
    p = os.path.abspath('/p/edit-dec')
    with flask_app.app_context():
        commit_charter(p, add_decision='Original A', updated_by_conv='cA')
        commit_charter(p, add_decision='Original B', updated_by_conv='cA')
        ver = read_charter(p)['version']
        r = update_decision(p, 0, 'Edited A', expected_version=ver,
                            updated_by_conv='human')
        assert r['ok'] and r['version'] == ver + 1
        rec = read_charter(p)
    texts = [d['text'] for d in rec['decisions']]
    assert texts == ['Edited A', 'Original B']
    # The edit is auditable as a 'decided' event.
    assert 'decided' in _feed_kinds(flask_app, p)


def test_editing_a_decision_changes_what_agents_ACTUALLY_READ(flask_app):
    """THE headline guard. Editing a decision must change the injected line.

    Why the existing edit test could not see this: it asserts ``d['text']``,
    while the per-turn injection renders ``_decision_headline``, which prefers
    the stored ``summary``. ``update_decision`` had no ``summary`` parameter at
    all, so a human correction rewrote the body, returned ok=True, bumped the
    version — and left the ONE line every sibling conversation reads untouched,
    forever. Measured on the live project: decision #0's body described the
    shipped design while its summary still broadcast the design it replaced.

    That is the same failure shape as a badge asserting something already
    untrue: the panel shows the edit applied and the prompt is inert.
    """
    from lib.conversations.project_charter import (
        commit_charter, render_charter_injection_block, update_decision,
    )
    p = os.path.abspath('/p/edit-headline')
    with flask_app.app_context():
        commit_charter(p, add_decision='OLD BODY with its evidence chain',
                       decision_kind='invariant',
                       summary='OLD RULE that is now wrong',
                       updated_by_conv='cA')
        assert 'OLD RULE that is now wrong' in render_charter_injection_block(p)

        r = update_decision(p, 0, 'NEW BODY: the corrected rule',
                            summary='NEW RULE that is correct',
                            updated_by_conv='human')
        assert r['ok'], r
        block = render_charter_injection_block(p)

    assert 'NEW RULE that is correct' in block, (
        'the human edited the decision and agents still read the old rule')
    assert 'OLD RULE that is now wrong' not in block, (
        'the stale summary survived the edit — this is the live defect')


def test_omitting_summary_on_an_entry_that_has_one_is_REFUSED(flask_app):
    """Silently keeping the old summary is the one behaviour that must not remain.

    A caller that rewrites the body without saying what the new rule line is has
    almost certainly hit the stale-summary trap. Two safe answers exist (refuse,
    or clear so the headline falls back to the fresh text); the unsafe one is to
    keep broadcasting the old line. We REFUSE, because clearing silently
    downgrades a curated one-liner to an abridged first line — a quiet loss the
    human never asked for — whereas a refusal is a question they can answer.

    Passing ``summary=''`` EXPLICITLY is the escape hatch: that is an intentional
    "drop the summary, render my text" instruction, not an omission.
    """
    from lib.conversations.project_charter import (
        commit_charter, read_charter, render_charter_injection_block,
        update_decision,
    )
    p = os.path.abspath('/p/edit-summary-required')
    with flask_app.app_context():
        commit_charter(p, add_decision='BODY', decision_kind='invariant',
                       summary='THE RULE', updated_by_conv='cA')
        before = read_charter(p)['version']

        r = update_decision(p, 0, 'NEW BODY', updated_by_conv='human')
        assert r['ok'] is False, 'omitting summary must not silently succeed'
        assert r['error'] == 'summary_required', r
        rec = read_charter(p)

    # A refusal must change NOTHING — not the text, not the version.
    assert rec['decisions'][0]['text'] == 'BODY'
    assert rec['decisions'][0].get('summary') == 'THE RULE'
    assert rec['version'] == before, 'a refused edit must not bump the version'

    with flask_app.app_context():
        cleared = update_decision(p, 0, 'NEW BODY explicitly unsummarised',
                                  summary='', updated_by_conv='human')
        assert cleared['ok'], cleared
        block = render_charter_injection_block(p)
    assert 'THE RULE' not in block, 'summary="" must really drop the summary'
    assert 'NEW BODY explicitly unsummarised' in block, (
        'after clearing, the headline must fall back to the fresh text')


def test_a_legacy_entry_without_a_summary_edits_without_ceremony(flask_app):
    """COMPLEMENT: the refusal must not spread to entries that have no summary.

    Pre-summary entries (and anything committed without one) render an abridged
    first line. Requiring a summary to edit those would make the guard above a
    tax on every legacy edit rather than a trap for the stale-summary case.
    """
    from lib.conversations.project_charter import (
        commit_charter, render_charter_injection_block, update_decision,
    )
    p = os.path.abspath('/p/edit-legacy')
    with flask_app.app_context():
        commit_charter(p, add_decision='LEGACY first line\nand a long tail',
                       updated_by_conv='cA')
        r = update_decision(p, 0, 'CORRECTED first line\nand a long tail',
                            updated_by_conv='human')
        assert r['ok'], r
        block = render_charter_injection_block(p)
    assert 'CORRECTED first line' in block
    assert 'LEGACY first line' not in block


def test_the_REST_route_can_change_what_agents_read(flask_app, flask_client):
    """The library fix must not stop at the library boundary.

    ``update_decision`` gained a ``summary`` parameter, but the panel reaches it
    only through ``POST /charter/decision/update``. If that route does not pass
    the field through, the human-facing surface is exactly as broken as before
    while the unit tests are green — the shape this project keeps re-learning.

    Also pins the wire-level distinction the sentinel exists for: an ABSENT key
    is refused (400 summary_required) while ``summary: ''`` is honoured. Those
    two are the same value in most serialisations, so if the route collapses
    them the refusal silently becomes a clear.
    """
    from lib.conversations.project_charter import (
        commit_charter, render_charter_injection_block,
    )
    p = os.path.abspath('/p/route-summary')
    with flask_app.app_context():
        commit_charter(p, add_decision='BODY', decision_kind='invariant',
                       summary='OLD RULE via route', updated_by_conv='cA')

    # 1) Omitted key → refused, nothing changed.
    r = flask_client.post('/api/v1/project/charter/decision/update',
                          json={'path': p, 'index': 0, 'text': 'NEW BODY'})
    assert r.status_code == 400, r.get_data(as_text=True)
    import json as _json
    assert _json.loads(r.get_data(as_text=True)).get('error') == 'summary_required'
    with flask_app.app_context():
        assert 'OLD RULE via route' in render_charter_injection_block(p)

    # 2) Explicit summary → the injected line really changes.
    r = flask_client.post('/api/v1/project/charter/decision/update',
                          json={'path': p, 'index': 0, 'text': 'NEW BODY',
                                'summary': 'NEW RULE via route'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        block = render_charter_injection_block(p)
    assert 'NEW RULE via route' in block
    assert 'OLD RULE via route' not in block

    # 3) Explicit empty string → cleared, headline falls back to the body.
    r = flask_client.post('/api/v1/project/charter/decision/update',
                          json={'path': p, 'index': 0,
                                'text': 'BODY IS THE HEADLINE', 'summary': ''})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        block = render_charter_injection_block(p)
    assert 'BODY IS THE HEADLINE' in block
    assert 'NEW RULE via route' not in block


def test_the_panel_editor_offers_a_summary_field_when_one_exists():
    """The deepest half: the human must be able to SEE and EDIT that line.

    The panel already RENDERED the summary (``.pb-decision-summary``) while its
    inline editor edited only the body — so the human could read the line agents
    consume and had no control that changed it. With the backend now refusing a
    body-only edit, a body-only editor would turn a silent-wrong-rule bug into a
    dead Save button; both halves have to move together.

    Asserted over the shipped source with comments stripped (charter #24): the
    change that adds this necessarily explains it, and a raw substring scan is
    then satisfied by its own explanation.
    """
    from tests._source_scan import strip_comments
    from tests._runtime_sections import runtime_section_path
    js = runtime_section_path('project-brain.js')
    with open(js, encoding='utf-8') as f:
        src = strip_comments(f.read(), lang='js')

    assert 'pb-inline-editor-summary' in src, (
        'the inline editor has no summary input — the human cannot change the '
        'one line agents read')
    assert 'summarySelector' in src, 'the editor is not wired for a summary field'
    # The update call must be able to carry it, and must send the key ONLY when
    # the entry has a summary (absent key = refused, which is the point).
    assert 'body.summary = nextSummary' in src, (
        'the panel never puts the edited summary on the request body')
    assert 'hasSummary' in src, (
        'the panel does not distinguish entries that HAVE a summary — it would '
        'either refuse every legacy edit or send an empty summary blindly')

    # A field with no stylesheet rule is the invisible-control defect.
    css = os.path.join(ROOT, 'static', 'styles.css')
    with open(css, encoding='utf-8') as f:
        css_src = f.read()
    assert '.pb-inline-editor-summary' in css_src, (
        'the summary input has no CSS rule — it would render unstyled')
    assert '.pb-inline-editor-label' in css_src


def test_update_decision_optimistic_lock(flask_app):
    from lib.conversations.project_charter import commit_charter, update_decision
    p = os.path.abspath('/p/edit-lock')
    with flask_app.app_context():
        commit_charter(p, add_decision='D', updated_by_conv='cA')  # version 1
        stale = update_decision(p, 0, 'X', expected_version=0)
        assert stale['ok'] is False and stale['error'] == 'version_conflict'
        assert stale['current_version'] == 1


def test_update_decision_index_out_of_range(flask_app):
    from lib.conversations.project_charter import commit_charter, update_decision
    p = os.path.abspath('/p/edit-oor')
    with flask_app.app_context():
        commit_charter(p, add_decision='D', updated_by_conv='cA')
        r = update_decision(p, 5, 'X')
    assert r['ok'] is False and r['error'] == 'index_out_of_range'


def test_delete_decision_removes_only_that_one(flask_app):
    from lib.conversations.project_charter import (
        commit_charter, delete_decision, read_charter,
    )
    p = os.path.abspath('/p/del-dec')
    with flask_app.app_context():
        commit_charter(p, add_decision='Keep 1', updated_by_conv='cA')
        commit_charter(p, add_decision='Drop', updated_by_conv='cA')
        commit_charter(p, add_decision='Keep 2', updated_by_conv='cA')
        ver = read_charter(p)['version']
        r = delete_decision(p, 1, expected_version=ver, updated_by_conv='human')
        assert r['ok'] and r['version'] == ver + 1
        rec = read_charter(p)
    texts = [d['text'] for d in rec['decisions']]
    assert texts == ['Keep 1', 'Keep 2'], 'only the addressed decision is removed'


def test_delete_decision_optimistic_lock(flask_app):
    from lib.conversations.project_charter import commit_charter, delete_decision
    p = os.path.abspath('/p/del-lock')
    with flask_app.app_context():
        commit_charter(p, add_decision='D', updated_by_conv='cA')  # version 1
        stale = delete_decision(p, 0, expected_version=0)
        assert stale['ok'] is False and stale['error'] == 'version_conflict'


def test_delete_charter_removes_row(flask_app):
    from lib.conversations.project_charter import (
        commit_charter, delete_charter, read_charter,
    )
    p = os.path.abspath('/p/del-all')
    with flask_app.app_context():
        commit_charter(p, content='NS', updated_by_conv='cA')
        commit_charter(p, add_decision='D', summary='D', updated_by_conv='cA')
        ver = read_charter(p)['version']
        r = delete_charter(p, expected_version=ver, updated_by_conv='human')
        assert r['ok'] and r.get('deleted') is True
        rec = read_charter(p)
    assert rec['exists'] is False and rec['version'] == 0
    assert _charter_rows(flask_app, p) == []


def test_delete_charter_optimistic_lock(flask_app):
    from lib.conversations.project_charter import commit_charter, delete_charter
    p = os.path.abspath('/p/del-all-lock')
    with flask_app.app_context():
        commit_charter(p, content='NS', updated_by_conv='cA')  # version 1
        stale = delete_charter(p, expected_version=0)
        assert stale['ok'] is False and stale['error'] == 'version_conflict'
        # The row must still be intact after a rejected delete.
        from lib.conversations.project_charter import read_charter
        with flask_app.app_context():
            assert read_charter(p)['exists'] is True


def test_delete_missing_charter_is_noop_success(flask_app):
    from lib.conversations.project_charter import delete_charter
    with flask_app.app_context():
        r = delete_charter(os.path.abspath('/p/del-none'))
    assert r['ok'] is True and r.get('deleted') is False


def test_route_commit_add_decision_requires_summary(flask_app, flask_client):
    """The human-side回流缺口 (owner 2026-07-28): the tool path already
    forces kind+summary; the REST human path must too, or kindless entries
    flow back in through the panel's door."""
    import json as _json
    p = os.path.abspath('/p/route-no-summary')
    r = flask_client.post('/api/v1/project/charter/commit',
                          json={'path': p, 'add_decision': 'D'})
    assert r.status_code == 400
    assert 'summary' in r.get_data(as_text=True)
    # Nothing was committed.
    data = _json.loads(flask_client.get(
        '/api/v1/project/charter?path=' + p).get_data(as_text=True))
    assert not data.get('exists') or not data.get('decisions')
    # content-ONLY commits (north-star edits) stay summary-free.
    r2 = flask_client.post('/api/v1/project/charter/commit',
                           json={'path': p, 'content': 'NS only'})
    assert r2.status_code == 200, r2.get_data(as_text=True)


def test_routes_charter_edit_delete_roundtrip(flask_app, flask_client):
    import json as _json
    p = os.path.abspath('/p/route-editdel')
    flask_client.post('/api/v1/project/charter/commit',
                      json={'path': p, 'content': 'NS'})
    flask_client.post('/api/v1/project/charter/commit',
                      json={'path': p, 'add_decision': 'D0', 'summary': 'D0'})
    flask_client.post('/api/v1/project/charter/commit',
                      json={'path': p, 'add_decision': 'D1', 'summary': 'D1'})
    ver = _json.loads(flask_client.get(
        '/api/v1/project/charter?path=' + p).get_data(as_text=True))['version']
    # Edit decision 0 via route. REVERSED IN PLACE 2026-07-30: this used to send
    # `text` alone against an entry committed WITH a summary ('D0') and assert
    # 200 — i.e. it certified the stale-summary trap, because the edit landed
    # while the injected headline kept rendering the old 'D0'. A body-only edit
    # on a summarised entry is now refused, and the caller must say what the new
    # rule line is.
    refused = flask_client.post('/api/v1/project/charter/decision/update', json={
        'path': p, 'index': 0, 'text': 'D0-edited', 'expected_version': ver})
    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert _json.loads(refused.get_data(as_text=True))['error'] == 'summary_required'
    re = flask_client.post('/api/v1/project/charter/decision/update', json={
        'path': p, 'index': 0, 'text': 'D0-edited', 'summary': 'D0-edited',
        'expected_version': ver})
    assert re.status_code == 200, re.get_data(as_text=True)
    ver = _json.loads(re.get_data(as_text=True))['version']
    # Delete decision 1 via route.
    rd = flask_client.post('/api/v1/project/charter/decision/delete', json={
        'path': p, 'index': 1, 'expected_version': ver})
    assert rd.status_code == 200
    data = _json.loads(flask_client.get(
        '/api/v1/project/charter?path=' + p).get_data(as_text=True))
    assert [d['text'] for d in data['decisions']] == ['D0-edited']
    # Delete the whole charter via route.
    ver = data['version']
    ra = flask_client.post('/api/v1/project/charter/delete',
                           json={'path': p, 'expected_version': ver})
    assert ra.status_code == 200
    gone = _json.loads(flask_client.get(
        '/api/v1/project/charter?path=' + p).get_data(as_text=True))
    assert gone['exists'] is False


def test_routes_charter_edit_delete_version_conflict_409(flask_app, flask_client):
    p = os.path.abspath('/p/route-editdel-409')
    flask_client.post('/api/v1/project/charter/commit',
                      json={'path': p, 'add_decision': 'D',
                            'summary': 'D'})  # version 1
    # A stale expected_version on each mutation route → 409.
    assert flask_client.post('/api/v1/project/charter/decision/update', json={
        'path': p, 'index': 0, 'text': 'X', 'expected_version': 0}).status_code == 409
    assert flask_client.post('/api/v1/project/charter/decision/delete', json={
        'path': p, 'index': 0, 'expected_version': 0}).status_code == 409
    assert flask_client.post('/api/v1/project/charter/delete', json={
        'path': p, 'expected_version': 0}).status_code == 409


def test_routes_charter_edit_delete_require_path_and_index(flask_client):
    # missing path
    assert flask_client.post('/api/v1/project/charter/decision/update',
                             json={'index': 0, 'text': 'x'}).status_code == 400
    assert flask_client.post('/api/v1/project/charter/delete',
                             json={}).status_code == 400
    # missing/invalid index
    assert flask_client.post('/api/v1/project/charter/decision/update',
                             json={'path': '/p/x', 'text': 'y'}).status_code == 400
    assert flask_client.post('/api/v1/project/charter/decision/delete',
                             json={'path': '/p/x'}).status_code == 400


def test_NC_delete_decision_ignores_index_deletes_wrong_row(flask_app):
    """NC: neuter delete_decision so it always pops index 0 regardless of the
    requested index → deleting index 1 wrongly removes the FIRST decision,
    breaking test_delete_decision_removes_only_that_one's ordering assertion.
    Byte-identical restore."""
    def run():
        import lib.conversations.project_charter as pc
        p = os.path.abspath('/p/nc-del')
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_charter WHERE project_path=?', (p,))
            db.commit()
            pc.commit_charter(p, add_decision='Keep 1', updated_by_conv='cA')
            pc.commit_charter(p, add_decision='Drop', updated_by_conv='cA')
            ver = pc.read_charter(p)['version']
            pc.delete_decision(p, 1, expected_version=ver)
            rec = pc.read_charter(p)
        texts = [d['text'] for d in rec['decisions']]
        # With the index ignored, index 0 ('Keep 1') is wrongly removed.
        assert texts == ['Drop'], \
            'NC: ignoring the index must delete the wrong decision'

    _patch_restore(
        _CHARTER_SRC,
        "        removed = decisions.pop(index)",
        "        removed = decisions.pop(0)  # NC (ignore index)",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  Agent self-commit tool (owner-directed 2026-07-12): project_charter_commit
#  appends a DECISION only, can NEVER write the north-star `content`, and a
#  matching proposal drops out of pending via resolves_proposal.
# ════════════════════════════════════════════════════════════════════

def _charter_tool(fn_name, fn_args, *, conv='cAgent', project_path):
    from lib.conversations.project_charter import execute_charter_tool
    return execute_charter_tool(fn_name, fn_args,
                                current_conv_id=conv, project_path=project_path)


def _human_commit(project_path, decision, summary=None, **kw):
    """Commit a decision the way the ONLY remaining writer does.

    RETARGETED 2026-07-30: these storage properties (version bump, proposal
    dequeue, rolling truncation, append-commutes) used to be driven through the
    ``project_charter_commit`` AGENT tool. That tool was withdrawn — a charter
    always requires human review — so the same properties are now exercised
    through the human REST path's ``commit_charter``. The properties did not
    change; only who is allowed to trigger them did, so the coverage is
    retargeted rather than deleted."""
    from lib.conversations.project_charter import commit_charter
    return commit_charter(project_path, add_decision=decision,
                          decision_kind='invariant',
                          summary=(summary if summary is not None else decision),
                          **kw)


def test_agent_commit_appends_decision_and_bumps_version(flask_app):
    """A committed decision bumps the version and lands in the charter (read +
    injected block). Driven through the human route — see _human_commit."""
    from lib.conversations.project_charter import read_charter, render_charter_block
    p = os.path.abspath('/p/agent-commit')
    with flask_app.app_context():
        res = _human_commit(p, 'Adopt AST boundaries in lib/.',
                            updated_by_conv='human')
        assert res['ok'] and res['version'] == 1, res
        rec = read_charter(p)
        block = render_charter_block(p)
    assert rec['version'] == 1
    assert any(d['text'] == 'Adopt AST boundaries in lib/.' for d in rec['decisions'])
    # The decision reaches the model via the injected block.
    assert 'Adopt AST boundaries in lib/.' in block
    # And it is auditable as a 'decided' event (same path as a human commit).
    assert 'decided' in _feed_kinds(flask_app, p)


def test_agent_commit_resolves_proposal_dequeues(flask_app):
    """An agent PROPOSES; the human commit resolves it, and the proposal drops
    out of the pending-review list (no over-count). This is now the whole
    lifecycle: propose is the agent\'s only charter verb."""
    from lib.conversations.project_charter import pending_proposals, propose_amendment
    p = os.path.abspath('/p/agent-resolve')
    with flask_app.app_context():
        pid = propose_amendment(p, 'cAgent', 'Move logic into lib/.')['proposalId']
        assert len(pending_proposals(p)) == 1
        res = _human_commit(p, 'Move logic into lib/.',
                            resolves_proposal=pid, updated_by_conv='human')
        assert res['ok'], res
        pend = pending_proposals(p)
    assert pend == [], 'a resolved proposal must not stay pending'


def test_agent_commit_cannot_write_content_NC(flask_app):
    """STRENGTHENED 2026-07-30. This used to assert the agent commit tool could
    append a decision but never touch `content`. The tool is now WITHDRAWN
    entirely (a charter always requires human review), so the guarantee is
    stronger and simpler: the call is refused, and NOTHING it carried — neither
    a decision nor a smuggled `content` — reaches the charter."""
    from lib.conversations.project_charter import commit_charter, read_charter
    p = os.path.abspath('/p/agent-no-content')
    with flask_app.app_context():
        # A human sets the north star.
        commit_charter(p, content='HUMAN NORTH STAR', updated_by_conv='human')
        # The agent commits a decision AND maliciously tries to smuggle a
        # `content` arg — the tool must ignore it entirely.
        out = _charter_tool('project_charter_commit',
                            {'kind': 'invariant',
                             'decision': 'agent decision',
                             'summary': 'agent decision',
                             'content': 'AGENT-HIJACKED GOAL'},
                            project_path=p)
        rec = read_charter(p)
    assert 'project_charter_propose' in out, out
    assert rec['content'] == 'HUMAN NORTH STAR', \
        'the withdrawn tool must not be able to change the north-star content'
    assert not any(d['text'] == 'agent decision' for d in rec['decisions']), \
        'the withdrawn tool must not append a decision either'


def test_agent_commit_source_never_passes_content():
    """REVERSED 2026-07-30: the branch must now REFUSE, not commit.

    It previously asserted the branch calls commit_charter(add_decision=...)
    without content=. Since the tool was withdrawn, the branch must reach no
    writer at all — and must name the route that still works, so a model with
    the old tool in its transcript is redirected rather than stonewalled."""
    with open(_CHARTER_SRC, encoding='utf-8') as f:
        src = f.read()
    start = src.index("if fn_name == 'project_charter_commit':")
    end = src.index("return f\"Error: Unknown charter tool", start)
    branch = src[start:end]
    assert 'commit_charter(' not in branch, \
        'the withdrawn commit branch must not reach any charter writer'
    assert 'add_decision=' not in branch and 'content=' not in branch
    assert 'project_charter_propose' in branch, \
        'the refusal must point at the route that still works'


def test_agent_commit_survives_a_stale_version(flask_app):
    """A stale expected_version must NOT refuse an agent's decision append.

    Re-anchored 2026-07-29 — this test previously asserted the OPPOSITE. The
    agent tool can only append, and appends COMMUTE: another conversation
    committing in between is not a conflict, it is the normal state of a busy
    project. Refusing here made the tool fail precisely when siblings were
    active, and the same contract made the panel's Commit button 409 on a
    version it had rendered moments earlier.

    The property that actually matters is asserted on the DATA: BOTH decisions
    are present afterwards. So this is not "the refusal was deleted" but "the
    append landed without eating the other one". Overwrites keep their hard
    lock — see tests/test_project_charter_concurrency.py.
    """
    from lib.conversations.project_charter import commit_charter, read_charter
    p = os.path.abspath('/p/agent-conflict')
    with flask_app.app_context():
        commit_charter(p, add_decision='v1', summary='v1',
                       updated_by_conv='human')          # version 1
        commit_charter(p, add_decision='sibling', summary='sibling',
                       updated_by_conv='sibB')           # version 2 — moved on
        res = _human_commit(p, 'mine', expected_version=0,
                            updated_by_conv='human')
        texts = [d['text'] for d in read_charter(p)['decisions']]
    assert res['ok'], res
    assert texts == ['v1', 'sibling', 'mine'], \
        f'the append must land and no sibling decision may be lost; {texts}'


def test_agent_commit_hits_max_decisions_truncation(flask_app):
    """The append path hits the EXISTING _MAX_DECISIONS rolling truncation
    (no pagination added):
    committing past the cap keeps only the most-recent _MAX_DECISIONS, and the
    newest survives while the oldest is dropped."""
    import lib.conversations.project_charter as pc
    from lib.conversations.project_charter import read_charter
    p = os.path.abspath('/p/agent-cap')
    cap = pc._MAX_DECISIONS
    with flask_app.app_context():
        for i in range(cap + 5):
            _human_commit(p, f'decision #{i}', updated_by_conv='human')
        rec = read_charter(p)
    texts = [d['text'] for d in rec['decisions']]
    assert len(texts) == cap, 'rolling truncation must cap at _MAX_DECISIONS'
    # The 5 oldest were dropped; the newest is retained.
    assert f'decision #{cap + 4}' in texts
    assert 'decision #0' not in texts


def test_agent_commit_requires_decision_text(flask_app):
    """REVERSED 2026-07-30: argument validation is moot once the tool is
    withdrawn — an empty decision is refused for the same reason a well-formed
    one is. Asserting the refusal keeps the case covered without implying the
    tool still validates and might therefore still write."""
    p = os.path.abspath('/p/agent-empty')
    with flask_app.app_context():
        out = _charter_tool('project_charter_commit', {'decision': '  '},
                            project_path=p)
    assert 'project_charter_propose' in out, out


def test_promote_watch_item_still_routes_through_commit(flask_app):
    """The watch→charter bridge still routes a CONCERN through commit_charter.

    RETARGETED 2026-07-30: this used a goal. Goals no longer travel to the
    charter at all — they inject directly from the watch lane — so promoting one
    is refused. A concern/question is what the bridge is for now."""
    from lib.conversations.project_watch import add_watch_item, promote_watch_item
    from lib.conversations.project_charter import read_charter
    p = os.path.abspath('/p/promote')
    with flask_app.app_context():
        item = add_watch_item(p, 'concern', 'Keep the DB layer dual-backend',
                              created_by_conv='human')
        assert item['ok'], item
        iid = item['item']['item_id']
        res = promote_watch_item(iid, updated_by_conv='human')
        assert res['ok'], res
        rec = read_charter(p)
    assert any('Keep the DB layer dual-backend' in d['text']
               for d in rec['decisions']), \
        'promote must land the item as a committed decision via commit_charter'


# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROLS
# ════════════════════════════════════════════════════════════════════


def test_NC1_propose_writing_table_breaks_isolation(flask_app):
    """NC-1: make propose_amendment ALSO write the charter table → the
    'propose never writes the table' behavioral test FAILS."""
    def run():
        import lib.conversations.project_charter as pc
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


def test_NC2_injection_noop_breaks_injection(flask_app):
    """NC-2: no-op the charter branch of the injection seam → the injection
    test FAILS (the charter is no longer injected)."""
    def run(neutered):
        # compose_context re-exports the canonical collector at import time.
        # Point that one seam at the throwaway provider module for this NC;
        # the shipped module and filesystem remain untouched.
        import lib.tasks_pkg.context_composer as composer
        original = composer.collect_context_blocks
        composer.collect_context_blocks = neutered.collect_context_blocks
        try:
            out = _run_inject(flask_app, '/nc2', has_charter=True)
            assert '[PROJECT CHARTER]' not in out, \
                'NC-2: with the provider no-opped, the charter must NOT appear'
        finally:
            composer.collect_context_blocks = original

    _patch_restore(
        _CONTEXT_PROVIDERS_SRC,
        '        charter = render_charter_injection_block(path)',
        "        charter = ''  # NC-2: provider no-op",
        run,
    )
