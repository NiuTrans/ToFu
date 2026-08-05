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
  • **Trigger needs no new global / thread.** ``on_epic_completed`` is called
    from ``complete_task`` (a completion may unblock a dependent); it reuses
    the existing post-task ``dispatch_next_queued`` machinery to actually start
    the enqueued kickoff. No background poller is added here.
  • **Event channel (2026-07-27).** The 30 s sweep is the crash/lease/strand
    SAFETY NET, not the starter: common flows dispatch AT THE EVENT —
    ``on_epic_posted`` (post time, idle existing target), ``on_conv_idle``
    (a task completes with an empty queue), ``on_epic_completed`` /
    ``on_epic_answered`` (dependency done / human answered).
  • **Per ``project_path``, never a process-global.**
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


def _resolve_dispatch_config(target_conv_id: str) -> dict:
    """Resolve a REAL task config for a brain-dispatched kickoff from the
    target conversation's stored settings.

    The kickoff is drained by ``dispatch_next_queued`` into ``create_task``,
    which needs a real model + projectPath + tool flags. An EMPTY config (the
    old behaviour — callers passed none, so the kickoff carried ``{}``) would
    spawn a task with no model and no project context, unable to do the work.
    Reuses the scheduler's ``build_task_config`` (settings → task config) — the
    SAME merge the timer/proactive background paths use. Best-effort: returns
    ``{}`` on any failure (the task then falls back to server defaults).
    """
    if not target_conv_id:
        return {}
    try:
        import json

        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (target_conv_id,)).fetchone()
        settings = json.loads(row['settings'] or '{}') if row else {}
        from lib.scheduler._shared import build_task_config
        return build_task_config({}, settings)
    except Exception as e:
        logger.debug('[Dispatch] config resolve failed conv=%s: %s',
                     (target_conv_id[:8] if target_conv_id else '?'), e)
        return {}


def _drain_idle_target(target_conv_id: str) -> str | None:
    """Start a just-enqueued brain kickoff in an IDLE target conversation.

    THE cold-start fix. The heartbeat sweep and the completion trigger only
    CLAIM + ENQUEUE a ``workflow_step`` kickoff; nothing else drains an idle
    conversation's queue — ``dispatch_next_queued`` fires ONLY after a task
    COMPLETES (the manager post-task hook) or on a human send. So a cold-start
    kickoff would rot in the queue until the 30-min soft lease expires, then
    ``_epic_already_queued`` blocks re-dispatch → the epic oscillates
    open↔claimed and is NEVER worked. This closes that gap: after enqueuing
    into a conv with NO live task, drain it here via the SAME
    ``dispatch_next_queued`` seam the completion hook uses, spawning the task
    from the scheduler thread.

    Invariants (owner-set):
      • Only drains a conv with NO live non-aborted task (reuses
        ``_conv_has_live_task`` — never races a running turn).
      • ``dispatch_next_queued`` acquires ``_dispatch_lock`` itself, so the
        drain is already serialized (we must NOT re-acquire that non-reentrant
        lock here).
      • A spawn failure is LOGGED and the claim is left to expire so the next
        sweep retries cleanly — never a silent strand. Best-effort; never
        raises into the sweep/completion path.
    """
    if not target_conv_id:
        return None
    if _conv_has_live_task(target_conv_id):
        return None
    # Guard: only drain into a conversation that actually EXISTS. dequeue_next
    # DELETES the queue row before dispatch_next_queued checks the conv row, so
    # draining into a missing conv would silently LOSE the kickoff. If the conv
    # row is absent we leave the kickoff queued (the claim expires → a later
    # sweep retries cleanly) rather than consuming it.
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT 1 FROM conversations WHERE id=? AND user_id=1 LIMIT 1',
            (target_conv_id,)).fetchone()
        if not row:
            logger.debug('[Dispatch] idle-drain skipped conv=%s (no conversation '
                         'row); kickoff left queued for a later drain',
                         target_conv_id[:8])
            return None
    except Exception as e:
        logger.debug('[Dispatch] idle-drain conv-existence probe failed conv=%s: %s',
                     target_conv_id[:8], e)
        return None
    try:
        from lib.message_queue import dispatch_next_queued
        task_id = dispatch_next_queued(target_conv_id)
        if task_id:
            logger.info('[Dispatch] cold-start drained idle conv=%s → started '
                        'task %s', target_conv_id[:8], task_id[:8])
        else:
            logger.warning('[Dispatch] idle-drain produced no task for conv=%s '
                           '(kickoff failed to spawn or queue empty); the claim '
                           'will expire and re-dispatch on a later sweep',
                           target_conv_id[:8])
        return task_id
    except Exception as e:
        logger.error('[Dispatch] idle-drain failed conv=%s: %s',
                     target_conv_id[:8], e, exc_info=True)
        return None

# A brain-dispatched kickoff carries this marker in its queue payload so the
# turn is recognisable as engine-injected (NOT a human turn) downstream.
BRAIN_DISPATCH_MARKER = '_brainDispatch'

# Which dispatch SEAM fired the kickoff — stamped by each event seam on the
# epic dict it hands to dispatch_epic (``_via``), surfaced verbatim in the
# message's ``_brainEpic`` provenance card so the frontend can say HOW this
# conversation was picked. 'heartbeat' is the default (the 30 s sweep, the
# most common path — sweep_dispatch deliberately does not stamp).
DISPATCH_VIAS = frozenset({
    'heartbeat',         # 30 s sweep_dispatch picked a genuinely-pickable epic
    'dependency_done',   # on_epic_completed — a dependency just finished
    'answered',          # on_epic_answered — the human answered the gate
    'posted',            # on_epic_posted — startable at post time
    'conv_idle',         # on_conv_idle — the conv just went idle
})


