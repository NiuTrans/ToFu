"""tests/test_project_board_lease.py — the Board resource/path LEASE (kind='lease').

The need this closes: a durational file-avoidance signal ("I'm rewriting
styles.css for the next hour, hold off on CSS edits"). This is NOT a broadcast
message (rejected: ephemeral, active-peers-only, a new storm-class); it is a
STATE — a proactive, path-level RESERVATION posted onto the coordination board
that reaches EVERY sibling on its next prompt assembly, INCLUDING an idle one
the autonomous heartbeat wakes later, via the ambient ``[PROJECT BOARD]`` block.

A lease reuses the SAME ``project_tasks`` row + soft TTL-lease + at-read-time
expiry (``_effective_status``) as an epic claim, but with ``kind='lease'`` it is
(a) EXCLUDED from ``select_dispatchable`` (a reservation is never auto-dispatched
as work) and (b) rendered in its own "Held" section, partitioned out of the epic
lanes. It is complementary to — not a duplicate of —
``lib.presence.conflict.detect_overlaps`` (the reactive, active-peers-only,
file-level overlap REPORT): the lease PREVENTS the collision the detector would
otherwise later report, and re-derives nothing the detector computes (it reads
``project_tasks`` rows, not the presence snapshot).

Load-bearing triple-neuter, each byte-reverting a single guard:
  • NC-1 — revert the ``select_dispatchable`` ``kind == 'lease'`` skip → an
    EXPIRED lease reclaims claimed→open and LEAKS into the candidate set (the
    exact defect: the heartbeat sweep + _drain_idle_target would spawn a
    spurious BILLED kickoff at TTL expiry). Proves the filter is load-bearing.
  • NC-2 — drop the "Held" render branch → a live lease is INVISIBLE to
    siblings (the whole point — a held path a sibling can't see does nothing).
  • NC-3 — revert the ``kind`` nullable-default handling in ``_row_to_task`` so
    a pre-migration row (kind absent/NULL) raises instead of defaulting to
    'epic' → an old ``epic`` row misreads and drops off the dispatch board.
    Proves the migration/default is safe on pre-existing rows.
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


def _feed(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        return read_project_feed(project_path, limit=500)['events']


def _set_lease(flask_app, project_path, task_id, lease_ms):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET lease_expires_at=? WHERE id=? AND project_path=?',
                   (lease_ms, task_id, project_path))
        db.commit()


def _patch_restore(path, old, new, run):
    """Byte-revert a guard, run the neutered assertion, restore byte-identical."""
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


# ════════════════════════════════════════════════════════════════════
#  claim_lease / release_lease — behaviour
# ════════════════════════════════════════════════════════════════════

def test_claim_lease_creates_held_row(flask_app):
    from lib.conversations.project_board import claim_lease, read_board
    with flask_app.app_context():
        res = claim_lease('/l/1', 'cA', 'static/styles.css')
        board = read_board('/l/1')
    assert res['ok'] and res['id'].startswith('pt_')
    t = board['tasks'][0]
    assert t['kind'] == 'lease' and t['status'] == 'claimed'
    assert t['owner_conv_id'] == 'cA' and t['title'] == 'static/styles.css'
    # emits an observable feed note
    assert any(e['kind'] == 'note' and e.get('payload', {}).get('lease')
               for e in _feed(flask_app, '/l/1'))


def test_reclaim_same_conv_refreshes_lease(flask_app):
    from lib.conversations.project_board import claim_lease, read_board
    with flask_app.app_context():
        r1 = claim_lease('/l/2', 'cA', 'lib/x.py', ttl_ms=60_000)
        r2 = claim_lease('/l/2', 'cA', 'lib/x.py', ttl_ms=600_000)
        board = read_board('/l/2')
    # same row refreshed (one lease, not two), later expiry
    assert r1['id'] == r2['id']
    assert len(board['tasks']) == 1
    assert r2['lease_expires_at'] > r1['lease_expires_at']


def test_different_conv_live_lease_is_advisory_refusal(flask_app):
    from lib.conversations.project_board import claim_lease
    with flask_app.app_context():
        claim_lease('/l/3', 'cA', 'lib/x.py')
        res = claim_lease('/l/3', 'cB', 'lib/x.py')
    assert res['ok'] is False and res['error'] == 'already_held'
    assert res['owner'] == 'cA'


def test_expired_lease_can_be_reclaimed_by_other(flask_app):
    from lib.conversations.project_board import claim_lease, read_board
    with flask_app.app_context():
        r = claim_lease('/l/4', 'cA', 'lib/x.py')
    _set_lease(flask_app, '/l/4', r['id'], 1)  # force-expire
    with flask_app.app_context():
        res = claim_lease('/l/4', 'cB', 'lib/x.py')
        board = read_board('/l/4')
    assert res['ok'], 'an expired lease must be reclaimable by another conv'
    assert board['tasks'][0]['owner_conv_id'] == 'cB'
    assert len([t for t in board['tasks'] if t['kind'] == 'lease']) == 1


def test_release_by_holder(flask_app):
    from lib.conversations.project_board import claim_lease, read_board, release_lease
    with flask_app.app_context():
        claim_lease('/l/5', 'cA', 'lib/x.py')
        res = release_lease('/l/5', 'cA', 'lib/x.py')
        board = read_board('/l/5')
    assert res['ok']
    assert not board['tasks'], 'a released lease row is deleted'
    assert any(e.get('payload', {}).get('released') for e in _feed(flask_app, '/l/5'))


def test_release_by_non_holder_refused(flask_app):
    from lib.conversations.project_board import claim_lease, read_board, release_lease
    with flask_app.app_context():
        claim_lease('/l/6', 'cA', 'lib/x.py')
        res = release_lease('/l/6', 'cB', 'lib/x.py')
        board = read_board('/l/6')
    assert res['ok'] is False and res['error'] == 'held_by_other'
    assert board['tasks'], 'a non-holder release must NOT delete the lease'


def test_release_missing_lease(flask_app):
    from lib.conversations.project_board import release_lease
    with flask_app.app_context():
        res = release_lease('/l/7', 'cA', 'nope.py')
    assert res['ok'] is False and res['error'] == 'no such lease'


# ════════════════════════════════════════════════════════════════════
#  Render: leases go in a "Held" lane, NOT the epic lanes
# ════════════════════════════════════════════════════════════════════

def test_held_lane_renders_and_partitions_from_epics(flask_app):
    from lib.conversations.project_board import (
        claim_lease, post_task, render_board_block,
    )
    with flask_app.app_context():
        post_task('/l/8', 'cA', 'Refactor the parser')  # an open epic
        claim_lease('/l/8', 'cB', 'static/styles.css')  # a held path
        block = render_board_block('/l/8', current_conv_id='cREADER')
    assert 'Held (do NOT edit' in block
    assert 'static/styles.css' in block
    # the lease MUST NOT appear as an epic in any epic lane
    assert 'Open (unclaimed' in block and 'Refactor the parser' in block
    # the held path is not listed under the open/claimed epic lanes
    held_line = [ln for ln in block.splitlines() if 'static/styles.css' in ln][0]
    assert 'held by cB' in held_line


def test_expired_lease_not_rendered_as_held(flask_app):
    from lib.conversations.project_board import claim_lease, render_board_block
    with flask_app.app_context():
        r = claim_lease('/l/9', 'cA', 'lib/x.py')
    _set_lease(flask_app, '/l/9', r['id'], 1)  # expire it
    with flask_app.app_context():
        block = render_board_block('/l/9', current_conv_id='cR')
    # An expired lease holds nothing → not rendered (and no epic lanes exist).
    assert 'Held (do NOT edit' not in block


# ════════════════════════════════════════════════════════════════════
#  Dispatch: a lease is NEVER a work candidate (open OR expired)
# ════════════════════════════════════════════════════════════════════

def test_live_lease_not_dispatchable(flask_app):
    from lib.conversations.project_board import claim_lease
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        r = claim_lease('/l/10', 'cA', 'lib/x.py')
        cands = [c['id'] for c in select_dispatchable('/l/10')]
    assert r['id'] not in cands


def test_expired_lease_not_dispatchable(flask_app):
    """The core defect guard: an EXPIRED lease reads open via _effective_status
    but MUST still be excluded from dispatch (else the sweep spawns a spurious
    billed kickoff at TTL expiry)."""
    from lib.conversations.project_board import claim_lease
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        r = claim_lease('/l/11', 'cA', 'lib/x.py')
    _set_lease(flask_app, '/l/11', r['id'], 1)  # force-expire → reads 'open'
    with flask_app.app_context():
        cands = [c['id'] for c in select_dispatchable('/l/11')]
    assert r['id'] not in cands, \
        'an EXPIRED lease must NEVER be dispatched as work'


def test_epic_still_dispatchable_alongside_lease(flask_app):
    """Sanity: the lease filter does not over-exclude — a normal open epic in
    the same project is still dispatchable."""
    from lib.conversations.project_board import claim_lease, post_task
    from lib.conversations.project_dispatch import select_dispatchable
    with flask_app.app_context():
        tid = post_task('/l/12', 'cA', 'real epic')['id']
        claim_lease('/l/12', 'cB', 'lib/x.py')
        cands = [c['id'] for c in select_dispatchable('/l/12')]
    assert tid in cands


# ════════════════════════════════════════════════════════════════════
#  NC-1 — select_dispatchable lease-skip is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_1_lease_dispatch_skip_is_load_bearing(flask_app):
    """Byte-revert the ``kind == 'lease'`` skip in select_dispatchable → an
    EXPIRED lease reads open and LEAKS into the candidate set (the exact defect
    that would spawn a spurious billed kickoff)."""
    import importlib

    def run():
        import lib.conversations.project_dispatch as pd
        importlib.reload(pd)
        from lib.conversations.project_board import claim_lease
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/nc1'")
            get_thread_db(DOMAIN_CHAT).commit()
            r = claim_lease('/nc1', 'cA', 'lib/x.py')
            get_thread_db(DOMAIN_CHAT).execute(
                'UPDATE project_tasks SET lease_expires_at=1 WHERE id=?', (r['id'],))
            get_thread_db(DOMAIN_CHAT).commit()
            cands = [c['id'] for c in pd.select_dispatchable('/nc1')]
        assert r['id'] in cands, \
            'NC-1: with the lease-skip removed, an expired lease must LEAK ' \
            'into the dispatch candidate set (reproduces the billed-kickoff defect)'

    _patch_restore(
        _DISPATCH_SRC,
        "        if t.get('kind') == 'lease':\n            continue\n",
        "        if False:  # NC-1 (lease-skip disabled)\n            continue\n",
        run,
    )
    import lib.conversations.project_dispatch as pd
    importlib.reload(pd)


# ════════════════════════════════════════════════════════════════════
#  NC-2 — the "Held" render branch is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_2_held_render_branch_is_load_bearing(flask_app):
    """Byte-neuter the held-lane render (force held_t empty) → a live lease is
    INVISIBLE to siblings (a held path a sibling can't see coordinates nothing)."""
    import importlib

    def run():
        import lib.conversations.project_board as pb
        importlib.reload(pb)
        with flask_app.app_context():
            from lib.database import DOMAIN_CHAT, get_thread_db
            get_thread_db(DOMAIN_CHAT).execute(
                "DELETE FROM project_tasks WHERE project_path='/nc2'")
            get_thread_db(DOMAIN_CHAT).commit()
            pb.claim_lease('/nc2', 'cA', 'static/styles.css')
            block = pb.render_board_block('/nc2', current_conv_id='cR')
        assert 'static/styles.css' not in block, \
            'NC-2: with the Held branch neutered, the held path must be ' \
            'invisible to siblings'

    _patch_restore(
        _BOARD_SRC,
        "    held_t = [t for t in tasks if t.get('kind') == 'lease' and t['status'] == 'claimed']",
        "    held_t = []  # NC-2 (held-lane render disabled)",
        run,
    )
    import lib.conversations.project_board as pb
    importlib.reload(pb)


# ════════════════════════════════════════════════════════════════════
#  NC-3 — the kind nullable-default in _row_to_task is load-bearing
#  (migration safety on pre-existing rows)
# ════════════════════════════════════════════════════════════════════

def _legacy_row(**over):
    """A row mapping that PREDATES the kind column — i.e. it has no 'kind' key
    at all (the real pre-migration shape a defensive read must survive; a true
    NULL is impossible because the column is NOT NULL DEFAULT 'epic', which is
    the schema-level backfill guard). Missing-key access raises KeyError, the
    exact exception the _row_to_task guard catches."""
    row = {
        'id': 'pt_legacy', 'title': 'legacy epic', 'status': 'open',
        'owner_conv_id': '', 'lease_expires_at': 0, 'created_by_conv': 'cA',
        'depends_on': '[]', 'dispatched': 0, 'created_at': 0, 'updated_at': 0,
    }
    row.update(over)
    return row


def test_pre_migration_row_reads_as_epic():
    """A row that predates the kind column (no 'kind' key) must read as 'epic'
    — never crash, never silently misclassify — so a legacy epic stays a
    dispatchable epic. Exercises the _row_to_task nullable-safe guard directly."""
    from lib.conversations.project_board import _row_to_task
    t = _row_to_task(_legacy_row(), now_ms=1_000_000)
    assert t['kind'] == 'epic', 'a pre-migration (kind-less) row must read as an epic'
    assert t['status'] == 'open'


def test_NC_3_kind_default_is_load_bearing():
    """Byte-revert the nullable-safe kind default in _row_to_task so a
    kind-less (pre-migration) row is read via a bare ``r['kind']`` → KeyError
    propagates out of _row_to_task. Proves the try/except default is what keeps
    pre-existing rows from crashing the board read."""
    import importlib

    def run():
        import lib.conversations.project_board as pb
        importlib.reload(pb)
        raised = False
        try:
            pb._row_to_task(_legacy_row(), now_ms=1_000_000)
        except KeyError:
            raised = True
        assert raised, \
            'NC-3: with the nullable default removed, a kind-less legacy row ' \
            'must raise KeyError out of _row_to_task (proves the guard)'

    _patch_restore(
        _BOARD_SRC,
        "    try:\n        kind = r['kind'] or 'epic'\n    except (KeyError, IndexError, TypeError):\n        kind = 'epic'",
        "    kind = r['kind']  # NC-3 (nullable default removed)",
        run,
    )
    import lib.conversations.project_board as pb
    importlib.reload(pb)


# ════════════════════════════════════════════════════════════════════
#  Tool surface + agent reachability
# ════════════════════════════════════════════════════════════════════

def test_lease_tools_in_schema_and_name_set():
    from lib.tools import BOARD_TOOLS, BOARD_TOOL_NAMES
    names = [t['function']['name'] for t in BOARD_TOOLS]
    assert 'project_claim_path' in names and 'project_release_path' in names
    assert 'project_claim_path' in BOARD_TOOL_NAMES
    assert 'project_release_path' in BOARD_TOOL_NAMES


def test_registry_routes_lease_tools_to_board_handler():
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers.misc import _handle_board_tool
    assert tool_registry.lookup('project_claim_path', {}) is _handle_board_tool
    assert tool_registry.lookup('project_release_path', {}) is _handle_board_tool


def test_execute_board_tool_claim_and_release(flask_app):
    from lib.conversations.project_board import execute_board_tool, read_board
    with flask_app.app_context():
        out = execute_board_tool(
            'project_claim_path', {'resource': 'static/styles.css'},
            current_conv_id='cA', project_path='/l/14')
        board = read_board('/l/14')
    assert 'held' in out.lower()
    assert board['tasks'][0]['kind'] == 'lease'
    with flask_app.app_context():
        out2 = execute_board_tool(
            'project_release_path', {'resource': 'static/styles.css'},
            current_conv_id='cA', project_path='/l/14')
        board2 = read_board('/l/14')
    assert 'released' in out2.lower()
    assert not board2['tasks']
