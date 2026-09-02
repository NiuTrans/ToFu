"""tests/test_project_board_migration.py — idle-sibling epic migration.

Dispatch always routes an epic to ``created_by_conv``. If that originator is
genuinely UNABLE to run it (conv deleted, kickoff repeatedly fails to spawn,
abandoned), the epic + its undrained kickoff are re-attempted on the same dead
conv forever — it can never move to an idle sibling that COULD do it. This adds
a mutable ``dispatch_target`` (routing) ALONGSIDE the immutable
``created_by_conv`` (authorship), and migrates a stuck epic to a genuinely-idle
sibling.

Design doc: docs/modules/conversations_project_brain.md.

THIS suite covers the MECHANISM ONLY (owner asked to review before sweep
wiring): the dispatch-target routing helper, ``_originator_stuck`` detection
(no new timer — reuses the queued-kickoff age vs the lease TTL), the
idle-sibling target picker, and the ``migrate_epic`` act. It does NOT assert the
``sweep_dispatch`` integration — that lands after the owner sees the mechanism.

Owner invariants under test:
  • Provenance (``created_by_conv``) is NEVER overwritten.
  • Stuck = NO live task AND kickoff undrained past the lease TTL (reused
    clock). A merely-busy originator is NOT stuck.
  • An epic on a live cooldown / live wait-on-path is NOT stuck (compose).
  • Never migrate INTO a busy / queued / absent target.
  • Bounded + audited; dispatch_target resets on complete/reopen.

NCs (load-bearing):
  • NC-age: revert the age>lease-TTL gate → a FRESH kickoff wrongly reads stuck.
  • NC-target-busy: revert the target busy-guard → a busy sibling is picked
    (moving the strand).
"""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/x.py'}

import os
import uuid

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_BOARD_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_board.py')
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(chat_sidecar):
    # The in-memory task registry is process-global — a drain-spawned REAL
    # task from an earlier test (sweep/reconcile dispatch a genuine worker
    # that retries dispatch_stream) lingers 'running' into the NEXT test and
    # flips _conv_has_live_task, which the neutered-module NCs consult with
    # the REAL implementation (the exec overwrites the monkeypatched seam).
    # That made NC-age red only when run after reconcile/sweep tests.
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        tasks.clear()
    yield


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_live_tasks(monkeypatch):
    """Default: no conv has a live task (override per-test)."""
    monkeypatch.setattr('lib.conversations.project_dispatch._conv_has_live_task',
                        lambda *_a, **_k: False)
    monkeypatch.setattr('lib.conversations.project_dispatch._drain_idle_target',
                        lambda *_a, **_k: None)


def _mk_conv(flask_app, conv_id, project_path):
    """Create a real conversation row bound to project_path (so the target
    picker + existence guard see it)."""
    del flask_app
    import time
    from tests._seed import delete_conversation, seed_conversation
    delete_conversation(conv_id, user_id=TEST_OWNER_USER_ID)
    seed_conversation(
        conv_id, user_id=TEST_OWNER_USER_ID, title='c',
        settings={'projectPath': project_path},
        created_at=int(time.time() * 1000))


def _queue_kickoff(flask_app, conv_id, task_id, *, created_at):
    """Enqueue a KIND_WORKFLOW kickoff row with an explicit created_at (ms)."""
    del flask_app
    from lib.storage import get_storage_client
    from lib.message_queue import KIND_WORKFLOW
    queue_id = uuid.uuid4().hex
    return get_storage_client(write=True).command(
        'queue.enqueue', {
            'user_id': TEST_OWNER_USER_ID, 'conv_id': conv_id,
            'queue_id': queue_id,
            'message': {
                'text': 'kick', '_brainDispatch': True,
                'boardTaskId': task_id,
            },
            'config': {'projectPath': _conversation_project_path(conv_id)},
            'kind': KIND_WORKFLOW, 'priority': 50,
            'created_at_ms': int(created_at),
        }, queue_id)