def _resolve_conv_title(conv_id: str) -> str:
    """The conversation's human title for the provenance card (the card shows
    a clickable TITLE, not a bare conv id). One bounded SELECT, best-effort —
    a lookup failure degrades to '' (the card falls back to a generic
    "untitled" label), never blocks the dispatch."""
    conv_id = (conv_id or '').strip()
    if not conv_id:
        return ''
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT title FROM conversations WHERE id=? LIMIT 1',
            (conv_id,)).fetchone()
        return (row['title'] or '') if row else ''
    except Exception as e:
        logger.debug('[Dispatch] conv-title resolve failed conv=%s: %s',
                     conv_id[:8], e)
        return ''


def _brain_meta(epic: dict, target_conv_id: str) -> dict:
    """The display-only provenance record stamped onto the kickoff payload as
    ``_brainEpic`` — WHO created the epic (title + conv id, for a clickable
    chip), HOW it was dispatched (which seam fired), and WHY this conversation
    was picked (its creator / migrated from an idle-stranded target / the
    completing-conv fallback). Display-only: conv_message_builder reads only
    ``content`` for the wire, so this never reaches the model.

    ``route`` derivation keys on the board's OWN provenance fields, not the
    seam: ``dispatch_target`` is the mutable routing override (idle-sibling
    migration sets it), ``created_by_conv`` the immutable authorship."""
    origin = (epic.get('created_by_conv') or '').strip()
    override = (epic.get('dispatch_target') or '').strip()
    target = (target_conv_id or '').strip()
    if override and target == override and origin and target != origin:
        route = 'migrated'
    elif origin and target == origin:
        route = 'creator'
    else:
        route = 'fallback'
    via = str(epic.get('_via') or 'heartbeat')
    if via not in DISPATCH_VIAS:
        via = 'heartbeat'
    return {
        'epicId': epic.get('id') or '',
        # Display cap only — the kickoff text carries the full title.
        'epicTitle': (epic.get('title') or '')[:300],
        'originatorConv': origin,
        'originatorTitle': _resolve_conv_title(origin),
        'method': via,
        'route': route,
        'answered': bool((epic.get('human_answer') or '').strip()),
    }


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
    import time as _time

    from lib.conversations.project_board import read_board
    board = read_board(project_path)
    tasks = board['tasks']
    now_ms = int(_time.time() * 1000)
    # Dependencies are satisfied only by epics whose EFFECTIVE status is done.
    done_ids = {t['id'] for t in tasks if t['status'] == 'done'}

    candidates = []
    for t in tasks:
        # ── kind filter: a 'lease' row is a durational resource/path
        #    RESERVATION, never a work-item. It MUST NOT be auto-dispatched —
        #    without this skip, an EXPIRED lease reclaims claimed→open (via
        #    _effective_status), passes the status=='open' check below, and the
        #    sweep + _drain_idle_target would spawn a spurious BILLED kickoff at
        #    TTL expiry. DENYLIST (not an allowlist on 'epic') so a
        #    pre-migration None/'' kind still reads as a dispatchable epic. ──
        if t.get('kind') == 'lease':
            continue
        # ── live-claim filter: only OPEN epics are pickable. A claimed epic
        #    with an unexpired lease (effective status 'claimed') is excluded
        #    — never double-dispatch live-claimed work. ──
        if t['status'] != 'open':
            continue
        # ── block-cooldown filter: an epic that hit a genuine external gate was
        #    stamped blocked_until = now + an escalating cooldown by block_task.
        #    While that window is live, SKIP it — this is what stops the ~30-min
        #    lease-expiry re-dispatch churn (a billed agent turn each cycle to
        #    re-discover the same unmet dep). At-READ-time expiry: once the
        #    window lapses the epic is pickable again (a resolved dep IS
        #    retried), with NO reaper and NO human un-block gate. ──
        if int(t.get('blocked_until') or 0) > now_ms:
            continue
        # ── pending-question filter: an epic blocked WITH a structured human
        #    question waits for the ANSWER, not for time — re-dispatching
        #    before the human answers can only re-discover the same gate (the
        #    billed-turn loop this redesign exists to kill). answer_task
        #    clears the question + cooldown, so an ANSWERED epic falls through
        #    to normal pick-up (and carries its answer into the kickoff). ──
        if t.get('block_question') and not (t.get('human_answer') or '').strip():
            continue
        # ── dependency filter: every dependency must be DONE. An epic with an
        #    unfinished (or unknown) dependency is NOT yet pickable. ──
        deps = t.get('depends_on') or []
        if any(d not in done_ids for d in deps):
            continue
        candidates.append(t)

    # ── Write-set partitioning: prefer a candidate
    #    whose declared write_set is DISJOINT from every LIVE-CLAIMED epic's
    #    write_set, so two conversations aren't handed epics that will fight
    #    over the same files. This is a SOFT preference (stable reorder,
    #    disjoint-first) NOT a hard filter — a conflicting epic is still
    #    dispatchable (last), and an epic with an empty/undeclared write_set is
    #    "unknown footprint" → treated as non-conflicting → never demoted. ──
    claimed_write_sets = [
        _write_set_of(t) for t in tasks
        if t.get('status') == 'claimed' and _write_set_of(t)
    ]
    if len(candidates) > 1:
        def _demote_key(c):
            # A wait-on-path conflict (isolation-demoted above) OR a declared
            # write_set that overlaps a live-claimed epic's → hand out LAST.
            # Both are SOFT: a conflicting epic is still dispatchable, just
            # after every disjoint one, so no colliding pair is handed out
            # concurrently while independent work exists. Stable sort keeps
            # relative order within each bucket.
            if c.get('_conflict_demote'):
                return 1
            if claimed_write_sets and _write_set_conflicts(
                    _write_set_of(c), claimed_write_sets):
                return 1
            return 0
        candidates.sort(key=_demote_key)
    return candidates


