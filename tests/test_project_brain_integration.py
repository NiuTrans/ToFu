"""tests/test_project_brain_integration.py — the AUTONOMOUS FLYWHEEL, end to end.

Every prior Project Brain test proves a GEAR in isolation (monkeypatched
presence, stubbed Api, direct `build_brain_summary`). This proves the FLYWHEEL:
the "live" chain nobody had exercised as a whole —

    sweep_all_active_projects()            (the real scheduler entry)
      → select_dispatchable                (real board read, real lease eval)
      → dispatch_epic → claim + enqueue    (real message_queue workflow_step)
      → dispatch_next_queued               (real queue drain)
      → create_task + spawn_task           (real task lifecycle; spawn stubbed)
      → complete_task                      (real board complete)
      → on_epic_completed                  (real dependent unblock + re-dispatch)

Nothing on the dispatch/queue/board path is stubbed. ONLY `spawn_task` (the
thread that would actually run an LLM) is replaced with a recorder — so we
prove "a real task was created and handed to the spawner" without a network
call. Everything else runs against a real (conftest-forced SQLite) DB under an
app context.

Includes ONE source-level negative control: no-op the `sweep_all_active_projects()`
call inside the scheduler tick → the flywheel never self-starts → the cold-start
assertion FAILS. Byte-identical restore.
"""

from __future__ import annotations

import os
import time

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_SCHEDULER_SRC = os.path.join(ROOT, 'lib', 'scheduler', 'manager.py')
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')


def _clear_task_registry():
    """Wipe the in-proc task registry so a prior test's stubbed-spawn task
    (which never completes, so it lingers as 'running') can't make a later
    conv look busy via _conv_has_live_task. Best-effort."""
    try:
        from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
        with tasks_lock:
            tasks.clear()
    except Exception:
        pass


def _mark_busy(conv_id, task_id='busytask0000001'):
    """Register a fake LIVE task so the conv reads busy — the production-real
    shape of an agent posting an epic MID-TURN. The post-time event seam
    (on_epic_posted) deliberately defers busy targets to the sweep/nudge, so
    tests exercising the SWEEP path must post while busy. Clear with
    _clear_task_registry()."""
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        tasks[task_id] = {'id': task_id, 'convId': conv_id, 'status': 'running',
                          '_userId': 1, 'aborted': False, 'config': {},
                          'toolRounds': []}
    return task_id


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Every test uses unique sidecar keys; only ephemeral in-process task state
    # needs resetting. Never clean retired legacy SQLite tables behind the
    # storage authority.
    _clear_task_registry()
    # Best-effort push stub (feed/presence emit) — no live WS in the test.
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield
    _clear_task_registry()


def _seed_conv(flask_app, conv_id, project_path):
    """Create a real conversation row so dispatch_next_queued can append to it."""
    del flask_app
    from tests._seed import seed_conversation
    now = int(time.time() * 1000)
    result = seed_conversation(
        conv_id,
        title='Worker conv',
        messages=[{'role': 'user', 'content': 'seed'}],
        settings={'projectPath': project_path, 'projectEnabled': True},
        created_at=now,
        updated_at=now,
    )
    assert result['applied'] is True


def _stub_spawn(monkeypatch):
    """Replace the LLM-running spawner with a recorder — prove a task was
    created + handed off, WITHOUT running a model. Patch the defining module
    used by the queue dispatcher."""
    spawned = []
    import lib.tasks_pkg.spawn as task_spawn
    monkeypatch.setattr(
        task_spawn, 'spawn_task', lambda task: spawned.append(task))
    return spawned


def _queue_workflow_ids(flask_app, conv_id):
    from lib.message_queue import KIND_WORKFLOW, get_queue
    with flask_app.app_context():
        return [
            q for q in get_queue(conv_id, user_id=1)
            if q['kind'] == KIND_WORKFLOW
        ]


def _queue_workflow_payloads(flask_app, conv_id):
    """Read durable workflow payloads through the sidecar authority."""
    del flask_app
    from lib.storage import get_storage_client
    rows = get_storage_client().query(
        'queue.list', {'conv_id': conv_id, 'user_id': 1}) or []
    return [
        row.get('payload') or {} for row in rows
        if row.get('kind') == 'workflow_step'
    ]


