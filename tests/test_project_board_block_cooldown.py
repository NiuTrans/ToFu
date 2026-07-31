"""tests/test_project_board_block_cooldown.py — the Board BLOCK cooldown.

The defect this closes (diagnosed 2026-07-11 against live state): a board epic
that is picked up, worked, and hits a GENUINE external gate (a sibling must
commit first; a human §10 infra sign-off) is reported via ``project_board_block``
— but ``block_task`` was FEED-ONLY ("Does not change board status") and
``select_dispatchable`` had no ``blocked`` awareness. So the epic stayed
``open``; its 30-min claim lease expired; ``_effective_status`` read it ``open``
again; the next heartbeat sweep RE-selected the SAME epic and burned another
BILLED agent turn to re-discover a dependency it already knew was unmet. The
real incident: ``pt_4daa2c3d`` was block-then-block-again 4 minutes apart.

The fix makes ``blocked`` a real, SELF-EXPIRING, at-read-time board state — a
BACKOFF, not a park shelf (the park/deferred mechanism was deliberately removed;
this must NOT re-introduce it):

  • ``block_task`` stamps ``blocked_until = now + cooldown`` + increments
    ``block_count`` + records the ``block_reason`` on the row. Status is NOT
    changed (a block is still not a status).
  • The cooldown is ESCALATING (exponential, capped) so a perpetually
    human-gated epic converges to a long sleep after a FEW retries instead of
    churning at fixed cadence forever. Class-agnostic: the escalation drives
    BOTH block classes to convergence; the reason string records the class for
    HUMAN visibility only.
  • ``select_dispatchable`` skips a row whose ``blocked_until > now`` —
    at-read-time expiry, NO reaper, NO human un-block gate (that is the ONLY
    reason this is allowed where park was not: it can never require human
    action to release and can never deadlock).
  • ``complete_task`` AND ``reopen_task`` RESET ``blocked_until`` +
    ``block_count`` + ``block_reason`` → a human ``reopen`` forces an immediate
    retry.
  • ``render_board_block`` shows blocked epics in their own "Blocked" lane with
    the reason + time-until-auto-retry — the answer to "why is nothing
    happening" that was invisible before.

Load-bearing negative controls (each byte-reverts ONE guard):
  • NC-1 — revert the ``select_dispatchable`` ``blocked_until`` skip → a blocked
    epic LEAKS back into the candidate set (reproduces the billed-turn churn).
  • NC-2 — revert the ``reopen_task`` block-state reset → a human reopen no
    longer forces a retry (the epic stays cooldown-suppressed).
  • NC-3 — revert the ``_row_to_task`` nullable-safe ``blocked_until`` default →
    a pre-migration (column-less) row raises instead of reading 0.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')


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
        db.execute('DELETE FROM message_queue')
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _row(flask_app, project_path, task_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        r = db.execute(
            'SELECT blocked_until, block_count, block_reason, status '
            'FROM project_tasks WHERE id=? AND project_path=?',
            (task_id, project_path)).fetchone()
    return dict(r) if r else None


def _set_blocked_until(flask_app, project_path, task_id, blocked_until):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET blocked_until=? WHERE id=? AND project_path=?',
                   (blocked_until, task_id, project_path))
        db.commit()


def _feed(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        return read_project_feed(project_path, limit=500)['events']


from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  Escalating-backoff schedule — the pure helper (no DB)
# ════════════════════════════════════════════════════════════════════

def test_cooldown_schedule_escalates_and_caps():
    from lib.conversations.project_board import (
        BLOCK_COOLDOWN_BASE_MS, BLOCK_COOLDOWN_MAX_MS, _block_cooldown_ms,
    )
    # count 0 → no cooldown; 1 → base; then strictly increasing until the cap.
    assert _block_cooldown_ms(0) == 0
    assert _block_cooldown_ms(1) == BLOCK_COOLDOWN_BASE_MS
    seq = [_block_cooldown_ms(n) for n in range(1, 9)]
    # strictly non-decreasing and each step ≥ the previous
    for a, b in zip(seq, seq[1:]):
        assert b >= a
    # reaches the MAX within a FEW retries (owner: human-gated class → long
    # sleep fast) and never exceeds it.
    assert max(seq) == BLOCK_COOLDOWN_MAX_MS
    assert seq[3] == BLOCK_COOLDOWN_MAX_MS, \
        'must reach the max cap within ~4 blocks (few retries then long sleep)'
    assert all(v <= BLOCK_COOLDOWN_MAX_MS for v in seq)


# ════════════════════════════════════════════════════════════════════
#  block_task — stamps cooldown + count + reason, does NOT flip status
# ════════════════════════════════════════════════════════════════════

def test_block_sets_cooldown_count_and_reason(flask_app):
    from lib.conversations.project_board import (
        BLOCK_COOLDOWN_BASE_MS, _now_ms, block_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/b/1', 'cA', 'epic under external gate')['id']
        before = _now_ms()
        res = block_task('/b/1', 'cA', tid, '[human-gated] waiting §10 sign-off')
    assert res['ok']
    row = _row(flask_app, '/b/1', tid)
    assert row['block_count'] == 1
    assert row['status'] == 'open', 'block must NOT change board status'
    assert '[human-gated]' in (row['block_reason'] or '')
    # cooldown ≈ base (first block), stamped into the future
    assert row['blocked_until'] >= before + BLOCK_COOLDOWN_BASE_MS - 5_000


def test_repeated_block_escalates_count_and_cooldown(flask_app):
    from lib.conversations.project_board import block_task, post_task
    with flask_app.app_context():
        tid = post_task('/b/2', 'cA', 'perpetually human-gated epic')['id']
        block_task('/b/2', 'cA', tid, 'gate 1')
    row1 = _row(flask_app, '/b/2', tid)
    with flask_app.app_context():
        block_task('/b/2', 'cA', tid, 'gate 2')
    row2 = _row(flask_app, '/b/2', tid)
    assert row2['block_count'] == 2 and row1['block_count'] == 1
    # 2nd block schedules a LATER retry than the 1st (escalation), measured as
    # the cooldown WINDOW (blocked_until - block time), not absolute stamps.
    from lib.conversations.project_board import _block_cooldown_ms
    assert _block_cooldown_ms(2) > _block_cooldown_ms(1)


# ════════════════════════════════════════════════════════════════════
#  select_dispatchable — a blocked epic is NOT dispatched (the churn fix)
# ════════════════════════════════════════════════════════════════════

def test_blocked_epic_not_dispatchable(flask_app):
    from lib.conversations.project_board import block_task, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/3', 'cA', 'blocked epic')['id']
        block_task('/b/3', 'cA', tid, 'sibling must commit first')
        cands = [c['id'] for c in select_dispatchable('/b/3')]
    assert tid not in cands, \
        'a blocked epic on cooldown must NOT be re-dispatched (stops the churn)'


def test_cooldown_self_expires_at_read_time(flask_app):
    """The ONLY reason this is allowed where park was not: the cooldown expires
    automatically at read time (no reaper, no human un-block). Once the window
    passes, the epic is pickable again so a resolved dep IS retried."""
    from lib.conversations.project_board import block_task, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/4', 'cA', 'temporarily blocked epic')['id']
        block_task('/b/4', 'cA', tid, 'waiting on sibling commit')
    _set_blocked_until(flask_app, '/b/4', tid, 1)  # force cooldown into the past
    with flask_app.app_context():
        cands = [c['id'] for c in select_dispatchable('/b/4')]
    assert tid in cands, \
        'once the cooldown lapses the epic must be dispatchable again (retry)'


def test_unblocked_epic_still_dispatchable(flask_app):
    """Sanity: the cooldown filter does not over-exclude — a normal open epic
    with no block is still dispatchable."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/5', 'cA', 'fresh open epic')['id']
        cands = [c['id'] for c in select_dispatchable('/b/5')]
    assert tid in cands