def _write_set_of(task: dict) -> list:
    """The epic's declared write_set as a clean list of strings (empty when
    undeclared → unknown footprint → treated as non-conflicting)."""
    ws = task.get('write_set') or []
    return [str(s) for s in ws if isinstance(ws, list) and str(s).strip()]


def _paths_intersect(a: str, b: str) -> bool:
    """True iff two write-set entries name overlapping targets. Handles a
    plain-prefix / directory-containment relationship in EITHER direction
    (``lib/`` vs ``lib/x.py``) and exact match; a trailing-``*`` glob is
    treated as its directory prefix. Deliberately conservative — a false
    "overlap" only demotes an epic in the ordering (safe), never drops it."""
    a = (a or '').rstrip('/*')
    b = (b or '').rstrip('/*')
    if not a or not b:
        return False
    if a == b:
        return True
    return a.startswith(b + '/') or b.startswith(a + '/')


def _write_set_conflicts(ws: list, others: list) -> bool:
    """True iff ``ws`` shares any target with ANY of the ``others`` write-sets.
    An empty ``ws`` (unknown footprint) never conflicts (fail-open)."""
    if not ws:
        return False
    for other in others:
        for x in ws:
            for y in other:
                if _paths_intersect(x, y):
                    return True
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
            f"finished. If you hit a genuine external gate you cannot clear "
            f"yourself, report it with project_board_block and PREFIX the reason "
            f"with the block class — '[human-gated] …' (only a human can satisfy "
            f"it) or '[sibling] …' (auto-resolves when another conversation "
            f"commits). When the blocker is a sibling that must commit specific "
            f"file(s) first, name them in a structured token "
            f"'[sibling] path=lib/x.py,static/js/y.js …' — the brain then HOLDS "
            f"this epic precisely while a sibling holds a lease on those paths "
            f"(releasing automatically when they do), instead of blind retries. "
            f"Either way the block puts the epic on a self-expiring cooldown so "
            f"it is not pointlessly re-dispatched. When the gate needs a HUMAN "
            f"decision, also pass the question (and options when the choice is "
            f"enumerable) to project_board_block — the human answers with one "
            f"click on the board and the answer re-dispatches you immediately "
            f"(a question-block does NOT auto-retry). Do NOT silently no-op."
        )
        # An epic unblocked by a HUMAN ANSWER carries that answer into the
        # kickoff — the assignee proceeds on it directly instead of
        # re-discovering (or worse, re-asking) the question.
        answer = (epic.get('human_answer') or '').strip()
        if answer:
            kickoff += (
                f"\n\nThis epic was blocked waiting on a human decision — "
                f"the human has now answered: \"{answer}\". Proceed on "
                f"that basis; do NOT re-ask the same question or re-block "
                f"on the same gate unless the answer genuinely does not "
                f"resolve it."
            )
        # Resolve a REAL config from the target conv's settings when the caller
        # passed none (the sweep/completion callers do): the kickoff is later
        # drained into create_task, which needs a model + projectPath to work.
        dispatch_config = config if config else _resolve_dispatch_config(target_conv_id)
        from lib.message_queue import KIND_WORKFLOW, enqueue_message
        res = enqueue_message(
            target_conv_id,
            {'text': kickoff, BRAIN_DISPATCH_MARKER: True,
             'boardTaskId': task_id,
             '_brainEpic': _brain_meta(epic, target_conv_id)},
            dispatch_config,
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


def _has_queued_kickoff(conv_id: str) -> bool:
    """True iff a brain-dispatched ``workflow_step`` kickoff (for ANY epic) is
    currently sitting in the conversation's queue.

    Unlike ``_epic_already_queued`` (which asks about ONE board id), this is the
    per-conversation "is there an undrained kickoff to reconcile" probe used by
    the self-healing sweep pass. Best-effort: on error report False (a missed
    reconcile is retried on the next 30s sweep — never a spurious drain)."""
    if not conv_id:
        return False
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.message_queue import KIND_WORKFLOW
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT 1 FROM message_queue WHERE conv_id=? AND kind=? LIMIT 1',
            (conv_id, KIND_WORKFLOW)).fetchone()
        return bool(row)
    except Exception as e:
        logger.debug('[Dispatch] queued-kickoff check failed conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e)
        return False


def _convs_holding_undrained_kickoffs(project_path: str, board: dict) -> set:
    """Every conversation that may be holding an UNDRAINED brain kickoff.

    THE SCAN SET, and the thing that must not be keyed on the epic's momentary
    status. The obvious set — "convs owning a *claimed* epic" — has a hole with
    a 30-minute fuse: ``_effective_status`` reclaims an expired soft lease AT
    READ TIME, so once the lease lapses the epic reads ``open`` and its
    ``owner_conv_id`` is blanked. The conv then vanishes from a claimed-keyed
    scan, while its queue row is still sitting there. Measured in production
    (2026-07-29): 7 ``workflow_step`` rows stranded across 3 idle conversations,
    none of them reachable.

    And the normal dispatch loop cannot rescue them either — it re-selects the
    now-``open`` epic, but ``_epic_already_queued`` sees the surviving kickoff
    and refuses to enqueue a second one. Both doors shut on the same row, which
    is what made the strand permanent rather than merely slow.

    So we take the union of two sources:

      * the board's ``owner_conv_id`` for CLAIMED epics — covers a conv whose
        lease is still live but whose drain chain broke; and
      * every conv with a queued ``workflow_step`` row and a ``projectPath``
        pointing at THIS project — covers the lease-expired case, keyed on the
        durable fact (the queue row) instead of the expiring one (the lease).

    Scoped to this project so a sweep never drains a sibling project's queue.
    Best-effort: a probe failure degrades to the board-derived set, which is
    the old behaviour — never an exception into the sweep.
    """
    convs = {t['owner_conv_id'] for t in board.get('tasks', [])
             if t.get('status') == 'claimed' and t.get('owner_conv_id')}
    try:
        import json as _json

        from lib.conversations.project_feed import normalize_project_path
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.message_queue import KIND_WORKFLOW
        want = normalize_project_path(project_path)
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT DISTINCT conv_id, config FROM message_queue WHERE kind=?',
            (KIND_WORKFLOW,)).fetchall()
        for r in rows:
            cid = r['conv_id']
            if not cid or cid in convs:
                continue
            try:
                cfg = _json.loads(r['config'] or '{}')
            except (TypeError, ValueError) as e:
                logger.debug('[Dispatch] skipping queue row with unparsable '
                             'config conv=%s: %s', cid, e)
                continue
            row_proj = cfg.get('projectPath') or ''
            if row_proj and normalize_project_path(row_proj) == want:
                convs.add(cid)
    except Exception as e:
        logger.debug('[Dispatch] queued-kickoff scan failed proj=%.40r '
                     '(falling back to board-derived set): %s', project_path, e)
    return convs


