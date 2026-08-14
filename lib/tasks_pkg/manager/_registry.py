"""Task registry & lifecycle — create / discard / list / abort / quiesce, plus
the aborted-task terminal floor.

Reads/writes the shared singletons from ``_state`` (``tasks``, ``tasks_lock``,
``_chat_runtime``, the conv→latest-task index). ``_write_aborted_terminal_floor``
borrows the low-level persist helpers.
"""

import json
import threading
import time
import uuid

from lib.error_envelope import to_json as _err_to_json
from lib.log import get_logger

from lib.tasks_pkg.manager._state import (
    _abort_tombstones,
    _abort_tombstones_lock,
    _ABORT_TOMBSTONES_CAP,
    _chat_runtime,
    _clear_latest_task,
    _record_latest_task,
    tasks,
    tasks_lock,
)
from lib.tasks_pkg.manager._persist import (
    _merge_tool_rounds,
    _tool_rounds_have_dedicated_home,
    _upsert_task_row,
    build_result_meta,
)

logger = get_logger(__name__)


def task_user_id(task):
    """Resolve the owning ``user_id`` from a task dict for the SSOT channel.

    pt_abae3a85a92440fd (2026-07-25): background-thread callers of
    ``notify_conv_changed`` (autopilot / swarm / message_queue / sync /
    persistence_store) use this to thread request-scoped identity into the
    outbound busy signal. c6d1bd71 already stashed ``task['_userId']`` at
    ``create_task`` time; this helper is the canonical read.

    Falls back to ``DEFAULT_USER_ID = 1`` when the task dict is missing,
    empty, or lacks a bound user (personal-install / pre-auth / open-mode
    default). c6d1bd71's ``notify_conv_changed`` seam then coerces that
    default to unscoped for byte-identical single-user behaviour, so this
    helper is safe to call unconditionally at every write-path site.
    """
    from routes.common import DEFAULT_USER_ID
    if not task:
        return DEFAULT_USER_ID
    uid = task.get('_userId') if isinstance(task, dict) else None
    if not uid:
        return DEFAULT_USER_ID
    try:
        return int(uid) if str(uid).isdigit() else uid
    except (TypeError, ValueError) as _e:
        logger.debug('task user id: unexpected type/unparseable (%s)', _e)
        return uid


def is_carrier_task(task: dict) -> bool:
    """True if ``task`` is a non-streaming CARRIER/HOLDER, not user-visible work.

    Some flows use ``create_task`` purely as a message container that runs a
    synchronous sub-turn and is never a plain reconnect target:

      * the autopilot virtual-user (VU) sub-task (``_vu_subtask``), and
      * inline reporter / summarize holders (``_inline_messages``).

    These are ``status='running'`` while they execute. ``GET /api/chat/active``
    hides them and they are discarded from the registry the moment their
    synchronous run returns.

    ⚠️ CAREFUL WITH THE REASON — it is narrower than it looks. The historical
    justification was "a carrier never streams a ``done`` of its own, so
    reconnecting would birth a stuck 'Waiting…' bubble". For the VU carrier that
    is NO LONGER TRUE: ``lib.chat_dispatch._live_tick`` emits
    ``build_carrier_terminal_done(task)`` for ``_vu_subtask`` and closes the
    stream (observed live: conv ms5j3qi7wd1g7u task da0717c8, "emitting carrier
    done (VU sub-task terminal)"). The real constraint is narrower: a VU
    carrier's stream carries ONLY the ``autopilot_vu_*`` contract, so binding a
    real assistant placeholder to it renders the VU's frames as a ghost second
    "Agent" bubble. It must not be a PLAIN reconnect target — but it IS
    reachable through the VU connector.

    So a live VU carrier is deliberately visible-as-busy and reachable:
    ``snapshot_running_by_conv`` surfaces it as ``<tid>#vu``, and the client
    resolves it via ``pickVuCarrierForAttach`` → ``connectToTask(...,
    {vuCarrier: true})`` → ``_connectAutopilotKick`` (detached dummy assistant).
    Treating "carrier" as "must stay invisible" is what left cold attach with no
    route at all and produced 282.6s of generation the UI reported as finished.

    This predicate is the SINGLE SOURCE OF TRUTH for "carrier, not a plain
    reconnect target". BOTH the reconnect endpoint (``routes/chat.py``
    ``/api/chat/active``) AND the self-update restart guard
    (``list_running_tasks``) consult it, so the two can never again disagree
    about whether a carrier counts as a running conversation — the exact
    divergence that made the restart dialog report "N conversations running"
    while the sidebar showed none.

    The autopilot-KICK carrier (``_autopilot_kick``) is deliberately NOT a
    carrier here: it is a real UI-streaming task and must stay reconnectable.
    """
    return bool(task.get('_inline_messages') or task.get('_vu_subtask'))


def _vu_window_secs() -> float:
    """Ceiling on the PRE-CARRIER finalize sliver only (see below).

    Scope note (deliberately narrow): this bounds ONLY the window between
    the parent's terminal flip and the moment its VU carrier is registered
    — measured 2.5–26.7s (objective resolve + message assembly, see
    ``autopilot_event_forwarding._emit_vu_setup_phase``). Once the carrier
    exists, busy-ness is keyed on the CARRIER'S OWN LIVENESS and this
    ceiling is irrelevant.

    It is emphatically NOT a general "how long may a conv stay busy" timer:
    a VU turn legitimately runs for minutes, and bounding that by wall
    clock is exactly the bug this module fixes. 30s > the 26.7s measured
    worst case, and mirrors the ceiling the SSE reader applies to the same
    latch in ``lib/chat_dispatch.py``.
    """
    return 30.0


def is_vu_carrier_alive_for_conv(conv_id: str) -> bool:
    """True if ``conv_id`` has a LIVE autopilot VU carrier in the registry.

    Anchored on the CARRIER ITSELF — deliberately not on any parent's
    ``_vu_carrier_id`` back-pointer. The parent leaves the registry once
    its finalize returns, and the VU turn outlives it by minutes, so a
    parent-anchored lookup evaporates precisely during the window it is
    supposed to cover (the ms34u49egqwhug incident: carrier ran 8 rounds
    over ~7 minutes with no parent left to point at it).

    The carrier carries everything needed to stand on its own:
    ``_vu_subtask`` marks it, ``convId`` places it (the pt_8dc03017
    cutover registers it under the REAL conv id), and its ``status`` /
    ``aborted`` give liveness.
    """
    if not conv_id:
        return False
    with tasks_lock:
        for _tid, t in tasks.items():
            if (t.get('_vu_subtask')
                    and (t.get('convId') or '') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('aborted')):
                return True
    return False


