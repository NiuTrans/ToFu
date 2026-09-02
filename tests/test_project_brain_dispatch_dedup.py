"""tests/test_project_brain_dispatch_dedup.py — one epic, one queued kickoff,
however many times the completion seam re-fires and however long the target
conversation stays busy.

Incident (2026-07-28, conv ms4b67gmthqc17): the queue held **11 rows** while
the board had only **4** distinct epics routed there —
``pt_3c7f29f8bfc3425d`` ×3, ``pt_c2e59181e4c14b8d`` ×3,
``pt_2c613da17eac43c5`` ×2, ``pt_c1e3318ac6994573`` ×2 (measured dispatch
timestamps 17:48:42 / 19:01:48 / 19:42:13 / 20:20:26). Every one of the ten
came from ``on_epic_completed`` — the heartbeat sweep logged
``heartbeat sweep dispatched`` **zero** times, because the sweep DOES carry
``_conv_has_live_task or _epic_already_queued``.

Root cause — a refuted unreachability argument. ``on_epic_completed`` carried a
comment arguing ``_epic_already_queued`` was unreachable there ("dispatch_epic
claims the epic and select_dispatchable excludes claimed"). That holds only
while the claim LIVES. The claim is a 30-minute soft lease
(``DEFAULT_LEASE_TTL_MS``) and the target's task ran for hours, so at every
lease expiry the board read the epic ``open`` again, ``select_dispatchable``
re-selected it, and the seam stacked another kickoff onto a conversation that
had never drained the first. The guard was not unreachable — it was reachable
once every 30 minutes, which is why the earlier NEUTER (run against a
LIVE-lease fixture) failed to bite.

Two independent fixes, both guarded here:

  A. ``on_epic_completed`` consults ``_epic_already_queued`` — the epic-scoped
     probe only. ``_conv_has_live_task`` stays OUT of this seam on purpose:
     the dependency chain requires enqueuing into a still-busy conv (pinned by
     ``test_project_brain_integration::test_full_autonomous_flywheel``).
  B. ``enqueue_message`` is IDEMPOTENT per ``(conv_id, boardTaskId)`` — the
     structural floor. Any present or future producer that re-dispatches the
     same epic collapses onto the existing row instead of stacking, so the
     invariant does not depend on every call site remembering to probe.

Assertions are on the CONSEQUENCE (how many rows exist / how many would drain),
never on the shape of the implementation. Each fix has a source-level NEUTER.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)

@pytest.fixture(autouse=True)
def _clean(chat_sidecar):
    _clear_task_registry()
    yield
    _clear_task_registry()


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _clear_task_registry():
    try:
        from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
        with tasks_lock:
            tasks.clear()
    except Exception as e:  # pragma: no cover - registry absent in some runs
        print('registry clear skipped: %s' % e)


def _expire_lease(flask_app, project_path: str, task_id: str):
    """Force the epic's claim lease to have EXPIRED — the incident's shape.

    This is the step the pre-existing duplicate guard never took, which is why
    it read as green while production stacked ten rows.
    """
    from lib.conversations.project_board import read_board
    from lib.storage import get_storage_client
    from tests._seed import seed_board_task
    with flask_app.app_context():
        task = next(
            item for item in read_board(
                project_path, user_id=TEST_OWNER_USER_ID)['tasks']
            if item['id'] == task_id)
        deleted = get_storage_client(write=True).command(
            'board.mutate', {
                'action': 'delete', 'project_path': project_path,
                'user_id': TEST_OWNER_USER_ID, 'task_id': task_id,
            }, f'expire-fixture-delete:{task_id}')
        assert deleted.get('ok'), deleted
        seed_board_task(
            task_id, project_path, user_id=TEST_OWNER_USER_ID,
            title=task['title'], status='claimed',
            owner_conv_id=task.get('owner_conv_id') or '',
            lease_expires_at=1,
            created_by_conv=task.get('created_by_conv') or '',
            depends_on=task.get('depends_on') or [],
            kind=task.get('kind') or '', dispatched=1,
            write_set=task.get('write_set') or [])


def _busy(conv_id: str):
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        tasks['live-' + conv_id] = {
            'id': 'live-' + conv_id, 'convId': conv_id,
            '_userId': TEST_OWNER_USER_ID,
            'status': 'running', 'aborted': False,
        }


def _kickoff_rows(flask_app, conv_id: str) -> list[dict]:
    """Every queued brain kickoff on ``conv_id``, decoded."""
    from lib.storage import get_storage_client
    from lib.message_queue import KIND_WORKFLOW
    with flask_app.app_context():
        rows = get_storage_client().query(
            'queue.list', {
                'conv_id': conv_id, 'user_id': TEST_OWNER_USER_ID,
            }) or []
    return [dict(row.get('payload') or {}) for row in rows
            if row.get('kind') == KIND_WORKFLOW]


def _mk_conv(flask_app, conv_id: str, project_path: str):
    from tests._seed import delete_conversation, seed_conversation
    with flask_app.app_context():
        delete_conversation(conv_id, user_id=TEST_OWNER_USER_ID)
        seed_conversation(
            conv_id, user_id=TEST_OWNER_USER_ID,
            title='dispatch dedup guard',
            settings={'projectPath': project_path, 'model': 'test-model'})


# ════════════════════════════════════════════════════════════════════
#  A — the completion seam across a LEASE EXPIRY (the incident)
# ════════════════════════════════════════════════════════════════════

def test_completion_seam_does_not_restack_after_lease_expiry(flask_app):
    """THE incident: target busy for hours, claim lease expires, a sibling
    completes something → the seam must NOT stack a second kickoff.

    Timings mirror production: the first dispatch at 17:48, the lease TTL is
    30 min, the next completion fired at 19:01 (73 min later) — i.e. always
    past expiry. We express that by expiring the lease outright rather than
    sleeping.
    """
    from lib.conversations.project_board import post_task, read_board
    from lib.conversations.project_dispatch import on_epic_completed

    project = '/dedup/leaseexp'
    conv = 'cDEDUP_LEASE'
    _mk_conv(flask_app, conv, project)
    _busy(conv)   # busy BEFORE posting → the kickoff stays observable

    with flask_app.app_context():
        epic_id = post_task(project, conv, 'epic whose target stays busy for hours', user_id=TEST_OWNER_USER_ID)['id']
        first = on_epic_completed(project, completed_conv_id=conv, user_id=TEST_OWNER_USER_ID)
    rows_after_first = _kickoff_rows(flask_app, conv)

    assert first == 1 and len(rows_after_first) == 1, (
        'the completion seam failed to advance the epic at all — the dependency '
        'chain the event channel relies on is broken')

    # ── 30 minutes pass under a still-running task: the claim lapses. ──
    _expire_lease(flask_app, project, epic_id)
    with flask_app.app_context():
        board = read_board(project, user_id=TEST_OWNER_USER_ID)
        effective = next(t for t in board['tasks'] if t['id'] == epic_id)
    assert effective['status'] == 'open', (
        'fixture precondition: an expired claim must read back as open — if it '
        'does not, this test is no longer reproducing the incident shape')

    # A sibling finishes an unrelated epic → the seam re-fires (twice, as the
    # real 20:38:01 scatter did within one second).
    with flask_app.app_context():
        again = (on_epic_completed(project, completed_conv_id=conv, user_id=TEST_OWNER_USER_ID)
                 + on_epic_completed(project, completed_conv_id=conv, user_id=TEST_OWNER_USER_ID))
    rows_after = _kickoff_rows(flask_app, conv)
    mine = [p for p in rows_after if p.get('boardTaskId') == epic_id]

    assert len(mine) == 1, (
        'the completion seam stacked %d kickoffs for ONE epic after the claim '
        'lease expired — this is the ms4b67gmthqc17 shape (10 rows for 4 '
        'epics). The "unreachable guard" argument only holds while the claim '
        'LIVES; it lapses every 30 min under a long task.' % len(mine))
    assert again == 0, (
        'on_epic_completed reported a fresh dispatch for an epic that already '
        'had an undrained kickoff — the queue depth the user sees becomes a lie')


def test_completion_seam_still_enqueues_into_a_BUSY_conv(flask_app):
    """The complement that keeps the fix honest: the dependency chain REQUIRES
    enqueuing into a still-busy conversation.

    If the fix had reached for ``_conv_has_live_task`` instead of the
    epic-scoped probe, this goes red — and so does
    ``test_full_autonomous_flywheel``.
    """
    from lib.conversations.project_board import complete_task, post_task
    from lib.conversations.project_dispatch import on_epic_completed

    project = '/dedup/chain'
    conv = 'cDEDUP_CHAIN'
    _mk_conv(flask_app, conv, project)
    _busy(conv)

    with flask_app.app_context():
        dep = post_task(project, conv, 'dependency', user_id=TEST_OWNER_USER_ID)['id']
        dependent = post_task(project, conv, 'dependent work', depends_on=[dep], user_id=TEST_OWNER_USER_ID)['id']
        complete_task(project, conv, dep, user_id=TEST_OWNER_USER_ID)
        on_epic_completed(project, completed_conv_id=conv, user_id=TEST_OWNER_USER_ID)

    ids = [p.get('boardTaskId') for p in _kickoff_rows(flask_app, conv)]
    assert dependent in ids, (
        'a dependent epic was NOT enqueued because its target conv was busy — '
        'the busy conv is exactly when the chain must enqueue (the post-task '
        'drain starts it); this guard exists so nobody "fixes" duplication by '
        'adding _conv_has_live_task to this seam')


def test_distinct_epics_still_each_get_a_kickoff(flask_app):
    """Anti-over-fix: dedup is per EPIC, not per conversation. Two different
    open epics routed to the same conv must both be enqueued."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import on_epic_completed

    project = '/dedup/twoepics'
    conv = 'cDEDUP_TWO'
    _mk_conv(flask_app, conv, project)
    _busy(conv)

    with flask_app.app_context():
        a = post_task(project, conv, 'epic A', user_id=TEST_OWNER_USER_ID)['id']
        b = post_task(project, conv, 'epic B', user_id=TEST_OWNER_USER_ID)['id']
        on_epic_completed(project, completed_conv_id=conv, user_id=TEST_OWNER_USER_ID)

    ids = {p.get('boardTaskId') for p in _kickoff_rows(flask_app, conv)}
    assert {a, b} <= ids, (
        'dedup collapsed DIFFERENT epics onto one row (%r) — the guard must be '
        'keyed on boardTaskId, never on the conversation alone' % (ids,))