def _reconcile_stranded_kickoffs(project_path: str) -> int:
    """Re-drain idle conversations that still hold an UNDRAINED brain kickoff.

    THE self-healing safety net for the drain chain — this is what makes
    "queued but never dequeued" recover on its own. ``_drain_idle_target``
    only fires for the FIRST epic dispatched to a conv in a sweep: once that
    epic's task goes live, the busy guard makes the same-sweep drain a no-op
    for every additional epic, so the rest rely on the post-task-completion
    hook CHAIN (each finishing task drains the next). If that chain breaks
    ANYWHERE — a task that never reaches ``persist_task_result`` (hard crash),
    a restart between completions, an autopilot follow-up that owns the drain,
    or simply a sweep that dispatched N>1 epics to one conv — the leftover
    kickoffs rot in the queue. And it becomes PERMANENT: the (now-claimed)
    epic is excluded from ``select_dispatchable``, and once its lease expires
    and it reads ``open`` again, ``_epic_already_queued`` sees the stranded
    kickoff and blocks re-dispatch → nothing ever drains it.

    This pass reconciles that on EVERY sweep (every ~30s): for each
    conversation that owns a claimed epic on this board — the only convs that
    could be holding an undrained kickoff — if it is IDLE and still has a
    queued kickoff, drain ONE via the same ``dispatch_next_queued`` seam. It
    mirrors ``redispatch_orphaned_queue_on_startup`` but runs continuously, not
    only at boot. Bounded (one drain per conv per sweep — the drained task's
    completion hook, or the next sweep, handles any remaining kickoffs).
    Best-effort; never raises into the sweep.

    Returns the number of conversations re-drained.
    """
    if not project_path:
        return 0
    drained = 0
    try:
        from lib.conversations.project_board import read_board
        board = read_board(project_path)
        # Convs that may hold an undrained kickoff. Keyed on the DURABLE fact
        # (a queued workflow_step row) unioned with the board's claimed owners —
        # never on the epic's momentary status, whose 30-min lease expiry used
        # to make this scan blind to the strand (see the helper's docstring).
        convs = _convs_holding_undrained_kickoffs(project_path, board)
        for conv_id in convs:
            if _conv_has_live_task(conv_id):
                continue
            if not _has_queued_kickoff(conv_id):
                continue
            if _drain_idle_target(conv_id):
                drained += 1
                logger.info('[Dispatch] reconciled stranded kickoff → re-drained '
                            'idle conv=%s (broken completion chain / restart / '
                            'multi-dispatch sweep)', conv_id[:8])
    except Exception as e:
        logger.warning('[Dispatch] stranded-kickoff reconcile failed proj=%.40r: %s',
                       project_path, e)
    return drained


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
            except (TypeError, ValueError) as e:
                logger.debug('[Dispatch] queued row payload parse failed (skipping): %s', e)
                continue
            if p.get('boardTaskId') == board_task_id:
                return True
        return False
    except Exception as e:
        logger.debug('[Dispatch] queued-kickoff probe failed (assuming queued): %s', e)
        return True


# ═══════════════════════════════════════════════════════════════════
#  Idle-sibling migration (Pillar #5) — route a stuck epic to an idle peer
#  WITHOUT overwriting authorship. See docs/PROJECT_BRAIN_MIGRATION.md.
# ═══════════════════════════════════════════════════════════════════

# "Originator stuck" threshold = one soft-lease window. A healthy idle conv
# drains its kickoff within a 30s sweep; a kickoff still queued after a FULL
# lease TTL means the drain has failed across ~60 sweeps AND the claim would
# have expired + re-dispatched + re-failed — unambiguously stuck, never a
# transient. Reuses the lease clock (owner: no new timer).
def _migration_stuck_ms() -> int:
    from lib.conversations.project_board import DEFAULT_LEASE_TTL_MS
    return DEFAULT_LEASE_TTL_MS


MIGRATION_STUCK_MS = 30 * 60 * 1000  # mirror of DEFAULT_LEASE_TTL_MS (see above)


