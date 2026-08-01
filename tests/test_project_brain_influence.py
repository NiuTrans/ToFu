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
    # content and add_decision are mutually exclusive (commit_charter refuses
    # the combination outright — a partial application is worse than none), so
    # the north star and the decision are two separate commits.
    commit_charter(p, content='North star: ship it.', updated_by_conv='convA')
    commit_charter(p, add_decision='Use PostgreSQL', updated_by_conv='convA')
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


# ══════════════════════════════════════════════════════════════════════
#  The two CHANNELS: injected-into-the-prompt vs. reachable-via-a-tool
#
#  The lens is titled "how this conversation is influenced". Measured on the
#  live project 2026-07-31, it was answering that question wrongly in two
#  independent ways, and BOTH were invisible because the surface reported a
#  healthy-looking verdict either way:
#    • the GOALS lane did not exist here, while _inject.py ★4.455 was shipping
#      a [PROJECT GOALS] block every single turn;
#    • `board.injected` was read off render_board_block — the pull-based TOOL
#      renderer — not render_board_injection_block, the one the prompt uses.
#  Both are "a message asserting something that is not true", so both get a
#  guard keyed on the PROPERTY (which renderer / which lane), not on prose.
# ══════════════════════════════════════════════════════════════════════

def _seed_goal(flask_app, p, text='Ship it cheaply and correctly.'):
    from lib.conversations.project_watch import add_watch_item
    return add_watch_item(p, 'goal', text)['item']['item_id']


def test_goals_lane_present_and_mirrors_the_injected_block(flask_app):
    """A goal set in Status & Focus MUST appear in the influence verdict, and
    its `chars` must equal the REAL [PROJECT GOALS] block the prompt ships."""
    from lib.conversations.project_brain_influence import build_conv_influence
    from lib.conversations.project_watch import render_goals_injection_block
    p = os.path.abspath('/tmp/infl-goals')
    with flask_app.app_context():
        _seed_goal(flask_app, p, 'Long-term extensibility over quick patches.')
        inf = build_conv_influence(p, 'convA')
        block = render_goals_injection_block(p)
    assert inf['goals']['injected'] is True
    assert [g['text'] for g in inf['goals']['items']] == \
        ['Long-term extensibility over quick patches.']
    # The count is the MEASURED size of the real block — not an estimate.
    assert inf['goals']['chars'] == len(block) > 0


def test_goals_lane_withdraws_when_resolved(flask_app):
    """Resolving a goal is the human's lever to stop injecting it. The lens
    must follow the SAME rule the renderer uses, or it keeps showing a goal as
    steering the model after it stopped."""
    from lib.conversations.project_brain_influence import build_conv_influence
    from lib.conversations.project_watch import set_watch_status
    p = os.path.abspath('/tmp/infl-goals-resolved')
    with flask_app.app_context():
        gid = _seed_goal(flask_app, p)
        set_watch_status(gid, 'resolved')
        inf = build_conv_influence(p, 'convA')
    assert inf['goals']['injected'] is False
    assert inf['goals']['items'] == []
    assert inf['goals']['chars'] == 0


def test_board_injected_uses_the_PROMPT_renderer_not_the_tool_renderer(flask_app):
    """`board.chars` must equal render_board_INJECTION_block — the abridged
    block the prompt ships — and NOT render_board_block, the full pull-based
    TOOL render. Measured live: 8,845 vs 18,178 chars for the same board.

    A boolean alone cannot catch this (both renderers are non-empty on a live
    board, so the flag agreed by coincidence for the whole time the wrong call
    site was there); comparing the SIZE is what makes the two distinguishable.
    """
    from lib.conversations.project_board import (
        render_board_block, render_board_injection_block,
    )
    from lib.conversations.project_brain_influence import build_conv_influence
    p = os.path.abspath('/tmp/infl-renderer')
    with flask_app.app_context():
        # A long epic title is what makes the two renderers diverge at all —
        # abridgement only bites past _INJECT_TITLE_MAX_CHARS.
        from lib.conversations.project_board import post_task
        post_task(p, 'convB', 'X' * 900)
        inf = build_conv_influence(p, 'convA')
        prompt_block = render_board_injection_block(p, current_conv_id='convA')
        tool_block = render_board_block(p, current_conv_id='convA')
    assert len(tool_block) > len(prompt_block), \
        'fixture is inert: the two renderers must differ for this to test anything'
    assert inf['board']['chars'] == len(prompt_block)
    assert inf['board']['chars'] != len(tool_block)


