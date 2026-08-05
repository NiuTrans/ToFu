"""tests/test_project_brain_stale_dispatch.py — the CONSUME-TIME re-check that
stops a brain kickoff for an ALREADY-DONE epic from spawning a billed task.

Root incident (epic pt_1613ab83b1934884, 2026-07-27 20:38:01):
``on_epic_completed`` fired when a SIBLING finished an unrelated epic
(pt_e4ea42bb) and scatter-re-dispatched THREE epics in that one second
(pt_130129b5 / pt_6dd0050e / pt_78770b6) — all reading ``open`` only because
their 30-min claim lease had expired under a task that was still running
(measured lifetime 88 min). One of those kickoffs then sat in the queue and
**drained 62 minutes AFTER its epic was marked done**, spawning an Opus-5 task
that burned ¥26 re-verifying finished work (conv ms34yw0k74o2lq, task 2ef5fcaa).

Two independent holes, two independent fixes — this suite guards BOTH:

  ① ``dispatch_next_queued`` re-checks the board at CONSUME time: a kickoff
     whose ``boardTaskId`` is no longer ``open`` is DISCARDED instead of being
     rendered into a user turn. This invariant ("never checked at produce time,
     ALWAYS checked at consume time") holds regardless of lease semantics —
     which is why owner ruled OUT lease renewal as the fix.
  ② ``on_epic_completed`` advances a dispatchable epic EXACTLY once (one claim,
     one queued kickoff) however often it re-fires. NOTE: the ticket proposed
     TWO guards for this seam and BOTH were refuted on measurement — see that
     test's docstring. ``_conv_has_live_task`` breaks the dependency chain the
     flywheel guard pins down (A/B: 6/6 without, 5/6 with);
     ``_epic_already_queued`` is unreachable here and its NEUTER did not bite.
     Hole ① is therefore the whole fix; ② is a non-regression guard.

Discipline (charter): every assertion below is on the CONSEQUENCE (was a task
spawned? was the queue row consumed?), never on the shape of the implementation
— so a reasonable rewrite of either seam keeps these guards biting. Each has a
matching NEUTER negative control: revert the fix, the guard must go red.
"""

from __future__ import annotations

import json
import os
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _clear_task_registry():
    """Wipe the in-proc task registry so a stubbed-spawn task (which never
    completes and would linger as 'running') can't make a conv look busy via
    _conv_has_live_task in a LATER test. Best-effort."""
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            tasks.clear()
    except Exception:
        pass


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    """Reuse the suite-wide app fixture so the DB schema exists."""
    yield


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    _clear_task_registry()
    # post_task auto-claims + enqueues + DRAINS a kickoff through
    # on_epic_posted → project_dispatch._drain_idle_target. That drain runs
    # BEFORE each test's own create_task/spawn_task stubs land, so it spawns
    # a REAL task (live LLM attempt), whose lingering 'running' registry
    # entry then trips dispatch_next_queued's per-conv double-dispatch guard
    # (81f515e0) when the test drains manually — deterministic red. Tests in
    # this module always drive the drain themselves (or block it with a busy
    # fake), so neutralize the automatic one — same discipline as the
    # registry wipe above. test_completion_trigger_still_dispatches_to_idle_
    # target re-patches the same seam with its own recorder (applied after
    # this fixture, so it wins).
    monkeypatch.setattr(
        'lib.conversations.project_dispatch._drain_idle_target',
        lambda *a, **k: None, raising=False)
    yield
    _clear_task_registry()