def conv_has_work_in_flight(task: dict, *, now: float | None = None) -> bool:
    """True if ``task`` means its conversation is STILL WORKING for the user.

    This answers a DIFFERENT question from ``status == 'running'``, and the
    difference is the ms34u49egqwhug incident (2026-07-27): the orchestrator
    flips the parent to ``status='done'`` at the terminal seam
    (``orchestrator/_finalize.py``) and only THEN, in the same synchronous
    call stack, runs ``maybe_run_autopilot`` — which executes an entire VU
    turn that can take minutes. During that window ``status`` correctly means
    "the stream body is finished", but the conversation is emphatically NOT
    idle: an autopilot turn is mid-flight and the user must see a live
    indicator and be able to stop it.

    Historically only ONE of the three readers of that fact honoured it:

      * the SSE live-tick (``lib/chat_dispatch.py``) held its LATE-done while
        ``task['_finalize_started_at']`` was fresh — which is why the stream
        stayed open and the transcript kept updating;
      * the busy projection (``snapshot_running_by_conv`` → the sidebar dot
        and the composer Send/Stop button) did not — so the UI reported
        "generation complete" while 8 LLM rounds ran;
      * the reconnect view (``/api/chat/active``) does not either, but that is
        a deliberate and separate concern (reconnecting a carrier's SSE would
        birth a stuck bubble) — discoverability, not busy-ness.

    Rather than teach the busy reader a second, divergent notion of "running",
    this predicate is the ONE place that definition lives.

    Two ways a task carries work in flight:

      1. ``status == 'running'`` and not aborted — the ordinary case. NOTE
         this ALREADY covers a live VU carrier, which is a running task in
         its own right; the projection's job is merely not to filter it out.
      2. a FRESH ``_finalize_started_at`` latch — covers ONLY the sliver
         between the parent's terminal flip and its carrier's registration
         (see :func:`_vu_window_secs`). Everything after the carrier exists
         is covered by term 1 via the carrier itself, NOT by this timer.

    ``aborted`` always wins: the instant the user presses Stop (or supersede
    fires) the conversation must read idle so the composer returns to Send.
    """
    if not task or task.get('aborted'):
        return False
    if task.get('status') == 'running':
        return True
    # Pre-carrier finalize sliver ONLY. Once the carrier is registered it is
    # itself a running task and term 1 covers the conv — this branch must
    # never be the thing keeping a long VU turn visible.
    _fin = task.get('_finalize_started_at') or 0
    if _fin:
        _now = time.time() if now is None else now
        if (_now - _fin) < _vu_window_secs():
            return True
    return False