def _dispatch_target(epic: dict) -> str:
    """Who should RUN this epic next: the mutable ``dispatch_target`` routing
    override if set, else the immutable ``created_by_conv`` (authorship). This
    is the ONE routing seam every dispatch path consults — provenance is never
    consulted for routing directly."""
    return ((epic.get('dispatch_target') or '').strip()
            or (epic.get('created_by_conv') or '').strip())


def _kickoff_age_ms(conv_id: str, board_task_id: str, now_ms: int) -> int | None:
    """Age (ms) of the OLDEST queued ``KIND_WORKFLOW`` kickoff for THIS epic on
    ``conv_id``, using the durable ``message_queue.created_at`` (no new clock).
    Returns None when no such kickoff is queued. Best-effort → None on error."""
    if not conv_id or not board_task_id:
        return None
    try:
        import json as _json

        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.message_queue import KIND_WORKFLOW
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT payload, created_at FROM message_queue '
            'WHERE conv_id=? AND kind=?', (conv_id, KIND_WORKFLOW)).fetchall()
        oldest = None
        for r in rows:
            try:
                p = _json.loads(r['payload']) if r['payload'] else {}
            except (TypeError, ValueError) as e:
                logger.debug('[Dispatch] kickoff payload parse failed, skipping: %s', e)
                continue
            if p.get('boardTaskId') != board_task_id:
                continue
            ca = int(r['created_at'] or 0)
            if oldest is None or ca < oldest:
                oldest = ca
        if oldest is None:
            return None
        return max(0, now_ms - oldest)
    except Exception as e:
        logger.debug('[Dispatch] kickoff-age probe failed conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e)
        return None


def _paths_waited_but_held(epic: dict, board_tasks: list) -> list:
    """The subset of the epic's ``wait_paths`` currently under a LIVE path
    lease held by a DIFFERENT conversation than the epic's dispatch target.

    The inverse read of the kind='lease' board rows
    (docs/PROJECT_BRAIN_WAIT_ON_PATH.md): the lease claim says "conv X is
    actively touching path Y — hold off"; wait-on-path reads the same rows
    from the epic's side ("hold my epic while Y is held by someone else").
    A lease the epic's OWN target holds is not a hold — that conv is the one
    supposed to run the work.

    Fail-open by construction (design invariant 3): an empty/unparseable
    ``wait_paths``, or a path nobody leases, resolves to [] (not held) so a
    stale entry can never strand an epic. Matching reuses the write-set
    ``_paths_intersect`` semantics (exact or containment either direction) —
    conservative: a false overlap only HOLDS an epic (safe), never migrates
    one. Returns the held subset (empty = not waiting).
    """
    paths = epic.get('wait_paths') or []
    if not isinstance(paths, list) or not paths:
        return []
    target = _dispatch_target(epic)
    live_foreign_leases = [
        t for t in (board_tasks or [])
        if isinstance(t, dict)
        and t.get('kind') == 'lease'
        and t.get('status') == 'claimed'          # effective: lease unexpired
        and (t.get('owner_conv_id') or '') != target
    ]
    if not live_foreign_leases:
        return []
    held = []
    for p in paths:
        ps = str(p).strip()
        if not ps:
            continue
        for lease in live_foreign_leases:
            if _paths_intersect(ps, lease.get('title') or ''):
                held.append(ps)
                break
    return held


def _originator_stuck(project_path: str, epic: dict, board_tasks: list,
                      now_ms: int) -> bool:
    """True iff the epic's current dispatch target is GENUINELY unable to run
    it — the precise, self-correcting migration trigger (owner-defined).

    ALL must hold (else NOT stuck — never migrate a merely-busy or
    correctly-held epic):
      1. its kickoff has been queued on the target LONGER than one lease TTL
         (``_kickoff_age_ms`` > ``MIGRATION_STUCK_MS``) — a healthy idle conv
         drains within a sweep, so this is unambiguous, no new timer; AND
      2. the target has NO live task (a busy conv is WORKING, not stuck); AND
      3. the epic is NOT on a live block-cooldown AND NOT on a live
         wait-on-path (those mean it is correctly HELD — compose, don't
         override).
    Best-effort; on any error report NOT stuck (never migrate on uncertainty).
    """
    try:
        target = _dispatch_target(epic)
        if not target:
            return False
        # 3 — correctly held (cooldown) is NOT stuck.
        if int(epic.get('blocked_until') or 0) > now_ms:
            return False
        # 3b — correctly held (wait-on-path: a listed path is under a LIVE
        # lease owned by a DIFFERENT conversation) is NOT stuck. Migrating
        # would override the hold the epic declared, and the hold self-expires
        # with the lease (never a deadlock, so never a migration trigger).
        if _paths_waited_but_held(epic, board_tasks):
            return False
        # 2 — a busy target is working, not stuck.
        if _conv_has_live_task(target):
            return False
        # 1 — kickoff undrained past a full lease window.
        age_ms = _kickoff_age_ms(target, epic.get('id', ''), now_ms)
        if age_ms is None:
            return False  # nothing queued → nothing to migrate
        if age_ms < MIGRATION_STUCK_MS:
            return False
        return True
    except Exception as e:
        logger.debug('[Dispatch] originator-stuck probe failed epic=%s: %s',
                     epic.get('id', '?'), e)
        return False


def _pick_migration_target(project_path: str, exclude_conv: str,
                           now_ms: int) -> str:
    """Pick a GENUINELY-idle sibling conversation of ``project_path`` to receive
    a migrated epic — never move the strand into another dead end.

    A candidate must: belong to this project, NOT be ``exclude_conv`` (the
    stuck originator), have NO live task, hold NO queued kickoff of its own, and
    its conversation row must EXIST (so the drain can spawn). Prefers the
    most-recently-updated idle sibling (likeliest live). Returns '' when none
    qualifies → the epic stays with its originator (no migration).
    """
    if not project_path:
        return ''
    try:
        from lib.conversations.project_summary import DEFAULT_USER_ID
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            "SELECT id FROM conversations "
            "WHERE user_id=? AND json_extract(settings, '$.projectPath') = ? "
            "ORDER BY updated_at DESC LIMIT 50",
            (DEFAULT_USER_ID, project_path)).fetchall()
    except Exception as e:
        logger.debug('[Dispatch] migration-target query failed proj=%.40r: %s',
                     project_path, e)
        return ''
    for r in rows:
        cid = r['id']
        if not cid or cid == exclude_conv:
            continue
        if _conv_has_live_task(cid):
            continue
        if _has_queued_kickoff(cid):
            continue
        return cid
    return ''


