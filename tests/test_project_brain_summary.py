"""tests/test_project_brain_summary.py — the collaboration-bar summary.

The star of this slice is ``peerEpics``: an active presence peer joined to the
Board epic it is *advancing* (its live claim). That join is what turns the bar
from the meaningless "(untitled) · generating" into the deep signal
"conversation X · advancing «Refactor the parser»".

Covers: board open/claimed/done counts, pendingDecisions (proposed_decision
feed count), activePeers, and the peer→epic join — plus a source-level negative
control: no-op the join branch → the "peer shows its epic" assertion FAILS.
"""

from __future__ import annotations

import os

import pytest

import lib.presence.registry as reg

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_SUMMARY_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_brain_summary.py')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app, monkeypatch):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('DELETE FROM project_tasks')
        db.execute('DELETE FROM project_events')
        db.execute('DELETE FROM project_charter')
        db.commit()
    # fresh presence state, no sweeper thread, stub push
    monkeypatch.setattr(reg, '_state', {})
    monkeypatch.setattr(reg, '_sweeper_started', True)
    import lib.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', lambda *a, **k: None)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def test_summary_board_counts(flask_app):
    # Set the three statuses via DIRECT status writes, NOT complete_task —
    # complete_task fires on_epic_completed, which would auto-dispatch (claim)
    # the open epic and skew the counts (the autonomy working as designed).
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_brain_summary import build_brain_summary
    from lib.database import DOMAIN_CHAT, get_thread_db
    p = os.path.abspath('/tmp/bs-counts')
    with flask_app.app_context():
        post_task(p, 'cA', 'open epic')
        cl = post_task(p, 'cA', 'claimed epic')['id']
        dn = post_task(p, 'cA', 'done epic')['id']
        claim_task(p, 'cB', cl)
        db = get_thread_db(DOMAIN_CHAT)
        db.execute("UPDATE project_tasks SET status='done' WHERE id=?", (dn,))
        db.commit()
        s = build_brain_summary(p)
    assert s['epicsOpen'] == 1
    assert s['epicsClaimed'] == 1
    assert s['epicsDone'] == 1


def test_summary_pending_decisions(flask_app):
    from lib.conversations.project_charter import propose_amendment
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/tmp/bs-pending')
    with flask_app.app_context():
        propose_amendment(p, 'cA', 'Adopt X')
        propose_amendment(p, 'cB', 'Adopt Y')
        s = build_brain_summary(p)
    assert s['pendingDecisions'] == 2


def test_summary_active_peers_and_peer_epic_join(flask_app):
    """THE decisive test: an active peer that has CLAIMED a board epic appears
    in peerEpics mapped to that epic's title — the deep-collaboration signal."""
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/tmp/bs-join')
    with flask_app.app_context():
        epic = post_task(p, 'cA', 'Refactor the parser')['id']
        # conv-worker is an ACTIVE presence peer that CLAIMS the epic.
        reg.announce(p, 'conv-worker', task_id='t1', title='Worker conv')
        claim_task(p, 'conv-worker', epic)
        s = build_brain_summary(p)
    assert s['activePeers'] == 1
    # The join: the active peer is mapped to the epic TITLE it advances.
    assert s['peerEpics'].get('conv-worker') == 'Refactor the parser'


def test_summary_excludes_requesting_conv_from_active_peers(flask_app):
    """When conv_id is given, the DISPLAYED conversation is excluded from
    activePeers/peerEpics so the count means "OTHER conversations online" —
    matching the frontend local-mirror semantics (which drops self). This is
    what lets the collab bar render its peer segment from the backend count
    without off-by-one against a live push frame."""
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/tmp/bs-selfexcl')
    with flask_app.app_context():
        e1 = post_task(p, 'cA', 'Self epic')['id']
        e2 = post_task(p, 'cA', 'Peer epic')['id']
        reg.announce(p, 'conv-self', task_id='ts', title='Self')
        reg.announce(p, 'conv-peer', task_id='tp', title='Peer')
        claim_task(p, 'conv-self', e1)
        claim_task(p, 'conv-peer', e2)
        # No conv_id → both peers counted (project-wide view).
        s_all = build_brain_summary(p)
        # conv_id=conv-self → self excluded, only the peer remains.
        s_self = build_brain_summary(p, 'conv-self')
    assert s_all['activePeers'] == 2
    assert 'conv-self' in s_all['peerEpics'] and 'conv-peer' in s_all['peerEpics']
    assert s_self['activePeers'] == 1, 'requesting conv must be excluded'
    assert 'conv-self' not in s_self['peerEpics'], 'self must not join its own epic'
    assert s_self['peerEpics'].get('conv-peer') == 'Peer epic'


def test_summary_self_only_reports_zero_peers(flask_app):
    """A project where the ONLY active peer is the displayed conv reports
    activePeers=0 with conv_id given — the collab bar correctly shows no
    "N online" segment for a solo session (no phantom self-count)."""
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/tmp/bs-selfonly')
    with flask_app.app_context():
        reg.announce(p, 'conv-self', task_id='ts', title='Self')
        s = build_brain_summary(p, 'conv-self')
    assert s['activePeers'] == 0 and s['peerEpics'] == {}