def create_task(conv_id, messages, config, *, supersede=True):
    """Create (and register) a chat task.

    ``supersede`` (default True) makes superseding the INVARIANT of task
    creation: after registering as the conversation's latest task, any OTHER
    still-running task for the same ``conv_id`` is force-aborted (via
    ``abort_running_tasks_for_conv``). This is the single source of truth for
    the "a new task supersedes the old one" rule — every background path that
    creates a task (queue ``dispatch_next_queued``, scheduler/proactive/timer
    ``inject_and_run_task``) is automatically covered, instead of each entry
    point having to remember to call the abort sweep.

    Pass ``supersede=False`` for a DELIBERATE concurrency axis that must run
    alongside its siblings under the same conv_id — currently only
    ``chat_branch_start`` (a branch is an intentional parallel turn and must
    NOT abort the main task or sibling branches).
    """
    task_id = str(uuid.uuid4())
    # ── Extract the user's original question from the last user message ──
    # This is passed to the content filter alongside the search query so
    # the filter can assess relevance against the ORIGINAL intent, not just
    # the model-generated search keywords.
    last_user_query = ''
    last_user_idx = -1
    for i in range(len(messages or []) - 1, -1, -1):
        m = messages[i]
        if m.get('role') == 'user':
            c = m.get('content', '')
            if isinstance(c, list):
                # multimodal: extract text blocks
                c = ' '.join(b.get('text', '') for b in c if isinstance(b, dict) and b.get('type') == 'text')
            last_user_query = (c or '')[:500]
            last_user_idx = i
            break

    # ── UserPromptSubmit hooks (Claude Agent SDK parity) ──
    # Fire ONCE per turn, BEFORE the prompt enters the agent loop. Hooks
    # can rewrite the latest user message (PII redaction, safety filters,
    # prompt augmentation).  Only the rewritten text is propagated; the
    # message structure (role, attachments, tool_call_id) is preserved.
    if last_user_idx >= 0 and isinstance(messages[last_user_idx].get('content'), str):
        try:
            from lib.tasks_pkg.tool_hooks import run_user_prompt_hooks
            _orig = messages[last_user_idx]['content']
            _rewritten = run_user_prompt_hooks(_orig, {
                'id': task_id, 'convId': conv_id, 'config': config or {},
            })
            if _rewritten != _orig:
                messages[last_user_idx]['content'] = _rewritten
                last_user_query = _rewritten[:500]
        except Exception as e:
            logger.warning('[Task %s] UserPromptSubmit hooks failed: %s',
                           task_id[:8], e, exc_info=True)

    # Create through TaskRuntime so the task is registered in the unified
    # store. Then augment with every chat-specific field that downstream
    # code (orchestrator, route handlers, tool_display, …) depends on.
    task = _chat_runtime.create(
        task_id=task_id,
        meta={'convId': conv_id, 'msg_count': len(messages or [])},
    )
    task.update({
        'convId': conv_id, 'messages': messages, 'config': config,
        # ★ Stable assistant message id, minted CLIENT-SIDE before the send
        #   POST and shipped in config.assistantMsgId. The frontend stamps the
        #   same id on the streaming bubble (data-msg-id), so live progressive
        #   translation frames (incremental._Acc._push_progressive) can route
        #   to the still-streaming message — which has no DB index yet. Also
        #   reused as the final commit's msg_id so the in-stream preview and the
        #   committed translation address the SAME message. Empty for non-UI /
        #   external callers → live preview is simply skipped (no regression).
        '_assistantMsgId': (config or {}).get('assistantMsgId') or '',
        # v2 lifecycle identity. Internal task ids remain executor plumbing;
        # public state is addressed only by this stable turn / attempt pair.
        '_turnProtocolV2': bool((config or {}).get('_turnProtocolV2')),
        '_turnId': (config or {}).get('_turnId') or '',
        '_attemptId': (config or {}).get('_attemptId') or '',
        # Override TaskRuntime defaults with chat-specific shape:
        'status': 'running',          # chat tasks start running, not pending
        'content': '', 'thinking': '', 'error': None,
        'aborted': False, 'toolRounds': [],
        'content_lock': threading.Lock(),
        '_contentEpoch': 0,
        'finishReason': None, 'usage': None, 'toolSummary': None,
        'phase': None,                # current phase for polling fallback
        # ★ Timing anchor: when the task was created (route thread). Used by
        #   run_task / stream_llm_response to log queue-wait, prep time, and
        #   time-to-first-token so the "waiting" window can be analysed.
        '_t_created': time.time(),
        'lastUserQuery': last_user_query,
        '_initial_msg_count': len(messages or []),  # cross-talk detection
        '_premature_retry_count_phase': 0,
        # '_force_rotate_pair' is set transiently by analyse_stream_result
        # and consumed (cleared) by stream_llm_response on the next call.
    })
    try:
        from lib.log import req_id as _current_request_id
        task['_requestId'] = _current_request_id() or task.get('_requestId', '')
    except Exception as e:
        logger.debug('[Task %s] request correlation capture failed: %s',
                     task_id[:8], e)
    # ★ Identity scope for the personal-preference profile. Resolved HERE,
    #   in the request thread, because the post-turn consolidation runs in a
    #   detached daemon with no request context. The scope is the multi-user
    #   tenant's user_id (populated only by login); open/private mode leave it
    #   empty → the single global profile (personal-install semantic, no
    #   migration). Best-effort: any failure (no request ctx) → '' = global.
    #
    # pt_ab42421158214591 (2026-07-25): also stash ``_userId`` for the
    # SSOT conv-state channel. Same source (current_auth()), same
    # request-thread capture rationale — the task registry's
    # snapshot_running_by_conv uses this to scope by owner so a sibling
    # device on a different tenant doesn't receive the wrong busy dot.
    # Empty string is the pre-auth / single-user default and is treated
    # as "unscoped" by every reader (back-compat with the personal
    # install).
    try:
        from lib.memory.user_profile import resolve_profile_scope
        from routes.api_v1.auth import current_auth
        _ctx = current_auth()
        task['_profileScope'] = resolve_profile_scope(_ctx)
        task['_userId'] = getattr(_ctx, 'user_id', '') or '' if _ctx else ''
    except Exception as e:
        logger.debug('[Task %s] identity resolve failed: %s', task_id[:8], e)
        task['_profileScope'] = ''
        task['_userId'] = ''

    # ── Ingress affinity capture ──────────────────────────────────────
    # The browser derives this key before task creation (stable per
    # conversation, random only without one) and the load balancer hashes it.
    # Persisting it with the durable-at-birth row lets /active on
    # ANY replica hand a reloading/second-device client the exact same routing
    # key. It is routing metadata only (never an auth credential).
    #
    # Background children inherit their conversation's last direct key from
    # the shared runtime store. Only a task created in an actual request is
    # marked reconnectable here: that excludes invisible VU/inline carriers
    # from the cross-replica DB projection while the local registry continues
    # to apply its richer is_carrier_task predicate.
    _direct_affinity = False
    _affinity_key = ''
    try:
        from quart import has_request_context, request
        if has_request_context():
            _affinity_key = (
                request.headers.get('X-Tofu-Affinity-Key', '') or '')[:256]
            _direct_affinity = bool(_affinity_key)
    except Exception as e:
        logger.debug('[Task %s] request affinity capture failed: %s',
                     task_id[:8], e)
    try:
        from lib.runtime_state_store import get_store
        _affinity_store = get_store()
        if not _affinity_key and conv_id:
            _affinity_key = (
                _affinity_store.get_value('conv-affinity', conv_id) or '')[:256]
        if _affinity_key and conv_id:
            # Mapping retention is not a task deadline; it only permits a
            # future child/reconnect to route to the existing owner.
            _affinity_store.set_value(
                'conv-affinity', conv_id, _affinity_key, 365 * 24 * 3600)
    except Exception as e:
        logger.debug('[Task %s] shared affinity mirror failed: %s',
                     task_id[:8], e)
    if _affinity_key:
        task['_affinityKey'] = _affinity_key
    task['_reconnectable'] = _direct_affinity

    # ★ Durable-at-birth: write the task_results row AT CREATION
    #   (status='running', empty content/thinking). The running-checkpoint
    #   writers only fire on content/thinking deltas and per-round boundaries,
    #   so a task killed by a server restart BEFORE its first delta left NO row
    #   at all — and the cold-replay / poll-DB / startup-recovery stale-scan
    #   all found NOTHING (the ms43foj3 incident: resume task killed 87s in,
    #   R1 pure tool_calls → zero content/thinking deltas →
    #   checkpoint_task_partial's empty-guard no-op'd every time → poll and
    #   stream returned 404 'Task not found' → the frontend minted a terminal
    #   error bubble for what was really a transport-level task loss). With
    #   the row existing from second 0, every one of those readers resolves
    #   the task to its real state (running → interrupted after recovery)
    #   instead of a 404. Best-effort: a write failure must never break task
    #   creation; the checkpoint/persist writers upsert over it last-wins.
    try:
        _birth_meta = {}
        _bcfg = config or {}
        if _bcfg.get('model'):
            _birth_meta['model'] = _bcfg['model']
        if _bcfg.get('preset'):
            _birth_meta['preset'] = _bcfg['preset']
        if _bcfg.get('thinkingDepth'):
            _birth_meta['thinkingDepth'] = _bcfg['thinkingDepth']
        if task.get('_affinityKey'):
            _birth_meta['affinityKey'] = task['_affinityKey']
        if task.get('_reconnectable'):
            _birth_meta['reconnectable'] = True
        if task.get('_userId') not in (None, ''):
            _birth_meta['userId'] = str(task['_userId'])
        if task.get('_requestId'):
            _birth_meta['requestId'] = task['_requestId']
        _upsert_task_row(
            task, conv_id or '', content='', thinking='', status='running',
            error_json=None, tr_json=None,
            meta_json=(json.dumps(_birth_meta, ensure_ascii=False)
                       if _birth_meta else None))
    except Exception as e:
        logger.warning('[Task %s] durable-at-birth row write failed (non-fatal): %s',
                       task_id[:8], e)

    # ★ Project-brain Activity Feed: a 'started' pulse, EXCEPT for autopilot
    #   follow-up turns (config.autopilotRunId set) — a deep autopilot run is
    #   dozens of tasks and would flood the feed; those collapse to a single
    #   'run_concluded' event at run close-out (autopilot._emit_run_concluded).
    #   Best-effort: emit_project_event never raises, but guard the lookup too
    #   so feed wiring can NEVER break task creation.
    try:
        _cfg = config or {}
        _proj = (_cfg.get('projectPath') or '').strip()
        if _proj and conv_id and not (_cfg.get('autopilotRunId') or '').strip():
            from lib.conversations.project_feed import emit_project_event
            emit_project_event(
                _proj, conv_id, 'started',
                (last_user_query or '').strip() or 'New turn started',
                task_id=task_id)
    except Exception as e:
        logger.debug('[Task %s] project-feed started emit skipped: %s',
                     task_id[:8], e)

    # ★ Register as the LATEST task for this conversation — freshness guard
    if conv_id:
        _record_latest_task(conv_id, task_id)
        # ★ Supersede invariant (see docstring): abort any other running task
        #   for this conv so "a new task replaced the old one without aborting
        #   it" is structurally impossible. Registered as latest FIRST so the
        #   superseded tasks' freshness guard classifies their late writes as
        #   expected (superseded_by_new_task), not as the unexpected-WARNING
        #   never-aborted branch. Best-effort: never let it break creation.
        if supersede:
            try:
                abort_running_tasks_for_conv(conv_id, exclude_task_id=task_id)
            except Exception as e:
                logger.warning('[Task %s] supersede abort sweep failed: %s',
                               task_id[:8], e, exc_info=True)
    logger.info('[Task %s] Created for conv=%s lastUserQuery=%r', task_id[:8], conv_id, last_user_query[:80])
    return task