def _drop_epic_kickoffs(conv_id: str, board_task_id: str) -> int:
    """Delete every queued ``KIND_WORKFLOW`` kickoff for THIS epic from
    ``conv_id``'s queue — so a migrated epic's STALE kickoff on the dead
    originator can't keep being re-drained (or block re-dispatch). Returns the
    number of rows removed. Best-effort → 0 on error."""
    if not conv_id or not board_task_id:
        return 0
    try:
        import json as _json

        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.message_queue import KIND_WORKFLOW
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            'SELECT id, payload FROM message_queue WHERE conv_id=? AND kind=?',
            (conv_id, KIND_WORKFLOW)).fetchall()
        removed = 0
        for r in rows:
            try:
                p = _json.loads(r['payload']) if r['payload'] else {}
            except (TypeError, ValueError) as e:
                logger.debug('[Dispatch] drop-kickoff payload parse failed, skipping: %s', e)
                continue
            if p.get('boardTaskId') == board_task_id:
                db.execute('DELETE FROM message_queue WHERE id=?', (r['id'],))
                removed += 1
        if removed:
            db.commit()
        return removed
    except Exception as e:
        logger.warning('[Dispatch] drop-kickoff failed conv=%s epic=%s: %s',
                       conv_id[:8] if conv_id else '?', board_task_id, e)
        return 0


def migrate_epic(project_path: str, epic: dict, new_target: str) -> dict:
    """Migrate a stuck epic to ``new_target``: set the mutable
    ``dispatch_target`` (routing) WITHOUT touching ``created_by_conv``
    (immutable authorship), drop the stale kickoff on the originator, reopen the
    claim so ``select_dispatchable`` re-picks it and routes to the new target,
    and record the reassignment in the feed + audit. Best-effort; never raises.
    Returns ``{'ok', 'from'?, 'to'?, 'error'?}``.
    """
    if not project_path or not epic or not new_target:
        return {'ok': False, 'error': 'missing project/epic/target'}
    task_id = epic.get('id') or ''
    origin = (epic.get('created_by_conv') or '').strip()
    if not task_id:
        return {'ok': False, 'error': 'epic has no id'}
    try:
        from lib.conversations.project_feed import normalize_project_path
        from lib.database import DOMAIN_CHAT, get_thread_db
        norm = normalize_project_path(project_path)
        db = get_thread_db(DOMAIN_CHAT)
        # Set the routing override + reopen the (stuck) claim so it re-dispatches
        # to new_target. created_by_conv is deliberately NOT in the SET list.
        import time as _time
        db.execute(
            "UPDATE project_tasks SET dispatch_target=?, status='open', "
            "owner_conv_id='', lease_expires_at=0, dispatched=0, updated_at=? "
            'WHERE id=? AND project_path=?',
            (new_target, int(_time.time() * 1000), task_id, norm))
        db.commit()
        # Drop the stale kickoff on the (dead) originator so the reconcile pass
        # stops re-draining it.
        if origin:
            _drop_epic_kickoffs(origin, task_id)
    except Exception as e:
        logger.error('[Dispatch] migrate_epic failed proj=%.40r epic=%s: %s',
                     project_path, task_id, e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path, new_target, 'note',
            f'Migrated epic to {new_target[:8]} (originator {origin[:8] or "?"} '
            f'was idle-stranded)',
            payload={'taskId': task_id, 'migratedFrom': origin,
                     'migratedTo': new_target})
    except Exception as e:
        logger.debug('[Dispatch] migrate feed emit skipped: %s', e)
    audit_log('brain_migrate', project_path=project_path, task_id=task_id,
              from_conv=origin, to_conv=new_target)
    logger.info('[Dispatch] migrated epic %s originator=%s → idle sibling %s',
                task_id, origin[:8] or '?', new_target[:8])
    return {'ok': True, 'from': origin, 'to': new_target}