def _conversation_project_path(conv_id):
    from tests._seed import conv_document
    document = conv_document(conv_id, user_id=TEST_OWNER_USER_ID) or {}
    return str(
        ((document.get('metadata') or {}).get('settings') or {}).get(
            'projectPath') or '')


def _expire_board_task(project_path, task_id):
    """Re-seed one claimed task with an expired lease via storage.v1."""
    from lib.conversations.project_board import read_board
    from lib.storage import get_storage_client
    from tests._seed import seed_board_task
    task = next(
        item for item in read_board(
            project_path, user_id=TEST_OWNER_USER_ID)['tasks']
        if item['id'] == task_id)
    result = get_storage_client(write=True).command(
        'board.mutate', {
            'action': 'delete', 'project_path': project_path,
            'user_id': TEST_OWNER_USER_ID, 'task_id': task_id,
        }, f'expire-migration-fixture:{task_id}')
    assert result.get('ok'), result
    seed_board_task(
        task_id, project_path, user_id=TEST_OWNER_USER_ID,
        title=task['title'], status='claimed',
        owner_conv_id=task.get('owner_conv_id') or '',
        lease_expires_at=1,
        created_by_conv=task.get('created_by_conv') or '',
        depends_on=task.get('depends_on') or [],
        kind=task.get('kind') or '', dispatched=1,
        dispatch_target=task.get('dispatch_target') or '',
        write_set=task.get('write_set') or [])


def _reset_fixture(project_path, *conv_ids):
    from lib.message_queue import clear_queue
    from tests._seed import clear_board, delete_conversation
    clear_board(project_path, user_id=TEST_OWNER_USER_ID)
    for conv_id in conv_ids:
        clear_queue(conv_id, user_id=TEST_OWNER_USER_ID)
        delete_conversation(conv_id, user_id=TEST_OWNER_USER_ID)


# ════════════════════════════════════════════════════════════════════
#  schema + routing helper
# ════════════════════════════════════════════════════════════════════

def test_row_exposes_dispatch_target(flask_app):
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/m/1', 'cA', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        board = read_board('/m/1', user_id=TEST_OWNER_USER_ID)
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['dispatch_target'] == ''


def test_dispatch_target_routes_to_override_then_origin():
    from lib.conversations.project_dispatch import _dispatch_target
    assert _dispatch_target({'created_by_conv': 'cA', 'dispatch_target': ''}) == 'cA'
    assert _dispatch_target({'created_by_conv': 'cA', 'dispatch_target': 'cB'}) == 'cB'
    # missing keys → ''
    assert _dispatch_target({}) == ''


# ════════════════════════════════════════════════════════════════════
#  _originator_stuck — the no-new-timer detection
# ════════════════════════════════════════════════════════════════════

def _epic(**over):
    e = {'id': 'pt_e', 'created_by_conv': 'cA', 'dispatch_target': '',
         'status': 'open', 'blocked_until': 0, 'wait_paths': []}
    e.update(over)
    return e


