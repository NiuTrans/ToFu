"""tests/test_project_board.py — Pillar #3 project-brain coordination Board.

The Board is what turns perception into AUTO-COORDINATION. The two
load-bearing properties:

  • **Anti-deadlock (soft lease).** A ``claimed`` epic whose ``lease_expires_at``
    has passed MUST read as ``open`` — evaluated at READ TIME, with no reaper.
    An abandoned/crashed conversation can never hold an epic forever.
  • **Auto-avoidance injection.** When another conversation holds an UNEXPIRED
    claim, the injected board block carries an explicit "avoid duplicating"
    hint — this is the signal a reading conversation acts on to step aside.

Two MANDATORY source-level negative controls:
  • NC-1: no-op the expired-lease→open reclaim in ``_effective_status`` → the
    anti-deadlock test FAILS (an expired claim stays locked).
  • NC-2: no-op the avoid-duplication hint branch in ``render_board_block`` →
    the avoidance-injection test FAILS.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')


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
        db.execute('DELETE FROM project_tasks')
        db.execute('DELETE FROM project_events')
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _feed_kinds(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        return [e['kind'] for e in read_project_feed(project_path, limit=500)['events']]


def _set_lease(flask_app, project_path, task_id, lease_ms):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET lease_expires_at=? WHERE id=? AND project_path=?',
                   (lease_ms, task_id, project_path))
        db.commit()


# ════════════════════════════════════════════════════════════════════
#  post / read / complete
# ════════════════════════════════════════════════════════════════════

def test_post_then_read(flask_app):
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        r = post_task('/b/p', 'cA', 'Build the widget')
        assert r['ok'] and r['id'].startswith('pt_')
        board = read_board('/b/p')
    assert board['open'] == 1 and board['claimed'] == 0
    assert board['tasks'][0]['title'] == 'Build the widget'
    assert board['tasks'][0]['status'] == 'open'


def test_complete(flask_app):
    from lib.conversations.project_board import complete_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/b/c', 'cA', 'epic')['id']
        assert complete_task('/b/c', 'cA', tid)['ok']
        board = read_board('/b/c')
    assert board['done'] == 1
    assert 'completed' in _feed_kinds(flask_app, '/b/c')


# ════════════════════════════════════════════════════════════════════
#  claim writes owner + lease; emits claimed
# ════════════════════════════════════════════════════════════════════

def test_claim_writes_owner_and_lease(flask_app):
    from lib.conversations.project_board import claim_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/b/cl', 'cA', 'epic')['id']
        res = claim_task('/b/cl', 'cB', tid)
        assert res['ok'] and res['lease_expires_at'] > 0
        board = read_board('/b/cl')
    t = board['tasks'][0]
    assert t['status'] == 'claimed'
    assert t['owner_conv_id'] == 'cB'
    assert t['lease_expires_at'] > 0
    assert 'claimed' in _feed_kinds(flask_app, '/b/cl')


def test_dispatched_badge_flows_through(flask_app):
    """A claim minted with dispatched=True surfaces dispatched=True on the
    board card; a normal claim does not; completing resets it."""
    from lib.conversations.project_board import (
        claim_task, complete_task, post_task, read_board,
    )
    with flask_app.app_context():
        d_id = post_task('/b/disp', 'cA', 'brain epic')['id']
        n_id = post_task('/b/disp', 'cA', 'human epic')['id']
        claim_task('/b/disp', 'cBRAIN', d_id, dispatched=True)
        claim_task('/b/disp', 'cHUMAN', n_id)   # normal claim
        board = read_board('/b/disp')
    by_id = {t['id']: t for t in board['tasks']}
    assert by_id[d_id]['dispatched'] is True, 'brain-dispatched claim → badge'
    assert by_id[n_id]['dispatched'] is False, 'human claim → no badge'
    # Completing resets the flag.
    with flask_app.app_context():
        complete_task('/b/disp', 'cBRAIN', d_id)
        board2 = read_board('/b/disp')
    assert [t for t in board2['tasks'] if t['id'] == d_id][0]['dispatched'] is False


def test_claim_refused_when_held_by_other(flask_app):
    from lib.conversations.project_board import claim_task, post_task
    with flask_app.app_context():
        tid = post_task('/b/cf', 'cA', 'epic')['id']
        assert claim_task('/b/cf', 'cB', tid)['ok']
        # A different conversation cannot claim an actively-held epic.
        res = claim_task('/b/cf', 'cC', tid)
    assert res['ok'] is False and res['error'] == 'already_claimed'
    assert res['owner'] == 'cB'


# ════════════════════════════════════════════════════════════════════
#  ANTI-DEADLOCK: expired lease reads as open (the core property)
# ════════════════════════════════════════════════════════════════════

def test_expired_lease_reads_as_open(flask_app):
    """A claimed epic whose lease has expired MUST read as open — the
    anti-deadlock core (evaluated at read time, no reaper)."""
    from lib.conversations.project_board import claim_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/b/exp', 'cA', 'epic')['id']
        claim_task('/b/exp', 'cB', tid)
    # Force the lease into the past.
    _set_lease(flask_app, '/b/exp', tid, 1)  # 1ms since epoch = long expired
    with flask_app.app_context():
        board = read_board('/b/exp')
    t = board['tasks'][0]
    assert t['status'] == 'open', 'expired claim must read as open (anti-deadlock)'
    assert t['owner_conv_id'] == '', 'expired claim must drop the owner in the read view'
    assert board['open'] == 1 and board['claimed'] == 0


def test_expired_lease_is_reclaimable(flask_app):
    """After a lease expires, a DIFFERENT conversation can claim the epic."""
    from lib.conversations.project_board import claim_task, post_task
    with flask_app.app_context():
        tid = post_task('/b/recl', 'cA', 'epic')['id']
        claim_task('/b/recl', 'cB', tid)
    _set_lease(flask_app, '/b/recl', tid, 1)  # expired
    with flask_app.app_context():
        res = claim_task('/b/recl', 'cC', tid)  # different conv reclaims
    assert res['ok'], 'an expired lease must be reclaimable by another conversation'


def test_effective_status_unit():
    from lib.conversations.project_board import _effective_status
    now = 1_000_000
    # unexpired claim stays claimed
    assert _effective_status('claimed', now + 5000, now) == 'claimed'
    # expired claim → open
    assert _effective_status('claimed', now - 5000, now) == 'open'
    # open/done untouched
    assert _effective_status('open', 0, now) == 'open'
    assert _effective_status('done', 0, now) == 'done'


# ════════════════════════════════════════════════════════════════════
#  blocked produced ONLY by the board path
# ════════════════════════════════════════════════════════════════════

def test_block_emits_blocked_kind(flask_app):
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/b/blk', 'cA', 'epic')['id']
        res = block_task('/b/blk', 'cA', tid, 'waiting on API key')
    assert res['ok']
    kinds = _feed_kinds(flask_app, '/b/blk')
    assert kinds.count('blocked') == 1


# ════════════════════════════════════════════════════════════════════
#  Auto-avoidance injection
# ════════════════════════════════════════════════════════════════════

def test_render_avoid_duplication_hint_present(flask_app):
    """When ANOTHER conversation holds an unexpired claim, the rendered board
    carries an explicit avoid-duplication hint keyed to that owner."""
    from lib.conversations.project_board import claim_task, post_task, render_board_block
    with flask_app.app_context():
        tid = post_task('/b/inj', 'cA', 'Refactor the parser')['id']
        claim_task('/b/inj', 'cOWNER', tid)
        # A DIFFERENT conversation reads the board.
        block = render_board_block('/b/inj', current_conv_id='cREADER')
    assert '[PROJECT BOARD]' in block
    assert 'cOWNER' in block
    assert 'AVOID DUPLICATING' in block or 'avoid' in block.lower()
    assert 'do not redo' in block.lower()


def test_render_no_hint_for_own_claim(flask_app):
    """The reader's OWN claim is marked '(you)', not an avoid-duplication warning."""
    from lib.conversations.project_board import claim_task, post_task, render_board_block
    with flask_app.app_context():
        tid = post_task('/b/own', 'cA', 'My epic')['id']
        claim_task('/b/own', 'cME', tid)
        block = render_board_block('/b/own', current_conv_id='cME')
    assert '(you)' in block
    assert 'do not redo' not in block.lower()