def _persisted_last_user(flask_app, conv_id):
    """Return the LAST user message dict as persisted on the conversation row.

    Brain-dispatch attribution lives in the canonical ``origin`` projection on
    the persisted conversation turn. It is intentionally stripped from the
    task's api_messages, so assertions must read the conv row, not
    task['messages']."""
    del flask_app
    from tests._seed import conv_document
    document = conv_document(conv_id, user_id=1) or {}
    msgs = document.get('messages') or []
    users = [m for m in msgs if m.get('role') == 'user']
    return users[-1] if users else {}


def _assert_persisted_brain_dispatch(message, board_task_id):
    """Assert the canonical, public brain-dispatch attribution contract."""
    origin = message.get('origin') or {}
    assert origin.get('initiator') == 'brain'
    assert origin.get('boardTaskId') == board_task_id


def _feed_kinds_ordered(flask_app, project_path):
    from lib.conversations.project_feed import read_project_feed
    with flask_app.app_context():
        feed = read_project_feed(project_path, limit=500, user_id=1)
    # read_project_feed returns newest-first; reverse to chronological.
    return [e['kind'] for e in reversed(feed['events'])]


# ════════════════════════════════════════════════════════════════════
#  THE FLYWHEEL
# ════════════════════════════════════════════════════════════════════

def test_full_autonomous_flywheel(flask_app, monkeypatch):
    from lib.conversations.project_board import (
        complete_task, post_task, read_board,
    )
    from lib.conversations.project_brain_summary import build_brain_summary
    from lib.conversations.project_dispatch import sweep_all_active_projects

    proj = os.path.abspath('/tmp/flywheel-proj')
    conv = 'conv-flywheel-worker'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)

    # Make sweep_all_active_projects find THIS project (it enumerates recent
    # projects); stub the enumeration to our seeded project deterministically.
    monkeypatch.setattr('lib.project_mod.get_recent_projects',
                        lambda *, user_id: [{'path': proj}])

    with flask_app.app_context():
        # 1) Two epics: A (no deps) + B (depends_on A), both posted by our conv.
        #    Busy-at-post (the production-real shape: an agent posts mid-turn)
        #    so the post-time event seam defers to the sweep under test.
        _mark_busy(conv)
        a_id = post_task(proj, conv, 'Epic A — foundation', user_id=1)['id']
        b_id = post_task(proj, conv, 'Epic B — builds on A',
                         user_id=1, depends_on=[a_id])['id']
        _clear_task_registry()

        # 2) The HEARTBEAT (real scheduler entry) sweeps → A dispatchable,
        #    B blocked by its unfinished dependency. The sweep now SELF-DRAINS
        #    the cold-start kickoff (the fix): A is claimed AND a task is
        #    spawned for the idle conv in the same pass.
        dispatched = sweep_all_active_projects(user_id=1)
        assert dispatched >= 1, 'sweep must dispatch the dependency-free epic A'

        board = read_board(proj, user_id=1)
        by_id = {t['id']: t for t in board['tasks']}
        # A claimed under our conv; B still open (dependency unmet).
        assert by_id[a_id]['status'] == 'claimed', 'A must be claimed by the sweep'
        assert by_id[a_id]['owner_conv_id'] == conv
        assert by_id[b_id]['status'] == 'open', 'B must NOT dispatch (dep unmet)'

        # The kickoff for A was DRAINED by the sweep (self-start), so a REAL
        # task was created + handed to the (stubbed) spawner — targeting A,
        # carrying the project path + brain-dispatch attribution.
        assert len(spawned) == 1, 'the sweep must self-drain → spawn exactly one task'
        task_a = spawned[0]
        assert task_a['convId'] == conv
        assert (task_a['config'] or {}).get('projectPath') == proj
    # Attribution markers persist on the conv row (stripped from api_messages).
    last_user_a = _persisted_last_user(flask_app, conv)
    _assert_persisted_brain_dispatch(last_user_a, a_id)
    with flask_app.app_context():

        # 3) Summary mid-flight: A is claimed by our (active) peer → peerEpics
        #    joins conv → "Epic A". (announce the peer so it's active.)
        import lib.presence.registry as reg
        monkeypatch.setattr(reg, '_state', {})
        monkeypatch.setattr(reg, '_sweeper_started', True)
        reg.announce(
            proj, conv, task_id='t-a', title='Worker conv', user_id=1)
        s_mid = build_brain_summary(proj, user_id=1)
        assert s_mid['epicsClaimed'] == 1 and s_mid['epicsOpen'] == 1
        assert s_mid['peerEpics'].get(conv) == 'Epic A — foundation'

        # 4) A completes → on_epic_completed fires → B unblocks + re-dispatches.
        #    (The conv is now busy running A's stubbed task, so B is claimed +
        #    enqueued but NOT drained — the busy guard holds.)
        complete_task(proj, conv, a_id, user_id=1)

        board2 = read_board(proj, user_id=1)
        by_id2 = {t['id']: t for t in board2['tasks']}
        assert by_id2[a_id]['status'] == 'done', 'A must be done'
        assert by_id2[b_id]['status'] == 'claimed', \
            'B must be auto-dispatched (claimed) once its dependency completed'
        assert by_id2[b_id]['owner_conv_id'] == conv

        # B's kickoff is now in the queue (a NEW workflow_step for B).
        board_ids2 = [
            payload.get('boardTaskId')
            for payload in _queue_workflow_payloads(flask_app, conv)
        ]
        assert b_id in board_ids2, 'B kickoff must be enqueued after A completes'

        # 6) Feed recorded the real sequence: A claimed → A completed (chrono).
        kinds = _feed_kinds_ordered(flask_app, proj)
        assert 'claimed' in kinds and 'completed' in kinds
        # the first claimed precedes the completed of A
        assert kinds.index('claimed') < kinds.index('completed')

        # Final summary: A done, B claimed → counts reflect the flywheel state.
        s_end = build_brain_summary(proj, user_id=1)
        assert s_end['epicsDone'] == 1
        assert s_end['epicsClaimed'] == 1  # B now claimed
        assert s_end['peerEpics'].get(conv) == 'Epic B — builds on A'


# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROL: the scheduler-tick wiring is load-bearing
# ════════════════════════════════════════════════════════════════════

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_scheduler_tick_wiring_is_load_bearing(flask_app, monkeypatch):
    """NC: no-op the `sweep_all_active_projects()` call inside the scheduler
    tick → the heartbeat never fires → a cold-start epic is NEVER self-started.

    We prove the WIRING (the call the tick makes) is load-bearing by invoking
    the real tick method (`_check_and_run_due_tasks`) with no due tasks and
    asserting: with the call intact an open epic gets claimed; with it no-opped
    it stays open.
    """
    proj = os.path.abspath('/tmp/flywheel-nc')
    conv = 'conv-nc-worker'
    run_count = {'value': 0}

    def _drive_tick_and_check(expect_claimed, scheduler_module=None):
        del expect_claimed
        run_count['value'] += 1
        drive_proj = f"{proj}-{run_count['value']}"
        drive_conv = f"{conv}-{run_count['value']}"
        # The neutered scheduler module (when active) is live in sys.modules via
        # the harness; import it directly (no reload — that would re-read the
        # clean file). Outside the NC context this is the canonical module.
        import lib.scheduler.manager as canonical_scheduler
        sched = scheduler_module or canonical_scheduler
        from lib.conversations.project_board import post_task, read_board
        monkeypatch.setattr('lib.project_mod.get_recent_projects',
                            lambda *, user_id: [{'path': drive_proj}])
        _stub_spawn(monkeypatch)
        _seed_conv(flask_app, drive_conv, drive_proj)
        with flask_app.app_context():
            # Suppress the post-time seam: this negative control isolates the
            # scheduler heartbeat wiring, then makes the target idle for tick.
            _mark_busy(drive_conv)
            epic = post_task(
                drive_proj, drive_conv, 'Cold-start epic', user_id=1)['id']
            _clear_task_registry()
            # Drive the REAL tick method (no due tasks → it falls through to
            # the sweep call at the end).
            mgr = sched.get_scheduler()
            from lib.identity import PrincipalContext
            mgr._process_principal = PrincipalContext.system(
                subject_id='project-brain-test',
                owner_user_id=1,
                scopes={'scheduler:run'},
            )
            mgr._check_and_run_due_tasks()
            board = read_board(drive_proj, user_id=1)
            claimed = [t for t in board['tasks']
                       if t['id'] == epic and t['status'] == 'claimed']
        return bool(claimed)

    # First: sanity — with the wiring intact, the tick self-starts the epic.
    assert _drive_tick_and_check(True), \
        'baseline: the scheduler tick must self-start a cold-start epic'

    # NC: neuter the sweep call inside the tick → epic must stay open.
    def run(neutered_scheduler):
        assert not _drive_tick_and_check(
            False, scheduler_module=neutered_scheduler), \
            'NC: with the tick sweep no-opped, the epic must NOT self-start'

    _patch_restore(
        _SCHEDULER_SRC,
        '                sweep_all_active_projects('
        'user_id=personal_owner_user_id)',
        '                pass  # NC sweep disabled',
        run,
    )