def discard_task(task_id: str, conv_id: str | None = None) -> None:
    """Remove a non-streaming carrier/holder task from the active registry.

    Some flows use ``create_task`` purely as a message container for a
    synchronous reporter sub-turn (e.g. ``autopilot.summarize_run``) — the
    carrier is NEVER spawned and NEVER reaches a terminal status, so it would
    otherwise linger forever as a phantom ``status='running'`` row that
    ``/api/chat/active`` reports and the frontend orphan-recovery turns into a
    permanently-stuck "Waiting…" placeholder. (TTL cleanup only evicts
    done/error/aborted tasks, so a never-finalized carrier is immortal.)

    This drops the task from ``tasks`` AND clears any ``_conv_latest_task``
    entry it claimed, so the carrier is invisible to every reconnect path. Safe
    to call unconditionally (idempotent, best-effort).
    """
    # Observability (pt_a21cd6eb ③-1): this is the ONLY registry pop for
    # non-terminal chat tasks. A live task that vanished from the registry
    # while its worker thread kept running (fb6d1f8d / 7ddbc751, 2026-08-01)
    # left zero fingerprints — log every pop with the caller so the next
    # evaporation leaves a trail. Rare path; the frame read is cheap enough.
    try:
        import sys as _sys
        _caller = _sys._getframe(1).f_code.co_name
    except Exception as e:
        logger.debug('[Manager] discard_task: caller-frame read failed: %s', e)
        _caller = '?'
    with tasks_lock:
        _popped = tasks.pop(task_id, None)
    logger.info('[Manager] discard_task: task=%s conv=%s popped=%s caller=%s',
                (task_id or '?')[:8], (conv_id or '')[:8],
                bool(_popped), _caller)
    if _popped:
        try:
            from lib.observability import record_registry_eviction
            record_registry_eviction('chat', 'discard')
        except Exception as exc:
            logger.debug('[Manager] discard_task metric skipped: %s', exc)
    if conv_id:
        # Clears the store mirror too — a local-only delete leaves the
        # store-backed _latest_task_for_conv returning this corpse for up to
        # 1h (TTL), which is the msb6ohqi 2026-08-02 stall class.
        _clear_latest_task(conv_id, expect_task_id=task_id)


# ═══════════════════════════════════════════════════════════════════════════
#  Abort tombstone channel (pt_a21cd6eb ③-3)
# ═══════════════════════════════════════════════════════════════════════════

_DB_TOMBSTONE_POLL_S = 5.0  # abort_check 回读 DB tombstone 的最小间隔


def _db_abort_tombstoned(task_id: str) -> bool:
    """True when the task_results row carries a durable abort tombstone."""
    if not task_id:
        return False
    try:
        from lib.database import DOMAIN_CHAT, pooled_db
        with pooled_db(DOMAIN_CHAT) as db:
            row = db.execute(
                'SELECT abort_requested_at FROM task_results WHERE task_id=?',
                (task_id,)).fetchone()
        if not row:
            return False
        return bool(int(row['abort_requested_at'] or 0))
    except Exception as e:
        logger.debug('[Manager] db tombstone probe failed task=%s: %s',
                     task_id[:8], e)
        return False