def test_render_empty_board(flask_app):
    from lib.conversations.project_board import render_board_block
    with flask_app.app_context():
        assert render_board_block('/b/none') == ''


def test_injection_present_when_board_nonempty(flask_app):
    out = _run_inject(flask_app, '/b/seam', seed=True)
    assert '[PROJECT BOARD]' in out


def test_injection_absent_when_board_empty(flask_app):
    out = _run_inject(flask_app, '/b/seam2', seed=False)
    assert '[PROJECT BOARD]' not in out


def _run_inject(flask_app, project_path, seed):
    from lib.conversations.project_board import claim_task, post_task
    from lib.tasks_pkg import system_context as sc
    with flask_app.app_context():
        if seed:
            tid = post_task(project_path, 'cA', 'Seam epic')['id']
            claim_task(project_path, 'cOWNER', tid)
        messages = [{'role': 'user', 'content': 'hi'}]
        sc._inject_system_contexts(
            messages, project_path, True,
            False, False, False, True,
            conv_id='cREADER', task=None)
    parts = []
    for m in messages:
        c = m.get('content', '')
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for seg in c:
                if isinstance(seg, dict):
                    parts.append(seg.get('text', '') or '')
    return '\n'.join(parts)


# ════════════════════════════════════════════════════════════════════
#  Route: GET /board
# ════════════════════════════════════════════════════════════════════