def _mk_conv(conv_id: str, project_path: str):
    """Create a real conversation row routed at ``project_path``."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    settings = json.dumps({'projectPath': project_path, 'model': 'test-model'})
    db.execute(
        'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, settings, '
        'created_at, updated_at, msg_count) VALUES (?,1,?,?,?,?,?,0)',
        (conv_id, 'stale-dispatch guard', json.dumps([]), settings, now, now))
    db.commit()


def _enqueue_brain_kickoff(conv_id: str, board_task_id: str, project_path: str):
    """Enqueue a kickoff shaped EXACTLY like dispatch_epic produces one."""
    from lib.conversations.project_dispatch import BRAIN_DISPATCH_MARKER
    from lib.message_queue import KIND_WORKFLOW, enqueue_message
    return enqueue_message(
        conv_id,
        {'text': '[Project Brain — autonomous dispatch] pick up the epic.',
         BRAIN_DISPATCH_MARKER: True,
         'boardTaskId': board_task_id},
        {'model': 'test-model', 'projectPath': project_path},
        kind=KIND_WORKFLOW)


def _queue_depth(conv_id: str) -> int:
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT COUNT(*) c FROM message_queue WHERE conv_id=?',
        (conv_id,)).fetchone()
    return int(row['c'] if row else 0)


# ────────────────────────────────────────────────────────────────────
#  ① consume-time re-check: a kickoff for a DONE epic must not spawn
# ────────────────────────────────────────────────────────────────────

def test_stale_kickoff_for_done_epic_is_discarded_not_spawned(tmp_path, monkeypatch):
    """THE incident, reproduced: epic completes while its kickoff still sits in
    the queue → the drain must NOT spawn a task.

    Fixture timings mirror the real incident (charter: fixtures MUST use the
    real incident's sequence, never values chosen to pass): the kickoff is
    enqueued BEFORE the epic is marked done, exactly as at 20:38:01 → 21:01:55.
    """
    from lib.conversations.project_board import complete_task, post_task
    from lib.message_queue import dispatch_next_queued

    project = str(tmp_path / 'proj')
    os.makedirs(project, exist_ok=True)
    conv = 'stalecv_done01'
    _mk_conv(conv, project)

    posted = post_task(project, conv,
                       'epic that finishes before its kickoff drains')
    epic_id = posted['id']

    # 20:38:01 — kickoff enqueued (epic still open/claimed)
    assert _enqueue_brain_kickoff(conv, epic_id, project).get('queueId')
    assert _queue_depth(conv) == 1

    # 21:01:55 — the epic's OWN task finishes the work and marks it done
    complete_task(project, conv, epic_id)

    # Never let a real task spawn during the test; record if it was attempted.
    spawned = []

    def _fake_create_task(conv_id, api_messages, config, **kw):
        spawned.append(conv_id)
        return {'id': 'should-not-happen'}

    monkeypatch.setattr('lib.tasks_pkg.create_task', _fake_create_task,
                        raising=False)
    monkeypatch.setattr('lib.tasks_pkg.spawn_task', lambda *a, **k: None,
                        raising=False)

    # 21:03:07 — the queue drains
    task_id = dispatch_next_queued(conv)

    assert task_id is None, (
        'a kickoff for an ALREADY-DONE epic spawned a task — this is the ¥26 '
        'Opus-5 re-verification burn (conv ms34yw0k74o2lq task 2ef5fcaa)')
    assert not spawned, (
        'create_task was reached for a done-epic kickoff: %r' % (spawned,))
    assert _queue_depth(conv) == 0, (
        'the stale kickoff must be DISCARDED (not left to retry forever) — a '
        'row left behind makes _epic_already_queued block re-dispatch as well')


def test_kickoff_for_still_open_epic_still_dispatches(tmp_path, monkeypatch):
    """The complement — REQUIRED to distinguish "fixed it" from "broke the
    dispatcher". A kickoff whose epic is genuinely still open MUST spawn."""
    from lib.conversations.project_board import post_task
    from lib.message_queue import dispatch_next_queued

    project = str(tmp_path / 'proj')
    os.makedirs(project, exist_ok=True)
    conv = 'stalecv_open01'
    _mk_conv(conv, project)

    posted = post_task(project, conv, 'epic that is still open at drain time')
    epic_id = posted['id']
    assert _enqueue_brain_kickoff(conv, epic_id, project).get('queueId')

    spawned = []

    def _fake_create_task(conv_id, api_messages, config, **kw):
        spawned.append(conv_id)
        return {'id': 'tsk_open_ok'}

    monkeypatch.setattr('lib.tasks_pkg.create_task', _fake_create_task,
                        raising=False)
    monkeypatch.setattr('lib.tasks_pkg.spawn_task', lambda *a, **k: None,
                        raising=False)

    task_id = dispatch_next_queued(conv)
    assert task_id == 'tsk_open_ok', (
        'an OPEN epic\'s kickoff was discarded — the consume-time re-check is '
        'too aggressive and has broken normal brain dispatch')
    assert spawned == [conv]


def test_plain_human_message_is_never_board_gated(tmp_path, monkeypatch):
    """A normal queued HUMAN turn carries no boardTaskId and MUST be immune to
    the board re-check — a human's message is never discardable."""
    from lib.message_queue import enqueue_message
    from lib.message_queue import dispatch_next_queued

    project = str(tmp_path / 'proj')
    os.makedirs(project, exist_ok=True)
    conv = 'stalecv_human1'
    _mk_conv(conv, project)

    assert enqueue_message(
        conv, {'text': 'a real human turn'},
        {'model': 'test-model', 'projectPath': project}).get('queueId')

    spawned = []
    monkeypatch.setattr(
        'lib.tasks_pkg.create_task',
        lambda c, m, cfg, **kw: (spawned.append(c), {'id': 'tsk_human'})[1],
        raising=False)
    monkeypatch.setattr('lib.tasks_pkg.spawn_task', lambda *a, **k: None,
                        raising=False)

    assert dispatch_next_queued(conv) == 'tsk_human'
    assert spawned == [conv]


def test_board_lookup_failure_fails_OPEN_and_still_dispatches(tmp_path, monkeypatch):
    """If the board lookup itself fails, the kickoff MUST still dispatch.

    The accepted failure mode is "a stale kickoff occasionally slips through"
    (recoverable, costs one task). The UNACCEPTABLE one is "brain dispatch
    silently stops" — invisible, and it stalls every autonomous workstream. This
    test exists because the fail-open branch is otherwise unexercised: inverting
    it to fail-closed left the whole suite green (NEUTER A2 did not bite), i.e.
    the safety property was unguarded.
    """
    from lib.conversations.project_board import post_task
    from lib.message_queue import dispatch_next_queued

    project = str(tmp_path / 'proj')
    os.makedirs(project, exist_ok=True)
    conv = 'stalecv_failopen'
    _mk_conv(conv, project)

    posted = post_task(project, conv, 'epic whose board read will blow up')
    assert _enqueue_brain_kickoff(conv, posted['id'], project).get('queueId')

    def _boom(*a, **k):
        raise RuntimeError('simulated board read failure')

    monkeypatch.setattr('lib.conversations.project_board.read_board', _boom,
                        raising=False)

    spawned = []
    monkeypatch.setattr(
        'lib.tasks_pkg.create_task',
        lambda c, m, cfg, **kw: (spawned.append(c), {'id': 'tsk_failopen'})[1],
        raising=False)
    monkeypatch.setattr('lib.tasks_pkg.spawn_task', lambda *a, **k: None,
                        raising=False)

    assert dispatch_next_queued(conv) == 'tsk_failopen', (
        'a board-lookup failure SWALLOWED a legitimate kickoff — the re-check '
        'must fail OPEN, or one DB hiccup silently halts all brain dispatch')
    assert spawned == [conv]


# ────────────────────────────────────────────────────────────────────
#  ② on_epic_completed must not stack a DUPLICATE kickoff
# ────────────────────────────────────────────────────────────────────

def test_completion_trigger_advances_chain_without_duplicating(tmp_path, monkeypatch):
    """``on_epic_completed`` must advance an epic EXACTLY once — one claim, one
    queued kickoff — no matter how many times it re-fires in the same instant.

    ★ Two guards the ticket proposed for this seam were REJECTED on measurement
    (recorded here so nobody re-adds them):

      • ``_conv_has_live_task`` breaks the dependency chain this seam exists
        for. When A completes, dependent B must be claimed + enqueued *while
        the conv is still busy finishing A*, then drained by the post-task
        queue chain. ``test_project_brain_integration::
        test_full_autonomous_flywheel`` pins that down and goes RED with the
        check in place (A/B measured: 6/6 without, 5/6 with).
      • ``_epic_already_queued`` is unreachable here — ``dispatch_epic`` claims
        the epic and ``select_dispatchable`` excludes ``claimed``, so its
        NEUTER did not bite. A guard that cannot fail is not a guard.

    So the scatter's HARM is stopped at CONSUME time instead (the discard tested
    above), and what this test protects is that the chain still advances once
    and only once.
    """
    from lib.conversations.project_board import post_task, read_board
    from lib.conversations.project_dispatch import on_epic_completed

    project = str(tmp_path / 'proj')
    os.makedirs(project, exist_ok=True)
    conv = 'stalecv_busy01'
    _mk_conv(conv, project)

    # Busy BEFORE posting: post_task's own on_epic_posted seam would otherwise
    # drain the queue immediately. This reproduces the incident shape — a conv
    # already working when the epic becomes dispatchable — and keeps the queued
    # row observable instead of instantly consumed.
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks['live-task-busy'] = {
            'id': 'live-task-busy', 'convId': conv,
            'status': 'running', 'aborted': False,
        }
    try:
        posted = post_task(project, conv, 'epic advanced by the completion seam')
        epic_id = posted['id']
        first = on_epic_completed(project, completed_conv_id=conv)
        depth_after_first = _queue_depth(conv)
        # The real scatter fired several epics inside ONE second; re-entrant
        # calls must not stack further rows for the same epic.
        again = (on_epic_completed(project, completed_conv_id=conv)
                 + on_epic_completed(project, completed_conv_id=conv))
        depth_after_repeat = _queue_depth(conv)
    finally:
        with tasks_lock:
            tasks.pop('live-task-busy', None)

    assert first == 1 and depth_after_first == 1, (
        'the completion seam did not advance the epic (claim + ONE queued '
        'kickoff) — the dependency chain the event channel relies on is broken')
    assert again == 0 and depth_after_repeat == depth_after_first, (
        're-firing on_epic_completed stacked another kickoff for the same epic '
        '— each extra row drains into another billed task (20:38:01 scatter)')
    epic = next(t for t in read_board(project)['tasks'] if t['id'] == epic_id)
    assert epic['status'] == 'claimed' and epic['owner_conv_id'] == conv, (
        'the epic was queued without being claimed to its target — a second '
        'dispatcher pass would then select it again')


def test_completion_trigger_still_dispatches_to_idle_target(tmp_path, monkeypatch):
    """The complement: an IDLE target must still receive the dependent epic —
    otherwise the busy check has silently disabled the completion trigger."""
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import on_epic_completed

    project = str(tmp_path / 'proj')
    os.makedirs(project, exist_ok=True)
    conv = 'stalecv_idle01'
    _mk_conv(conv, project)

    drained = []
    monkeypatch.setattr(
        'lib.conversations.project_dispatch._drain_idle_target',
        lambda c: (drained.append(c), 'tsk_stub')[1], raising=False)

    # An IDLE target must receive the epic. Either seam may be the one that
    # does it — ``post_task``→``on_epic_posted`` sees it first (event channel),
    # ``on_epic_completed`` covers the dependency case. NEITHER firing means the
    # busy guard is over-broad and has killed the trigger.
    posted = post_task(project, conv, 'epic routed to an idle conv')
    epic_id = posted['id']
    dispatched = on_epic_completed(project, completed_conv_id=conv)

    from lib.conversations.project_board import read_board
    epic = next(t for t in read_board(project)['tasks'] if t['id'] == epic_id)
    assert dispatched == 1 or epic['status'] == 'claimed', (
        'an idle target got NO dispatch from either seam — the busy guard is '
        'over-broad and has killed the trigger the event channel depends on')
    assert drained, (
        'the epic was claimed but never drained into a turn — the cold-start '
        'drain is what makes dispatch self-starting rather than only claiming')