def _write_abort_tombstone_row(task_id: str, source: str) -> bool:
    """Best-effort DB half of the tombstone (cross-process visibility).

    Dedicated columns are deliberately excluded from checkpoint UPSERTs, so a
    concurrent whole-metadata replacement cannot erase this mark.
    """
    try:
        from lib.database import (
            DOMAIN_CHAT, db_execute_with_retry, pooled_db)
        with pooled_db(DOMAIN_CHAT) as db:
            cursor = db_execute_with_retry(
                db,
                'UPDATE task_results SET abort_requested_at=?, abort_source=? '
                "WHERE task_id=? AND status='running'",
                (int(time.time() * 1000), source, task_id),
                return_cursor=True)
        return cursor.rowcount == 1
    except Exception as e:
        logger.debug('[Manager] abort tombstone row write failed task=%s: %s',
                     task_id[:8], e)
        return False


def plant_abort_tombstone(task_id: str, *, source: str) -> bool:
    """Record an abort request for a task the registry has lost.

    Returns True only when a LIVE (status='running') task_results row exists
    — the endpoint uses that to distinguish “signal planted” from a genuine
    404 (no such task / already terminal).
    """
    if not task_id:
        return False
    live = _write_abort_tombstone_row(task_id, source)
    if not live:
        return False
    with _abort_tombstones_lock:
        if len(_abort_tombstones) >= _ABORT_TOMBSTONES_CAP:
            logger.warning('[Manager] abort tombstone set at cap %d — clearing '
                           'before insert (pathological volume)',
                           _ABORT_TOMBSTONES_CAP)
            _abort_tombstones.clear()
        _abort_tombstones.add(task_id)
    logger.info('[Manager] ⚠️ abort tombstone planted for task=%s (source=%s) — '
                'registry lost this task; the live worker consumes the mark '
                'at its next abort poll', task_id[:8], source)
    try:
        from lib.log import audit_log as _audit
        _audit('task_abort_tombstone', task_id=task_id, source=source)
    except Exception as _ae:
        logger.debug('[Manager] tombstone audit failed: %s', _ae)
    return True


def plant_abort_tombstones_for_conv(conv_id: str, *, source: str) -> int:
    """Tombstone every running DB row for ``conv_id`` the registry has lost.

    Rows still IN the registry are skipped — the plain supersede/abort sweep
    already flags them cooperatively.
    """
    if not conv_id:
        return 0
    try:
        from lib.database import DOMAIN_CHAT, pooled_db
        with pooled_db(DOMAIN_CHAT) as db:
            rows = db.execute(
                "SELECT task_id FROM task_results WHERE conv_id=? AND status='running'",
                (conv_id,)).fetchall()
    except Exception as e:
        logger.warning('[Manager] conv tombstone scan failed conv=%s: %s',
                       conv_id[:8], e)
        return 0
    n = 0
    with tasks_lock:
        live_ids = set(tasks.keys())
    for r in rows:
        tid = r['task_id'] if hasattr(r, 'keys') else r[0]
        if tid and tid not in live_ids:
            if plant_abort_tombstone(tid, source=source):
                n += 1
    if n:
        logger.info('[Manager] conv=%s tombstoned %d registry-lost running '
                    'task(s)', conv_id[:8], n)
    return n


def has_abort_tombstone(task_id: str) -> bool:
    with _abort_tombstones_lock:
        return task_id in _abort_tombstones


def make_task_abort_check(task: dict):
    """Build the abort_check closure for dispatch/stream retry loops.

    ANDs three channels (pt_a21cd6eb ③-3):
      1. the cooperative in-memory flag ``task['aborted']`` — the normal path;
      2. the in-process tombstone set — an abort that arrived while the task
         was MISSING from the registry (the 2026-08-01 evaporation family);
      3. a throttled (>= ``_DB_TOMBSTONE_POLL_S``) read-back of the
         task_results metadata tombstone — the cross-process leg.
    The dispatch loop polls this every 429/retry cycle, so a tombstoned ghost
    dies at its next cycle with the normal AbortedError unwind.
    """
    task_id = (task or {}).get('id', '')
    _st = {'hit': False, 'last_db': 0.0}

    def _check() -> bool:
        if _st['hit']:
            return True
        if task.get('aborted'):
            return True
        with _abort_tombstones_lock:
            if task_id in _abort_tombstones:
                _st['hit'] = True
                logger.info('[Task %s] abort tombstone consumed (in-memory '
                            'channel) — aborting', task_id[:8])
                return True
        now = time.monotonic()
        if now - _st['last_db'] >= _DB_TOMBSTONE_POLL_S:
            _st['last_db'] = now
            if _db_abort_tombstoned(task_id):
                _st['hit'] = True
                logger.info('[Task %s] abort tombstone consumed (db channel) '
                            '— aborting', task_id[:8])
                return True
        return False

    return _check


def write_carrier_terminal_row(task, status: str) -> None:
    """Persist a terminal ``task_results`` row for a synchronous CARRIER task.

    The autopilot VU sub-task (and any future row-producing carrier) runs
    under ``_endpoint_managed=True``, which BY DESIGN suppresses the
    orchestrator's terminal-status flip + ``persist_task_result`` — the
    carrier's own finalize early-returns. Its per-round
    ``checkpoint_task_partial`` writes therefore leave the row at
    ``status='running'`` forever (the ms2gipv5 zombie generator,
    pt_8a491f9d): the in-memory ``discard_task`` only cleans the registry,
    and the next startup recovery sweep collects the stale row as a
    crash-interrupted turn.

    The carrier's LIFECYCLE OWNER (``autopilot.run_virtual_user``'s
    finally) calls this right after ``discard_task`` so the row reaches a
    terminal state in the same breath as the registry cleanup. Idempotent,
    last-writer-wins (keyed on task_id, same ``_upsert_task_row`` channel as
    ``_write_aborted_terminal_floor``); best-effort — a settle failure must
    never break the owner's finally.

    ``status`` is derived by the caller from the carrier's end state:
    'done' (turn completed — the normal path), 'aborted' (parent abort /
    real-message preemption), 'error' (died before any finish reason).
    """
    if status not in ('done', 'aborted', 'error'):
        logger.warning('[Task %s] write_carrier_terminal_row: unexpected status %r '
                       '— defaulting to done', (task.get('id') or '?')[:8], status)
        status = 'done'
    try:
        conv_id = task.get('convId', '') or ''
        tr_json = (None if _tool_rounds_have_dedicated_home(task)
                   else json.dumps(_merge_tool_rounds(task), ensure_ascii=False))
        meta = build_result_meta(task)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status=status,
                         error_json=error_json, tr_json=tr_json, meta_json=meta_json)
        logger.info('[Task %s] conv=%s Carrier terminal row settled: status=%s',
                    task['id'][:8], conv_id[:8], status)
    except Exception as e:
        logger.warning('[Task %s] Failed to settle carrier terminal row: %s',
                       (task.get('id') or '?')[:8], e, exc_info=True)