def test_route_board_read(flask_app, flask_client):
    import json as _json
    from lib.conversations.project_board import claim_task, post_task
    with flask_app.app_context():
        tid = post_task('/b/route', 'cA', 'epic one')['id']
        claim_task('/b/route', 'cOWNER', tid)
        post_task('/b/route', 'cA', 'epic two')
    r = flask_client.get('/api/v1/project/board?path=/b/route')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert data['claimed'] == 1 and data['open'] == 1
    claimed = [t for t in data['tasks'] if t['status'] == 'claimed'][0]
    assert claimed['owner_conv_id'] == 'cOWNER'


def test_route_board_requires_path(flask_client):
    assert flask_client.get('/api/v1/project/board').status_code == 400



# ════════════════════════════════════════════════════════════════════
#  Trailing-slash path normalization (the screenshot "board empty with
#  data" root cause: write side kept a trailing slash, read side stripped
#  it → keys diverged → reads found nothing). All board reads+writes must
#  canonicalise the path so a `path` and a `path/` variant hit the SAME
#  storage key. Proven end-to-end through the REAL GET /board route.
# ════════════════════════════════════════════════════════════════════

def test_trailing_slash_write_read_agree(flask_app):
    """An epic posted under a trailing-slash path is found reading the
    stripped path, and vice-versa — the write/read keys canonicalise equal."""
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        # Write with a trailing slash; read with the stripped form.
        r = post_task('/b/slash/', 'cA', 'Slashed epic')
        assert r['ok']
        stripped = read_board('/b/slash')
        slashed = read_board('/b/slash/')
    assert stripped['open'] == 1, 'stripped read must find the slash-written epic'
    assert slashed['open'] == 1, 'slashed read must find the same epic'
    assert stripped['tasks'][0]['id'] == slashed['tasks'][0]['id'], \
        'both path variants must resolve to the SAME row (one storage key)'


