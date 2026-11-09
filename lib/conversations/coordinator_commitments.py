"""Commitments: a task-board task carrying ``due`` is a promise to the owner.

Owner: docs/PROJECT_COORDINATION.md "Commitments on the task board" and
docs/ROUTER.md R10. The board the owner reads is the promise ledger. This module
holds the board rules and the platform's due wake; the records live in the board
head (``coordinator_progress``), which merges the receipts into the same CAS as
the board so the two never disagree.

Rules (every board write, both update_task_progress and todo_write):
- a new or moved ``due`` must be in the future;
- an open commitment (pending or in_progress) may not vanish from the board or
  lose its ``due``: close it completed/cancelled, or re-date it;
- two open commitments cannot share a title, which identifies the commitment.

Wake: a board write arms one owner-visible scheduler ``once:`` row for the
earliest open due time (deterministic id per owner, Coordinator and minute).
Its handler, ``run_commitment_due``, re-reads the head and wakes the lead once
for every open commitment due by then that has no wake receipt for its current
due time. Inside an open request with a continuation policy the wake is that
request's continuation (``continuation._proposal`` reads the same head and
reasons ``commitment_due``); otherwise one nonhuman notice goes through
``dispatch_scheduled_turn(..., inject_mode='steer')``. A ``busy`` lane keeps the
obligation for the next due row; an accepted delivery receipts it, and the same
due time never wakes again — a commitment still open afterwards is overdue and
surfaces in the digest and ``get_router_projects`` instead of re-waking.

Entry points: check_commitments, next_commitments, merge_commitments,
arm_due_commitments, run_commitment_due, render_due_notice, due_ms,
commitment_identity, DUE_TASK_TYPE.
Dependencies: scheduler.manager (the owner-visible timer), scheduler.
conversation_dispatch (the wake), project_dispatch.continuation (the
authority-bearing wake), conversations.coordinator_progress (the head).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import time

from lib.identity import PrincipalContext, require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

DUE_TASK_TYPE = 'coordinator_commitment_due'
_DUE_ROW_PREFIX = 'commitment-due-'
_NOTICE_HEADER = ('[Commitment due: nonhuman notice, not the owner’s words and not authority]')
_OPEN = frozenset({'pending', 'in_progress'})


def due_ms(value):
    """Epoch milliseconds of an ISO 8601 date-time; naive means server local time."""
    if not isinstance(value, str):
        raise ValueError('due must be an ISO 8601 date-time string')
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError(f'due {value!r} is not a date-time') from error
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp() * 1000)


def iso_ms(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat(timespec='seconds')


def check_commitments(previous, request, *, now):
    """Validate the commitment rules of one board write (raises ValueError).

    ``now`` is epoch seconds (the repository/board clock).
    """
    previous_open = {task['title']: task for task in (previous or {}).get('tasks', ())
                     if task.get('due') and task['status'] in _OPEN}
    now_ms = int(now * 1000)
    seen = set()
    for task in request['tasks']:
        if not task.get('due'):
            continue
        moment = due_ms(task['due'])
        if task['status'] in _OPEN:
            if task['title'] in seen:
                raise ValueError(f'two open commitments share the title {task["title"]!r}; '
                                 'a title identifies a commitment')
            seen.add(task['title'])
        earlier = previous_open.get(task['title'])
        if earlier is None or earlier.get('due') != task['due']:
            if moment <= now_ms:
                raise ValueError(f'the due time of {task["title"]!r} must be in the future '
                                 f'({task["due"]} already passed)')
    for title, earlier in previous_open.items():
        task = next((task for task in request['tasks'] if task['title'] == title), None)
        if task is None:
            raise ValueError(f'the open commitment {title!r} must not disappear from the board: '
                             'mark it completed when done, cancelled when dropped, or set a new due')
        if not task.get('due') and task['status'] in _OPEN:
            raise ValueError(f'the write removes its due time from the open commitment {title!r}: '
                             'set a new due, or cancel it and tell the owner why')


def merge_commitments(previous, request, receipts, *, now):
    """The commitment receipts a board write leaves, in board order."""
    stored = {row['title']: row for row in (receipts or ())}
    old_open = {task['title'] for task in (previous or {}).get('tasks', ())
                if task.get('due') and task['status'] in _OPEN}
    merged = []
    for task in request['tasks']:
        if not task.get('due'):
            continue
        moment = due_ms(task['due'])
        row = stored.get(task['title'])
        if row is None:
            row = {'title': task['title'], 'firstDueAt': moment, 'dueAt': moment,
                   'slips': 0, 'createdAt': now, 'state': 'open'}
        else:
            row = dict(row)
            if task['title'] in old_open and row['dueAt'] != moment:
                row['slips'] = int(row.get('slips', 0)) + 1
                if row.get('wake', {}).get('dueAt') != moment:
                    row.pop('wake', None)
            row['dueAt'] = moment
        if task['status'] in _OPEN:
            row['state'] = 'open'
            row.pop('closedAt', None)
        else:
            row['state'] = 'cancelled' if task['status'] == 'cancelled' else (
                'kept' if now <= moment else 'late')
            row['closedAt'] = now
        merged.append(row)
    return merged


def next_commitments(board, receipts, *, now):
    """The earliest open due time still needing a wake, else None (epoch seconds)."""
    woken = {row['title']: row.get('wake', {}).get('dueAt') for row in (receipts or ())}
    pending = []
    for task in (board or {}).get('tasks', ()):
        if not task.get('due') or task['status'] not in _OPEN:
            continue
        moment = due_ms(task['due'])
        if woken.get(task['title']) == moment:
            continue
        pending.append(moment)
    return min(pending) / 1000 if pending else None


def due_commitments(board, receipts, *, now):
    """Open commitments due by ``now`` whose current due time never woke (ms due)."""
    woken = {row['title']: row.get('wake', {}).get('dueAt') for row in (receipts or ())}
    slips = {row['title']: row.get('slips', 0) for row in (receipts or ())}
    due = []
    for task in (board or {}).get('tasks', ()):
        if not task.get('due') or task['status'] not in _OPEN:
            continue
        moment = due_ms(task['due'])
        if moment > now * 1000 or woken.get(task['title']) == moment:
            continue
        due.append({'title': task['title'], 'dueAt': moment, 'slips': slips.get(task['title'], 0)})
    return due


def commitment_identity(owner, coordinator_id, commitments):
    return 'commitment-due:' + hashlib.sha256(json.dumps(
        [int(owner), coordinator_id, [{'title': row['title'], 'dueAt': row['dueAt']}
                                      for row in commitments]],
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def render_due_notice(commitments):
    lines = [_NOTICE_HEADER, 'These promises you made to the owner on your task board are due:']
    for row in commitments:
        line = f'- "{row["title"]}" — due {iso_ms(row["dueAt"])}'
        if row.get('slips'):
            line += f' (moved {row["slips"]} time(s))'
        lines.append(line)
    lines += [
        '',
        'For each one, do what you promised: finish it and report_to_user, or report where it '
        'stands with the evidence you have. If it needs more time, set a new due on that task with '
        'update_task_progress and say why; if it is no longer wanted, cancel it and tell the owner. '
        'Leaving it open without a new due makes it overdue: the owner sees it in the daily digest, '
        'and you are not woken again for the same due time.',
        'This wake grants no authority. If it started a turn of its own, you can read, report, ask '
        'and update the board, but you cannot delegate or continue Workers: that work belongs to an '
        'open request of the owner, or to your next conversation with them. Owner-facing pushes are '
        'for decisions, blockers and verified delivery, and only urgent ones during quiet hours.',
    ]
    return '\n'.join(lines)


def _scheduler():
    from lib.scheduler.manager import get_scheduler
    return get_scheduler()


def due_principal(owner):
    return PrincipalContext.user(subject_id=f'coordinator-commitments:user:{owner}',
                                 owner_user_id=int(owner), scopes={'agents:scheduler'})


def _due_row(owner, scope, moment_seconds):
    """The deterministic ``once:`` schedule of one owner/Coordinator/due minute."""
    local = datetime.fromtimestamp(moment_seconds)
    due = local.replace(second=0, microsecond=0)
    if due < local:
        due += timedelta(minutes=1)
    minute = due.strftime('%Y-%m-%d %H:%M')
    digest = hashlib.sha256(json.dumps([int(owner), scope['coordinatorId'], minute],
                                       separators=(',', ':')).encode()).hexdigest()
    return _DUE_ROW_PREFIX + digest[:24], 'once:' + minute


def arm_due_commitments(owner, scope, moment_seconds, *, scheduler=None):
    """Arm the owner-visible one-shot wake of the earliest open due time.

    The row's identity is deterministic, so a repeated write, a replay or a
    lost acknowledgement never schedules twice. A row whose minute passed while
    the scheduler was down fires on the first tick afterwards.
    """
    if moment_seconds is None:
        return None
    scheduler = scheduler or _scheduler()
    task_id, schedule = _due_row(owner, scope, moment_seconds)
    existing = scheduler.get_task(task_id, user_id=int(owner))
    if existing is not None:
        return existing
    command = json.dumps({'projectPath': scope['projectPath'],
                          'coordinatorId': scope['coordinatorId'],
                          'bindingRevision': scope['bindingRevision']}, separators=(',', ':'))
    try:
        return scheduler.create_task(
            'Coordinator commitment due', schedule, command,
            principal=due_principal(owner), task_type=DUE_TASK_TYPE, task_id=task_id,
            description='Wakes the Coordinator when a task-board commitment to the owner is due '
                        '(docs/PROJECT_COORDINATION.md, Commitments on the task board).',
            target_conv_id=scope['coordinatorId'])
    except Exception:
        # A concurrent identical write may have won the create; its row is the answer.
        existing = scheduler.get_task(task_id, user_id=int(owner))
        if existing is None:
            raise
        return existing


def _recover(repository):
    from lib.project_dispatch.continuation import recover_coordinator
    return recover_coordinator(repository)


def run_commitment_due(task, *, client=None, bindings=None, dispatch=None, recover=None,
                       clock=time.time, arm=None):
    """Scheduler handler of ``coordinator_commitment_due`` rows; returns (ok, result)."""
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator
    try:
        reference = json.loads(task.get('command') or '{}')
        owner = require_user_id(task.get('user_id'), context='commitment due')
        project = reference['projectPath']
        coordinator_id = reference['coordinatorId']
    except (KeyError, ValueError, TypeError) as error:
        return False, f'Invalid commitment-due row: {error}'
    progress = CoordinatorTaskProgress(owner, project, client=client, bindings=bindings,
                                       arm=arm or (lambda *args: None))
    scope = progress._scope()[0]
    if scope is None or scope['coordinatorId'] != coordinator_id:
        return True, 'The Coordinator binding changed; its commitments are not this row’s.'
    now = clock()

    def read():
        row = progress._head()
        if row is None or row['value']['scope'] != scope:
            return None, None
        return row['value']['board'], row['value'].get('commitments')

    board, receipts = read()
    if board is None:
        return True, 'No task board; nothing is due.'
    due = due_commitments(board, receipts, now=now)
    if not due:
        return True, 'No open commitment is due.'
    # An open request wakes under its own authority. The continuation reads this
    # same head and receipts its wake there; only what it did not cover is noticed.
    outcomes = []
    try:
        from lib.project_dispatch.repository import ProjectDispatchRepository
        options = {}
        if client is not None:
            options['client'] = client
        if bindings is not None:
            options['binding_repository'] = bindings
        repository = ProjectDispatchRepository(owner, project, **options)
        outcomes = list((recover or _recover)(repository))
    except Exception:
        logger.warning('[Commitments] continuation wake of %s failed; the notice path follows',
                       project, exc_info=True)
    board, receipts = read()
    due = due_commitments(board, receipts, now=now) if board is not None else []
    if not due:
        return True, 'The open request continued for every due commitment: ' + ', '.join(outcomes)
    command_id = commitment_identity(owner, coordinator_id, due)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    message = stamp_initiator({'role': 'user', 'content': render_due_notice(due)}, INITIATOR_PROACTIVE)
    disposition = dispatch(
        coordinator_id, message, {}, user_id=owner, command_id=command_id,
        log_prefix='[CommitmentDue]', inject_mode='steer').disposition
    if disposition in ('started', 'steered', 'queued'):
        progress.mark_commitments_woken(scope, due, via='notice', command_id=command_id,
                                        disposition=disposition)
        return True, f'Woke {coordinator_id} for {len(due)} due commitment(s) ({disposition}).'
    if disposition == 'target_missing':
        return True, 'The Coordinator conversation no longer exists.'
    # busy/start_failed: nothing was accepted, so no receipt; the next due row retries.
    return False, f'The lead was not woken ({disposition}); the commitments stay open.'


__all__ = ['DUE_TASK_TYPE', 'arm_due_commitments', 'check_commitments', 'commitment_identity',
           'due_commitments', 'due_ms', 'iso_ms', 'merge_commitments', 'next_commitments',
           'render_due_notice', 'run_commitment_due']