def list_running_tasks(exclude_conv_id: str | None = None) -> list[dict]:
    """Return one entry per CONVERSATION with genuinely-live running work.

    Used by the self-update restart guard to refuse a process re-exec that
    would kill sibling conversations' in-flight work. A restart is an
    unconditional ``os.execv`` of the whole server, so EVERY running task
    dies with it — this lets the caller detect that and require an explicit
    override.

    Three filters make the count reflect reality rather than registry cruft, so
    the guard never blocks a restart for work that is not actually running:

      * **Carrier filter (same judge as ``/api/chat/active``).** A non-streaming
        CARRIER/HOLDER (``is_carrier_task``: the autopilot VU sub-task or an
        inline reporter holder) is ``status='running'`` while it executes but is
        invisible to the frontend by design (the reconnect endpoint hides it,
        the sidebar never lights a dot for it). Counting it made the restart
        dialog report "N conversations running" that the user could see nowhere.
      * **Activity filter (same judge as the reaper).** A task whose BOTH
        liveness clocks (``_t_last_event`` and ``_dispatch_heartbeat``) have
        been silent past ``_stuck_task_max_silent_secs()`` is WEDGED — the exact
        signal ``reap_stuck_running_tasks`` uses to force-fail it. Such a task
        is excluded here too, so a just-died zombie does not block a restart for
        the whole 30-minute reaper window (it would otherwise be counted until
        the next reaper tick flips it terminal). ``status=='running'`` alone is
        NOT liveness — it is exactly the false signal that produced the "63
        other conversations have running tasks" phantom. If the reaper is
        disabled (threshold ``<=0``) no task is treated as wedged (mirrors the
        reaper), so behaviour is unchanged there.
      * **Per-conversation dedup.** A single conversation (autopilot especially)
        can spawn dozens of tasks; counting per-task turned "3 busy convs" into
        "63". Entries are keyed by ``convId`` so the count is the number of
        distinct conversations a restart would interrupt. Tasks with no convId
        (headless / external callers) are NOT collapsed — each stays its own
        entry keyed on its task id.

    Args:
        exclude_conv_id: When set, running tasks belonging to this conversation
            are omitted (the caller triggering the restart doesn't count its
            own conversation against itself).

    Returns:
        A list of ``{'taskId', 'convId', 'elapsed'}`` dicts, one per distinct
        live conversation (representative = the earliest-created live task of
        that conv). Best-effort snapshot taken under ``tasks_lock``.
    """
    try:
        from lib.tasks_pkg.manager._maintenance import _stuck_task_max_silent_secs
        max_silent = _stuck_task_max_silent_secs()
    except Exception as e:
        logger.debug('[Manager] list_running_tasks: reaper threshold lookup failed '
                     '(%s) — skipping activity filter', e)
        max_silent = 0

    now = time.time()
    # Keyed by dedup identity so one conversation counts once. Keep the
    # earliest-created live task as the representative (stable, oldest work).
    by_key: dict[str, tuple[float, dict]] = {}
    with tasks_lock:
        for tid, t in tasks.items():
            if t.get('status') != 'running' or t.get('aborted'):
                continue
            # ★ Skip non-streaming CARRIER/HOLDER tasks (VU sub-task / inline
            #   reporter) — same predicate GET /api/chat/active uses to hide
            #   them from reconnect. Without this a background autopilot VU
            #   carrier (convId='', never surfaced in the sidebar) counted as
            #   a "running conversation" and made the restart dialog claim work
            #   was in flight that the user could not see anywhere.
            if is_carrier_task(t):
                continue
            conv = t.get('convId') or ''
            if exclude_conv_id and conv == exclude_conv_id:
                continue
            created = t.get('created_at', now)
            # Activity filter — exclude WEDGED tasks (both clocks stale), the
            # same predicate reap_stuck_running_tasks uses. Either clock fresh
            # = alive. Disabled (max_silent<=0) → never treat as wedged.
            if max_silent > 0:
                last_event = t.get('_t_last_event', created)
                heartbeat = t.get('_dispatch_heartbeat', created)
                if (now - last_event) >= max_silent and (now - heartbeat) >= max_silent:
                    continue
            # Dedup key: real conversations collapse by convId; convId-less
            # tasks each stay distinct (keyed on their unique task id).
            key = conv if conv else ('\x00task:' + tid)
            entry = {
                'taskId': tid,
                'convId': conv,
                'elapsed': round(now - created, 1),
            }
            prior = by_key.get(key)
            if prior is None or created < prior[0]:
                by_key[key] = (created, entry)
    return [entry for _created, entry in by_key.values()]