def test_board_reports_prompt_side_abridgement(flask_app):
    """The panel shows each epic's FULL stored title while the prompt ships an
    abridged headline. `abridgedInPrompt` says so; without it the human reads
    a 900-char epic here and assumes the model received all of it."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_brain_influence import build_conv_influence
    p_long = os.path.abspath('/tmp/infl-abridged')
    p_short = os.path.abspath('/tmp/infl-not-abridged')
    with flask_app.app_context():
        post_task(p_long, 'convB', 'Y' * 900)
        inf_long = build_conv_influence(p_long, 'convA')
        post_task(p_short, 'convB', 'Short epic')
        inf_short = build_conv_influence(p_short, 'convA')
    assert inf_long['board']['abridgedInPrompt'] is True
    # A board of short epics is NOT abridged — the flag must discriminate,
    # otherwise it is decoration that always fires.
    assert inf_short['board']['abridgedInPrompt'] is False


def test_charter_chars_is_the_injection_block_size(flask_app):
    from lib.conversations.project_brain_influence import build_conv_influence
    from lib.conversations.project_charter import (
        render_charter_injection_block,
    )
    p = os.path.abspath('/tmp/infl-charter-chars')
    with flask_app.app_context():
        _seed(flask_app, p)
        inf = build_conv_influence(p, 'convA')
        block = render_charter_injection_block(p)
    assert inf['charter']['chars'] == len(block) > 0


def test_tool_visible_lane_names_only_real_tools(flask_app):
    """The pull-only channel must name tools that ACTUALLY exist in the
    registry — a panel advertising a phantom tool is the same defect class as
    a digest header naming tools the model cannot call."""
    from lib.conversations.project_brain_influence import build_conv_influence
    from lib.tools.conversation import (
        BOARD_TOOL_NAMES, CHARTER_TOOL_NAMES, PEER_TOOL_NAMES,
    )
    known = BOARD_TOOL_NAMES | CHARTER_TOOL_NAMES | PEER_TOOL_NAMES
    with flask_app.app_context():
        inf = build_conv_influence(os.path.abspath('/tmp/infl-tools'), 'convA')
    names = [t['tool'] for t in inf['toolVisible']]
    assert names, 'the tool-visible channel must not be empty'
    for n in names:
        assert n in known, f'{n} is not a registered project-brain tool'
    # Every entry carries an i18n label key so the panel never prints a bare
    # function name at the human.
    assert all(t.get('labelKey') for t in inf['toolVisible'])


def test_empty_shape_carries_the_new_lanes(flask_app):
    """A falsy project path returns the SAME shape — a consumer must never get
    `undefined` for goals/toolVisible and silently render nothing."""
    from lib.conversations.project_brain_influence import build_conv_influence
    inf = build_conv_influence('', 'convA')
    assert inf['goals'] == {'injected': False, 'chars': 0, 'items': []}
    assert [t['tool'] for t in inf['toolVisible']]
    assert inf['board']['chars'] == 0
    assert inf['board']['abridgedInPrompt'] is False


def test_NC_goals_lane_is_load_bearing(flask_app):
    """NC: neuter the goals renderer call so the lane always reads empty →
    a project WITH an open goal reports goals.injected False → FAILS."""
    def run():
        import lib.conversations.project_brain_influence as infl
        p = os.path.abspath('/tmp/infl-nc-goals')
        with flask_app.app_context():
            _seed_goal(flask_app, p)
            inf = infl.build_conv_influence(p, 'convA')
        assert inf['goals']['injected'] is False, \
            'NC: emptying the goals block must make the lane report not-injected'

    _patch_restore(
        _INFL_SRC,
        "_goals_block = render_goals_injection_block(project_path)",
        "_goals_block = ''",
        run,
    )


def test_NC_board_prompt_renderer_is_load_bearing(flask_app):
    """NC: put the TOOL renderer back where the PROMPT renderer belongs — the
    exact 2026-07-31 defect — → board.chars stops matching the injected block
    → FAILS. This is the guard that would have caught the original bug."""
    def run():
        import lib.conversations.project_brain_influence as infl
        from lib.conversations.project_board import (
            post_task, render_board_injection_block,
        )
        p = os.path.abspath('/tmp/infl-nc-renderer')
        with flask_app.app_context():
            post_task(p, 'convB', 'Z' * 900)
            inf = infl.build_conv_influence(p, 'convA')
            prompt_block = render_board_injection_block(p, current_conv_id='convA')
        assert inf['board']['chars'] != len(prompt_block), \
            'NC: sourcing the flag from the tool renderer must break the match'

    _patch_restore(
        _INFL_SRC,
        "        _board_block = render_board_injection_block(\n"
        "            project_path, current_conv_id=conv_id)",
        "        from lib.conversations.project_board import render_board_block\n"
        "        _board_block = render_board_block(\n"
        "            project_path, current_conv_id=conv_id)",
        run,
    )


def test_NC_abridged_flag_discriminates(flask_app):
    """NC: hard-wire abridgedInPrompt True → the short-epic board wrongly
    claims abridgement → the discriminating assertion FAILS."""
    def run():
        import lib.conversations.project_brain_influence as infl
        from lib.conversations.project_board import post_task
        p = os.path.abspath('/tmp/infl-nc-abridge')
        with flask_app.app_context():
            post_task(p, 'convB', 'Short epic')
            inf = infl.build_conv_influence(p, 'convA')
        assert inf['board']['abridgedInPrompt'] is True, \
            'NC: hard-wiring the flag must make a short board claim abridgement'

    _patch_restore(
        _INFL_SRC,
        "        out['board']['abridgedInPrompt'] = bool(_board_block) and any(",
        "        out['board']['abridgedInPrompt'] = bool(_board_block) or any(",
        run,
    )
