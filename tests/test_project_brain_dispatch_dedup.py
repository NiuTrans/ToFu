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

import json
import os
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_DISPATCH_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_dispatch.py')
_QUEUE_SRC = os.path.join(ROOT, 'lib', 'message_queue.py')


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
    _clear_task_registry()
    yield
    _clear_task_registry()


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _clear_task_registry():
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            tasks.clear()
    except Exception as e:  # pragma: no cover - registry absent in some runs
        print('registry clear skipped: %s' % e)


def _expire_lease(flask_app, project_path: str, task_id: str):
    """Force the epic's claim lease to have EXPIRED — the incident's shape.

    This is the step the pre-existing duplicate guard never took, which is why
    it read as green while production stacked ten rows.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE project_tasks SET lease_expires_at=1 '
                   'WHERE id=? AND project_path=?', (task_id, project_path))
        db.commit()


def _busy(conv_id: str):
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks['live-' + conv_id] = {
            'id': 'live-' + conv_id, 'convId': conv_id,
            'status': 'running', 'aborted': False,
        }


def _kickoff_rows(flask_app, conv_id: str) -> list[dict]:
    """Every queued brain kickoff on ``conv_id``, decoded."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import KIND_WORKFLOW
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT payload FROM message_queue WHERE conv_id=? AND kind=?',
            (conv_id, KIND_WORKFLOW)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r['payload'] or '{}'))
        except (TypeError, ValueError):
            out.append({})
    return out


def _mk_conv(flask_app, conv_id: str, project_path: str):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        settings = json.dumps({'projectPath': project_path, 'model': 'test-model'})
        db.execute('DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.execute(
            'INSERT INTO conversations (id, user_id, title, messages, settings, '
            'created_at, updated_at, msg_count) VALUES (?,1,?,?,?,?,?,0)',
            (conv_id, 'dispatch dedup guard', json.dumps([]), settings, now, now))
        db.commit()


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
        epic_id = post_task(project, conv, 'epic whose target stays busy for hours')['id']
        first = on_epic_completed(project, completed_conv_id=conv)
    rows_after_first = _kickoff_rows(flask_app, conv)

    assert first == 1 and len(rows_after_first) == 1, (
        'the completion seam failed to advance the epic at all — the dependency '
        'chain the event channel relies on is broken')

    # ── 30 minutes pass under a still-running task: the claim lapses. ──
    _expire_lease(flask_app, project, epic_id)
    with flask_app.app_context():
        board = read_board(project)
        effective = next(t for t in board['tasks'] if t['id'] == epic_id)
    assert effective['status'] == 'open', (
        'fixture precondition: an expired claim must read back as open — if it '
        'does not, this test is no longer reproducing the incident shape')

    # A sibling finishes an unrelated epic → the seam re-fires (twice, as the
    # real 20:38:01 scatter did within one second).
    with flask_app.app_context():
        again = (on_epic_completed(project, completed_conv_id=conv)
                 + on_epic_completed(project, completed_conv_id=conv))
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
        dep = post_task(project, conv, 'dependency')['id']
        dependent = post_task(project, conv, 'dependent work', depends_on=[dep])['id']
        complete_task(project, conv, dep)
        on_epic_completed(project, completed_conv_id=conv)

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
        a = post_task(project, conv, 'epic A')['id']
        b = post_task(project, conv, 'epic B')['id']
        on_epic_completed(project, completed_conv_id=conv)

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
            kind=KIND_WORKFLOW)


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
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import enqueue_message

    project = '/dedup/human'
    conv = 'cDEDUP_HUMAN'
    _mk_conv(flask_app, conv, project)

    with flask_app.app_context():
        enqueue_message(conv, {'text': 'same thing'},
                        {'model': 'test-model', 'projectPath': project})
        enqueue_message(conv, {'text': 'same thing'},
                        {'model': 'test-model', 'projectPath': project})
        db = get_thread_db(DOMAIN_CHAT)
        n = db.execute('SELECT COUNT(*) c FROM message_queue WHERE conv_id=?',
                       (conv,)).fetchone()['c']
    assert int(n) == 2, (
        'a human turn was swallowed by the board dedup — only brain kickoffs '
        'carry a boardTaskId and only they may collapse')