def test_trailing_slash_route_matches_stripped(flask_app, flask_client):
    """The REAL GET /board route: an epic written under the stripped path is
    returned when the browser queries the trailing-slash variant (mirrors the
    frontend sending conv.projectPath verbatim vs the panel's stripped form)."""
    import json as _json
    from lib.conversations.project_board import post_task
    with flask_app.app_context():
        post_task('/b/routeslash', 'cA', 'route epic')  # stored stripped
    # Browser queries WITH a trailing slash → must still resolve to the row.
    r = flask_client.get('/api/v1/project/board?path=/b/routeslash/')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert data['open'] == 1, \
        'trailing-slash query must resolve to the stripped-key row (not empty)'


def test_NC3_no_normalization_breaks_slash_match(flask_app):
    """NC-3: no-op normalize_project_path in project_feed → the board's read
    and write keys diverge on a trailing slash → the slash/stripped reads
    disagree (the exact screenshot bug reproduces). Byte-identical restore."""
    import importlib

    _FEED_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_feed.py')

    def run():
        import lib.conversations.project_board as pb
        import lib.conversations.project_feed as pf
        importlib.reload(pf)
        importlib.reload(pb)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            db.execute("DELETE FROM project_tasks WHERE project_path IN ('/nc3','/nc3/')")
            db.commit()
            pb.post_task('/nc3/', 'cA', 'epic')       # write under slash
            stripped = pb.read_board('/nc3')          # read stripped
        # With normalization no-opped, the slash-write lands under '/nc3/' but
        # the stripped read queries '/nc3' → MISS → empty board (the bug).
        assert stripped['open'] == 0, \
            'NC-3: without normalization the stripped read must MISS the ' \
            'slash-written epic (reproduces the empty-board-with-data bug)'

    _patch_restore(
        _FEED_SRC,
        "    if not project_path:\n        return ''\n    return _TRAILING_SEP_RE.sub('', str(project_path))",
        "    return str(project_path or '')  # NC-3 (normalization disabled)",
        run,
    )
    # Reload both modules from the restored source so later tests see the fix.
    import lib.conversations.project_board as pb
    import lib.conversations.project_feed as pf
    importlib.reload(pf)
    importlib.reload(pb)


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