# ════════════════════════════════════════════════════════════════════
#  COLD-START DRAIN: the tick must actually SPAWN a task (not just claim)
# ════════════════════════════════════════════════════════════════════

def test_cold_start_tick_spawns_task_in_idle_conv(flask_app, monkeypatch):
    """The bug this closes: a cold-start epic in an IDLE conv was CLAIMED by
    the sweep but its kickoff rotted in the queue — nothing drained an idle
    conv, so no task ever ran. The old flywheel test hid this by calling
    dispatch_next_queued MANUALLY. This rides the REAL scheduler tick end to
    end and asserts a task is actually SPAWNED (handed to spawn_task), carries
    the projectPath (so it can do the work) AND the brain-dispatch attribution.
    """
    import lib.scheduler.manager as sched
    from lib.conversations.project_board import read_board, post_task

    proj = os.path.abspath('/tmp/coldstart-spawn')
    conv = 'conv-coldstart'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    spawned = _stub_spawn(monkeypatch)
    monkeypatch.setattr('lib.project_mod.get_recent_projects',
                        lambda *, user_id: [{'path': proj}])

    with flask_app.app_context():
        # Busy at post so the post-time event seam defers to the tick (the
        # path this test is named for).
        _mark_busy(conv)
        epic = post_task(proj, conv, 'Cold-start epic', user_id=1)['id']
        _clear_task_registry()
        # Drive the REAL tick (no due tasks → falls through to the sweep).
        mgr = sched.get_scheduler()
        from lib.identity import PrincipalContext
        mgr._process_principal = PrincipalContext.system(
            subject_id='project-brain-test',
            owner_user_id=1,
            scopes={'scheduler:run'},
        )
        mgr._check_and_run_due_tasks()

        # The epic is claimed …
        board = read_board(proj, user_id=1)
        epic_row = [t for t in board['tasks'] if t['id'] == epic][0]
        assert epic_row['status'] == 'claimed' and epic_row['owner_conv_id'] == conv

        # … AND — the actual fix — a task was SPAWNED for the idle conv.
        assert len(spawned) == 1, \
            'the tick must SPAWN a task for the cold-start epic, not just claim it'
        task = spawned[0]
        assert task['convId'] == conv
        # It carries a real projectPath (resolved from conv settings) so the
        # spawned agent can actually do the project work.
        assert (task['config'] or {}).get('projectPath') == proj, \
            'the dispatched task must carry the project path to work the epic'
    # It is attributable as brain-dispatched (markers persist on the conv row,
    # stripped from api_messages) — not mistaken for human input.
    last_user = _persisted_last_user(flask_app, conv)
    _assert_persisted_brain_dispatch(last_user, epic)

    _clear_task_registry()


# ════════════════════════════════════════════════════════════════════
#  SELF-HEAL: a STRANDED kickoff (broken drain chain) is re-drained by a
#  later sweep — the "queued but never dequeued" recovery.
# ════════════════════════════════════════════════════════════════════

def _strand_kickoff(flask_app, proj, conv, epic_id):
    """Reproduce the shipped strand: an epic CLAIMED under an IDLE conv with
    its workflow_step kickoff QUEUED but never drained (as if the completion
    chain broke — a crash / restart / a multi-dispatch sweep whose extra
    kickoffs the busy guard skipped). Uses dispatch_epic (real claim+enqueue)
    WITHOUT the drain, so the queue row is genuine."""
    from lib.conversations.project_dispatch import dispatch_epic, select_dispatchable
    with flask_app.app_context():
        epic = [
            e for e in select_dispatchable(proj, user_id=1)
            if e['id'] == epic_id
        ][0]
        res = dispatch_epic(
            proj, epic, conv, user_id=1)  # claims + enqueues, no drain
        assert res['ok'], f'strand setup failed: {res}'


