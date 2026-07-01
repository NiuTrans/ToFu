"""lib.conversations.project_dispatch — brain-driven dispatch (Pillar #5).

The last step that closes "无需人手": the Board lets conversations coordinate
when a human is driving them, but nothing on the board ever *starts* work. An
open epic with all dependencies met sits forever unless a human opens a tab.
This module is the spine that makes the project autonomous — it SELECTS
genuinely-pickable epics and KICKS them off into a conversation via the
existing ``message_queue`` (NOT a second turn-source), claiming each on
dispatch so siblings (and a re-dispatch pass) immediately avoid it.

Locked design (owner, 2026-06-30):

  • **Reuse the board's at-read-time lease eval.** ``select_dispatchable`` is
    built on ``read_board`` (whose ``_effective_status`` already reclaims an
    expired claim to ``open``) — there is exactly ONE deadlock path, not two.
  • **Dispatchable = open AND every dependency done AND no live claim.** An
    epic with an unfinished ``depends_on`` or a live (unexpired) claim is
    NEVER a candidate.
  • **Claim-on-dispatch = idempotency guard.** ``dispatch_epic`` claims the
    epic under the target conv BEFORE/with enqueuing the kickoff, so a second
    dispatch pass sees it as ``claimed`` and won't re-select it (no concurrent
    double-dispatch). The claim is the same soft lease — advisory, TTL-expiring.
  • **Trigger needs no new global / thread.** ``on_epic_completed`` is called
    from ``complete_task`` (a completion may unblock a dependent); it reuses
    the existing post-task ``dispatch_next_queued`` machinery to actually start
    the enqueued kickoff. No background poller is added here.
  • **Per ``project_path``, never a process-global.**
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

# A brain-dispatched kickoff carries this marker in its queue payload so the
# turn is recognisable as engine-injected (NOT a human turn) downstream.
BRAIN_DISPATCH_MARKER = '_brainDispatch'


def select_dispatchable(project_path: str) -> list[dict]:
    """Return board epics that are GENUINELY pickable right now.

    An epic qualifies iff (read via ``read_board``, so expired claims already
    read as open):
      • its effective status is ``open`` (NOT ``claimed`` with a live lease,
        NOT ``done``); AND
      • every id in its ``depends_on`` refers to an epic that is ``done``.

    Pure + side-effect-free — the testable core. Returns [] on no project.
    """
    if not project_path:
        return []
    from lib.conversations.project_board import read_board
    board = read_board(project_path)
    tasks = board['tasks']
    # Dependencies are satisfied only by epics whose EFFECTIVE status is done.
    done_ids = {t['id'] for t in tasks if t['status'] == 'done'}

    candidates = []
    for t in tasks:
        # ── live-claim filter: only OPEN epics are pickable. A claimed epic
        #    with an unexpired lease (effective status 'claimed') is excluded
        #    — never double-dispatch live-claimed work. ──
        if t['status'] != 'open':
            continue
        # ── dependency filter: every dependency must be DONE. An epic with an
        #    unfinished (or unknown) dependency is NOT yet pickable. ──
        deps = t.get('depends_on') or []
        if any(d not in done_ids for d in deps):
            continue
        candidates.append(t)
    return candidates


def dispatch_epic(project_path: str, epic: dict, target_conv_id: str, *,
                  config: dict | None = None) -> dict:
    """Kick off ONE board epic into ``target_conv_id`` autonomously.

    Claims the epic under the target conv (the idempotency guard: a re-dispatch
    pass now sees it ``claimed`` and skips it) and enqueues a brain-dispatched
    kickoff turn via the existing ``message_queue`` (kind ``workflow_step`` —
    dispatchable, engine-injected, NOT a human ``real`` turn).

    Returns ``{'ok', 'queueId'?, 'error'?}``. Does NOT itself start the task —
    the existing post-task ``dispatch_next_queued`` hook (or an idle-conv
    kickstart) drains the queue. Best-effort; never raises.
    """
    if not project_path or not epic or not target_conv_id:
        return {'ok': False, 'error': 'missing project/epic/conv'}
    task_id = epic.get('id') or ''
    title = (epic.get('title') or '').strip()
    if not task_id:
        return {'ok': False, 'error': 'epic has no id'}
    try:
        from lib.conversations.project_board import claim_task
        # ── Claim FIRST: this is the idempotency guard. If a DIFFERENT conv
        #    already holds a live claim, claim_task refuses → we do NOT enqueue
        #    (no double-dispatch). dispatched=True marks this claim as
        #    brain-minted so the board card can show the "brain-dispatched"
        #    badge. ──
        claim = claim_task(project_path, target_conv_id, task_id,
                           dispatched=True)
        if not claim.get('ok'):
            return {'ok': False, 'error': claim.get('error', 'claim_failed')}

        kickoff = (
            f"[Project Brain — autonomous dispatch] You are picking up an open "
            f"project epic so it does not stall waiting for a human. Epic: "
            f"\"{title}\". Read the project board and charter for context, do the "
            f"work, and mark the epic done with project_board_complete when "
            f"finished. If you get blocked, report it with project_board_block."
        )
        from lib.message_queue import KIND_WORKFLOW, enqueue_message
        res = enqueue_message(
            target_conv_id,
            {'text': kickoff, BRAIN_DISPATCH_MARKER: True,
             'boardTaskId': task_id},
            config or {},
            kind=KIND_WORKFLOW)
    except Exception as e:
        logger.error('[Dispatch] dispatch_epic failed proj=%.40r epic=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    audit_log('brain_dispatch', project_path=project_path, task_id=task_id,
              conv_id=target_conv_id)
    logger.info('[Dispatch] brain-dispatched epic %s → conv=%s queue=%s',
                task_id, target_conv_id[:8], res.get('queueId', '?')[:8])
    return {'ok': True, 'queueId': res.get('queueId')}


def _conv_has_live_task(conv_id: str) -> bool:
    """True iff the conversation currently has a non-aborted running task in
    the task registry — the canonical "conv is busy" signal (same registry
    /api/chat/send and abort_running_tasks_for_conv consult). Best-effort:
    on any error, report busy (fail-safe: never dispatch INTO uncertainty)."""
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            for t in tasks.values():
                if (t.get('convId') == conv_id
                        and t.get('status') == 'running'
                        and not t.get('aborted')):
                    return True
        return False
    except Exception as e:
        logger.debug('[Dispatch] live-task probe failed for %s (assuming busy): %s',
                     conv_id[:8] if conv_id else '?', e)
        return True


def _epic_already_queued(conv_id: str, board_task_id: str) -> bool:
    """True iff a brain-dispatched ``workflow_step`` kickoff for THIS epic is
    already sitting in the conversation's queue — prevents stacking a duplicate
    kickoff when the target conv is busy and the previous kickoff hasn't
    drained yet. Best-effort: on error, report 'queued' (fail-safe)."""
    if not conv_id or not board_task_id:
        return False
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.message_queue import KIND_WORKFLOW
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT payload FROM message_queue WHERE conv_id=? AND kind=?',
            (conv_id, KIND_WORKFLOW)).fetchall()
        import json as _json
        for r in rows:
            try:
                p = _json.loads(r['payload']) if r['payload'] else {}
            except (TypeError, ValueError):
                continue
            if p.get('boardTaskId') == board_task_id:
                return True
        return False
    except Exception as e:
        logger.debug('[Dispatch] queued-kickoff probe failed (assuming queued): %s', e)
        return True


def sweep_dispatch(project_path: str, *, max_per_sweep: int = 3) -> int:
    """The HEARTBEAT: dispatch genuinely-pickable epics on an idle project,
    even when nothing just completed (the completion trigger can only propagate
    motion already underway — this is what STARTS motion, incl. the cold-start
    first epic).

    Idempotent + bounded + safe:
      • ``select_dispatchable`` already excludes live-claimed epics, and
        ``dispatch_epic`` CLAIMS each → the NEXT sweep won't re-select it (the
        primary double-dispatch guard).
      • Belt-and-braces busy guard: skip an epic whose target conv already has
        a live task OR an already-queued kickoff for that epic — so a busy
        target never gets a stacked duplicate kickoff.
      • Capped at ``max_per_sweep`` so one tick can't flood.
      • Per ``project_path``; best-effort — a sweep failure must never break
        the scheduler tick.

    Returns the number of epics dispatched this sweep.
    """
    if not project_path:
        return 0
    dispatched = 0
    try:
        for epic in select_dispatchable(project_path):
            if dispatched >= max(1, max_per_sweep):
                break
            target = (epic.get('created_by_conv') or '').strip()
            if not target:
                continue  # never invent a conversation
            # Busy guard: don't stack a kickoff into a conv that's already
            # working or already has a pending kickoff for THIS epic.
            if _conv_has_live_task(target) or _epic_already_queued(target, epic.get('id', '')):
                continue
            res = dispatch_epic(project_path, epic, target)
            if res.get('ok'):
                dispatched += 1
    except Exception as e:
        logger.warning('[Dispatch] sweep failed proj=%.40r: %s', project_path, e)
    return dispatched


def sweep_all_active_projects(*, max_projects: int = 20,
                              max_per_sweep: int = 3) -> int:
    """Sweep dispatch across recent/active projects (the scheduler entry point).

    Enumerates recent projects (capped) and runs ``sweep_dispatch`` on each.
    No new global / thread — called from the existing scheduler 30s tick.
    Returns total epics dispatched. Best-effort.
    """
    total = 0
    try:
        from lib.project_mod import get_recent_projects
        projects = get_recent_projects() or []
    except Exception as e:
        logger.debug('[Dispatch] recent-projects enumeration failed: %s', e)
        return 0
    for p in projects[:max(1, max_projects)]:
        path = (p.get('path') if isinstance(p, dict) else '') or ''
        if not path:
            continue
        try:
            total += sweep_dispatch(path, max_per_sweep=max_per_sweep)
        except Exception as e:
            logger.debug('[Dispatch] sweep_dispatch failed for %.40r: %s', path, e)
    if total:
        logger.info('[Dispatch] heartbeat sweep dispatched %d epic(s) across %d project(s)',
                    total, len(projects[:max_projects]))
    return total


def on_epic_completed(project_path: str, completed_conv_id: str = '') -> int:
    """Trigger seam: a board epic just completed → some dependents may now be
    dispatchable. Dispatch each newly-pickable epic to a target conversation.

    Returns the number of epics dispatched. Best-effort: never raises into the
    completion path. The target conversation is the epic's ``created_by_conv``
    (the conversation that posted it) so the work returns to its originator;
    falls back to ``completed_conv_id`` when the poster is unknown.
    """
    if not project_path:
        return 0
    dispatched = 0
    try:
        candidates = select_dispatchable(project_path)
        for epic in candidates:
            target = (epic.get('created_by_conv') or '').strip() or completed_conv_id
            if not target:
                # No conversation to route the work to — leave it open for a
                # human (or a future idle-sibling selector). Never invent a conv.
                continue
            res = dispatch_epic(project_path, epic, target)
            if res.get('ok'):
                dispatched += 1
    except Exception as e:
        logger.warning('[Dispatch] on_epic_completed failed proj=%.40r: %s',
                       project_path, e)
    return dispatched


__all__ = [
    'select_dispatchable', 'dispatch_epic', 'on_epic_completed',
    'sweep_dispatch', 'sweep_all_active_projects', 'BRAIN_DISPATCH_MARKER',
]