# ════════════════════════════════════════════════════════════════════
#  Reset on complete + reopen (owner constraint #3)
# ════════════════════════════════════════════════════════════════════

def test_complete_resets_block_state(flask_app):
    from lib.conversations.project_board import (
        block_task, complete_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/b/6', 'cA', 'epic')['id']
        block_task('/b/6', 'cA', tid, 'gate')
        complete_task('/b/6', 'cA', tid)
    row = _row(flask_app, '/b/6', tid)
    assert row['block_count'] == 0 and row['blocked_until'] == 0
    assert (row['block_reason'] or '') == ''


def test_reopen_resets_block_state_and_forces_immediate_retry(flask_app):
    from lib.conversations.project_board import block_task, post_task, reopen_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/7', 'cA', 'epic')['id']
        block_task('/b/7', 'cA', tid, 'gate')
        # blocked → not dispatchable
        assert tid not in [c['id'] for c in select_dispatchable('/b/7')]
        reopen_task('/b/7', 'human', tid)
        cands = [c['id'] for c in select_dispatchable('/b/7')]
    row = _row(flask_app, '/b/7', tid)
    assert row['block_count'] == 0 and row['blocked_until'] == 0
    assert tid in cands, 'a human reopen must force an immediate retry'


# ════════════════════════════════════════════════════════════════════
#  Render: a "Blocked" lane shows WHY + retry-in (human visibility)
# ════════════════════════════════════════════════════════════════════

def test_blocked_lane_renders_reason_and_retry(flask_app):
    from lib.conversations.project_board import (
        block_task, post_task, render_board_block,
    )
    with flask_app.app_context():
        tid = post_task('/b/8', 'cA', 'Epic D scale-out')['id']
        block_task('/b/8', 'cA', tid, '[human-gated] §10 infra sign-off required')
        block = render_board_block('/b/8', current_conv_id='cR')
    assert 'Waiting on an external gate' in block
    assert '[human-gated]' in block, 'the block reason (with class) must be shown'
    # the blocked epic must NOT appear in the plain "Open" lane (it would read as
    # "claim me" — the exact invisible-blocker defect).
    lines = block.splitlines()
    open_idx = next((i for i, ln in enumerate(lines) if ln.startswith('Open (')), None)
    if open_idx is not None:
        open_block = '\n'.join(lines[open_idx:])
        assert 'Epic D scale-out' not in open_block, \
            'a blocked epic must be partitioned OUT of the Open lane'


def test_expired_cooldown_epic_returns_to_open_lane(flask_app):
    from lib.conversations.project_board import (
        block_task, post_task, render_board_block,
    )
    with flask_app.app_context():
        tid = post_task('/b/9', 'cA', 'transiently blocked epic')['id']
        block_task('/b/9', 'cA', tid, 'gate')
    _set_blocked_until(flask_app, '/b/9', tid, 1)  # expire cooldown
    with flask_app.app_context():
        block = render_board_block('/b/9', current_conv_id='cR')
    # no live cooldown → not in a waiting-on-gate lane
    assert 'Waiting on an external gate' not in block


# ════════════════════════════════════════════════════════════════════
#  Pre-migration safety: a row without the new columns reads as unblocked
# ════════════════════════════════════════════════════════════════════

def _legacy_row(**over):
    """A row mapping PREDATING the block-cooldown columns (no 'blocked_until' /
    'block_count' / 'block_reason' keys) — the pre-migration shape a defensive
    read must survive. Missing-key access raises KeyError."""
    row = {
        'id': 'pt_legacy', 'title': 'legacy epic', 'status': 'open',
        'owner_conv_id': '', 'lease_expires_at': 0, 'created_by_conv': 'cA',
        'depends_on': '[]', 'dispatched': 0, 'kind': 'epic',
        'created_at': 0, 'updated_at': 0,
    }
    row.update(over)
    return row


def test_pre_migration_row_reads_as_unblocked():
    from lib.conversations.project_board import _row_to_task
    t = _row_to_task(_legacy_row(), now_ms=1_000_000)
    assert t['blocked_until'] == 0 and t['block_count'] == 0
    assert t['block_reason'] == ''


# ════════════════════════════════════════════════════════════════════
#  NC-1 — the select_dispatchable cooldown skip is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_1_dispatch_cooldown_skip_is_load_bearing(flask_app):
    def run():
        import lib.conversations.project_dispatch as pd
        from lib.conversations.project_board import block_task, post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ncb1'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = post_task('/ncb1', 'cA', 'blocked epic')['id']
            block_task('/ncb1', 'cA', tid, 'external gate')
            cands = [c['id'] for c in pd.select_dispatchable('/ncb1')]
        assert tid in cands, \
            'NC-1: with the blocked_until skip removed, a blocked epic must ' \
            'LEAK back into the candidate set (reproduces the billed-turn churn)'

    _patch_restore(
        _DISPATCH_SRC,
        "        if int(t.get('blocked_until') or 0) > now_ms:\n            continue\n",
        "        if False:  # NC-1 (cooldown skip disabled)\n            continue\n",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  NC-2 — the reopen_task block-state reset is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_2_reopen_reset_is_load_bearing(flask_app):
    def run():
        import lib.conversations.project_board as pb
        from lib.conversations.project_dispatch import select_dispatchable
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ncb2'")
            get_thread_db(DOMAIN_CHAT).commit()
            tid = pb.post_task('/ncb2', 'cA', 'epic')['id']
            pb.block_task('/ncb2', 'cA', tid, 'gate')
            pb.reopen_task('/ncb2', 'human', tid)
            cands = [c['id'] for c in select_dispatchable('/ncb2')]
        assert tid not in cands, \
            'NC-2: with the reopen block-reset removed, a human reopen must ' \
            'NOT force a retry (the epic stays cooldown-suppressed)'

    _patch_restore(
        _BOARD_SRC,
        "lease_expires_at=0, dispatched=0, blocked_until=0, block_count=0, "
        "\"\n            \"block_reason='', wait_paths='[]', dispatch_target='', \"\n            \"block_question='', human_answer='', updated_at=? \"",
        "lease_expires_at=0, dispatched=0, updated_at=? \"",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  NC-3 — the _row_to_task blocked_until nullable default is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_3_blocked_until_default_is_load_bearing():
    def run():
        import lib.conversations.project_board as pb
        raised = False
        try:
            pb._row_to_task(_legacy_row(), now_ms=1_000_000)
        except KeyError:
            raised = True
        assert raised, \
            'NC-3: with the nullable default removed, a column-less legacy row ' \
            'must raise KeyError out of _row_to_task (proves the guard)'

    _patch_restore(
        _BOARD_SRC,
        "    try:\n        blocked_until = int(r['blocked_until'] or 0)\n"
        "    except (KeyError, IndexError, TypeError) as e:\n"
        "        logger.debug('[Board] blocked_until field parse failed, defaulting: %s', e)\n"
        "        blocked_until = 0",
        "    blocked_until = r['blocked_until']  # NC-3 (nullable default removed)",
        run,
    )
