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

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]
pytest_plugins = ('tests._chat_sidecar',)

TEST_OWNER_USER_ID = 1


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _row(flask_app, project_path, task_id):
    from lib.conversations.project_board import read_board
    return next((task for task in read_board(
        project_path, user_id=TEST_OWNER_USER_ID)['tasks']
        if task['id'] == task_id), None)


def _feed(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        return read_project_feed(project_path, limit=500, user_id=TEST_OWNER_USER_ID)['events']



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
        tid = post_task('/b/1', 'cA', 'epic under external gate', user_id=TEST_OWNER_USER_ID)['id']
        before = _now_ms()
        res = block_task('/b/1', 'cA', tid, '[human-gated] waiting §10 sign-off', user_id=TEST_OWNER_USER_ID)
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
        tid = post_task('/b/2', 'cA', 'perpetually human-gated epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/b/2', 'cA', tid, 'gate 1', user_id=TEST_OWNER_USER_ID)
    row1 = _row(flask_app, '/b/2', tid)
    with flask_app.app_context():
        block_task('/b/2', 'cA', tid, 'gate 2', user_id=TEST_OWNER_USER_ID)
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
        tid = post_task('/b/3', 'cA', 'blocked epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/b/3', 'cA', tid, 'sibling must commit first', user_id=TEST_OWNER_USER_ID)
        cands = [c['id'] for c in select_dispatchable('/b/3', user_id=TEST_OWNER_USER_ID)]
    assert tid not in cands, \
        'a blocked epic on cooldown must NOT be re-dispatched (stops the churn)'


def test_cooldown_self_expires_at_read_time(flask_app, monkeypatch):
    """The ONLY reason this is allowed where park was not: the cooldown expires
    automatically at read time (no reaper, no human un-block). Once the window
    passes, the epic is pickable again so a resolved dep IS retried."""
    from lib.conversations.project_board import block_task, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/4', 'cA', 'temporarily blocked epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/b/4', 'cA', tid, 'waiting on sibling commit', user_id=TEST_OWNER_USER_ID)
    import lib.conversations.project_dispatch as project_dispatch
    monkeypatch.setattr(project_dispatch.time, 'time', lambda: 10_000_000_000)
    with flask_app.app_context():
        cands = [c['id'] for c in select_dispatchable('/b/4', user_id=TEST_OWNER_USER_ID)]
    assert tid in cands, \
        'once the cooldown lapses the epic must be dispatchable again (retry)'


def test_unblocked_epic_still_dispatchable(flask_app):
    """Sanity: the cooldown filter does not over-exclude — a normal open epic
    with no block is still dispatchable."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/5', 'cA', 'fresh open epic', user_id=TEST_OWNER_USER_ID)['id']
        cands = [c['id'] for c in select_dispatchable('/b/5', user_id=TEST_OWNER_USER_ID)]
    assert tid in cands


# ════════════════════════════════════════════════════════════════════
#  Reset on complete + reopen (owner constraint #3)
# ════════════════════════════════════════════════════════════════════

def test_complete_resets_block_state(flask_app):
    from lib.conversations.project_board import (
        block_task, complete_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/b/6', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/b/6', 'cA', tid, 'gate', user_id=TEST_OWNER_USER_ID)
        complete_task('/b/6', 'cA', tid, user_id=TEST_OWNER_USER_ID)
    row = _row(flask_app, '/b/6', tid)
    assert row['block_count'] == 0 and row['blocked_until'] == 0
    assert (row['block_reason'] or '') == ''


def test_reopen_resets_block_state_and_forces_immediate_retry(flask_app):
    from lib.conversations.project_board import block_task, post_task, reopen_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/b/7', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/b/7', 'cA', tid, 'gate', user_id=TEST_OWNER_USER_ID)
        # blocked → not dispatchable
        assert tid not in [c['id'] for c in select_dispatchable('/b/7', user_id=TEST_OWNER_USER_ID)]
        reopen_task('/b/7', 'human', tid, user_id=TEST_OWNER_USER_ID)
        cands = [c['id'] for c in select_dispatchable('/b/7', user_id=TEST_OWNER_USER_ID)]
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
        tid = post_task('/b/8', 'cA', 'Epic D scale-out', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/b/8', 'cA', tid, '[human-gated] §10 infra sign-off required', user_id=TEST_OWNER_USER_ID)
        block = render_board_block('/b/8', current_conv_id='cR', user_id=TEST_OWNER_USER_ID)
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


def test_expired_cooldown_epic_returns_to_open_lane(flask_app, monkeypatch):
    from lib.conversations.project_board import (
        block_task, post_task, render_board_block,
    )
    with flask_app.app_context():
        tid = post_task('/b/9', 'cA', 'transiently blocked epic', user_id=TEST_OWNER_USER_ID)['id']
        block_task('/b/9', 'cA', tid, 'gate', user_id=TEST_OWNER_USER_ID)
    import lib.conversations.project_board as project_board
    monkeypatch.setattr(project_board, '_now_ms', lambda: 10_000_000_000_000)
    with flask_app.app_context():
        block = render_board_block('/b/9', current_conv_id='cR', user_id=TEST_OWNER_USER_ID)
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


def test_nullable_block_fields_read_as_unblocked():
    from lib.storage_sidecar.operations_pkg._board import _board_public
    t = _board_public(_legacy_row(
        project_path='/legacy', blocked_until=None, block_count=None,
        block_reason='', wait_paths='[]', dispatch_target='', write_set='[]',
        block_question='', human_answer='', blocked_by=''), now=1_000_000)
    assert t['blocked_until'] == 0 and t['block_count'] == 0
    assert t['block_reason'] == ''