def test_sweep_reconciles_stranded_kickoff(flask_app, monkeypatch):
    """THE bug in the screenshot: a brain kickoff sits CLAIMED+QUEUED in an
    idle conv and never dequeues (the drain chain broke). A later heartbeat
    sweep must self-heal it: the reconcile pass re-drains the idle conv →
    spawns the task → the queue empties. Rides the REAL sweep_dispatch.

    Crucially the stranded epic is EXCLUDED from select_dispatchable (it's
    claimed) so the normal dispatch loop can never touch it — only the
    reconcile pass can. This is what makes recovery automatic, not permanent.
    """
    from lib.conversations.project_board import post_task, read_board
    from lib.conversations.project_dispatch import sweep_dispatch
    from lib.message_queue import KIND_WORKFLOW, get_queue

    proj = os.path.abspath('/tmp/strand-heal')
    conv = 'conv-strand'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    monkeypatch.setattr('lib.project_mod.get_recent_projects',
                        lambda *, user_id: [{'path': proj}])

    with flask_app.app_context():
        # Busy at post so the post-time seam defers (the strand setup needs
        # the epic OPEN to claim+enqueue it manually).
        _mark_busy(conv)
        epic = post_task(proj, conv, 'Stranded epic', user_id=1)['id']
        _clear_task_registry()
    # Strand it: claimed + kickoff queued, conv idle, NOT drained.
    _strand_kickoff(flask_app, proj, conv, epic)

    with flask_app.app_context():
        # Precondition: the kickoff is genuinely stuck in the queue and the
        # epic is claimed (so select_dispatchable ignores it).
        q_before = [
            x for x in get_queue(conv, user_id=1)
            if x['kind'] == KIND_WORKFLOW
        ]
        assert len(q_before) == 1, 'setup: exactly one stranded kickoff queued'
        assert read_board(proj, user_id=1)['tasks'][0]['status'] == 'claimed'

    spawned = _stub_spawn(monkeypatch)
    with flask_app.app_context():
        # The heartbeat sweep runs — its reconcile pass must re-drain the conv.
        sweep_dispatch(proj, user_id=1)
        q_after = [
            x for x in get_queue(conv, user_id=1)
            if x['kind'] == KIND_WORKFLOW
        ]

    assert len(spawned) == 1, \
        'the reconcile pass must re-drain the stranded kickoff → spawn a task'
    assert spawned[0]['convId'] == conv
    assert len(q_after) == 0, 'the stranded kickoff must be drained from the queue'
    # Attribution survives the re-drain (persisted turn is brain-dispatched).
    last_user = _persisted_last_user(flask_app, conv)
    _assert_persisted_brain_dispatch(last_user, epic)
    _clear_task_registry()


def _expire_lease(flask_app, proj, epic_id):
    """Project the same authority state produced by lease reclamation.

    ``_effective_status`` reclaims an expired claim AT READ TIME, so after this
    the board reports the epic ``open`` with ``owner_conv_id`` blanked — while
    the workflow_step kickoff is still sitting in the owner's queue. That is
    the exact production state after a 30-minute lease lapses under a long
    task, and it is a DIFFERENT strand from the claimed one above.

    The real sidecar clock is intentionally not injectable through the storage
    protocol. Board-policy unit tests pin time-based expiry; this flywheel test
    enters its identical durable projection through the semantic reopen command
    instead of mutating the retired legacy SQLite tables behind the authority.
    """
    del flask_app
    import uuid
    from lib.storage import get_storage_client
    result = get_storage_client(write=True).command(
        'board.reopen', {
            'project_path': proj, 'user_id': 1,
            'conv_id': 'test-lease-reclaim', 'task_id': epic_id,
        }, f'test-lease-reclaim:{epic_id}:{uuid.uuid4().hex[:10]}')
    assert result.get('ok'), result