# ════════════════════════════════════════════════════════════════════
#  B — enqueue_message is idempotent per (conv_id, boardTaskId)
# ════════════════════════════════════════════════════════════════════

def _enqueue_kickoff(flask_app, conv_id: str, board_task_id: str, project: str):
    from lib.conversations.project_dispatch import BRAIN_DISPATCH_MARKER
    from lib.message_queue import KIND_WORKFLOW, enqueue_message
    with flask_app.app_context():
        return enqueue_message(
            conv_id,
            {'text': '[Project Brain — autonomous dispatch] pick up the epic.',
             BRAIN_DISPATCH_MARKER: True,
             'boardTaskId': board_task_id},
            {'model': 'test-model', 'projectPath': project},
            kind=KIND_WORKFLOW, user_id=TEST_OWNER_USER_ID)


def test_enqueue_is_idempotent_per_board_task(flask_app):
    """The STRUCTURAL floor: whoever calls it, a second kickoff for the same
    epic on the same conv collapses onto the existing row.

    This is what makes the invariant independent of call sites — the incident
    happened because ONE producer forgot to probe.
    """
    project = '/dedup/enq'
    conv = 'cDEDUP_ENQ'
    _mk_conv(flask_app, conv, project)

    first = _enqueue_kickoff(flask_app, conv, 'pt_dedup_epic', project)
    second = _enqueue_kickoff(flask_app, conv, 'pt_dedup_epic', project)
    third = _enqueue_kickoff(flask_app, conv, 'pt_dedup_epic', project)

    rows = _kickoff_rows(flask_app, conv)
    assert len(rows) == 1, (
        'enqueue_message stacked %d rows for ONE epic — the structural dedup '
        'floor is missing, so any producer that re-dispatches (lease expiry, a '
        'future seam, a restart replay) re-inflates the queue' % len(rows))
    assert first.get('queueId'), 'the first enqueue must really insert'
    assert second['queueId'] == first['queueId'] == third['queueId'], (
        'a collapsed enqueue must report the EXISTING row id, not a fresh uuid '
        'that no row carries — a caller storing it would hold a dangling id')