def test_NC1_expired_lease_noop_breaks_antideadlock(flask_app):
    """NC-1: no-op the expired-lease→open reclaim → an expired claim stays
    locked → the anti-deadlock test FAILS."""
    import importlib

    def run():
        import lib.conversations.project_board as pb
        importlib.reload(pb)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute("DELETE FROM project_tasks WHERE project_path='/nc1b'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = pb.post_task('/nc1b', 'cA', 'epic')['id']
            pb.claim_task('/nc1b', 'cB', tid)
            get_thread_db(DOMAIN_CHAT).execute(
                "UPDATE project_tasks SET lease_expires_at=1 WHERE id=?", (tid,))
            get_thread_db(DOMAIN_CHAT).commit()
            board = pb.read_board('/nc1b')
        # With the reclaim no-opped, the expired claim stays 'claimed'.
        assert board['tasks'][0]['status'] == 'claimed', \
            'NC-1: with reclaim disabled, expired claim must stay locked'

    _patch_restore(
        _BOARD_SRC,
        "    if stored_status == 'claimed' and lease_expires_at and lease_expires_at <= now_ms:\n        return 'open'\n    return stored_status",
        "    return stored_status  # NC-1 (reclaim disabled)",
        run,
    )
    import lib.conversations.project_board as pb
    importlib.reload(pb)


def test_NC2_avoidance_hint_noop_breaks_injection(flask_app):
    """NC-2: no-op the avoid-duplication hint → the rendered board no longer
    warns a reader off a sibling's claimed epic → the avoidance test FAILS."""
    import importlib

    def run():
        import lib.conversations.project_board as pb
        importlib.reload(pb)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute("DELETE FROM project_tasks WHERE project_path='/nc2b'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = pb.post_task('/nc2b', 'cA', 'epic')['id']
            pb.claim_task('/nc2b', 'cOWNER', tid)
            block = pb.render_board_block('/nc2b', current_conv_id='cREADER')
        assert 'do not redo' not in block.lower(), \
            'NC-2: with the hint disabled, no avoid-duplication warning must appear'

    _patch_restore(
        _BOARD_SRC,
        "            hint = '' if mine else ' — another conversation is advancing this; ' \\\n                   'pick a different epic or coordinate, do not redo it'",
        "            hint = ''  # NC-2 (avoidance hint disabled)",
        run,
    )
    import lib.conversations.project_board as pb
    importlib.reload(pb)



# ════════════════════════════════════════════════════════════════════
#  reopen_task — the HUMAN override (done|claimed → open)
#  A direct status write (NOT a lease mutation): clears owner + lease so the
#  epic is claimable again; emits a `note` feed event so the transition is
#  observable. Permitted from done (revive) and claimed (break a stuck claim).
# ════════════════════════════════════════════════════════════════════

def test_reopen_done_to_open(flask_app):
    from lib.conversations.project_board import (
        complete_task, post_task, read_board, reopen_task,
    )
    with flask_app.app_context():
        tid = post_task('/b/reo1', 'cA', 'finished epic')['id']
        complete_task('/b/reo1', 'cA', tid)
        res = reopen_task('/b/reo1', 'cHUMAN', tid)
        board = read_board('/b/reo1')
    assert res['ok'] and res['from'] == 'done'
    t = board['tasks'][0]
    assert t['status'] == 'open' and t['owner_conv_id'] == ''
    assert board['open'] == 1 and board['done'] == 0
    assert 'note' in _feed_kinds(flask_app, '/b/reo1')


def test_reopen_claimed_clears_owner_and_lease(flask_app):
    """Reopening a LIVE claimed epic breaks the claim: status→open, owner and
    lease cleared, so a sibling can pick it up (the human 'break a stuck live
    claim' lever). The feed note records who previously held it."""
    from lib.conversations.project_board import (
        claim_task, post_task, read_board, reopen_task,
    )
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        tid = post_task('/b/reo2', 'cA', 'held epic')['id']
        claim_task('/b/reo2', 'cOWNER', tid)   # live, unexpired lease
        res = reopen_task('/b/reo2', 'cHUMAN', tid)
        board = read_board('/b/reo2')
        events = read_project_feed('/b/reo2', limit=500)['events']
    assert res['ok'] and res['from'] == 'claimed'
    t = board['tasks'][0]
    assert t['status'] == 'open', 'reopened claim must read open'
    assert t['owner_conv_id'] == '', 'reopen must clear the owner'
    assert t['lease_expires_at'] == 0, 'reopen must clear the lease (not a lease mutation)'
    # The transition is observable and names the previous owner.
    note = [e for e in events if e['kind'] == 'note'
            and e.get('payload', {}).get('reopened')]
    assert note, 'reopen must emit an observable note event'
    assert note[0]['payload'].get('prevOwner') == 'cOWNER'


def test_reopen_already_open_is_refused(flask_app):
    from lib.conversations.project_board import post_task, reopen_task
    with flask_app.app_context():
        tid = post_task('/b/reo3', 'cA', 'open epic')['id']
        res = reopen_task('/b/reo3', 'cHUMAN', tid)
    assert res['ok'] is False and res['error'] == 'already_open'


def test_reopen_missing_task(flask_app):
    from lib.conversations.project_board import reopen_task
    with flask_app.app_context():
        res = reopen_task('/b/reo4', 'cHUMAN', 'pt_does_not_exist')
    assert res['ok'] is False and res['error'] == 'task not found'


def test_reopened_claim_flips_to_open_in_prev_owner_injection(flask_app):
    """The stated edge case: after a human reopens a live claim, the previous
    owner's injected [PROJECT BOARD] block no longer marks the epic '(you)' —
    it shows as a plain OPEN epic on the owner's NEXT prompt assembly (the
    block is re-read per turn, so the owner is not interrupted mid-turn)."""
    from lib.conversations.project_board import (
        claim_task, post_task, render_board_block, reopen_task,
    )
    with flask_app.app_context():
        tid = post_task('/b/reo5', 'cA', 'Owned epic')['id']
        claim_task('/b/reo5', 'cOWNER', tid)
        before = render_board_block('/b/reo5', current_conv_id='cOWNER')
        reopen_task('/b/reo5', 'cHUMAN', tid)
        after = render_board_block('/b/reo5', current_conv_id='cOWNER')
    assert '(you)' in before, 'owner saw the epic as its own before reopen'
    assert '(you)' not in after, 'after reopen the owner no longer owns it'
    assert 'Open (unclaimed' in after and 'Owned epic' in after, \
        'reopened epic appears in the open lane on the next assembly'


# ════════════════════════════════════════════════════════════════════
#  Routes: POST /board/post|complete|block|reopen (human mutations)
# ════════════════════════════════════════════════════════════════════

def test_route_board_post_uses_conv_as_creator(flask_app, flask_client):
    """POST /board/post: convId becomes created_by_conv (the dispatch target),
    so a human-posted epic is dispatchable exactly like an agent-posted one."""
    import json as _json
    from lib.conversations.project_board import read_board
    r = flask_client.post('/api/v1/project/board/post', json={
        'path': '/b/rpost', 'title': 'Human epic', 'convId': 'cDISPLAYED'})
    assert r.status_code == 200, r.get_data(as_text=True)
    tid = _json.loads(r.get_data(as_text=True))['id']
    with flask_app.app_context():
        board = read_board('/b/rpost')
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['created_by_conv'] == 'cDISPLAYED', \
        'displayed conv must be the epic creator (dispatch target)'
    assert t['status'] == 'open'


def test_route_board_post_requires_conv(flask_client):
    """No conversation context → refused (never invents one / falls to _state)."""
    r = flask_client.post('/api/v1/project/board/post',
                          json={'path': '/b/rpost2', 'title': 'x'})
    assert r.status_code == 400
    assert 'convId' in r.get_data(as_text=True)


def test_route_board_post_requires_path_and_title(flask_client):
    assert flask_client.post('/api/v1/project/board/post',
                             json={'title': 'x', 'convId': 'c'}).status_code == 400
    assert flask_client.post('/api/v1/project/board/post',
                             json={'path': '/p', 'convId': 'c'}).status_code == 400


def test_route_board_complete(flask_app, flask_client):
    import json as _json
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/b/rcomp', 'cA', 'epic')['id']
    r = flask_client.post('/api/v1/project/board/complete', json={
        'path': '/b/rcomp', 'taskId': tid, 'convId': 'cHUMAN'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert read_board('/b/rcomp')['done'] == 1


def test_route_board_reopen(flask_app, flask_client):
    import json as _json
    from lib.conversations.project_board import claim_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/b/rreo', 'cA', 'epic')['id']
        claim_task('/b/rreo', 'cOWNER', tid)
    r = flask_client.post('/api/v1/project/board/reopen', json={
        'path': '/b/rreo', 'taskId': tid, 'convId': 'cHUMAN'})
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert data['from'] == 'claimed'
    with flask_app.app_context():
        board = read_board('/b/rreo')
    assert board['open'] == 1 and board['claimed'] == 0


def test_route_board_mutations_require_path(flask_client):
    for ep in ('complete', 'block', 'reopen'):
        r = flask_client.post('/api/v1/project/board/' + ep,
                              json={'taskId': 't', 'convId': 'c'})
        assert r.status_code == 400, ep


# ════════════════════════════════════════════════════════════════════
#  NC-4: reopen must CLEAR the owner. No-op the owner-clear in reopen_task
#  → a reopened claimed epic keeps its owner → the owner-clear test FAILS.
# ════════════════════════════════════════════════════════════════════

def test_NC4_reopen_owner_clear_noop_breaks(flask_app):
    import importlib

    def run():
        import lib.conversations.project_board as pb
        importlib.reload(pb)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/nc4'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = pb.post_task('/nc4', 'cA', 'epic')['id']
            pb.claim_task('/nc4', 'cOWNER', tid)
            pb.reopen_task('/nc4', 'cHUMAN', tid)
            board = pb.read_board('/nc4')
        t = board['tasks'][0]
        # With the owner-clear no-opped, the row keeps status='claimed' with the
        # stale owner + lease → the epic reads as still-claimed (the yank fails).
        assert t['status'] == 'claimed' and t['owner_conv_id'] == 'cOWNER', \
            'NC-4: with owner-clear disabled, reopen must NOT free the claim'

    _patch_restore(
        _BOARD_SRC,
        "        db.execute(\n            \"UPDATE project_tasks SET status='open', owner_conv_id='', \"\n            'lease_expires_at=0, dispatched=0, updated_at=? '\n            'WHERE id=? AND project_path=?',\n            (_now_ms(), task_id, project_path))\n        db.commit()",
        "        pass  # NC-4 (reopen status/owner write disabled)",
        run,
    )
    import lib.conversations.project_board as pb
    importlib.reload(pb)


# ════════════════════════════════════════════════════════════════════
#  Route strict-path keying + audit_log. The mutating routes must key
#  STRICTLY on the explicit `path` body field (never _state) — proven by
#  the path-required 400s above — AND audit-log the human action. This
#  drives the REAL route through flask_client and captures audit_log.
# ════════════════════════════════════════════════════════════════════

def test_route_board_post_audit_logs_and_keys_on_explicit_path(
        flask_app, flask_client, monkeypatch):
    """A human post through the REAL route audit-logs with the EXPLICIT path
    from the body (never the active-project global) and lands the epic under
    exactly that path."""
    captured = []
    monkeypatch.setattr('lib.conversations.project_board.audit_log',
                        lambda action, **kw: captured.append((action, kw)))
    from lib.conversations.project_board import read_board
    r = flask_client.post('/api/v1/project/board/post', json={
        'path': '/b/raudit', 'title': 'Audited epic', 'convId': 'cH'})
    assert r.status_code == 200, r.get_data(as_text=True)
    # audit_log('board_post', ...) fired with the explicit path.
    posts = [kw for action, kw in captured if action == 'board_post']
    assert posts, 'board/post must audit_log the human action'
    assert posts[0].get('project_path') == '/b/raudit', \
        'audit must record the EXPLICIT path from the body, not a global'
    # And the epic really landed under that path only.
    with flask_app.app_context():
        assert read_board('/b/raudit')['open'] == 1


# ════════════════════════════════════════════════════════════════════
#  NC-5: the human board action must be audit-logged. No-op the
#  audit_log('board_post', …) call in the engine → the audit capture is
#  empty → the audit-trail contract test FAILS. Byte-identical restore.
# ════════════════════════════════════════════════════════════════════

def test_NC5_board_post_audit_noop_breaks(flask_app):
    import importlib

    from lib import log as _log
    _orig_audit = _log.audit_log

    def run():
        captured = []
        _log.audit_log = lambda action, **kw: captured.append((action, kw))
        try:
            import lib.conversations.project_board as pb
            importlib.reload(pb)   # rebinds `from lib.log import audit_log`
            with flask_app.app_context():
                from lib.database import DOMAIN_CHAT, get_thread_db
                get_thread_db(DOMAIN_CHAT).execute(
                    "DELETE FROM project_tasks WHERE project_path='/nc5'")
                get_thread_db(DOMAIN_CHAT).commit()
                pb.post_task('/nc5', 'cA', 'epic')
            # With the audit call no-opped, NO board_post audit event fired.
            assert not [kw for a, kw in captured if a == 'board_post'], \
                'NC-5: with the audit call disabled, no board_post audit fires'
        finally:
            _log.audit_log = _orig_audit

    _patch_restore(
        _BOARD_SRC,
        "    audit_log('board_post', project_path=project_path, task_id=task_id, conv_id=conv_id)\n    return {'ok': True, 'id': task_id}",
        "    return {'ok': True, 'id': task_id}  # NC-5 (audit disabled)",
        run,
    )
    import lib.conversations.project_board as pb
    importlib.reload(pb)