def test_summary_peer_without_claim_absent_from_peerEpics(flask_app):
    """An active peer that hasn't claimed anything is counted but NOT in the
    peerEpics map (the bar falls back to its activity word for such peers)."""
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/tmp/bs-noclaim')
    with flask_app.app_context():
        reg.announce(p, 'conv-idle-ish', task_id='t1', title='No claim conv')
        s = build_brain_summary(p)
    assert s['activePeers'] == 1
    assert 'conv-idle-ish' not in s['peerEpics']


def test_summary_expired_claim_drops_from_peer_epics(flask_app):
    """An expired lease reads as open (via read_board), so the peer's epic
    disappears from peerEpics — reuses the one anti-deadlock path."""
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_brain_summary import build_brain_summary
    from lib.database import DOMAIN_CHAT, get_thread_db
    p = os.path.abspath('/tmp/bs-expired')
    with flask_app.app_context():
        epic = post_task(p, 'cA', 'Expiring epic')['id']
        reg.announce(p, 'conv-worker', task_id='t1', title='Worker')
        claim_task(p, 'conv-worker', epic)
        # Force the lease into the past.
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET lease_expires_at=1 WHERE id=?', (epic,))
        db.commit()
        s = build_brain_summary(p)
    assert s['epicsOpen'] == 1 and s['epicsClaimed'] == 0
    assert 'conv-worker' not in s['peerEpics']


def test_summary_empty_project(flask_app):
    from lib.conversations.project_brain_summary import build_brain_summary
    with flask_app.app_context():
        s = build_brain_summary(os.path.abspath('/tmp/bs-empty'))
    assert s['epicsOpen'] == 0 and s['activePeers'] == 0 and s['peerEpics'] == {}
    assert build_brain_summary('') == {
        'epicsOpen': 0, 'epicsClaimed': 0, 'epicsDone': 0,
        'pendingDecisions': 0, 'activePeers': 0, 'peerEpics': {},
        'charterExists': False, 'conflicts': 0, 'conflictMessages': [],
        'statusLine': ''}


def test_summary_conflicts_from_file_overlap(flask_app):
    """Two active peers touching the SAME file → summary.conflicts counts the
    advisory, recomputed from the SAME detect_overlaps the live broadcast uses
    (no second mirror)."""
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/tmp/bs-conflict')
    with flask_app.app_context():
        reg.announce(p, 'convA', task_id='tA', title='A')
        reg.announce(p, 'convB', task_id='tB', title='B')
        # Both touch the same file → an overlap advisory.
        reg.record_files(p, 'convA', [{'path': 'src/shared.py', 'action': 'edit'}])
        reg.record_files(p, 'convB', [{'path': 'src/shared.py', 'action': 'edit'}])
        s = build_brain_summary(p)
    assert s['conflicts'] >= 1, 'a two-peer file overlap must surface as a conflict'
    assert any('shared.py' in m for m in s['conflictMessages'])


def test_summary_no_conflict_when_no_overlap(flask_app):
    from lib.conversations.project_brain_summary import build_brain_summary
    p = os.path.abspath('/tmp/bs-noconflict')
    with flask_app.app_context():
        reg.announce(p, 'convA', task_id='tA', title='A')
        reg.record_files(p, 'convA', [{'path': 'src/only_a.py', 'action': 'edit'}])
        s = build_brain_summary(p)
    assert s['conflicts'] == 0 and s['conflictMessages'] == []


# ── Route ──
# ── Route ──

def test_route_brain_summary(flask_app, flask_client):
    import json as _json

    from lib.conversations.project_board import claim_task, post_task
    p = os.path.abspath('/tmp/bs-route')
    with flask_app.app_context():
        epic = post_task(p, 'cA', 'Routed epic')['id']
        reg.announce(p, 'conv-w', task_id='t1', title='W')
        claim_task(p, 'conv-w', epic)
    r = flask_client.get('/api/v1/project/brain/summary?path=' + p)
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert data['epicsClaimed'] == 1
    assert data['peerEpics'].get('conv-w') == 'Routed epic'


def test_route_brain_summary_requires_path(flask_client):
    assert flask_client.get('/api/v1/project/brain/summary').status_code == 400


# ── Source-level NEGATIVE CONTROL ──

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_peer_epic_join_is_load_bearing(flask_app):
    """NC: no-op the peer→epic join loop → an active peer that DID claim an
    epic no longer appears in peerEpics → the join assertion FAILS."""
    def run():
        import lib.conversations.project_brain_summary as bs
        from lib.conversations.project_board import claim_task, post_task
        p = os.path.abspath('/tmp/bs-nc')
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute("DELETE FROM project_tasks WHERE project_path=?", (p,))
            db.commit()
            epic = post_task(p, 'cA', 'Joined epic')['id']
            reg.announce(p, 'conv-worker', task_id='t1', title='Worker')
            claim_task(p, 'conv-worker', epic)
            s = bs.build_brain_summary(p)
        # With the join disabled, the claimed epic is NOT mapped to the peer.
        assert 'conv-worker' not in s['peerEpics'], \
            'NC: disabling the join must drop the peer→epic mapping'

    _patch_restore(
        _SUMMARY_SRC,
        ("        for cid in conv_ids:\n"
         "            title = claim_by_conv.get(cid)\n"
         "            if title:\n"
         "                peer_epics[cid] = title"),
        "        pass  # NC (join disabled)",
        run,
    )