def test_enqueue_dedup_is_scoped_to_the_same_conversation(flask_app):
    """Migration must still work: the SAME epic enqueued on a DIFFERENT conv is
    a genuinely different row (that is what ``migrate_epic`` produces)."""
    project = '/dedup/scope'
    _mk_conv(flask_app, 'cSCOPE_A', project)
    _mk_conv(flask_app, 'cSCOPE_B', project)

    _enqueue_kickoff(flask_app, 'cSCOPE_A', 'pt_shared_epic', project)
    _enqueue_kickoff(flask_app, 'cSCOPE_B', 'pt_shared_epic', project)

    assert len(_kickoff_rows(flask_app, 'cSCOPE_A')) == 1
    assert len(_kickoff_rows(flask_app, 'cSCOPE_B')) == 1, (
        'dedup leaked ACROSS conversations — an epic migrated to an idle '
        'sibling would silently never be enqueued there')


def test_human_turns_are_never_deduped(flask_app):
    """A human can send the same text twice and MUST get two turns. Only rows
    carrying a ``boardTaskId`` are dedup-able."""
    from lib.message_queue import enqueue_message, get_queue

    project = '/dedup/human'
    conv = 'cDEDUP_HUMAN'
    _mk_conv(flask_app, conv, project)

    with flask_app.app_context():
        enqueue_message(conv, {'text': 'same thing'},
                        {'model': 'test-model', 'projectPath': project}, user_id=TEST_OWNER_USER_ID)
        enqueue_message(conv, {'text': 'same thing'},
                        {'model': 'test-model', 'projectPath': project}, user_id=TEST_OWNER_USER_ID)
        n = len(get_queue(conv, user_id=TEST_OWNER_USER_ID))
    assert int(n) == 2, (
        'a human turn was swallowed by the board dedup — only brain kickoffs '
        'carry a boardTaskId and only they may collapse')


