"""tests/test_project_brain_influence.py — per-conversation brain influence.

``build_conv_influence(project_path, conv_id)`` answers the conversation-scoped
question "how is THIS chat affected by the project brain?" — the charter it's
bound by, the board epics it OWNS (a live claim), the epics it must AVOID (a
sibling holds the lease), the open ones it could claim, and the decisions
awaiting a human.

The load-bearing behaviour is the PER-CONVERSATION ownership split: the SAME
board produces a different `mine`/`avoid` partition for convA vs. convB. This
is derived from `read_board` + the owner comparison (a faithful mirror of
`render_board_block`'s "(you)" vs "avoid" annotations, which the prompt uses),
NOT a second heuristic. Covers: the split from both perspectives, charter
binding, pending `mine` flag, the two `injected` flags mirroring the render
blocks, the empty-project shape, and a source-level negative control on the
ownership comparison.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_INFL_SRC = os.path.join(ROOT, 'lib', 'conversations',
                         'project_brain_influence.py')


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
    import lib.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', lambda *a, **k: None)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def _seed(flask_app, p):
    """charter (1 decision) + board: convA owns t_a, convB owns t_b, t_c open;
    one pending proposal raised by convB. Returns nothing (writes into DB)."""
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_charter import (
        commit_charter, propose_amendment,
    )
    commit_charter(p, content='North star: ship it.',
                   add_decision='Use PostgreSQL', updated_by_conv='convA')
    ta = post_task(p, 'convA', 'Refactor parser')['id']
    claim_task(p, 'convA', ta)
    tb = post_task(p, 'convB', 'Rewrite docs')['id']
    claim_task(p, 'convB', tb)
    post_task(p, 'convA', 'Add tests')  # open
    propose_amendment(p, 'convB', 'Adopt trunk-based dev')


def test_influence_split_from_conv_a(flask_app):
    from lib.conversations.project_brain_influence import build_conv_influence
    p = os.path.abspath('/tmp/infl-a')
    with flask_app.app_context():
        _seed(flask_app, p)
        inf = build_conv_influence(p, 'convA')
    # From convA's lens: owns "Refactor parser", must AVOID "Rewrite docs".
    assert [t['title'] for t in inf['board']['mine']] == ['Refactor parser']
    assert [(t['title'], t['owner']) for t in inf['board']['avoid']] == \
        [('Rewrite docs', 'convB')]
    assert [t['title'] for t in inf['board']['open']] == ['Add tests']
    # Charter binding surfaced + injected flag mirrors the injection block.
    assert inf['charter']['injected'] is True
    # Decisions are STRUCTURED (owner 2026-07-28: the frontend is a pure
    # renderer, never re-deriving kind/summary from raw text).
    assert [d['text'] for d in inf['charter']['decisions']] == ['Use PostgreSQL']
    entry = inf['charter']['decisions'][0]
    assert set(entry) >= {'text', 'summary', 'kind', 'ts', 'by_conv'}
    assert entry['by_conv'] == 'convA'
    # Health signals are computed backend-side for the panel's health strip.
    assert inf['charter']['contentSet'] is True
    assert inf['charter']['decisionCount'] == 1
    assert inf['charter']['injectedCount'] == 1
    assert inf['board']['injected'] is True
    # Pending proposal from convB → not mine.
    assert len(inf['pendingDecisions']) == 1
    assert inf['pendingDecisions'][0]['mine'] is False


def test_influence_split_is_per_conversation(flask_app):
    """THE decisive test: the SAME board yields a DIFFERENT mine/avoid split
    for convB — what is 'mine' for convA is 'avoid' for convB and vice-versa."""
    from lib.conversations.project_brain_influence import build_conv_influence
    p = os.path.abspath('/tmp/infl-b')
    with flask_app.app_context():
        _seed(flask_app, p)
        inf = build_conv_influence(p, 'convB')
    # From convB's lens the ownership FLIPS.
    assert [t['title'] for t in inf['board']['mine']] == ['Rewrite docs']
    assert [(t['title'], t['owner']) for t in inf['board']['avoid']] == \
        [('Refactor parser', 'convA')]
    assert [t['title'] for t in inf['board']['open']] == ['Add tests']
    # The proposal was raised BY convB → mine=True from its lens.
    assert inf['pendingDecisions'][0]['mine'] is True


def test_influence_injected_flags_follow_render_blocks(flask_app):
    """The two `injected` flags must be TRUE iff the SAME render block the
    prompt uses is non-empty — an empty project injects nothing."""
    from lib.conversations.project_brain_influence import build_conv_influence
    p = os.path.abspath('/tmp/infl-empty-inject')
    with flask_app.app_context():
        inf = build_conv_influence(p, 'convA')
    assert inf['charter']['injected'] is False
    assert inf['board']['injected'] is False


def test_influence_empty_project_shape(flask_app):
    from lib.conversations.project_brain_influence import build_conv_influence
    with flask_app.app_context():
        inf = build_conv_influence(os.path.abspath('/tmp/infl-empty'), 'convA')
    assert inf['board']['mine'] == [] and inf['board']['avoid'] == []
    assert inf['board']['open'] == [] and inf['pendingDecisions'] == []
    assert inf['charter']['exists'] is False
    # Falsy project path → empty shell, no raise.
    assert build_conv_influence('', 'convA')['board']['mine'] == []


def test_influence_expired_claim_reads_open(flask_app):
    """A peer's expired lease reads as open (via read_board), so a formerly-
    avoided epic becomes 'open' for everyone — reuses the anti-deadlock path."""
    from lib.conversations.project_board import claim_task, post_task
    from lib.conversations.project_brain_influence import build_conv_influence
    from lib.database import DOMAIN_CHAT, get_thread_db
    p = os.path.abspath('/tmp/infl-expired')
    with flask_app.app_context():
        tb = post_task(p, 'convB', 'Expiring epic')['id']
        claim_task(p, 'convB', tb)
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET lease_expires_at=1 WHERE id=?', (tb,))
        db.commit()
        inf = build_conv_influence(p, 'convA')
    assert [t['title'] for t in inf['board']['avoid']] == []
    assert [t['title'] for t in inf['board']['open']] == ['Expiring epic']


# ── Route ──

def test_route_brain_influence(flask_app, flask_client):
    import json as _json
    p = os.path.abspath('/tmp/infl-route')
    with flask_app.app_context():
        _seed(flask_app, p)
    r = flask_client.get(
        '/api/v1/project/brain/influence?path=' + p + '&convId=convA')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert [t['title'] for t in data['board']['mine']] == ['Refactor parser']
    assert [t['title'] for t in data['board']['avoid']] == ['Rewrite docs']


def test_route_brain_influence_requires_path_and_conv(flask_client):
    assert flask_client.get(
        '/api/v1/project/brain/influence').status_code == 400
    assert flask_client.get(
        '/api/v1/project/brain/influence?path=/tmp/x').status_code == 400


# ── Source-level NEGATIVE CONTROL ──

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_ownership_split_is_load_bearing(flask_app):
    """NC: break the ownership comparison so NO claimed epic is ever filed as
    'mine' (owner==conv_id never matches) → convA's own "Refactor parser" no
    longer appears under `mine` → the split assertion FAILS. Byte-identical
    restore."""
    def run():
        import lib.conversations.project_brain_influence as infl
        p = os.path.abspath('/tmp/infl-nc')
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('DELETE FROM project_tasks WHERE project_path=?', (p,))
            db.commit()
            _seed(flask_app, p)
            inf = infl.build_conv_influence(p, 'convA')
        # With the ownership match broken, convA's own epic is NOT in `mine`.
        assert 'Refactor parser' not in [t['title'] for t in inf['board']['mine']], \
            'NC: breaking owner==conv_id must drop the conv\'s own epic from mine'

    _patch_restore(
        _INFL_SRC,
        "if status == 'claimed' and owner and conv_id and owner == conv_id:",
        "if status == 'claimed' and owner and conv_id and owner == '__never__':",
        run,
    )