def _migrate_stranded_epics(project_path: str) -> int:
    """Migrate epics whose dispatch target is idle-stranded to a genuinely-idle
    sibling — the bounded (1/sweep) idle-sibling migration pass.

    For each dispatchable epic, if ``_originator_stuck`` (kickoff undrained past
    one lease TTL + target has no live task + NOT held by cooldown/wait) AND an
    idle sibling exists, ``migrate_epic`` re-routes it (sets ``dispatch_target``,
    drops the stale kickoff, reopens the claim). Runs AFTER the reconcile pass
    and BEFORE the dispatch loop, so a just-migrated epic is picked up in the
    SAME sweep and routed to its new target. Bounded to ONE migration per sweep
    (the next sweep handles any further strands) so a mass-stranded board can't
    thrash. Best-effort; never raises into the sweep.

    Returns the number of epics migrated (0 or 1).
    """
    if not project_path:
        return 0
    try:
        import time as _time
        now_ms = int(_time.time() * 1000)
        from lib.conversations.project_board import read_board
        board = read_board(project_path)
        tasks = board['tasks']
        for epic in tasks:
            if epic.get('kind') == 'lease' or epic.get('status') != 'open':
                continue
            if not _originator_stuck(project_path, epic, tasks, now_ms):
                continue
            target = _pick_migration_target(
                project_path, _dispatch_target(epic), now_ms)
            if not target:
                continue  # no idle sibling → leave it with the originator
            res = migrate_epic(project_path, epic, target)
            if res.get('ok'):
                return 1  # bounded: one migration per sweep
    except Exception as e:
        logger.warning('[Dispatch] migrate-stranded pass failed proj=%.40r: %s',
                       project_path, e)
    return 0


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
    # ── Self-heal FIRST: re-drain any idle conv still holding an undrained
    #    kickoff (a broken completion chain / restart / a prior multi-dispatch
    #    sweep). Without this, a stranded kickoff stays queued forever — the
    #    claimed epic is excluded from select_dispatchable, and once its lease
    #    expires _epic_already_queued blocks re-dispatch. This runs before the
    #    dispatch loop so a recovered conv is seen as busy and not stacked on.
    try:
        _reconcile_stranded_kickoffs(project_path)
    except Exception as e:
        logger.debug('[Dispatch] reconcile pass skipped proj=%.40r: %s', project_path, e)
    # ── Migrate ONE idle-stranded epic to an idle sibling (after reconcile,
    #    before the dispatch loop) so the just-migrated epic is picked up and
    #    routed to its new target in THIS same sweep. Bounded 1/sweep. ──
    try:
        _migrate_stranded_epics(project_path)
    except Exception as e:
        logger.debug('[Dispatch] migrate pass skipped proj=%.40r: %s', project_path, e)
    try:
        for epic in select_dispatchable(project_path):
            if dispatched >= max(1, max_per_sweep):
                break
            target = _dispatch_target(epic)
            if not target:
                continue  # never invent a conversation
            # Busy guard: don't stack a kickoff into a conv that's already
            # working or already has a pending kickoff for THIS epic.
            if _conv_has_live_task(target) or _epic_already_queued(target, epic.get('id', '')):
                continue
            res = dispatch_epic(project_path, epic, target)
            if res.get('ok'):
                dispatched += 1
                # Cold-start drain: the kickoff was just enqueued into an idle
                # conv; nothing else will start it, so drain it here (see
                # _drain_idle_target). This is what makes the heartbeat
                # genuinely self-starting instead of only claiming.
                _drain_idle_target(target)
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
            target = _dispatch_target(epic) or completed_conv_id
            if not target:
                # No conversation to route the work to — leave it open for a
                # human (or a future idle-sibling selector). Never invent a conv.
                continue
            # ── Which guard belongs on THIS seam (measured twice) ──
            # ``_conv_has_live_task`` must stay OUT: it BREAKS this seam's
            # actual job — the dependency chain. When A completes, dependent B
            # must be claimed + enqueued *while the conv is still busy
            # finishing A*, then drained by the post-task queue chain.
            # test_project_brain_integration::test_full_autonomous_flywheel
            # pins exactly that ("B claimed + enqueued but NOT drained" while
            # busy) and goes RED with the check in place — A/B measured 6/6
            # without it, 5/6 with it.
            #
            # ``_epic_already_queued`` DOES belong here. It was once argued
            # unreachable-by-construction (dispatch_epic claims the epic and
            # select_dispatchable excludes 'claimed', so a re-entrant call
            # cannot reach this line for the same epic) and its NEUTER did not
            # bite. That argument holds only while the claim LIVES. The claim
            # is a 30-min soft lease and a target task can run for hours, so at
            # every lease expiry the board reads the epic 'open' again and this
            # seam stacks ANOTHER kickoff onto a conv that never drained the
            # first. Measured 2026-07-28 on conv ms4b67gmthqc17: 11 queued rows
            # for 4 distinct epics (pt_3c7f29f8 ×3, pt_c2e59181 ×3, pt_2c613da1
            # ×2, pt_c1e3318a ×2), every one from this seam — the heartbeat
            # sweep, which carries both guards, dispatched zero. The earlier
            # NEUTER missed it because its fixture kept the lease LIVE; the
            # guard is not unreachable, it is reachable once per lease TTL.
            # Guarded by tests/test_project_brain_dispatch_dedup.py.
            #
            # Consume-time discard (message_queue.dispatch_next_queued) remains
            # the backstop for a kickoff whose epic finished while queued — it
            # stops the BILLED task, but only after the queue has already
            # misreported its depth to the user, so it does not replace this.
            if _epic_already_queued(target, epic.get('id', '')):
                continue
            res = dispatch_epic(project_path, {**epic, '_via': 'dependency_done'},
                                target)
            if res.get('ok'):
                dispatched += 1
                # Drain a dependent kicked into an IDLE conv (which may differ
                # from the completing conv — the manager post-task hook only
                # drains the completing conv). Same cold-start gap as the sweep.
                _drain_idle_target(target)
    except Exception as e:
        logger.warning('[Dispatch] on_epic_completed failed proj=%.40r: %s',
                       project_path, e)
    return dispatched


