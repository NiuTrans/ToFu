"""tests/test_project_write_set.py — worktree isolation §4: dispatch-time
write-set partitioning.

Each epic may declare a ``write_set`` — the paths/globs/subsystem-tags it
intends to WRITE. ``select_dispatchable`` PREFERS a candidate whose write_set is
DISJOINT from every LIVE-CLAIMED epic's write_set, shifting collision detection
LEFT from land-time to dispatch-time so two conversations aren't handed epics
that will fight over the same files.

Load-bearing properties pinned here:
  * write_set round-trips through post_task / set_write_set / read_board
    (nullable-safe: an undeclared epic reads []).
  * SOFT preference, not a hard filter: a conflicting epic is still a candidate
    (never dropped) — just ordered AFTER a disjoint one.
  * fail-open: an empty/undeclared write_set ("unknown footprint") never
    conflicts, so it is never demoted.
  * directory-containment overlap (lib/ vs lib/x.py) counts as a conflict.

NC-WS: no-op the disjoint-first reorder → a conflicting epic is NO LONGER
demoted below a disjoint one → the ordering assertion fails.
"""
from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/a.py', 'lib/b.py', 'lib/other.py', 'lib/shared.py', 'lib/x.py',
    'static/a.js', 'static/js/y.js',
}

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
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
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _claim_live(project_path, conv_id, task_id):
    from lib.conversations.project_board import claim_task
    claim_task(project_path, conv_id, task_id)


# ─────────────────────────────────────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────────────────────────────────────

def test_post_task_persists_write_set(flask_app):
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/ws/post', 'cA', 'epic',
                        write_set=['lib/x.py', 'static/js/y.js'])['id']
        board = read_board('/ws/post')
    row = [t for t in board['tasks'] if t['id'] == tid][0]
    assert row['write_set'] == ['lib/x.py', 'static/js/y.js']


def test_undeclared_write_set_reads_empty(flask_app):
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/ws/none', 'cA', 'epic')['id']
        board = read_board('/ws/none')
    row = [t for t in board['tasks'] if t['id'] == tid][0]
    assert row['write_set'] == []


def test_set_write_set_updates(flask_app):
    from lib.conversations.project_board import post_task, read_board, set_write_set
    with flask_app.app_context():
        tid = post_task('/ws/set', 'cA', 'epic')['id']
        res = set_write_set('/ws/set', 'cA', tid, ['lib/a.py', 'lib/a.py', ' '])
        board = read_board('/ws/set')
    assert res['ok'] and res['write_set'] == ['lib/a.py']  # de-duped, trimmed
    row = [t for t in board['tasks'] if t['id'] == tid][0]
    assert row['write_set'] == ['lib/a.py']


# ─────────────────────────────────────────────────────────────────────────
#  Disjoint-first preference in select_dispatchable
# ─────────────────────────────────────────────────────────────────────────

def test_disjoint_epic_preferred_over_conflicting(flask_app):
    """A live-claimed epic writes lib/shared.py. Of two open candidates, the
    one that ALSO writes lib/shared.py is ordered AFTER the disjoint one."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        claimed = post_task('/ws/pref', 'cA', 'claimed work',
                            write_set=['lib/shared.py'])['id']
        _claim_live('/ws/pref', 'cA', claimed)
        conflicting = post_task('/ws/pref', 'cB', 'conflicts',
                                write_set=['lib/shared.py'])['id']
        disjoint = post_task('/ws/pref', 'cC', 'disjoint',
                             write_set=['lib/other.py'])['id']
        cands = [c['id'] for c in select_dispatchable('/ws/pref')]
    # both are candidates (soft preference), disjoint one FIRST
    assert set(cands) == {conflicting, disjoint}
    assert cands.index(disjoint) < cands.index(conflicting), \
        'disjoint epic must be preferred (ordered first) over the conflicting one'


def test_conflicting_epic_not_dropped(flask_app):
    """SOFT preference: even when EVERY candidate conflicts with a claimed
    write_set, they all remain dispatchable (never a hard filter → no stall)."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        claimed = post_task('/ws/soft', 'cA', 'claimed',
                            write_set=['lib/shared.py'])['id']
        _claim_live('/ws/soft', 'cA', claimed)
        e1 = post_task('/ws/soft', 'cB', 'c1', write_set=['lib/shared.py'])['id']
        cands = [c['id'] for c in select_dispatchable('/ws/soft')]
    assert e1 in cands, 'a conflicting epic must still be dispatchable (soft, not hard)'