def snapshot_running_by_conv(user_id: str = '') -> dict[str, list[str]]:
    """Return ``{conv_id: [task_id, ...]}`` for every non-carrier running task.

    P1 of pt_conv_state_ssot: the single read the ``notify_conv_changed`` seam
    uses to project the busy fact into the outbound frame. This is the SSOT
    for "which conversations have live tasks", replacing the settings-derived
    ``activeTaskId`` (single value) heuristic that made cross-device sidebars
    disagree.

    Multi-tenant scoping (pt_ab42421158214591, 2026-07-25): when ``user_id``
    is set to a non-empty string, only tasks whose ``task['_userId']`` matches
    are included — closes the multi-tenant leak where user B's tab would
    otherwise receive a snapshot built from user A's registry. Empty string
    (default) returns EVERY non-carrier running task regardless of owner —
    this is the single-user / personal-install / pre-auth default, and also
    the fallback for write-path callers that have no request context to
    resolve a user from.

    Semantics — deliberately DIFFERENT from ``list_running_tasks``:

      * **NO activity/wedge filter.** A task whose event/heartbeat clocks have
        gone silent is still "supposed to be running" from the sidebar's POV
        — the reaper (~30 min) is what decides it is finally stuck, at which
        point it writes a terminal row and this snapshot drops it naturally.
        The restart-guard needs strict liveness ("is anyone actually working
        right now"); the busy-dot needs the broader "is it supposed to be
        working" so a temporarily-quiet task does not extinguish the dot and
        then re-light it 100 ms later. Two different questions → two
        different helpers.
      * **Carrier filter (same as list_running_tasks).** Autopilot VU
        sub-tasks and inline reporter carriers must not surface — they
        have no user-visible bubble and would light a permanent phantom
        sidebar dot.
      * **Empty-convId tasks dropped.** A task with no ``convId`` cannot be
        projected into any sidebar row; it stays invisible.
      * **Aborted / non-running dropped.** Once ``t['aborted']`` flips true
        the dot should extinguish immediately, so the client sees Send (not
        Stop) the instant supersede fires.
      * **Multiple tasks per conv preserved.** ``list_running_tasks`` dedups
        to one representative per conv for the restart guard's counting; we
        do NOT — the client needs the FULL set so ``_reconnectServerTaskIfIdle``
        can rejoin any of them, and later drift-check equality is strict.

    Read-only. Snapshot taken under ``tasks_lock``; safe to call from any
    thread. Ordering within each conv's list is registry-iteration order
    (approximately creation order) — deterministic per-process but not
    guaranteed across replicas. Clients treat the list as a SET.
    """
    scope_user = str(user_id or '')
    out: dict[str, list[str]] = {}
    _now = time.time()
    with tasks_lock:
        for tid, t in tasks.items():
            # Turn-v2 work is public by attemptId and reconnects through the
            # durable attempt cursor. Leaking its executor task id into the
            # legacy busy snapshot makes old clients call connectToTask and
            # create a second DOM owner for the same stable turn.
            if t.get('_turnProtocolV2'):
                continue
            # ★ "Is this conv still working for the user?" — the SHARED
            #   predicate, not a bare status read. This is what covers the
            #   finalize/VU window in which the parent is already
            #   status='done' while its autopilot VU turn still runs
            #   (ms34u49egqwhug: the sidebar and composer read "generation
            #   complete" through ~7 minutes of real backend work because
            #   the parent had flipped terminal AND the carrier was hidden
            #   by the carrier filter — both candidates dropped out for
            #   DIFFERENT reasons and the busy set went empty).
            if not conv_has_work_in_flight(t, now=_now):
                continue
            conv = t.get('convId') or ''
            if not conv:
                continue
            if scope_user:
                # Explicit scope: include ONLY tasks owned by this user.
                # Pre-auth tasks with empty ``_userId`` do NOT surface into a
                # scoped snapshot — a scoped caller is asking "what does
                # user X see?" and X does not own a legacy-null task.
                if str(t.get('_userId') or '') != scope_user:
                    continue
            # ★ Carrier split — a carrier is NOT independently reconnectable
            #   (its SSE never completes), so its id must never be offered as
            #   an ATTACH target — but a LIVE VU CARRIER is precisely the thing
            #   whose existence means "this conversation is working", so it
            #   MUST light the busy dot. Anchor the busy fact on the carrier
            #   itself; other carriers (inline reporters / summarize holders)
            #   genuinely have no user-visible work and still contribute
            #   neither id nor dot.
            #
            #   THE WIRE SHAPE IS LOAD-BEARING: the client treats an EMPTY
            #   runningTaskIds list as IDLE (computeConvBusy →
            #   ``_authoritativeActiveTaskIds.size > 0``). So "busy, but the
            #   only worker is a non-attachable VU carrier" must NOT look
            #   identical to "idle". We therefore surface the carrier's id
            #   with a NON-ATTACHABLE marker the reducer strips when building
            #   the busy Set — the conv reads busy (Set non-empty) while no
            #   dangling attach target leaks into the reconnect path.
            ids = out.setdefault(conv, [])
            if is_carrier_task(t):
                if t.get('_vu_subtask'):
                    ids.append(tid + '#vu')
            else:
                ids.append(tid)
    return out


def notify_terminal_busy_state(task) -> None:
    """Push the authoritative busy projection for ``task``'s conversation AT
    the task's terminal seam (epic pt_3ea0e045).

    Before this, the busy channel's CLEAR signal rode only the NEXT incidental
    conversation write (a checkpoint sync / a client PUT), and for the ~30s
    the ``_finalize_started_at`` latch was held even those frames were forced
    to say busy. Measured on conv ms91b45tva0sym (2026-08-01): the turn
    settled at 09:20:43 (message fr=stop, footer painted) yet the sidebar
    showed 回答中 and the composer showed Stop for 103s+, and every
    click-open attached to the DEAD task and replayed its done — the exact
    "finish tag vs generating" contradiction the owner reported. On a tab
    busy with other conversations the designed fallback (the
    ``_crossDeviceReconcile`` conv-state poll, gated on
    ``activeStreams.size === 0``) starves, so without a frame the stale
    badge survived until F5.

    Called from the orchestrator's ``_finalize_and_emit_done`` (after the
    latch pop — the autopilot hook has concluded by then, so a spawned VU
    carrier is registered and projects itself as ``<tid>#vu``, and an
    ordinary settle projects as IDLE) and from the endpoint loop's
    ``_finalize`` (which holds no latch). ``rev=None`` deliberately: the
    client applies the reducer half of a rev-less frame (busy update only)
    without triggering a body refetch.
    """
    try:
        conv_id = (task or {}).get('convId') or ''
        if not conv_id:
            return
        from lib.conversations import notify_conv_changed
        notify_conv_changed(conv_id, rev=None, user_id=task_user_id(task))
    except Exception as e:
        logger.debug('[Manager] terminal busy notify failed task=%s: %s',
                     ((task or {}).get('id') or '?')[:8], e)