def test_sweep_reconciles_stranded_kickoff_after_lease_expiry(flask_app, monkeypatch):
    """THE SECOND STRAND — an epic whose lease EXPIRED while its kickoff was
    still queued. Measured in production: 7 workflow_step rows sat in 3 idle
    conversations across hours, and nothing in the system could ever drain
    them.

    Why the claimed-strand fix above does NOT cover this. Once the 30-minute
    lease lapses, ``_effective_status`` reports the epic ``open``, so:

      * the reconcile scan — which collected only convs owning a **claimed**
        epic — no longer sees this conv at all, and
      * the normal dispatch loop DOES pick the epic up again, but
        ``_epic_already_queued`` finds the still-queued kickoff and refuses to
        re-dispatch.

    Both doors shut on the same row: permanently queued, never dequeued, with
    the user staring at "queued, nothing generating". The reconcile scan must
    therefore be keyed on "conv holds an undrained kickoff", not on the epic's
    momentary status — the queue row is the durable fact, the lease is not.
    """
    from lib.conversations.project_board import post_task, read_board
    from lib.conversations.project_dispatch import sweep_dispatch
    from lib.message_queue import KIND_WORKFLOW, get_queue

    proj = os.path.abspath('/tmp/strand-expired')
    conv = 'conv-strand-expired'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    monkeypatch.setattr('lib.project_mod.get_recent_projects',
                        lambda *, user_id: [{'path': proj}])

    with flask_app.app_context():
        _mark_busy(conv)
        epic = post_task(
            proj, conv, 'Epic stranded past its lease', user_id=1)['id']
        _clear_task_registry()
    # Strand it (claim + enqueue, no drain) …
    _strand_kickoff(flask_app, proj, conv, epic)
    # … then let the lease lapse — the state the claimed-only scan cannot see.
    _expire_lease(flask_app, proj, epic)

    with flask_app.app_context():
        q_before = [
            x for x in get_queue(conv, user_id=1)
            if x['kind'] == KIND_WORKFLOW
        ]
        assert len(q_before) == 1, 'setup: exactly one stranded kickoff queued'
        # Precondition that defines this strand: the board now reads OPEN, so
        # the claimed-only reconcile scan would skip this conv entirely.
        row = [
            t for t in read_board(proj, user_id=1)['tasks']
            if t['id'] == epic
        ][0]
        assert row['status'] == 'open', \
            'setup: the expired lease must read back as open'
        assert not row['owner_conv_id'], \
            'setup: an expired claim blanks the owner — this is why a scan ' \
            'keyed on owner_conv_id of claimed epics cannot find the strand'

    spawned = _stub_spawn(monkeypatch)
    with flask_app.app_context():
        sweep_dispatch(proj, user_id=1)
        q_after = [
            x for x in get_queue(conv, user_id=1)
            if x['kind'] == KIND_WORKFLOW
        ]

    assert len(spawned) == 1, (
        'the sweep must re-drain a kickoff stranded by lease expiry — this is '
        'the shape measured in production (7 rows across 3 idle convs, never '
        'dequeued): the reconcile scan skipped it because the epic read open, '
        'and _epic_already_queued blocked re-dispatch because the row existed')
    assert spawned[0]['convId'] == conv
    assert len(q_after) == 0, 'the stranded kickoff must be drained'
    last_user = _persisted_last_user(flask_app, conv)
    _assert_persisted_brain_dispatch(last_user, epic)
    _clear_task_registry()


