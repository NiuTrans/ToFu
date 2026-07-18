"""tests/test_project_board_class_aware_backoff.py — Tier-2 bleeding-control.

Guards the CLASS-AWARE block backoff on the shared-tree board model: a transient
``[sibling]`` block (auto-resolves the instant a sibling commits, visible
immediately on the shared checkout) must use a FLAT cooldown
(``SIBLING_BLOCK_COOLDOWN_MS`` == ``DEFAULT_LEASE_TTL_MS``, NO escalation), while
a ``[human-gated]`` / untagged block rides the exponential curve. Without the
flat class the transient block would ratchet an epic toward a 24 h sleep on
ordinary collaboration churn.

NOTE: the former event-driven wait-on-path wake tests were removed together with
the orphaned wait-on-path/lease apparatus (the path-lease agent tools were
deleted 2026-07-13, so no ``kind='lease'`` row is ever created and the wake path
was permanently dead). The flat cooldown is now the sole ``[sibling]`` throttle;
it converges because a sibling's commit is visible immediately on the single
shared checkout.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
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


# ════════════════════════════════════════════════════════════════════
#  (a) _block_cooldown_ms — the class-aware pure helper
# ════════════════════════════════════════════════════════════════════

def test_cooldown_ms_class_param_defaults_to_human():
    """Backward compat: the single-arg call keeps the exponential human curve."""
    from lib.conversations.project_board import (
        BLOCK_COOLDOWN_BASE_MS, _block_cooldown_ms,
    )
    assert _block_cooldown_ms(0) == 0
    assert _block_cooldown_ms(1) == BLOCK_COOLDOWN_BASE_MS
    assert _block_cooldown_ms(1) == _block_cooldown_ms(1, 'human')


def test_sibling_cooldown_is_flat_lease_clock():
    """A [sibling] block tracks the lease clock: flat == DEFAULT_LEASE_TTL_MS,
    NO escalation regardless of block_count."""
    from lib.conversations.project_board import (
        DEFAULT_LEASE_TTL_MS, SIBLING_BLOCK_COOLDOWN_MS, _block_cooldown_ms,
    )
    assert SIBLING_BLOCK_COOLDOWN_MS == DEFAULT_LEASE_TTL_MS
    sib = [_block_cooldown_ms(n, 'sibling') for n in range(1, 9)]
    assert all(v == SIBLING_BLOCK_COOLDOWN_MS for v in sib), \
        'sibling cooldown must be flat (no escalation)'


def test_human_curve_still_escalates_and_dominates_sibling():
    """Regression: the human curve is unchanged AND, past the first block,
    escalates strictly above the flat sibling clock — proof the sibling class
    can never ratchet toward the 24 h cap."""
    from lib.conversations.project_board import _block_cooldown_ms
    human = [_block_cooldown_ms(n, 'human') for n in range(1, 6)]
    for a, b in zip(human, human[1:]):
        assert b >= a
    # by the 2nd block the human curve exceeds the flat sibling clock
    assert _block_cooldown_ms(2, 'human') > _block_cooldown_ms(2, 'sibling')


# ════════════════════════════════════════════════════════════════════
#  (a) block_task — class derived from the [sibling] tag
# ════════════════════════════════════════════════════════════════════

def test_sibling_block_does_not_escalate(flask_app):
    from lib.conversations.project_board import (
        SIBLING_BLOCK_COOLDOWN_MS, _now_ms, block_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/caw/1', 'cA', 'epic blocked by a sibling')['id']
        block_task('/caw/1', 'cA', tid, '[sibling] path=lib/x.py waiting on commit')
        before2 = _now_ms()
        block_task('/caw/1', 'cA', tid, '[sibling] path=lib/x.py still waiting')
    row = _row(flask_app, '/caw/1', tid)
    # 2nd sibling block still schedules a retry ~one lease clock out, NOT escalated
    assert row['block_count'] == 2
    assert row['blocked_until'] <= before2 + SIBLING_BLOCK_COOLDOWN_MS + 5_000, \
        'a repeated [sibling] block must NOT escalate beyond the lease clock'


def test_human_gated_block_still_escalates(flask_app):
    from lib.conversations.project_board import (
        SIBLING_BLOCK_COOLDOWN_MS, _now_ms, block_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/caw/2', 'cA', 'human-gated epic')['id']
        block_task('/caw/2', 'cA', tid, '[human-gated] §10 sign-off 1')
        before2 = _now_ms()
        block_task('/caw/2', 'cA', tid, '[human-gated] §10 sign-off 2')
    row = _row(flask_app, '/caw/2', tid)
    assert row['block_count'] == 2
    # the 2nd human block escalates well past the flat sibling clock
    assert row['blocked_until'] > before2 + SIBLING_BLOCK_COOLDOWN_MS, \
        'a [human-gated] block must still escalate exponentially'


def test_untagged_block_treated_as_human(flask_app):
    """Conservative default: an untagged reason uses the human curve (a genuine
    unknown gate should escalate, not be treated as transient)."""
    from lib.conversations.project_board import (
        SIBLING_BLOCK_COOLDOWN_MS, _now_ms, block_task, post_task,
    )
    with flask_app.app_context():
        tid = post_task('/caw/3', 'cA', 'untagged block')['id']
        block_task('/caw/3', 'cA', tid, 'gate 1')
        before2 = _now_ms()
        block_task('/caw/3', 'cA', tid, 'gate 2')
    row = _row(flask_app, '/caw/3', tid)
    assert row['blocked_until'] > before2 + SIBLING_BLOCK_COOLDOWN_MS