def test_stuck_true_when_kickoff_older_than_lease_ttl(flask_app):
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/2')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
    with flask_app.app_context():
        stuck = _originator_stuck('/m/2', _epic(created_by_conv='mig-orig'), [], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is True


def test_stuck_false_when_kickoff_fresh(flask_app):
    """A kickoff younger than the lease TTL = a healthy conv that just hasn't
    drained yet (or is mid-sweep). NOT stuck."""
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/3')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - 5_000)
    with flask_app.app_context():
        stuck = _originator_stuck('/m/3', _epic(created_by_conv='mig-orig'), [], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is False


def test_stuck_false_when_no_kickoff_queued(flask_app):
    """No queued kickoff at all → nothing to migrate (not stuck)."""
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/4')
    with flask_app.app_context():
        stuck = _originator_stuck('/m/4', _epic(created_by_conv='mig-orig'), [], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is False


def test_stuck_false_when_originator_busy(flask_app, monkeypatch):
    """A busy originator is WORKING, not stuck — never migrate it."""
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/5')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
    monkeypatch.setattr('lib.conversations.project_dispatch._conv_has_live_task',
                        lambda cid, **_k: cid == 'mig-orig')
    with flask_app.app_context():
        stuck = _originator_stuck('/m/5', _epic(created_by_conv='mig-orig'), [], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is False


def test_stuck_false_when_epic_on_live_cooldown(flask_app):
    """An epic on a live block-cooldown is correctly HELD, not stuck (compose)."""
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/6')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
    with flask_app.app_context():
        stuck = _originator_stuck(
            '/m/6', _epic(created_by_conv='mig-orig', blocked_until=now + 3_600_000), [], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is False


def test_stuck_false_when_epic_waiting_on_path(flask_app):
    """An epic on a live wait-on-path is correctly HELD, not stuck (compose)."""
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/7')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
    epic = _epic(created_by_conv='mig-orig', wait_paths=['lib/x.py'])
    lease = {'id': 'l', 'kind': 'lease', 'title': 'lib/x.py', 'owner_conv_id': 'cB',
             'status': 'claimed', 'lease_expires_at': now + 60_000}
    with flask_app.app_context():
        stuck = _originator_stuck('/m/7', epic, [lease], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is False



def test_stuck_true_when_wait_path_leased_by_target_itself(flask_app):
    """A lease held by the epic's OWN dispatch target is not a hold — that
    conv is the one supposed to run the work. Stuck proceeds (complement of
    the foreign-lease hold: only OTHER-conv leases block migration)."""
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/7b')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
    epic = _epic(created_by_conv='mig-orig', wait_paths=['lib/x.py'])
    own_lease = {'id': 'l', 'kind': 'lease', 'title': 'lib/x.py',
                 'owner_conv_id': 'mig-orig',  # held BY the target itself
                 'status': 'claimed', 'lease_expires_at': now + 60_000}
    with flask_app.app_context():
        stuck = _originator_stuck('/m/7b', epic, [own_lease], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is True, "the target's own lease must not count as a wait-on-path hold"


def test_stuck_true_when_wait_path_leased_by_nobody(flask_app):
    """Fail-open (design invariant 3): a wait_paths entry nobody leases can
    never strand the epic — the hold requires a LIVE lease."""
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/7c')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
    epic = _epic(created_by_conv='mig-orig', wait_paths=['lib/x.py'])
    with flask_app.app_context():
        stuck = _originator_stuck('/m/7c', epic, [], now, user_id=TEST_OWNER_USER_ID)   # no lease rows at all
    assert stuck is True, 'an unleased wait_paths entry must never strand (fail-open)'


def test_stuck_true_when_wait_path_lease_expired(flask_app):
    """An EXPIRED lease reads 'open' (effective status) — the hold self-expires
    with the lease TTL, so the epic is migratable again (no reaper needed)."""
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    from lib.conversations.project_dispatch import _originator_stuck
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/7d')
    _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
    epic = _epic(created_by_conv='mig-orig', wait_paths=['lib/x.py'])
    expired_lease = {'id': 'l', 'kind': 'lease', 'title': 'lib/x.py',
                     'owner_conv_id': 'cB',
                     'status': 'open',          # effective: lease already expired
                     'lease_expires_at': now - 1}
    with flask_app.app_context():
        stuck = _originator_stuck('/m/7d', epic, [expired_lease], now, user_id=TEST_OWNER_USER_ID)
    assert stuck is True, 'an expired lease must release the wait-on-path hold'


def test_NC_wait_on_path_guard_is_load_bearing(flask_app):
    """NC: strip the 3b wait-on-path check → the held epic reads STUCK again
    (migration would override the declared hold). Proves the check bites."""
    def run(mod):
        from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
        import time
        now = int(time.time() * 1000)
        _mk_conv(flask_app, 'mig-orig', '/ncwp')
        _queue_kickoff(flask_app, 'mig-orig', 'pt_e',
                       created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
        epic = _epic(created_by_conv='mig-orig', wait_paths=['lib/x.py'])
        lease = {'id': 'l', 'kind': 'lease', 'title': 'lib/x.py',
                 'owner_conv_id': 'cB', 'status': 'claimed',
                 'lease_expires_at': now + 60_000}
        with flask_app.app_context():
            stuck = mod._originator_stuck('/ncwp', epic, [lease], now, user_id=TEST_OWNER_USER_ID)
        assert stuck is True, (
            'NC: with the 3b wait-on-path check removed, a held epic wrongly '
            'reads STUCK — the migration would override its declared hold')

    _patch_restore(
        _DISPATCH_SRC,
        "        if _paths_waited_but_held(epic, board_tasks):\n            return False",
        "        if False:  # NC (3b wait-on-path check disabled)\n            return False",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  _pick_migration_target
# ════════════════════════════════════════════════════════════════════

def test_pick_target_returns_idle_sibling(flask_app):
    from lib.conversations.project_dispatch import _pick_migration_target
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/8')
    _mk_conv(flask_app, 'mig-idle', '/m/8')
    with flask_app.app_context():
        got = _pick_migration_target(
            '/m/8', 'mig-orig', user_id=TEST_OWNER_USER_ID)
    assert got == 'mig-idle'


def test_pick_target_excludes_originator(flask_app):
    from lib.conversations.project_dispatch import _pick_migration_target
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/9')  # ONLY the originator exists
    with flask_app.app_context():
        got = _pick_migration_target(
            '/m/9', 'mig-orig', user_id=TEST_OWNER_USER_ID)
    assert got == '', 'no idle sibling → empty (stay with originator)'


def test_pick_target_skips_busy_sibling(flask_app, monkeypatch):
    from lib.conversations.project_dispatch import _pick_migration_target
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/10')
    _mk_conv(flask_app, 'mig-busy', '/m/10')
    monkeypatch.setattr('lib.conversations.project_dispatch._conv_has_live_task',
                        lambda cid, **_k: cid == 'mig-busy')
    with flask_app.app_context():
        got = _pick_migration_target(
            '/m/10', 'mig-orig', user_id=TEST_OWNER_USER_ID)
    assert got == '', 'the only sibling is busy → no target'


def test_pick_target_skips_sibling_with_queued_kickoff(flask_app):
    from lib.conversations.project_dispatch import _pick_migration_target
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/11')
    _mk_conv(flask_app, 'mig-hasq', '/m/11')
    _queue_kickoff(flask_app, 'mig-hasq', 'pt_other', created_at=now)
    with flask_app.app_context():
        got = _pick_migration_target(
            '/m/11', 'mig-orig', user_id=TEST_OWNER_USER_ID)
    assert got == '', 'a sibling already holding a kickoff is not idle'


# ════════════════════════════════════════════════════════════════════
#  migrate_epic — the act
# ════════════════════════════════════════════════════════════════════

def test_migrate_sets_target_preserves_provenance_and_reopens(flask_app):
    from lib.conversations.project_board import claim_task, post_task, read_board
    from lib.conversations.project_dispatch import migrate_epic
    with flask_app.app_context():
        tid = post_task('/m/12', 'mig-orig', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        claim_task('/m/12', 'mig-orig', tid, user_id=TEST_OWNER_USER_ID)  # originator holds a (stuck) claim
        res = migrate_epic('/m/12', {'id': tid, 'created_by_conv': 'mig-orig'}, 'mig-idle', user_id=TEST_OWNER_USER_ID)
        board = read_board('/m/12', user_id=TEST_OWNER_USER_ID)
    assert res['ok']
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['created_by_conv'] == 'mig-orig', 'provenance must NOT be overwritten'
    assert t['dispatch_target'] == 'mig-idle', 'routing points at the new target'
    assert t['status'] == 'open', 'migration reopens the claim so it re-dispatches'


def test_migrate_drops_stale_kickoff(flask_app):
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import _has_queued_kickoff, migrate_epic
    import time
    with flask_app.app_context():
        tid = post_task('/m/13', 'mig-orig', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        _mk_conv(flask_app, 'mig-orig', '/m/13')
        _queue_kickoff(flask_app, 'mig-orig', tid, created_at=int(time.time() * 1000))
        migrate_epic('/m/13', {'id': tid, 'created_by_conv': 'mig-orig'}, 'mig-idle', user_id=TEST_OWNER_USER_ID)
        still = _has_queued_kickoff('mig-orig', user_id=TEST_OWNER_USER_ID)
    assert still is False, 'the stale kickoff on the dead originator must be dropped'


def test_migrate_emits_feed_note(flask_app):
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import migrate_epic
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        tid = post_task('/m/14', 'mig-orig', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        migrate_epic('/m/14', {'id': tid, 'created_by_conv': 'mig-orig'}, 'mig-idle', user_id=TEST_OWNER_USER_ID)
        events = read_project_feed('/m/14', limit=50, user_id=TEST_OWNER_USER_ID)['events']
    assert any('migrat' in (e.get('summary') or '').lower() for e in events), \
        'migration must be visible in the feed'


def test_complete_clears_dispatch_target(flask_app):
    from lib.conversations.project_board import complete_task, post_task, read_board
    from lib.conversations.project_dispatch import migrate_epic
    with flask_app.app_context():
        tid = post_task('/m/15', 'mig-orig', 'epic', user_id=TEST_OWNER_USER_ID)['id']
        migrate_epic('/m/15', {'id': tid, 'created_by_conv': 'mig-orig'}, 'mig-idle', user_id=TEST_OWNER_USER_ID)
        complete_task('/m/15', 'mig-idle', tid, user_id=TEST_OWNER_USER_ID)
        board = read_board('/m/15', user_id=TEST_OWNER_USER_ID)
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['dispatch_target'] == ''


# ════════════════════════════════════════════════════════════════════
#  WIRING: dispatch routes through _dispatch_target (migrated epic → new
#  target, NOT originator)
# ════════════════════════════════════════════════════════════════════

def test_sweep_routes_migrated_epic_to_new_target(flask_app):
    """A migrated epic (dispatch_target set) is dispatched to the NEW target,
    not its originator — the load-bearing routing change."""
    from lib.conversations.project_board import read_board
    from lib.conversations.project_dispatch import sweep_dispatch
    from tests._seed import seed_board_task
    _mk_conv(flask_app, 'mig-orig', '/m/route')
    _mk_conv(flask_app, 'mig-idle', '/m/route')
    with flask_app.app_context():
        tid = 'pt_migration_route'
        seed_board_task(
            tid, '/m/route', user_id=TEST_OWNER_USER_ID,
            title='epic', created_by_conv='mig-orig',
            dispatch_target='mig-idle')
        sweep_dispatch('/m/route', user_id=TEST_OWNER_USER_ID)
        board = read_board('/m/route', user_id=TEST_OWNER_USER_ID)
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['status'] == 'claimed', 'the migrated epic must be dispatched'
    assert t['owner_conv_id'] == 'mig-idle', \
        'dispatch must route to dispatch_target (new), not created_by_conv (origin)'
    assert t['created_by_conv'] == 'mig-orig', 'provenance still intact'


def test_NC_routing_uses_dispatch_target(flask_app, monkeypatch):
    """NC: revert the sweep routing to created_by_conv → a migrated epic is
    (wrongly) dispatched back to its originator, not the new target."""
    import lib.conversations.project_dispatch as pd
    from lib.conversations.project_board import read_board
    from tests._seed import seed_board_task
    _mk_conv(flask_app, 'mig-orig', '/ncroute')
    _mk_conv(flask_app, 'mig-idle', '/ncroute')
    with flask_app.app_context():
        tid = 'pt_nc_migration_route'
        seed_board_task(
            tid, '/ncroute', user_id=TEST_OWNER_USER_ID,
            title='epic', created_by_conv='mig-orig',
            dispatch_target='mig-idle')
        monkeypatch.setattr(
            pd, '_dispatch_target',
            lambda epic: str(epic.get('created_by_conv') or ''))
        pd.sweep_dispatch('/ncroute', user_id=TEST_OWNER_USER_ID)
        board = read_board('/ncroute', user_id=TEST_OWNER_USER_ID)
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['owner_conv_id'] == 'mig-orig', \
        'routing reverted to provenance must send the epic to its originator'


# ════════════════════════════════════════════════════════════════════
#  WIRING: _migrate_stranded_epics migrates + dispatches in one sweep
# ════════════════════════════════════════════════════════════════════

def test_sweep_migrates_stranded_and_dispatches_to_sibling(flask_app):
    """End-to-end: an idle-stranded originator (kickoff older than the lease
    TTL, no live task) + an idle sibling → the sweep migrates the epic and
    dispatches it to the sibling in ONE pass."""
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS, read_board
    from lib.conversations.project_dispatch import sweep_dispatch
    from tests._seed import seed_board_task
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/e2e')
    _mk_conv(flask_app, 'mig-idle', '/m/e2e')
    with flask_app.app_context():
        tid = 'pt_migration_e2e'
        seed_board_task(
            tid, '/m/e2e', user_id=TEST_OWNER_USER_ID,
            title='stranded epic', status='claimed',
            owner_conv_id='mig-orig', lease_expires_at=1,
            created_by_conv='mig-orig', dispatched=1)
        _queue_kickoff(flask_app, 'mig-orig', tid, created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
        sweep_dispatch('/m/e2e', user_id=TEST_OWNER_USER_ID)
        board = read_board('/m/e2e', user_id=TEST_OWNER_USER_ID)
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['dispatch_target'] == 'mig-idle', 'the stranded epic must be migrated'
    assert t['owner_conv_id'] == 'mig-idle', 'and dispatched to the idle sibling'
    assert t['created_by_conv'] == 'mig-orig', 'provenance intact'


def test_NC_migrate_call_is_load_bearing(flask_app, monkeypatch):
    """NC: revert the _migrate_stranded_epics call in sweep_dispatch → a
    stranded epic is NOT migrated (dispatch_target stays '')."""
    import lib.conversations.project_dispatch as pd
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS, read_board
    from tests._seed import seed_board_task
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/ncmig')
    _mk_conv(flask_app, 'mig-idle', '/ncmig')
    with flask_app.app_context():
        tid = 'pt_nc_migration_e2e'
        seed_board_task(
            tid, '/ncmig', user_id=TEST_OWNER_USER_ID,
            title='epic', status='claimed', owner_conv_id='mig-orig',
            lease_expires_at=1, created_by_conv='mig-orig', dispatched=1)
        _queue_kickoff(
            flask_app, 'mig-orig', tid,
            created_at=now - DEFAULT_LEASE_TTL_MS - 60_000)
        monkeypatch.setattr(
            pd, '_migrate_stranded_epics', lambda *_a, **_k: 0)
        pd.sweep_dispatch('/ncmig', user_id=TEST_OWNER_USER_ID)
        board = read_board('/ncmig', user_id=TEST_OWNER_USER_ID)
    t = [x for x in board['tasks'] if x['id'] == tid][0]
    assert t['dispatch_target'] == '', \
        'without the migration pass, a stranded epic is never rerouted'


# ════════════════════════════════════════════════════════════════════
#  INTERACTION: after migration, reconcile does NOT resurrect the old
#  originator route (the strand-most-likely edge)
# ════════════════════════════════════════════════════════════════════

def test_reconcile_no_resurrection_of_old_originator_after_migration(flask_app):
    """After migrate_epic drops the originator's kickoff and reopens the claim,
    _reconcile_stranded_kickoffs must NOT re-drain the OLD originator (no dead
    route resurrection). The originator's kickoff is gone and it no longer owns
    a claimed epic, so the reconcile keys find nothing for it."""
    from lib.conversations.project_dispatch import (
        _has_queued_kickoff, _reconcile_stranded_kickoffs, migrate_epic,
    )
    from tests._seed import seed_board_task
    import time
    now = int(time.time() * 1000)
    _mk_conv(flask_app, 'mig-orig', '/m/resur')
    _mk_conv(flask_app, 'mig-idle', '/m/resur')
    with flask_app.app_context():
        tid = 'pt_migration_resurrection'
        seed_board_task(
            tid, '/m/resur', user_id=TEST_OWNER_USER_ID,
            title='epic', status='claimed', owner_conv_id='mig-orig',
            lease_expires_at=now + 60_000, created_by_conv='mig-orig')
        _queue_kickoff(flask_app, 'mig-orig', tid, created_at=now - 5_000)
        # migrate: drops the originator kickoff + reopens the claim
        migrate_epic('/m/resur', {'id': tid, 'created_by_conv': 'mig-orig'}, 'mig-idle', user_id=TEST_OWNER_USER_ID)
        orig_has_kickoff = _has_queued_kickoff('mig-orig', user_id=TEST_OWNER_USER_ID)
        # the reconcile pass must not re-drain the old originator
        _reconcile_stranded_kickoffs('/m/resur', user_id=TEST_OWNER_USER_ID)
        orig_still_clean = not _has_queued_kickoff('mig-orig', user_id=TEST_OWNER_USER_ID)
    assert orig_has_kickoff is False, 'migration must drop the originator kickoff'
    assert orig_still_clean, \
        'reconcile must NOT resurrect a kickoff on the migrated-away originator'


# ════════════════════════════════════════════════════════════════════
#  NC-age — the age>lease-TTL gate is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_age_gate_is_load_bearing(flask_app):
    def run(pd):
        import time
        now = int(time.time() * 1000)
        _mk_conv(flask_app, 'mig-orig', '/ncage')
        _queue_kickoff(flask_app, 'mig-orig', 'pt_e', created_at=now - 5_000)  # FRESH
        with flask_app.app_context():
            stuck = pd._originator_stuck('/ncage', _epic(created_by_conv='mig-orig'), [], now, user_id=TEST_OWNER_USER_ID)
        assert stuck is True, \
            'NC-age: with the age>lease-TTL gate removed, a FRESH kickoff must ' \
            'wrongly read as stuck (proves the age threshold is load-bearing)'

    _patch_restore(
        _DISPATCH_SRC,
        "        if age_ms < MIGRATION_STUCK_MS:\n            return False",
        "        if False:  # NC-age (age gate disabled)\n            return False",
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  NC-target-busy — the target busy-guard is load-bearing
# ════════════════════════════════════════════════════════════════════

def test_NC_target_busy_guard_is_load_bearing(flask_app, monkeypatch):
    import lib.conversations.project_dispatch as pd
    _mk_conv(flask_app, 'mig-orig', '/nctb')
    _mk_conv(flask_app, 'mig-busy', '/nctb')
    # Neuter the only source of busy truth. The complement above proves the
    # real predicate excludes this candidate.
    monkeypatch.setattr(pd, '_conv_has_live_task', lambda *_a, **_k: False)
    with flask_app.app_context():
        got = pd._pick_migration_target(
            '/nctb', 'mig-orig', user_id=TEST_OWNER_USER_ID)
    assert got == 'mig-busy', \
        'without busy truth, migration picks the busy sibling and moves the strand'