def test_peer_messages_are_never_deduped(flask_app):
    """Two distinct peer messages are two turns — they carry no boardTaskId."""
    from lib.message_queue import KIND_PEER_MSG, enqueue_message, get_queue

    project = '/dedup/peer'
    conv = 'cDEDUP_PEER'
    _mk_conv(flask_app, conv, project)

    with flask_app.app_context():
        enqueue_message(conv, {'text': 'peer one', '_peerMessage': True},
                        {'model': 'test-model', 'projectPath': project},
                        kind=KIND_PEER_MSG, user_id=TEST_OWNER_USER_ID)
        enqueue_message(conv, {'text': 'peer two', '_peerMessage': True},
                        {'model': 'test-model', 'projectPath': project},
                        kind=KIND_PEER_MSG, user_id=TEST_OWNER_USER_ID)
        n = len([
            row for row in get_queue(conv, user_id=TEST_OWNER_USER_ID)
            if row.get('kind') == KIND_PEER_MSG
        ])
    assert int(n) == 2, 'peer messages must never be collapsed'


def test_completion_seam_queued_probe_is_load_bearing(flask_app, monkeypatch):
    """If the producer-level probe lies, the seam attempts a second dispatch.

    The real Sidecar still supplies the structural de-dup floor; replacing the
    historical in-process source neuter with this boundary test keeps the
    producer guard observable after queue authority moved out of process.
    """
    from lib.conversations.project_board import post_task
    import lib.conversations.project_dispatch as pd

    project = '/nc_a/leaseexp'
    conv = 'cNC_A'
    _mk_conv(flask_app, conv, project)
    _busy(conv)
    with flask_app.app_context():
        epic_id = post_task(
            project, conv, 'epic', user_id=TEST_OWNER_USER_ID)['id']
        assert pd.on_epic_completed(
            project, completed_conv_id=conv,
            user_id=TEST_OWNER_USER_ID) == 1
    _expire_lease(flask_app, project, epic_id)

    attempted = []
    monkeypatch.setattr(
        pd, '_epic_already_queued', lambda *_a, **_k: False)
    monkeypatch.setattr(
        pd, 'dispatch_epic',
        lambda _p, epic, _target, **_k:
        attempted.append(epic['id']) or {'ok': True})
    monkeypatch.setattr(pd, '_drain_idle_target', lambda *_a, **_k: None)
    with flask_app.app_context():
        assert pd.on_epic_completed(
            project, completed_conv_id=conv,
            user_id=TEST_OWNER_USER_ID) == 1
    assert attempted == [epic_id]


def test_concurrent_enqueue_keeps_one_structural_kickoff(flask_app):
    """Concurrent producers converge on one Sidecar row and one queue id."""
    project = '/dedup/concurrent'
    conv = 'cDEDUP_CONCURRENT'
    _mk_conv(flask_app, conv, project)
    barrier = threading.Barrier(4)
    results = []
    failures = []

    def enqueue():
        try:
            barrier.wait()
            results.append(_enqueue_kickoff(
                flask_app, conv, 'pt_concurrent_epic', project))
        except Exception as error:  # pragma: no cover - asserted below
            failures.append(error)

    threads = [threading.Thread(target=enqueue) for _ in range(3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(10)
    assert not failures
    assert len(results) == 3
    assert len({result['queueId'] for result in results}) == 1
    assert len(_kickoff_rows(flask_app, conv)) == 1
