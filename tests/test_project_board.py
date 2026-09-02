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

import functools
import os

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

TEST_OWNER_USER_ID = 1

import tests._seed as seed  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')


@pytest.fixture(autouse=True)
def _bind_business_test_principal(monkeypatch):
    """Bind this business-behavior suite to an explicit fixture owner.

    Principal omission/default-deny behavior belongs to the identity contract
    suite.  These tests exercise board semantics, so their test adapter carries
    owner 1 while preserving any owner supplied by an individual test.
    """
    import lib.conversations.project_board as project_board

    for name in (
        'answer_task',
        'block_task',
        'claim_task',
        'complete_task',
        'delete_task',
        'post_task',
        'read_board',
        'render_board_block',
        'reopen_task',
    ):
        original = getattr(project_board, name)

        @functools.wraps(original)
        def call_as_test_user(*args, _original=original, **kwargs):
            kwargs.setdefault('user_id', 1)
            return _original(*args, **kwargs)

        monkeypatch.setattr(project_board, name, call_as_test_user)


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)

@pytest.fixture(autouse=True)
def _stub_dispatch(monkeypatch):
    # Board CRUD unit tests must not let the brain-dispatch event channel fire
    # (it claims/dispatches epics as a side effect of post/complete, which
    # couples otherwise-independent assertions). Dispatch has its own
    # dedicated suites (test_project_dispatch.py / test_project_brain_*).
    import lib.conversations.project_dispatch as _pd
    for name in ('on_epic_posted', 'on_epic_completed', 'on_epic_answered'):
        monkeypatch.setattr(_pd, name, lambda *a, **k: 0)


def _feed_kinds(project_path):
    from lib.conversations.project_feed import read_project_feed
    return [
        event['kind']
        for event in read_project_feed(
            project_path, user_id=1, limit=500,
        )['events']
    ]


def _board_tasks(project_path):
    from lib.storage import get_storage_client
    return get_storage_client().query(
        'board.list', {
            'user_id': 1, 'project_path': project_path,
        }).get('tasks', [])


def _seed_expired_claim(project_path, task_id, owner, title='epic'):
    """Seed a CLAIMED task whose lease is already in the past (the read-time
    reclaim path must report it open)."""
    seed.seed_board_task(task_id, project_path, user_id=1,
                         title=title, status='claimed',
                         owner_conv_id=owner, lease_expires_at=1)


# ════════════════════════════════════════════════════════════════════
#  post / read / complete
# ════════════════════════════════════════════════════════════════════

def test_post_then_read():
    from lib.conversations.project_board import post_task, read_board
    r = post_task('/b/p', 'cA', 'Build the widget', user_id=TEST_OWNER_USER_ID)
    assert r['ok'] and r['id'].startswith('pt_')
    board = read_board('/b/p', user_id=TEST_OWNER_USER_ID)
    assert board['open'] == 1 and board['claimed'] == 0
    assert board['tasks'][0]['title'] == 'Build the widget'
    assert board['tasks'][0]['status'] == 'open'