def abort_running_tasks_for_conv(conv_id: str, exclude_task_id: str | None = None) -> int:
    """Abort all running tasks for a conversation, except the excluded one.

    Called when starting a new task (send/regenerate/edit) to ensure the old
    task stops writing to the conversation DB. Returns the count of aborted tasks.

    This is the **critical fix** for the stale-task-overwrites-regeneration bug:
    without this, the old task's _sync_result_to_conversation races with the
    new task and may overwrite the conversation with stale content.
    """
    aborted = 0
    _aborted_tasks = []
    with tasks_lock:
        for tid, t in tasks.items():
            if (t.get('convId') == conv_id
                    and t['status'] == 'running'
                    and tid != exclude_task_id
                    and not t.get('aborted')):
                t['aborted'] = True
                t['_abort_timestamp'] = time.time()
                t['_abort_reason'] = 'superseded_by_new_task'
                aborted += 1
                _aborted_tasks.append(t)
                logger.info(
                    '[Task %s] conv=%s ⚠️ AUTO-ABORTED: superseded by new task %s — '
                    'content=%dchars elapsed=%.1fs',
                    tid[:8], conv_id[:8],
                    (exclude_task_id or '?')[:8],
                    len(t.get('content') or ''),
                    time.time() - t.get('created_at', time.time()),
                )
                try:
                    from lib.log import audit_log as _audit
                    _audit('task_abort',
                           task_id=tid,
                           conv_id=conv_id,
                           reason='superseded_by_new_task',
                           superseding_task_id=exclude_task_id or '',
                           content_chars=len(t.get('content') or ''),
                           elapsed_s=round(time.time() - t.get('created_at', time.time()), 2))
                except Exception as _aerr:
                    logger.debug('[Manager] audit_log task_abort failed: %s', _aerr)
    # ★ Zombie-task terminal floor (outside tasks_lock — this does DB I/O).
    #   An aborted task normally reaches a terminal task_results row only when
    #   ITS OWN thread runs finalize/persist. A thread that is wedged (e.g. a
    #   stream that never received a token, 0 events for hours) never gets
    #   there, so on a server restart (in-memory tasks cleared) a poll finds
    #   neither memory nor DB → 404 and the user loses the turn. Writing an
    #   aborted floor NOW guarantees a durable terminal state regardless of
    #   whether the thread ever unwedges. Idempotent: if the thread later does
    #   finalize, persist_task_result overwrites this floor with the real
    #   final content/status (last-writer-wins, keyed on task_id).
    for _t in _aborted_tasks:
        _write_aborted_terminal_floor(_t)
    if aborted:
        logger.info('[Manager] conv=%s Auto-aborted %d stale task(s) before starting new task %s',
                    conv_id[:8], aborted, (exclude_task_id or '?')[:8])
        # ── pt_conv_state_ssot P3: task lifecycle stop broadcast ──
        # Aborting a stale task flips ``t['aborted']=True`` but nobody
        # calls notify_conv_changed for this conv — the frame carrying
        # the fresh runningTaskIds projection (which no longer includes
        # the aborted tid, since snapshot_running_by_conv filters both
        # status!=running AND aborted) never leaves the server, so a
        # sibling device holding the busy dot for the superseded task
        # sees it stay lit until its next poll (25/90s later). Emit ONE
        # notify frame for the whole sweep (consolidates a multi-abort
        # into a single frame, not one per aborted tid). Fail-open: a
        # push transport error must never break the abort path.
        try:
            from lib.conversations.meta_cache import notify_conv_changed
            # pt_abae3a85a92440fd: derive user_id from an aborted task —
            # they all belong to the same conv → same owner. Falls back
            # to DEFAULT_USER_ID (via task_user_id) when the task pre-dates
            # the _userId stash (pre-c6d1bd71 legacy).
            _abort_uid = task_user_id(_aborted_tasks[0]) if _aborted_tasks else None
            notify_conv_changed(conv_id, rev=None,
                                user_id=_abort_uid) if _abort_uid is not None \
                else notify_conv_changed(conv_id, rev=None)
        except Exception as _ne:
            logger.warning(
                '[Manager] conv=%s supersede-abort notify skipped: %s',
                conv_id[:8], _ne)
    return aborted
def quiesce_running_tasks(reason: str = 'server_shutdown') -> int:
    """Signal EVERY running task to abort — called at server shutdown.

    The abort flag is cooperative: the orchestrator's abort seam checks
    ``task['aborted']`` between rounds / after each stream chunk / between
    tools, so a carrier stops issuing new LLM calls and DB writes soon after
    this is set. Setting it BEFORE the atexit ``stop_local_pg_if_owned`` hook
    fires is what prevents the shutdown cascade: without it, live carriers keep
    calling ``get_thread_db`` while PG is being stopped, producing the
    ``FATAL: the database system is shutting down`` + ``cannot schedule new
    futures after interpreter shutdown`` traceback storm.

    Best-effort, never raises. Returns the count of tasks newly marked aborted.
    """
    aborted = 0
    try:
        with tasks_lock:
            for tid, t in tasks.items():
                if t.get('status') == 'running' and not t.get('aborted'):
                    t['aborted'] = True
                    t['_abort_timestamp'] = time.time()
                    t['_abort_reason'] = reason
                    aborted += 1
    except Exception as e:
        logger.warning('[Manager] quiesce_running_tasks failed: %s', e)
        return aborted
    if aborted:
        logger.info('[Manager] Quiesced %d running task(s) for shutdown (reason=%s)',
                    aborted, reason)
    return aborted


def _write_aborted_terminal_floor(task) -> None:
    """Persist a terminal ``status='aborted'`` row to ``task_results`` for a
    just-aborted task, so a later poll (even after a restart that cleared the
    in-memory registry) resolves to a terminal state instead of a 404.

    Best-effort and idempotent — reuses the shared ``_upsert_task_row`` (keyed
    on task_id), so a subsequent real finalize by the task's own thread simply
    overwrites this floor with the authoritative final content/status. Only the
    partial content accumulated so far is written; that is strictly better than
    losing the turn to a 404.
    """
    try:
        conv_id = task.get('convId', '') or ''
        tr_json = (None if _tool_rounds_have_dedicated_home(task)
                   else json.dumps(_merge_tool_rounds(task), ensure_ascii=False))
        meta = build_result_meta(task)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status='aborted',
                         error_json=error_json, tr_json=tr_json, meta_json=meta_json)
        logger.debug('[Task %s] conv=%s Wrote aborted terminal floor to task_results',
                     task['id'][:8], conv_id[:8])
    except Exception as e:
        logger.warning('[Task %s] Failed to write aborted terminal floor: %s',
                       task.get('id', '?')[:8], e, exc_info=True)
