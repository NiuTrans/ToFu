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
    _chat_runtime,
    _conv_latest_task,
    _conv_latest_task_lock,
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
        # Override TaskRuntime defaults with chat-specific shape:
        'status': 'running',          # chat tasks start running, not pending
        'content': '', 'thinking': '', 'error': None,
        'aborted': False, 'toolRounds': [],
        'content_lock': threading.Lock(),
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
    # ★ Identity scope for the personal-preference profile. Resolved HERE,
    #   in the request thread, because the post-turn consolidation runs in a
    #   detached daemon with no request context. The scope is the multi-user
    #   tenant's user_id (populated only by login); open/private mode leave it
    #   empty → the single global profile (personal-install semantic, no
    #   migration). Best-effort: any failure (no request ctx) → '' = global.
    try:
        from lib.memory.user_profile import resolve_profile_scope
        from routes.api_v1.auth import current_auth
        task['_profileScope'] = resolve_profile_scope(current_auth())
    except Exception as e:
        logger.debug('[Task %s] profile scope resolve failed: %s', task_id[:8], e)
        task['_profileScope'] = ''

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
    with tasks_lock:
        tasks.pop(task_id, None)
    if conv_id:
        with _conv_latest_task_lock:
            if _conv_latest_task.get(conv_id) == task_id:
                del _conv_latest_task[conv_id]

def list_running_tasks(exclude_conv_id: str | None = None) -> list[dict]:
    """Return one entry per CONVERSATION with genuinely-live running work.

    Used by the self-update restart guard to refuse a process re-exec that
    would kill sibling conversations' in-flight work. A restart is an
    unconditional ``os.execv`` of the whole server, so EVERY running task
    dies with it — this lets the caller detect that and require an explicit
    override.

    Two filters make the count reflect reality rather than registry cruft, so
    the guard never blocks a restart for work that is not actually running:

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