def test_peer_messages_are_never_deduped(flask_app):
    """Two distinct peer messages are two turns — they carry no boardTaskId."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import KIND_PEER_MSG, enqueue_message

    project = '/dedup/peer'
    conv = 'cDEDUP_PEER'
    _mk_conv(flask_app, conv, project)

    with flask_app.app_context():
        enqueue_message(conv, {'text': 'peer one', '_peerMessage': True},
                        {'model': 'test-model', 'projectPath': project},
                        kind=KIND_PEER_MSG)
        enqueue_message(conv, {'text': 'peer two', '_peerMessage': True},
                        {'model': 'test-model', 'projectPath': project},
                        kind=KIND_PEER_MSG)
        db = get_thread_db(DOMAIN_CHAT)
        n = db.execute('SELECT COUNT(*) c FROM message_queue WHERE conv_id=? '
                       'AND kind=?', (conv, KIND_PEER_MSG)).fetchone()['c']
    assert int(n) == 2, 'peer messages must never be collapsed'


# ════════════════════════════════════════════════════════════════════
#  Source-level NEGATIVE CONTROLS
# ════════════════════════════════════════════════════════════════════

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_A_completion_seam_without_queued_probe_restacks(flask_app):
    """NC-A: drop BOTH the epic-scoped probe (A) AND the enqueue dedup floor
    (B) → the lease-expiry shape stacks a second row again.

    Both must be neutered together: with only A removed, B's structural dedup
    still collapses the second enqueue onto the first row, so the assertion
    would never bite. The combined neuter isolates A's necessity.
    """
    from lib.conversations.project_board import post_task
    from tests._nc_harness import neutered_source

    project = '/nc_a/leaseexp'
    conv = 'cNC_A'
    _mk_conv(flask_app, conv, project)
    _busy(conv)

    with neutered_source(
        _DISPATCH_SRC,
        ("            if _epic_already_queued(target, epic.get('id', '')):\n"
         "                continue"),
        "            if False:  # NC-A (queued probe disabled)\n                continue",
    ):
        with neutered_source(
            _QUEUE_SRC,
            "    existing = _existing_board_kickoff(db, conv_id, message_data, kind)\n",
            "    existing = None  # NC-A+B (dedup disabled)\n",
        ):
            import lib.conversations.project_dispatch as pd
            with flask_app.app_context():
                epic_id = post_task(project, conv, 'epic')['id']
                pd.on_epic_completed(project, completed_conv_id=conv)
            _expire_lease(flask_app, project, epic_id)
            with flask_app.app_context():
                pd.on_epic_completed(project, completed_conv_id=conv)

    mine = [p for p in _kickoff_rows(flask_app, conv)
            if p.get('boardTaskId') == epic_id]
    assert len(mine) >= 2, (
        'NC-A: without the queued probe the seam must re-stack after lease '
        'expiry (got %d rows)' % len(mine))


def test_NC_B_enqueue_without_dedup_stacks_rows(flask_app):
    """NC-B: drop the structural dedup in ``enqueue_message`` → repeated
    kickoffs for one epic stack again."""
    from tests._nc_harness import neutered_source

    project = '/nc_b/enq'
    conv = 'cNC_B'
    _mk_conv(flask_app, conv, project)
    from lib.conversations.project_dispatch import BRAIN_DISPATCH_MARKER
    from lib.message_queue import KIND_WORKFLOW

    with neutered_source(
        _QUEUE_SRC,
        "    existing = _existing_board_kickoff(db, conv_id, message_data, kind)\n",
        "    existing = None  # NC-B (dedup disabled)\n",
    ) as mq:
        with flask_app.app_context():
            for _ in range(3):
                mq.enqueue_message(
                    conv,
                    {'text': 'kickoff', BRAIN_DISPATCH_MARKER: True,
                     'boardTaskId': 'pt_nc_b_epic'},
                    {'model': 'test-model', 'projectPath': project},
                    kind=KIND_WORKFLOW)

    rows = _kickoff_rows(flask_app, conv)
    assert len(rows) >= 3, (
        'NC-B: without the dedup floor three enqueues must produce three '
        'rows (got %d)' % len(rows))