def on_epic_answered(project_path: str, task_id: str) -> int:
    """Trigger seam: the human just ANSWERED a board question → re-dispatch
    that epic IMMEDIATELY (no 30 s heartbeat wait). The epic is re-read fresh
    and sanity-gated (effectively open, answer present, dependencies done) so
    a stale/answered-elsewhere call can't double-dispatch; then routed via the
    normal ``_dispatch_target`` + drained if idle (same cold-start machinery
    as the sweep). Best-effort; returns 1 when dispatched.
    """
    if not project_path or not task_id:
        return 0
    try:
        from lib.conversations.project_board import read_board
        board = read_board(project_path)
        epic = next((t for t in board['tasks'] if t['id'] == task_id), None)
        if not epic or epic.get('status') != 'open':
            return 0
        if not (epic.get('human_answer') or '').strip():
            return 0  # nothing to act on — the heartbeat sweep stays the path
        done_ids = {t['id'] for t in board['tasks'] if t['status'] == 'done'}
        if any(d not in done_ids for d in (epic.get('depends_on') or [])):
            logger.info('[Dispatch] answer received for %s but deps unmet; '
                        'leaving to the heartbeat sweep', task_id)
            return 0
        target = _dispatch_target(epic)
        if not target:
            logger.info('[Dispatch] answer received for %s but no routing '
                        'target; leaving to the heartbeat sweep', task_id)
            return 0
        if _conv_has_live_task(target) or _epic_already_queued(target, task_id):
            logger.info('[Dispatch] answer received for %s; target conv=%s is '
                        'busy/already-queued — kickoff left to the sweep',
                        task_id, target[:8])
            return 0
        res = dispatch_epic(project_path, {**epic, '_via': 'answered'}, target)
        if res.get('ok'):
            _drain_idle_target(target)
            logger.info('[Dispatch] answer → immediate re-dispatch epic=%s '
                        'conv=%s', task_id, target[:8])
            return 1
        return 0
    except Exception as e:
        logger.warning('[Dispatch] on_epic_answered failed proj=%.40r '
                       'task=%s: %s', project_path, task_id, e)
        return 0


def on_epic_posted(project_path: str, task_id: str) -> int:
    """Trigger seam: an epic was just POSTED to the board → start it
    IMMEDIATELY when it can genuinely start (no 30 s heartbeat wait).

    Fires ONLY on the genuinely-startable shape — the epic reads ``open``,
    every dependency is ``done``, the routing target conversation EXISTS and
    is IDLE. Every other shape falls back to the existing machinery
    unchanged:

      • deps unmet → the completion trigger (``on_epic_completed``) owns it;
      • target busy (the common case — an agent posts mid-turn) → the
        completion nudge (``on_conv_idle``) picks it up the moment the
        poster's turn ends, else the next heartbeat sweep;
      • target conv row MISSING → deliberately NOT dispatched here:
        ``dispatch_epic`` claims FIRST, so dispatching into a dead conv would
        strand the claim until lease expiry (worse than today's ≤30 s sweep
        delay). The sweep's claim/migration path owns that shape.

    Best-effort; returns 1 when dispatched. Never raises into the post path.
    """
    if not project_path or not task_id:
        return 0
    try:
        from lib.conversations.project_board import read_board
        board = read_board(project_path)
        epic = next((t for t in board['tasks'] if t['id'] == task_id), None)
        if not epic or epic.get('status') != 'open':
            return 0
        done_ids = {t['id'] for t in board['tasks'] if t['status'] == 'done'}
        if any(d not in done_ids for d in (epic.get('depends_on') or [])):
            return 0  # unmet deps — the completion trigger owns this one
        target = _dispatch_target(epic)
        if not target:
            return 0
        if _conv_has_live_task(target) or _epic_already_queued(target, task_id):
            return 0  # busy target — the completion nudge / sweep owns it
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT 1 FROM conversations WHERE id=? AND user_id=1 LIMIT 1',
            (target,)).fetchone()
        if not row:
            return 0  # dead/missing target — the sweep's migration owns it
        res = dispatch_epic(project_path, {**epic, '_via': 'posted'}, target)
        if not res.get('ok'):
            return 0
        _drain_idle_target(target)
        logger.info('[Dispatch] epic %s started at POST time (target %s idle; '
                    'no heartbeat wait)', task_id, target[:8])
        return 1
    except Exception as e:
        logger.warning('[Dispatch] on_epic_posted failed proj=%.40r task=%s: %s',
                       project_path, task_id, e)
        return 0


def on_conv_idle(project_path: str, conv_id: str) -> int:
    """Trigger seam: a conversation's task just completed with an EMPTY queue
    — it is going idle. If an open epic routes to THIS conv, dispatch + drain
    it NOW (no 30 s heartbeat wait).

    Bounded ONE per call: the drained task's own completion hook re-fires this
    seam for any remaining epics — the same chain shape the queue drain uses,
    so a backlog of open epics advances one per completed turn with zero
    heartbeat involvement. Epics routed to OTHER convs are not this seam's
    business (their own completion hooks, or the sweep, handle them).
    Best-effort; returns 1 when an epic was dispatched.
    """
    if not project_path or not conv_id:
        return 0
    try:
        if _conv_has_live_task(conv_id):
            return 0  # a successor already took over (fail-safe: never stack)
        for epic in select_dispatchable(project_path):
            if _dispatch_target(epic) != conv_id:
                continue
            res = dispatch_epic(project_path, {**epic, '_via': 'conv_idle'},
                                conv_id)
            if not res.get('ok'):
                return 0
            _drain_idle_target(conv_id)
            logger.info('[Dispatch] epic %s started at completion-nudge time '
                        '(conv %s went idle; no heartbeat wait)',
                        epic.get('id', '?'), conv_id[:8])
            return 1
        return 0
    except Exception as e:
        logger.warning('[Dispatch] on_conv_idle failed proj=%.40r conv=%s: %s',
                       project_path, conv_id[:8], e)
        return 0


__all__ = [
    'select_dispatchable', 'dispatch_epic', 'on_epic_completed',
    'on_epic_answered', 'on_epic_posted', 'on_conv_idle',
    'sweep_dispatch', 'sweep_all_active_projects', 'BRAIN_DISPATCH_MARKER',
]