def test_complete():
    from lib.conversations.project_board import complete_task, post_task, read_board
    tid = post_task('/b/c', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
    assert complete_task('/b/c', 'cA', tid, user_id=TEST_OWNER_USER_ID)['ok']
    board = read_board('/b/c', user_id=TEST_OWNER_USER_ID)
    assert board['done'] == 1
    assert 'completed' in _feed_kinds('/b/c')


def test_isolated_epic_cannot_complete_before_candidate_merge(monkeypatch):
    from lib.conversations.project_board import complete_task, post_task, read_board
    tid = post_task('/b/integration-gate', 'cA', 'epic',
                    user_id=TEST_OWNER_USER_ID)['id']
    monkeypatch.setattr(
        'lib.integration_control.board_completion_gate',
        lambda *_args, **_kwargs: {
            'ok': False,
            'integrationRequired': True,
            'state': 'ready',
            'error': 'integration_not_merged',
        },
    )

    result = complete_task(
        '/b/integration-gate', 'cA', tid, user_id=TEST_OWNER_USER_ID)

    assert result == {
        'ok': False,
        'error': 'integration_not_merged',
        'integrationState': 'ready',
    }
    assert read_board(
        '/b/integration-gate', user_id=TEST_OWNER_USER_ID)['done'] == 0


# ════════════════════════════════════════════════════════════════════
#  delete (human lever — outright removal, unlike complete's done-history)
# ════════════════════════════════════════════════════════════════════

def test_delete_open_epic():
    from lib.conversations.project_board import delete_task, post_task, read_board
    tid = post_task('/b/del', 'cA', 'junk praise epic', user_id=TEST_OWNER_USER_ID)['id']
    assert delete_task('/b/del', 'cA', tid, user_id=TEST_OWNER_USER_ID)['ok']
    board = read_board('/b/del', user_id=TEST_OWNER_USER_ID)
    assert board['tasks'] == []
    assert 'note' in _feed_kinds('/b/del')


def test_delete_done_epic_removes_history():
    """Done epics are history — but a JUNK done epic (e.g. an epic whose
    title was praise, not work) must be removable too."""
    from lib.conversations.project_board import (
        complete_task, delete_task, post_task, read_board,
    )
    tid = post_task('/b/deldone', 'cA', 'praise, not work', user_id=TEST_OWNER_USER_ID)['id']
    assert complete_task('/b/deldone', 'cA', tid, user_id=TEST_OWNER_USER_ID)['ok']
    assert delete_task('/b/deldone', 'cA', tid, user_id=TEST_OWNER_USER_ID)['ok']
    assert read_board('/b/deldone', user_id=TEST_OWNER_USER_ID)['tasks'] == []
def test_delete_missing_task():
    from lib.conversations.project_board import delete_task
    res = delete_task('/b/delnone', 'cA', 'pt_nope', user_id=TEST_OWNER_USER_ID)
    assert not res['ok'] and 'not found' in res['error']


def test_delete_refused_while_active_dependent():
    """CONSISTENCY GATE: a deleted dep can never reach done, so deleting an
    epic that an ACTIVE epic depends on would strand the dependent forever.
    The refusal names the dependent; completing it first unblocks the delete."""
    from lib.conversations.project_board import (
        complete_task, delete_task, post_task, read_board,
    )
    a = post_task('/b/deldep', 'cA', 'dep epic', user_id=TEST_OWNER_USER_ID)['id']
    post_task('/b/deldep', 'cA', 'waiting epic', depends_on=[a], user_id=TEST_OWNER_USER_ID)
    res = delete_task('/b/deldep', 'cA', a, user_id=TEST_OWNER_USER_ID)
    assert not res['ok'] and res['error'] == 'has_dependents'
    assert any('waiting epic' in d for d in res['dependents'])
    # The refused delete removed NOTHING.
    assert len(read_board('/b/deldep', user_id=TEST_OWNER_USER_ID)['tasks']) == 2
    # Dependent completed → the gate opens.
    b = [t['id'] for t in read_board('/b/deldep', user_id=TEST_OWNER_USER_ID)['tasks']
         if t['title'] == 'waiting epic'][0]
    assert complete_task('/b/deldep', 'cA', b, user_id=TEST_OWNER_USER_ID)['ok']
    _r = delete_task('/b/deldep', 'cA', a, user_id=TEST_OWNER_USER_ID)
    assert _r['ok'], _r
    assert [t['title'] for t in read_board('/b/deldep', user_id=TEST_OWNER_USER_ID)['tasks']] == \
        ['waiting epic']


def test_delete_live_claim_allowed():
    """The claim lease is advisory — a deleting HUMAN outranks a live claim
    (the claimant is not interrupted mid-turn; its later completion simply
    misses the row, which that path already tolerates)."""
    from lib.conversations.project_board import (
        claim_task, delete_task, post_task, read_board,
    )
    tid = post_task('/b/delclaim', 'cA', 'claimed junk', user_id=TEST_OWNER_USER_ID)['id']
    assert claim_task('/b/delclaim', 'cB', tid, user_id=TEST_OWNER_USER_ID)['ok']
    assert delete_task('/b/delclaim', 'cA', tid, user_id=TEST_OWNER_USER_ID)['ok']
    assert read_board('/b/delclaim', user_id=TEST_OWNER_USER_ID)['tasks'] == []
def test_done_epics_do_not_count_toward_admission_cap():
    """A board full of COMPLETED epics must still accept a new post — the
    reported "board full indefinitely" bug. The active-only admission counts
    status!='done' rows, so completing epics frees the board back up."""
    from lib.conversations.project_board import (
        _MAX_ACTIVE_TASKS, complete_task, post_task,
    )
    import lib.conversations.project_board as pb
    # Fill the board to the active cap, then COMPLETE them all.
    ids = []
    # Shrink the cap for the test so we don't insert 200 rows.
    orig_active = pb._MAX_ACTIVE_TASKS
    pb._MAX_ACTIVE_TASKS = 3
    try:
        for i in range(3):
            r = post_task('/b/cap', 'cA', f'epic {i}', user_id=TEST_OWNER_USER_ID)
            assert r['ok'], r
            ids.append(r['id'])
        # At the cap now → a further active post is refused.
        refused = post_task('/b/cap', 'cA', 'one too many', user_id=TEST_OWNER_USER_ID)
        assert not refused['ok'] and 'full' in refused['error']
        # Complete them all → board should accept new epics again.
        for tid in ids:
            assert complete_task('/b/cap', 'cA', tid, user_id=TEST_OWNER_USER_ID)['ok']
        after = post_task('/b/cap', 'cA', 'now there is room', user_id=TEST_OWNER_USER_ID)
        assert after['ok'], \
            'completed epics must not count toward the admission cap'
    finally:
        pb._MAX_ACTIVE_TASKS = orig_active
    # sanity: the alias still points somewhere sensible
    assert _MAX_ACTIVE_TASKS >= 1



def test_old_done_epics_are_pruned_on_post():
    """Completed epics are retained but BOUNDED — posting past the done-retain
    cap prunes the OLDEST done rows so project_tasks can't grow forever."""
    from lib.conversations.project_board import post_task
    import lib.conversations.project_board as pb
    orig_done = pb._MAX_DONE_RETAINED
    pb._MAX_DONE_RETAINED = 3
    try:
        # Seed 5 done epics with staggered updated_at so "oldest" is
        # well-defined, then one more post triggers the prune.
        for i in range(5):
            seed.seed_board_task(f'pt_prune{i}', '/b/prune', user_id=1,
                                 title=f'done epic {i}', status='done',
                                 updated_at=1000 + i)
        # A fresh post runs the prune: keep only _MAX_DONE_RETAINED done rows.
        post_task('/b/prune', 'cA', 'the trigger', user_id=1)
        done = sorted((t for t in _board_tasks('/b/prune') if t['status'] == 'done'),
                      key=lambda t: t['updated_at'])
    finally:
        pb._MAX_DONE_RETAINED = orig_done
    titles = [r['title'] for r in done]
    assert len(titles) == 3, f'done rows must be pruned to the cap, got {titles}'
    # The oldest two (epic 0, epic 1) were pruned; the newest three remain.
    assert titles == ['done epic 2', 'done epic 3', 'done epic 4'], titles


def test_long_title_survives_roundtrip_uncapped():
    """A multi-sentence epic description (~1500 chars) MUST survive
    post_task → read_board → render_board_block with ZERO clipping.

    Regression guard for the silent write-time clip that stood for a long
    time: the epic-title cap had been set to project_feed._SUMMARY_MAX_CHARS
    (280), so any epic longer than a feed-row summary was truncated mid-word
    both in the panel and in the injected prompt block. The cap is now 2000;
    this pins it so the next person who copies the feed-summary reasoning (or
    'tidies' the cap back down) fails loudly instead of re-clipping silently.
    """
    from lib.conversations.project_board import (
        _TITLE_MAX_CHARS, post_task, read_board, render_board_block,
    )
    from lib.conversations.project_feed import _SUMMARY_MAX_CHARS
    # The title cap must stay well above the feed summary cap it was once
    # accidentally equated with.
    assert _TITLE_MAX_CHARS > _SUMMARY_MAX_CHARS, \
        'epic-title cap must NOT be reduced to the feed-row summary cap'
    tail = ' TAIL_SENTINEL_c0ffee_END'
    long_title = ('D data-tier scale-out ceiling ' * 60).strip()[:1500] + tail
    assert len(long_title) > _SUMMARY_MAX_CHARS * 4, 'title comfortably past any old cap'
    r = post_task('/b/long', 'cA', long_title, user_id=TEST_OWNER_USER_ID)
    assert r['ok']
    board = read_board('/b/long', user_id=TEST_OWNER_USER_ID)
    block = render_board_block('/b/long', current_conv_id='cREADER', user_id=TEST_OWNER_USER_ID)
    stored = board['tasks'][0]['title']
    assert stored == long_title, 'stored title must be BYTE-IDENTICAL (uncapped)'
    assert stored.endswith(tail), 'the tail must survive (not clipped mid-word)'
    assert tail in block, 'the full tail must appear in the injected board block'


# ════════════════════════════════════════════════════════════════════
#  claim writes owner + lease; emits claimed
# ════════════════════════════════════════════════════════════════════

def test_claim_writes_owner_and_lease():
    from lib.conversations.project_board import claim_task, post_task, read_board
    tid = post_task('/b/cl', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
    res = claim_task('/b/cl', 'cB', tid, user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['lease_expires_at'] > 0
    board = read_board('/b/cl', user_id=TEST_OWNER_USER_ID)
    t = board['tasks'][0]
    assert t['status'] == 'claimed'
    assert t['owner_conv_id'] == 'cB'
    assert t['lease_expires_at'] > 0
    assert 'claimed' in _feed_kinds('/b/cl')


def test_dispatched_badge_flows_through():
    """A claim minted with dispatched=True surfaces dispatched=True on the
    board card; a normal claim does not; completing resets it."""
    from lib.conversations.project_board import (
        claim_task, complete_task, post_task, read_board,
    )
    d_id = post_task('/b/disp', 'cA', 'brain epic', user_id=TEST_OWNER_USER_ID)['id']
    n_id = post_task('/b/disp', 'cA', 'human epic', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/disp', 'cBRAIN', d_id, dispatched=True, user_id=TEST_OWNER_USER_ID)
    claim_task('/b/disp', 'cHUMAN', n_id, user_id=TEST_OWNER_USER_ID)   # normal claim
    board = read_board('/b/disp', user_id=TEST_OWNER_USER_ID)
    by_id = {t['id']: t for t in board['tasks']}
    assert by_id[d_id]['dispatched'] is True, 'brain-dispatched claim → badge'
    assert by_id[n_id]['dispatched'] is False, 'human claim → no badge'
    # Completing resets the flag.
    complete_task('/b/disp', 'cBRAIN', d_id, user_id=TEST_OWNER_USER_ID)
    board2 = read_board('/b/disp', user_id=TEST_OWNER_USER_ID)
    assert [t for t in board2['tasks'] if t['id'] == d_id][0]['dispatched'] is False


def test_claim_refused_when_held_by_other():
    from lib.conversations.project_board import claim_task, post_task
    tid = post_task('/b/cf', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
    assert claim_task('/b/cf', 'cB', tid, user_id=TEST_OWNER_USER_ID)['ok']
    # A different conversation cannot claim an actively-held epic.
    res = claim_task('/b/cf', 'cC', tid, user_id=TEST_OWNER_USER_ID)
    assert res['ok'] is False and res['error'] == 'already_claimed'
    assert res['owner'] == 'cB'


# ════════════════════════════════════════════════════════════════════
#  ANTI-DEADLOCK: expired lease reads as open (the core property)
# ════════════════════════════════════════════════════════════════════

def test_expired_lease_reads_as_open():
    """A claimed epic whose lease has expired MUST read as open — the
    anti-deadlock core (evaluated at read time, no reaper)."""
    from lib.conversations.project_board import read_board
    # Seed a CLAIMED task whose lease is already in the past (1ms since epoch).
    _seed_expired_claim('/b/exp', 'pt_exp1', 'cB')
    board = read_board('/b/exp', user_id=TEST_OWNER_USER_ID)
    t = board['tasks'][0]
    assert t['status'] == 'open', 'expired claim must read as open (anti-deadlock)'
    assert t['owner_conv_id'] == '', 'expired claim must drop the owner in the read view'
    assert board['open'] == 1 and board['claimed'] == 0


def test_expired_lease_is_reclaimable():
    """After a lease expires, a DIFFERENT conversation can claim the epic."""
    from lib.conversations.project_board import claim_task
    _seed_expired_claim('/b/recl', 'pt_recl1', 'cB')
    res = claim_task('/b/recl', 'cC', 'pt_recl1', user_id=TEST_OWNER_USER_ID)  # different conv reclaims
    assert res['ok'], 'an expired lease must be reclaimable by another conversation'



# ════════════════════════════════════════════════════════════════════
#  blocked produced ONLY by the board path
# ════════════════════════════════════════════════════════════════════

def test_block_emits_blocked_kind():
    from lib.conversations.project_board import block_task, post_task
    tid = post_task('/b/blk', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
    res = block_task('/b/blk', 'cA', tid, 'waiting on API key', user_id=TEST_OWNER_USER_ID)
    assert res['ok']
    kinds = _feed_kinds('/b/blk')
    assert kinds.count('blocked') == 1


# ════════════════════════════════════════════════════════════════════
#  Auto-avoidance injection
# ════════════════════════════════════════════════════════════════════

def test_render_avoid_duplication_hint_present():
    """When ANOTHER conversation holds an unexpired claim, the rendered board
    carries an explicit avoid-duplication hint keyed to that owner."""
    from lib.conversations.project_board import claim_task, post_task, render_board_block
    tid = post_task('/b/inj', 'cA', 'Refactor the parser', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/inj', 'cOWNER', tid, user_id=TEST_OWNER_USER_ID)
    # A DIFFERENT conversation reads the board.
    block = render_board_block('/b/inj', current_conv_id='cREADER', user_id=TEST_OWNER_USER_ID)
    assert '[PROJECT BOARD]' in block
    assert 'cOWNER' in block
    assert 'AVOID DUPLICATING' in block or 'avoid' in block.lower()
    assert 'do not redo' in block.lower()


def test_render_no_hint_for_own_claim():
    """The reader's OWN claim is marked '(you)', not an avoid-duplication warning."""
    from lib.conversations.project_board import claim_task, post_task, render_board_block
    tid = post_task('/b/own', 'cA', 'My epic', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/own', 'cME', tid, user_id=TEST_OWNER_USER_ID)
    block = render_board_block('/b/own', current_conv_id='cME', user_id=TEST_OWNER_USER_ID)
    assert '(you)' in block
    assert 'do not redo' not in block.lower()


def test_render_empty_board():
    from lib.conversations.project_board import render_board_block
    assert render_board_block('/b/none', user_id=TEST_OWNER_USER_ID) == ''
def test_injection_present_when_board_nonempty():
    out = _run_inject('/b/seam', seed=True)
    assert '[PROJECT BOARD]' in out


def test_injection_absent_when_board_empty():
    out = _run_inject('/b/seam2', seed=False)
    assert '[PROJECT BOARD]' not in out


def _run_inject(project_path, seed):
    from lib.conversations.project_board import claim_task, post_task
    from lib.tasks_pkg.context_composer import compose_task_context
    if seed:
        tid = post_task(project_path, 'cA', 'Seam epic', user_id=TEST_OWNER_USER_ID)['id']
        claim_task(project_path, 'cOWNER', tid, user_id=TEST_OWNER_USER_ID)
    messages = [{'role': 'user', 'content': 'hi'}]
    compose_task_context(
        messages, user_id=TEST_OWNER_USER_ID,
        project_path=project_path, project_enabled=True,
        memory_enabled=False, search_enabled=False, has_real_tools=True,
        conv_id='cREADER', task={'_userId': 1, 'config': {}})
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

def test_route_board_read(flask_client):
    import json as _json
    from lib.conversations.project_board import claim_task, post_task
    tid = post_task('/b/route', 'cA', 'epic one', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/route', 'cOWNER', tid, user_id=TEST_OWNER_USER_ID)
    post_task('/b/route', 'cA', 'epic two', user_id=TEST_OWNER_USER_ID)
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

def test_trailing_slash_write_read_agree():
    """An epic posted under a trailing-slash path is found reading the
    stripped path, and vice-versa — the write/read keys canonicalise equal."""
    from lib.conversations.project_board import post_task, read_board
    # Write with a trailing slash; read with the stripped form.
    r = post_task('/b/slash/', 'cA', 'Slashed epic', user_id=TEST_OWNER_USER_ID)
    assert r['ok']
    stripped = read_board('/b/slash', user_id=TEST_OWNER_USER_ID)
    slashed = read_board('/b/slash/', user_id=TEST_OWNER_USER_ID)
    assert stripped['open'] == 1, 'stripped read must find the slash-written epic'
    assert slashed['open'] == 1, 'slashed read must find the same epic'
    assert stripped['tasks'][0]['id'] == slashed['tasks'][0]['id'], \
        'both path variants must resolve to the SAME row (one storage key)'


def test_trailing_slash_route_matches_stripped(flask_client):
    """The REAL GET /board route: an epic written under the stripped path is
    returned when the browser queries the trailing-slash variant (mirrors the
    frontend sending conv.projectPath verbatim vs the panel's stripped form)."""
    import json as _json
    from lib.conversations.project_board import post_task
    post_task('/b/routeslash', 'cA', 'route epic', user_id=TEST_OWNER_USER_ID)  # stored stripped
    # Browser queries WITH a trailing slash → must still resolve to the row.
    r = flask_client.get('/api/v1/project/board?path=/b/routeslash/')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert data['open'] == 1, \
        'trailing-slash query must resolve to the stripped-key row (not empty)'



# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROLS
# ════════════════════════════════════════════════════════════════════





# ════════════════════════════════════════════════════════════════════
#  reopen_task — the HUMAN override (done|claimed → open)
#  A direct status write (NOT a lease mutation): clears owner + lease so the
#  epic is claimable again; emits a `note` feed event so the transition is
#  observable. Permitted from done (revive) and claimed (break a stuck claim).
# ════════════════════════════════════════════════════════════════════

def test_reopen_done_to_open():
    from lib.conversations.project_board import (
        complete_task, post_task, read_board, reopen_task,
    )
    tid = post_task('/b/reo1', 'cA', 'finished epic', user_id=TEST_OWNER_USER_ID)['id']
    complete_task('/b/reo1', 'cA', tid, user_id=TEST_OWNER_USER_ID)
    res = reopen_task('/b/reo1', 'cHUMAN', tid, user_id=TEST_OWNER_USER_ID)
    board = read_board('/b/reo1', user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['from'] == 'done'
    t = board['tasks'][0]
    assert t['status'] == 'open' and t['owner_conv_id'] == ''
    assert board['open'] == 1 and board['done'] == 0
    assert 'note' in _feed_kinds('/b/reo1')


def test_reopen_claimed_clears_owner_and_lease():
    """Reopening a LIVE claimed epic breaks the claim: status→open, owner and
    lease cleared, so a sibling can pick it up (the human 'break a stuck live
    claim' lever). The feed note records who previously held it."""
    from lib.conversations.project_board import (
        claim_task, post_task, read_board, reopen_task,
    )
    from lib.conversations.project_feed import read_project_feed
    tid = post_task('/b/reo2', 'cA', 'held epic', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/reo2', 'cOWNER', tid, user_id=TEST_OWNER_USER_ID)   # live, unexpired lease
    res = reopen_task('/b/reo2', 'cHUMAN', tid, user_id=TEST_OWNER_USER_ID)
    board = read_board('/b/reo2', user_id=TEST_OWNER_USER_ID)
    events = read_project_feed('/b/reo2', user_id=1, limit=500)['events']
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


def test_reopen_already_open_is_refused():
    from lib.conversations.project_board import post_task, reopen_task
    tid = post_task('/b/reo3', 'cA', 'open epic', user_id=TEST_OWNER_USER_ID)['id']
    res = reopen_task('/b/reo3', 'cHUMAN', tid, user_id=TEST_OWNER_USER_ID)
    assert res['ok'] is False and res['error'] == 'already_open'


def test_reopen_missing_task():
    from lib.conversations.project_board import reopen_task
    res = reopen_task('/b/reo4', 'cHUMAN', 'pt_does_not_exist', user_id=TEST_OWNER_USER_ID)
    assert res['ok'] is False and res['error'] == 'task not found'


def test_reopened_claim_flips_to_open_in_prev_owner_injection():
    """The stated edge case: after a human reopens a live claim, the previous
    owner's injected [PROJECT BOARD] block no longer marks the epic '(you)' —
    it shows as a plain OPEN epic on the owner's NEXT prompt assembly (the
    block is re-read per turn, so the owner is not interrupted mid-turn)."""
    from lib.conversations.project_board import (
        claim_task, post_task, render_board_block, reopen_task,
    )
    tid = post_task('/b/reo5', 'cA', 'Owned epic', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/reo5', 'cOWNER', tid, user_id=TEST_OWNER_USER_ID)
    before = render_board_block('/b/reo5', current_conv_id='cOWNER', user_id=TEST_OWNER_USER_ID)
    reopen_task('/b/reo5', 'cHUMAN', tid, user_id=TEST_OWNER_USER_ID)
    after = render_board_block('/b/reo5', current_conv_id='cOWNER', user_id=TEST_OWNER_USER_ID)
    assert '(you)' in before, 'owner saw the epic as its own before reopen'
    assert '(you)' not in after, 'after reopen the owner no longer owns it'
    assert 'Open (unclaimed' in after and 'Owned epic' in after, \
        'reopened epic appears in the open lane on the next assembly'


# ════════════════════════════════════════════════════════════════════
#  Routes: POST /board/post|complete|block|reopen (human mutations)
# ════════════════════════════════════════════════════════════════════

def test_route_board_post_uses_conv_as_creator(flask_client):
    """POST /board/post: convId becomes created_by_conv (the dispatch target),
    so a human-posted epic is dispatchable exactly like an agent-posted one."""
    import json as _json
    from lib.conversations.project_board import read_board
    r = flask_client.post('/api/v1/project/board/post', json={
        'path': '/b/rpost', 'title': 'Human epic', 'convId': 'cDISPLAYED'})
    assert r.status_code == 200, r.get_data(as_text=True)
    tid = _json.loads(r.get_data(as_text=True))['id']
    board = read_board('/b/rpost', user_id=TEST_OWNER_USER_ID)
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


def test_route_board_complete(flask_client):
    import json as _json
    from lib.conversations.project_board import post_task, read_board
    tid = post_task('/b/rcomp', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
    r = flask_client.post('/api/v1/project/board/complete', json={
        'path': '/b/rcomp', 'taskId': tid, 'convId': 'cHUMAN'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert read_board('/b/rcomp', user_id=TEST_OWNER_USER_ID)['done'] == 1
def test_route_board_reopen(flask_client):
    import json as _json
    from lib.conversations.project_board import claim_task, post_task, read_board
    tid = post_task('/b/rreo', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/rreo', 'cOWNER', tid, user_id=TEST_OWNER_USER_ID)
    r = flask_client.post('/api/v1/project/board/reopen', json={
        'path': '/b/rreo', 'taskId': tid, 'convId': 'cHUMAN'})
    assert r.status_code == 200, r.get_data(as_text=True)
    data = _json.loads(r.get_data(as_text=True))
    assert data['from'] == 'claimed'
    board = read_board('/b/rreo', user_id=TEST_OWNER_USER_ID)
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
    assert read_board('/b/raudit', user_id=TEST_OWNER_USER_ID)['open'] == 1
# ════════════════════════════════════════════════════════════════════
#  NC-5: the human board action must be audit-logged. No-op the
#  audit_log('board_post', …) call in the engine → the audit capture is
#  empty → the audit-trail contract test FAILS. Byte-identical restore.
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
#  read_board COUNT PARTITION — the collab-bar / status-pillar counts MUST
#  use the SAME partition as render_board_block / the panel lanes /
#  select_dispatchable, so the top-bar "N open" number can never drift from
#  the panel's "待认领" lane. The bug this pins: a block-cooldown'd epic is
#  stored status='open' (block never changes status) so the naive
#  `out[status] += 1` counted it as OPEN — the top bar said "1 open" while the
#  panel (which partitions it into its Blocked lane) showed 0 to claim. And a
#  LIVE kind='lease' row was counted as 'claimed' though it is a path
#  reservation, not an epic being advanced.
# ════════════════════════════════════════════════════════════════════

def _insert_live_lease(project_path, task_id, path_title):
    """Insert a LIVE path lease (kind='lease', status='claimed', unexpired)
    directly — leases are minted by the path-lease subsystem, not post_task."""
    from lib.timeutil import now_ms
    ts = now_ms()
    seed.seed_board_task(
        task_id, project_path, user_id=1,
        title=path_title, status='claimed',
        owner_conv_id='cHOLDER', lease_expires_at=ts + 30 * 60 * 1000,
        created_by_conv='cHOLDER', kind='lease')
def test_read_board_blocked_epic_not_counted_open():
    """An epic on a LIVE block cooldown (stored status='open' + blocked_until in
    the future) must be counted as 'blocked', NOT 'open' — matching the panel's
    Blocked lane and render_board_block. This is the top-bar-vs-panel drift."""
    from lib.conversations.project_board import (
        block_task, post_task, read_board,
    )
    open_id = post_task('/b/cnt1', 'cA', 'genuinely open epic', user_id=TEST_OWNER_USER_ID)['id']
    blk_id = post_task('/b/cnt1', 'cA', 'gated epic', user_id=TEST_OWNER_USER_ID)['id']
    # Block it — human reason → a live (1h) cooldown, status stays 'open'.
    res = block_task('/b/cnt1', 'cA', blk_id, '[human-gated] waiting on sign-off', user_id=TEST_OWNER_USER_ID)
    assert res['ok'] and res['blocked_until'] > 0
    board = read_board('/b/cnt1', user_id=TEST_OWNER_USER_ID)
    # The gated epic drops OUT of 'open' and into 'blocked'.
    assert board['open'] == 1, 'only the genuinely-open epic counts as open'
    assert board['blocked'] == 1, 'the cooldown epic counts as blocked, not open'
    assert board['claimed'] == 0 and board['done'] == 0
    # Its stored status is still 'open' (block never changes status) — proving
    # the count partition, not a status change, is what fixed the drift.
    blk = [t for t in board['tasks'] if t['id'] == blk_id][0]
    assert blk['status'] == 'open' and int(blk['blocked_until']) > 0


def test_read_board_live_lease_not_counted_claimed():
    """A LIVE path lease (kind='lease', effective status 'claimed') is a
    reservation, not an epic — it must NOT inflate the 'claimed' count (the
    panel renders it in its own Held lane)."""
    from lib.conversations.project_board import (
        claim_task, post_task, read_board,
    )
    ep_id = post_task('/b/cnt2', 'cA', 'a real epic', user_id=TEST_OWNER_USER_ID)['id']
    claim_task('/b/cnt2', 'cOWNER', ep_id, user_id=TEST_OWNER_USER_ID)   # one genuinely-claimed epic
    _insert_live_lease('/b/cnt2', 'pt_lease_x', 'static/styles.css')
    board = read_board('/b/cnt2', user_id=TEST_OWNER_USER_ID)
    assert board['claimed'] == 1, 'only the real claimed epic counts (not the lease)'
    assert board['open'] == 0 and board['done'] == 0 and board['blocked'] == 0
    # The lease row is still present in tasks (readers that partition the list
    # themselves — e.g. the Held lane — must still see it).
    assert any(t['id'] == 'pt_lease_x' and t.get('kind') == 'lease'
               for t in board['tasks']), 'lease row still present in tasks list'