def test_expired_lease_strand_does_not_double_dispatch(flask_app, monkeypatch):
    """The paired risk of widening the scan: an epic that reads OPEN is also
    eligible for the NORMAL dispatch loop, so a naive fix could drain the
    queued kickoff AND dispatch a fresh one in the same sweep — two billed
    tasks for one epic. Exactly one task must be spawned, and the queue must
    end empty (not refilled by a second kickoff)."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import sweep_dispatch
    from lib.message_queue import KIND_WORKFLOW, get_queue

    proj = os.path.abspath('/tmp/strand-expired-nodup')
    conv = 'conv-strand-nodup'
    _clear_task_registry()
    _seed_conv(flask_app, conv, proj)
    monkeypatch.setattr('lib.project_mod.get_recent_projects',
                        lambda *, user_id: [{'path': proj}])

    with flask_app.app_context():
        _mark_busy(conv)
        epic = post_task(
            proj, conv, 'Epic to not double-dispatch', user_id=1)['id']
        _clear_task_registry()
    _strand_kickoff(flask_app, proj, conv, epic)
    _expire_lease(flask_app, proj, epic)

    spawned = _stub_spawn(monkeypatch)
    with flask_app.app_context():
        sweep_dispatch(proj, user_id=1)
        q_after = [
            x for x in get_queue(conv, user_id=1)
            if x['kind'] == KIND_WORKFLOW
        ]

    assert len(spawned) == 1, \
        f'exactly one task per epic — got {len(spawned)} (double-dispatch)'
    assert len(q_after) == 0, \
        f'queue must not be refilled by a duplicate kickoff: {q_after}'
    _clear_task_registry()


def test_NC_expired_lease_reconcile_is_load_bearing(flask_app, monkeypatch):
    """NC: revert the reconcile scan to claimed-epics-only → a kickoff
    stranded by lease expiry is NEVER re-drained (the measured production
    bug). Byte-identical restore."""
    proj = os.path.abspath('/tmp/strand-expired-nc')
    conv = 'conv-strand-expired-nc'
    run_count = {'value': 0}

    def _strand_expire_then_sweep(dispatch_module=None):
        import lib.conversations.project_dispatch as canonical_dispatch
        pd = dispatch_module or canonical_dispatch
        from lib.conversations.project_board import post_task
        from lib.message_queue import KIND_WORKFLOW, get_queue
        run_count['value'] += 1
        drive_proj = f"{proj}-{run_count['value']}"
        drive_conv = f"{conv}-{run_count['value']}"
        _clear_task_registry()
        monkeypatch.setattr('lib.project_mod.get_recent_projects',
                            lambda *, user_id: [{'path': drive_proj}])
        spawned = _stub_spawn(monkeypatch)
        _seed_conv(flask_app, drive_conv, drive_proj)
        with flask_app.app_context():
            _mark_busy(drive_conv)
            epic = post_task(
                drive_proj, drive_conv, 'Expired-lease strand', user_id=1)['id']
            _clear_task_registry()
            e = [
                x for x in pd.select_dispatchable(drive_proj, user_id=1)
                if x['id'] == epic
            ][0]
            pd.dispatch_epic(drive_proj, e, drive_conv, user_id=1)
        _expire_lease(flask_app, drive_proj, epic)
        with flask_app.app_context():
            pd.sweep_dispatch(drive_proj, user_id=1)
            q = [
                x for x in get_queue(drive_conv, user_id=1)
                if x['kind'] == KIND_WORKFLOW
            ]
        _clear_task_registry()
        return len(spawned), len(q)

    n_spawned, n_queued = _strand_expire_then_sweep()
    assert n_spawned == 1 and n_queued == 0, \
        (f'baseline: the sweep must re-drain a lease-expired strand '
         f'(spawned={n_spawned}, queued={n_queued})')

    def run(neutered_dispatch):
        n_spawned2, n_queued2 = _strand_expire_then_sweep(neutered_dispatch)
        assert n_spawned2 == 0 and n_queued2 == 1, \
            ('NC: with the scan keyed on claimed epics only, a lease-expired '
             'strand is never re-drained (queued forever — the measured bug)')

    _patch_restore(
        _DISPATCH_SRC,
        "        conv_ids = _convs_holding_undrained_kickoffs(\n"
        "            project_path, board, user_id=user_id\n"
        "        )",
        "        conv_ids = {task['owner_conv_id'] for task in board['tasks']\n"
        "                    if task.get('status') == 'claimed'\n"
        "                    and task.get('owner_conv_id')}",
        run,
    )


def test_NC_reconcile_is_load_bearing(flask_app, monkeypatch):
    """NC (the self-heal core): no-op the `_reconcile_stranded_kickoffs(...)`
    call in sweep_dispatch → a stranded kickoff is NEVER re-drained (it stays
    queued forever — exactly the shipped bug). With it intact: the stranded
    kickoff is drained + a task spawned. Neutered: zero tasks spawned, kickoff
    still queued. Byte-identical restore."""
    proj = os.path.abspath('/tmp/strand-nc')
    conv = 'conv-strand-nc'
    run_count = {'value': 0}

    def _strand_then_sweep_count(dispatch_module=None):
        import lib.conversations.project_dispatch as canonical_dispatch
        pd = dispatch_module or canonical_dispatch
        from lib.conversations.project_board import post_task
        from lib.message_queue import KIND_WORKFLOW, get_queue
        run_count['value'] += 1
        drive_proj = f"{proj}-{run_count['value']}"
        drive_conv = f"{conv}-{run_count['value']}"
        _clear_task_registry()
        monkeypatch.setattr('lib.project_mod.get_recent_projects',
                            lambda *, user_id: [{'path': drive_proj}])
        spawned = _stub_spawn(monkeypatch)
        _seed_conv(flask_app, drive_conv, drive_proj)
        with flask_app.app_context():
            _mark_busy(drive_conv)
            epic = post_task(
                drive_proj, drive_conv, 'Stranded epic', user_id=1)['id']
            _clear_task_registry()
            e = [
                x for x in pd.select_dispatchable(drive_proj, user_id=1)
                if x['id'] == epic
            ][0]
            pd.dispatch_epic(
                drive_proj, e, drive_conv,
                user_id=1)  # strand: claim+enqueue, no drain
            pd.sweep_dispatch(
                drive_proj, user_id=1)  # reconcile pass runs here
            q = [
                x for x in get_queue(drive_conv, user_id=1)
                if x['kind'] == KIND_WORKFLOW
            ]
        _clear_task_registry()
        return len(spawned), len(q)

    # Baseline: with the reconcile intact, the sweep re-drains the strand.
    n_spawned, n_queued = _strand_then_sweep_count()
    assert n_spawned == 1 and n_queued == 0, \
        f'baseline: reconcile must re-drain the strand (spawned={n_spawned}, queued={n_queued})'

    def run(neutered_dispatch):
        n_spawned2, n_queued2 = _strand_then_sweep_count(neutered_dispatch)
        assert n_spawned2 == 0 and n_queued2 == 1, \
            'NC: with _reconcile_stranded_kickoffs no-opped, the strand is ' \
            'NEVER re-drained (queued forever — the shipped bug)'

    _patch_restore(
        _DISPATCH_SRC,
        '    _reconcile_stranded_kickoffs(project_path, user_id=user_id)',
        '    pass  # NC reconcile disabled',
        run,
    )


def test_NC_idle_drain_is_load_bearing(flask_app, monkeypatch):
    """NC (the fix's core): no-op the `_drain_idle_target(target)` call in
    sweep_dispatch → the cold-start kickoff is enqueued + claimed but NEVER
    spawned. This proves the drain call is what makes the flywheel self-start.
    With it intact: a task is spawned. Neutered: zero tasks spawned (kickoff
    rots in the queue — exactly the shipped bug). Byte-identical restore.
    """
    proj = os.path.abspath('/tmp/coldstart-nc')
    conv = 'conv-coldstart-nc'
    run_count = {'value': 0}

    def _drive_and_count_spawned(dispatch_module=None):
        # The neutered project_dispatch (when active) is live in sys.modules via
        # the harness; scheduler imports sweep_all_active_projects at call time
        # so it resolves the swapped module too — no reload needed.
        import lib.conversations.project_dispatch as canonical_dispatch
        pd = dispatch_module or canonical_dispatch
        from lib.conversations.project_board import post_task
        run_count['value'] += 1
        drive_proj = f"{proj}-{run_count['value']}"
        drive_conv = f"{conv}-{run_count['value']}"
        _clear_task_registry()
        monkeypatch.setattr('lib.project_mod.get_recent_projects',
                            lambda *, user_id: [{'path': drive_proj}])
        spawned = _stub_spawn(monkeypatch)
        _seed_conv(flask_app, drive_conv, drive_proj)
        with flask_app.app_context():
            _mark_busy(drive_conv)
            post_task(drive_proj, drive_conv, 'Cold-start epic', user_id=1)
            _clear_task_registry()
            pd.sweep_dispatch(drive_proj, user_id=1)
        _clear_task_registry()
        return len(spawned)

    # Baseline: with the drain intact, the tick spawns the cold-start task.
    assert _drive_and_count_spawned() == 1, \
        'baseline: the idle-drain must spawn a task for the cold-start epic'

    def run(neutered_dispatch):
        assert _drive_and_count_spawned(neutered_dispatch) == 0, \
            'NC: with _drain_idle_target no-opped, the cold-start kickoff is ' \
            'claimed+enqueued but NEVER spawned (the shipped bug)'

    _patch_restore(
        _DISPATCH_SRC,
        '            dispatched += 1\n'
        '            _drain_idle_target(target, user_id=user_id)',
        '            dispatched += 1\n'
        '            pass  # NC idle-drain disabled',
        run,
    )