def test_empty_write_set_never_demoted(flask_app):
    """fail-open: an undeclared write_set (unknown footprint) is non-conflicting
    and must not be demoted below a declared-disjoint one — order stable."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        claimed = post_task('/ws/open', 'cA', 'claimed',
                            write_set=['lib/shared.py'])['id']
        _claim_live('/ws/open', 'cA', claimed)
        undeclared = post_task('/ws/open', 'cB', 'undeclared')['id']  # no write_set
        cands = [c['id'] for c in select_dispatchable('/ws/open')]
    assert undeclared in cands
    # undeclared is treated as non-conflicting → in the disjoint group (index 0)
    assert cands[0] == undeclared


def test_directory_containment_counts_as_conflict(flask_app):
    """lib/ (a directory prefix) overlaps lib/x.py → conflict → demoted."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        claimed = post_task('/ws/dir', 'cA', 'claimed', write_set=['lib/'])['id']
        _claim_live('/ws/dir', 'cA', claimed)
        nested = post_task('/ws/dir', 'cB', 'nested', write_set=['lib/x.py'])['id']
        disjoint = post_task('/ws/dir', 'cC', 'disjoint', write_set=['static/a.js'])['id']
        cands = [c['id'] for c in select_dispatchable('/ws/dir')]
    assert cands.index(disjoint) < cands.index(nested), \
        'lib/ must be recognized as containing lib/x.py (directory-prefix conflict)'


def test_no_claimed_write_sets_leaves_order_untouched(flask_app):
    """When no live-claimed epic declares a write_set, the reorder is a no-op
    (nothing to partition against) — all candidates present."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        a = post_task('/ws/noop', 'cA', 'a', write_set=['lib/a.py'])['id']
        b = post_task('/ws/noop', 'cB', 'b', write_set=['lib/b.py'])['id']
        cands = [c['id'] for c in select_dispatchable('/ws/noop')]
    assert set(cands) == {a, b}


# ─────────────────────────────────────────────────────────────────────────
#  Negative control
# ─────────────────────────────────────────────────────────────────────────

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_WS_no_reorder_leaves_conflicting_first(flask_app):
    """NC-WS: no-op the disjoint-first reorder → the conflicting epic is no
    longer demoted, so (posted first) it stays ahead of the disjoint one."""
    def run():
        import lib.conversations.project_dispatch as pd
        from lib.conversations.project_board import claim_task, post_task
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/ws/nc'")
            get_thread_db(DOMAIN_CHAT).commit()
            claimed = post_task('/ws/nc', 'cA', 'claimed',
                                write_set=['lib/shared.py'])['id']
            claim_task('/ws/nc', 'cA', claimed)
            # conflicting posted FIRST → without the reorder it stays ahead.
            conflicting = post_task('/ws/nc', 'cB', 'conflicts',
                                    write_set=['lib/shared.py'])['id']
            disjoint = post_task('/ws/nc', 'cC', 'disjoint',
                                 write_set=['lib/other.py'])['id']
            cands = [c['id'] for c in pd.select_dispatchable('/ws/nc')]
        assert cands.index(conflicting) < cands.index(disjoint), \
            'NC-WS: with the reorder disabled the conflicting epic is not demoted'

    _patch_restore(
        _DISPATCH_SRC,
        "        candidates.sort(key=_demote_key)",
        "        pass  # NC-WS (disjoint-first reorder disabled)",
        run,
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
