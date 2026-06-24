"""Autopilot mode — virtual user that auto-replies when the LLM stops.

When the model would normally hand control back to the user (either by
calling ``ask_human`` or by emitting a final assistant message with
``finish_reason='stop'``), Autopilot runs a one-shot LLM as the *user*
and feeds its reply back to the orchestrator as a brand-new turn.

Design constraints (locked in by the user, do not relax silently):

  • **Runs BEFORE the ``done`` SSE event.**  The hook fires inside
    ``_finalize_and_emit_done`` after the post-loop work but *before*
    ``append_event(done_evt)`` / ``persist_task_result``.  This lets
    the ``done`` event carry ``autopilotNextTaskId`` +
    ``autopilotVuMessage`` so the frontend attaches to the follow-up
    task directly instead of polling ``/api/chat/active`` after the
    SSE stream has already closed.  (Earlier design ran autopilot
    *after* persist; the SSE pipe was closed by the time the VU
    finished, so the synthetic user msg was invisible until manual
    refresh.)

  • **Independent of endpoint mode.**  Autopilot and endpoint mode are
    mutually exclusive — both share the same termination boundary
    ("the model stopped") so running them together would double-loop.
    The frontend hides one toggle when the other is on; this module
    additionally bails out when ``task['_endpoint_managed']`` is set.

  • **Reuse the conversation's main model.**  No separate VU model.

  • **Same tools as the worker.**  The VU runs through the full
    orchestrator (``_run_single_turn``) so it has access to every tool
    the parent task has — read_files, search, project edits, browser,
    memory, MCP, etc.  This lets the simulated user investigate before
    composing its reply (e.g. "let me check the file the assistant
    referenced before answering").  Inherited from
    ``task['config']`` verbatim — same tool list as
    ``_assemble_tool_list`` would build for the parent.

  • **Role override via a trailing directive turn**, mirroring how
    endpoint-mode's planner/critic announce their role.  We do NOT
    role-swap the conversation history: the LLM sees the real
    conversation and a final user-turn that says "for THIS turn play
    the simulated user".  Prefix-cache-friendly and avoids the
    swapped-history confusion with the orchestrator's injected
    system prompt.

  • **Full conversation passed through.**  We do NOT trim history
    here; the orchestrator's compaction layer (run_compaction_pipeline)
    handles bounding.  This keeps tool_calls / tool_result pairs
    contiguous and removes one place where context choices can drift
    between the worker and the simulated user.

  • **No turn cap, no state-change watchdog.**  The only graceful stop
    signal is the VU itself emitting ``[VU: TASK_DONE]``.  Other stops
    are: real-user abort, real-user sending a new message (handled
    automatically by ``abort_running_tasks_for_conv``), an error path,
    or the queue having a real queued message waiting (deferred to).

  • **Empty VU output does NOT stop the loop.**  An empty reply is
    treated as a valid "yeah, keep going" — the orchestrator just
    starts a fresh turn with that empty user message.  This is the
    user's explicit choice — see the design discussion in
    docs/ARCHITECTURE.md if rebooting decisions.

The "don't stop on empty output" rule means the only correctness escape
hatch is the real user clicking Stop or sending a new message.  Both are
already wired through ``task['aborted']`` and the freshness guard in
``manager._conv_latest_task``, so we don't need extra plumbing here.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from lib.agent_core.events import EventType, build_event
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


_VU_DONE_SENTINEL = '[VU: TASK_DONE]'


_VU_ROLE_PROMPT = (
    'You are simulating the user in a chat with an AI assistant.\n'
    'Reply briefly, in the first person, as if you were the user.\n\n'
    'Decision rules:\n'
    '- For code / engineering tasks: pick the most robust long-term '
    'solution. Do not optimize for cost, implementation speed, or '
    'backward compatibility. Prefer fixing root causes over patches.\n'
    '- For open-ended discussion: use your own judgment, stay concrete, '
    'pick a direction instead of asking more questions.\n'
    '- If the assistant has clearly finished the task and is just '
    'signing off (no question pending, no proposal awaiting review), '
    f'reply EXACTLY: {_VU_DONE_SENTINEL}\n'
    '- Never invent product requirements the real user never mentioned.\n'
    '- Keep replies to 1-3 sentences unless the assistant explicitly '
    'asked for detail.\n'
    '- Reply in the same language the assistant used.\n'
    '- Output ONLY the reply text — no quotation marks, no role labels, '
    f'no preamble. The {_VU_DONE_SENTINEL} sentinel must appear on its '
    'own when used.\n\n'
    'You MAY use any of the tools available to you (read_files, '
    'search, etc.) to investigate before answering — for example, to '
    'check a file the assistant referenced or to look up a fact.  Tool '
    'use is optional; if you already know how to reply, just reply.'
)


def is_autopilot_enabled(task: dict) -> bool:
    """True iff autopilot is active for this task AND endpoint mode is not.

    Autopilot is "active" when EITHER:
      • ``config['autopilot']`` is set (config-driven — toggle was ON at the
        real send, propagated into the task and its follow-ups), OR
      • a persistent autopilot armed-marker exists for the conversation
        (the mid-stream / idle "arm" gesture; survives page reload and is
        cancellable from the queue bar).

    Endpoint mode wins the mutual exclusion (both share the same
    "model stopped" boundary).  The VU sub-task (``_vu_subtask``) and
    inline tasks never consult the marker — only DB-backed parent/follow-up
    tasks do, so the cheap config flag covers the hot recursion guard.
    """
    cfg = task.get('config') or {}
    if cfg.get('endpointMode') or task.get('_endpoint_managed'):
        return False
    if cfg.get('autopilot'):
        return True
    # Persistent armed-marker fallback (mid-stream arm / reload survival).
    if task.get('_vu_subtask') or task.get('_inline_messages'):
        return False
    conv_id = task.get('convId') or ''
    if not conv_id:
        return False
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] marker probe failed (non-fatal): %s', e)
        return False


_VU_FORWARD_TYPES = frozenset({
    'delta', 'phase',
    'tool_start', 'tool_result', 'tool_progress', 'tool_complete',
    'tool_compacted',
    'stdin_request', 'stdin_resolved',
    'write_approval_request',
    'human_guidance_request', 'human_guidance_response',
})


class _VUEventForwarder(list):
    """List subclass that forwards the VU sub-task's events to the parent.

    The orchestrator drives all SSE updates by calling
    ``manager.append_event(task, ev)`` which does
    ``task['events'].append(ev)`` under the task's events_lock.  By
    swapping ``sub_task['events']`` with this subclass we get a hook on
    every event the VU sub-task emits, without monkey-patching
    ``append_event`` globally.

    For each VU event we still append it to the underlying list (so the
    sub-task's own SSE stream stays intact for any reader that ever
    connects to it), and additionally forward two flavours of derived
    events onto the PARENT task's stream:

      1. ``autopilot_vu_event`` — wraps the original VU sub-task event
         (delta / tool_start / tool_result / tool_progress / tool_complete /
         tool_compacted / stdin_* / write_approval_request /
         human_guidance_*) so the frontend can render the VU's reply +
         tool calls into the synthetic-user bubble *as they happen*,
         instead of materializing the whole bubble after the VU
         finishes.  The wrapper carries ``vuMsgId`` so the frontend can
         target the right message.

    The synthetic-user bubble itself is created eagerly by the
    ``autopilot_vu_start`` event (emitted from ``maybe_run_autopilot``
    BEFORE the VU sub-task runs), so the user sees an "Autopilot ·
    composing…" bubble in the USER lane the moment the worker stops —
    NOT a phase chip glued to the worker bubble.  All VU thinking, tool
    calls, and reply text then stream into that bubble via the wrapped
    events above.
    """

    def __init__(self, parent_task, vu_msg_id):
        super().__init__()
        self._parent = parent_task
        self._vu_msg_id = vu_msg_id

    def append(self, ev):
        super().append(ev)
        try:
            self._forward_to_parent(ev)
        except Exception as e:
            logger.debug('[Autopilot] event forward failed (non-fatal): %s', e)

    def _forward_to_parent(self, ev):
        from lib.tasks_pkg.manager import append_event as _ap_event
        et = (ev or {}).get('type')

        # Forward the inner event verbatim, wrapped so the frontend
        # routes it into the VU bubble (by vuMsgId) instead of the
        # parent's worker bubble.  We re-emit the parent-stream phase
        # chip below as well; the two are not mutually exclusive (one
        # paints the VU bubble, the other annotates the parent's chip).
        if et in _VU_FORWARD_TYPES:
            _ap_event(self._parent, build_event(
                EventType.AUTOPILOT_VU_EVENT,
                vuMsgId=self._vu_msg_id,
                inner=ev,
            ))


def run_virtual_user(task: dict, vu_msg_id: str | None = None) -> dict | None:
    """Run the VU LLM (with full tools) and return its reply + investigation.

    The VU runs as a fresh sub-task through the orchestrator's
    ``_run_single_turn``, inheriting the parent task's config so the
    same tools (read_files, search, project edits, memory, MCP, …)
    are available.  The trailing directive user-turn announces the
    "simulated user" role for THIS turn only — the conversation
    history itself is not role-swapped.

    Returns ``{'text': str, 'rounds': list}`` on success, where
    ``rounds`` is the VU sub-task's tool round history (suitable for
    attaching to the persisted synthetic user message so the user can
    see what Autopilot probed).  Returns ``None`` when the loop should
    stop — either because the VU emitted ``[VU: TASK_DONE]``, the
    sub-task failed, or the parent task was aborted while the VU was
    thinking.  An empty ``text`` is a valid "keep going" reply.
    """
    tid = task['id'][:8]
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted', tid)
        return None

    parent_messages = task.get('messages') or []
    if not parent_messages:
        logger.warning('[Autopilot %s] No messages — stopping', tid)
        return None

    # Append the role-override directive as a trailing user turn —
    # same pattern as endpoint_review._run_planner_turn / _run_critic_turn.
    # We pass the parent's full message list verbatim so the VU sees the
    # entire conversation (including tool_calls / tool_result pairs);
    # the orchestrator's compaction layer handles context bounding.
    vu_messages = [dict(m) for m in parent_messages]
    vu_messages.append({
        'role': 'user',
        'content': (
            '=== Your role for THIS turn: Simulated User ===\n'
            f'{_VU_ROLE_PROMPT}\n'
            '=== End simulated-user role ===\n\n'
            'Based on the conversation above, produce the simulated '
            'user\'s reply now.  Investigate first with tools if you '
            'want, then output the reply text only.'
        ),
        '_isVuDirective': True,
    })

    # Build a fresh sub-task that inherits the parent's config so
    # _assemble_tool_list constructs the same tool list the worker had.
    # ``_inline_messages=True`` keeps it out of the conv DB sync path,
    # ``convId=''`` keeps it out of the latest-task registry, and
    # ``_endpoint_managed=True`` suppresses the orchestrator's done
    # event + autopilot recursion.
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.orchestrator import _run_single_turn

    sub_cfg = dict(task.get('config') or {})
    # Strip checkpoint/continue flags so the sub-task starts clean.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        sub_cfg.pop(stale_key, None)
    # Endpoint mode is gated by is_autopilot_enabled but be defensive —
    # the sub-task must never re-enter endpoint mode.
    sub_cfg['endpointMode'] = False
    # Autopilot must NOT recurse (the parent's hook already runs us).
    sub_cfg['autopilot'] = False
    # Disable ask_human for the simulated user — the VU IS the user, so
    # asking another human makes no sense and would block forever (the
    # in-handler autopilot fallback is gated on cfg.autopilot which we
    # just turned off above).
    sub_cfg['humanGuidanceEnabled'] = False

    sub_task = create_task('', vu_messages, sub_cfg)
    sub_task['_inline_messages'] = True
    sub_task['_vu_subtask'] = True
    sub_task['_autopilotParent'] = task.get('id', '')

    # Swap in a forwarding event list so the VU sub-task's events
    # surface live on the parent stream:
    #   • inner events (delta / tool_start / tool_result / tool_progress
    #     / tool_complete / tool_compacted / stdin_* /
    #     write_approval_request / human_guidance_*) are wrapped as
    #     `autopilot_vu_event` and routed by the frontend into the VU
    #     bubble (created eagerly by the `autopilot_vu_start` event
    #     above) identified by `vuMsgId` — so the user sees the VU's
    #     tool calls and reply STREAM in, not "pop in" once the VU
    #     finishes.
    sub_task['events'] = _VUEventForwarder(task, vu_msg_id or '')

    # Mirror parent abort onto the sub-task so user-clicked Stop while
    # the VU is mid-tool-loop tears the sub-task down too.  Single
    # threaded poll is fine — the sub-task is short-lived and the
    # orchestrator already polls task['aborted'] each round.
    _stop_mirror = threading.Event()

    def _mirror_abort():
        while not _stop_mirror.is_set():
            if task.get('aborted') and not sub_task.get('aborted'):
                sub_task['aborted'] = True
                sub_task['_abort_timestamp'] = time.time()
                sub_task['_abort_reason'] = 'parent_aborted'
                logger.info('[Autopilot %s] Mirroring parent abort onto '
                            'VU sub-task %s', tid, sub_task['id'][:8])
                return
            _stop_mirror.wait(0.5)

    _mirror_thread = threading.Thread(
        target=_mirror_abort,
        name=f'autopilot-abort-mirror-{tid}',
        daemon=True,
    )
    _mirror_thread.start()

    try:
        result = _run_single_turn(sub_task)
    except Exception as e:
        logger.warning('[Autopilot %s] VU sub-task raised: %s — '
                       'stopping autopilot for this conv', tid, e,
                       exc_info=True)
        return None
    finally:
        _stop_mirror.set()

    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted during VU sub-task — stopping', tid)
        return None

    err = result.get('error')
    if err:
        logger.warning('[Autopilot %s] VU sub-task error: %.200s — '
                       'stopping autopilot for this conv', tid, err)
        return None

    text = (result.get('content') or '').strip()
    rounds = list(sub_task.get('toolRounds') or [])
    if _VU_DONE_SENTINEL in text:
        logger.info('[Autopilot %s] VU emitted TASK_DONE — stopping loop', tid)
        # Signal the hook to clear the persistent armed-marker (disarm) so the
        # loop ends and the queue-bar sentinel disappears.
        task['_vu_emitted_done'] = True
        audit_log('autopilot_stop',
                  task_id=task.get('id', ''),
                  conv_id=task.get('convId', ''),
                  reason='vu_task_done')
        return None

    logger.info('[Autopilot %s] VU reply: %.200s%s (used %d tool round(s))',
                tid, text, ' …' if len(text) > 200 else '', len(rounds))
    return {'text': text, 'rounds': rounds}


# ──────────────────────────────────────────────────────────────────
#  Follow-up scheduling — append a synthetic user msg + start a task
# ──────────────────────────────────────────────────────────────────

def _presync_parent_reply(task: dict) -> None:
    """Commit the parent task's FINAL assistant message to the conv DB.

    MUST run before this hook appends the VU turn / spawns the follow-up:
    once a follow-up registers as ``_conv_latest_task`` the freshness guard
    in ``manager._sync_result_to_conversation`` rejects the parent's final
    write, freezing the reply at its last streaming checkpoint (truncated
    content, ``finishReason=None``) and feeding that truncated copy to the
    follow-up.

    The orchestrator already calls this once before the hook when autopilot
    was enabled at task-creation time.  We repeat it here so the RUNTIME-ARM
    path (autopilot flipped on mid-stream via ``arm_autopilot``) is equally
    safe regardless of whether the arm landed before or after the
    orchestrator's gate — ``_sync_result_to_conversation`` only FILLS the
    trailing assistant slot (find-or-append), so a second call is an
    idempotent no-op when the orchestrator already synced.
    """
    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        return
    try:
        from lib.tasks_pkg.manager import (
            _sync_result_to_conversation,
            build_result_meta,
        )
        _sync_result_to_conversation(task, build_result_meta(task))
    except Exception as e:
        logger.warning('[Autopilot] parent pre-sync failed: %s — follow-up '
                       'may see a truncated parent reply', e, exc_info=True)


def _has_pending_real_message(conv_id: str) -> bool:
    """True if a real user message is queued — autopilot must defer."""
    if not conv_id:
        return False
    try:
        from lib.message_queue import get_queue_depth
        return get_queue_depth(conv_id) > 0
    except Exception as e:
        logger.debug('[Autopilot] queue depth probe failed (non-fatal): %s', e)
        return False


def _successor_already_running(task: dict, conv_id: str) -> bool:
    """True if another task has already taken over for this conversation.

    ``persist_task_result`` runs ``_dispatch_queued_message`` before our
    hook fires, so a queued real-user message will already have spawned
    its own follow-up task.  Spawning a VU follow-up on top of that
    would (a) abort the queued task via ``abort_running_tasks_for_conv``
    and (b) clobber the user's actual question.  Detect this by looking
    at the latest-task registry.
    """
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.manager import (
            _conv_latest_task,
            _conv_latest_task_lock,
        )
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        return bool(latest) and latest != task.get('id')
    except Exception as e:
        logger.debug('[Autopilot] latest-task probe failed (non-fatal): %s', e)
        return False


def _append_vu_message_to_conv(conv_id: str, vu_msg_id: str,
                                text: str,
                                rounds: list | None = None) -> dict | None:
    """Append the VU's reply as a user message in the conversation DB.

    Called ONLY after the VU has successfully produced a reply (i.e.
    after ``run_virtual_user`` returned non-``None``).  This is a
    deliberate design choice:

      • We DO NOT pre-write an empty placeholder before the VU runs.
        Doing so used to leave orphan empty rows in the DB whenever
        the cleanup path was missed (server crash, abort race, etc.)
        — visible to the user as "an empty VU bubble at the bottom"
        even when autopilot never actually took over.

      • The frontend lazily creates the VU bubble in memory when it
        receives the first ``autopilot_vu_event`` carrying actual
        content (``delta`` with text or ``tool_start``).  No DB write
        happens until success — so a VU that bails out (``[VU:
        TASK_DONE]``, abort, real user msg) leaves NO trace on disk.

    ``_msgId`` is the caller-minted id that the frontend used to route
    streaming updates; persisting it here lets a page reload right
    AFTER autopilot completes find the same message id and reconcile.
    """
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
            json_dumps_pg,
        )
    except Exception as e:
        logger.warning('[Autopilot] DB import failed: %s', e)
        return None

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('[Autopilot] conv=%s not found — cannot append VU msg',
                           conv_id[:8])
            return None
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Autopilot] conv=%s messages parse failed: %s',
                           conv_id[:8], e)
            return None

        vu_msg = {
            'role': 'user',
            'content': text,
            'timestamp': int(time.time() * 1000),
            '_msgId': vu_msg_id,
            '_isVirtualUser': True,
        }
        if rounds:
            vu_msg['toolRounds'] = rounds
        messages.append(vu_msg)

        now_ms = int(time.time() * 1000)
        try:
            from lib.conversations import build_search_text
            search_text = build_search_text(messages)
        except Exception as e:
            logger.debug('[Autopilot] build_search_text failed: %s', e)
            search_text = ''

        db_execute_with_retry(
            db,
            '''UPDATE conversations
                  SET messages=?, updated_at=?, msg_count=?, search_text=?
                  WHERE id=? AND user_id=1''',
            (json_dumps_pg(messages), now_ms, len(messages), search_text,
             conv_id),
        )
        logger.info('[Autopilot] conv=%s ✅ Appended VU msg %s (%d chars, %d rounds)',
                    conv_id[:8], vu_msg_id[:12], len(text), len(rounds or []))
        return vu_msg
    except Exception as e:
        logger.error('[Autopilot] conv=%s append failed: %s',
                     conv_id[:8], e, exc_info=True)
        return None


def _start_followup_task(task: dict, conv_id: str) -> str | None:
    """Build api_messages from the conversation and spawn a new task.

    Mirrors what ``_start_task_for_conv`` does, but inlined to avoid
    importing from ``routes`` (orchestrator must not pull route-layer
    code at module scope — circular).
    """
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    from lib.tasks_pkg.manager import abort_running_tasks_for_conv

    cfg = dict(task.get('config') or {})
    # Strip checkpoint / continue flags so the follow-up runs fresh.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        cfg.pop(stale_key, None)

    api_messages = build_api_messages_from_db(conv_id, cfg)
    if not api_messages:
        logger.warning('[Autopilot] conv=%s build_api_messages returned '
                       'empty — cannot start follow-up', conv_id[:8])
        return None

    # Belt-and-braces: any other still-running task for this conv is
    # superseded by this autopilot follow-up, same as a real user send.
    abort_running_tasks_for_conv(conv_id)

    new_task = create_task(conv_id, api_messages, cfg)
    new_task_id = new_task['id']
    new_task['_autopilotParent'] = task.get('id')

    logger.info('[Autopilot] Spawning follow-up task %s for conv=%s '
                '(parent=%s)', new_task_id[:8], conv_id[:8],
                task.get('id', '?')[:8])
    audit_log('autopilot_followup',
              parent_task_id=task.get('id', ''),
              new_task_id=new_task_id,
              conv_id=conv_id)

    try:
        from lib.tasks_pkg import spawn_task as _spawn_task
        _spawn_task(new_task)
    except Exception as e:
        logger.error('[Autopilot] Failed to start follow-up thread: %s',
                     e, exc_info=True)
        from lib.error_envelope import make_envelope as _make_env
        new_task['status'] = 'error'
        new_task['error'] = _make_env(
            'internal',
            detail='Autopilot failed to spawn follow-up thread.',
            model=cfg.get('model', ''),
            context='autopilot',
            source='autopilot',
            raw=str(e),
        )
        return None

    # Update conversation settings.activeTaskId so reload still finds the
    # live task.  Best-effort — failure here doesn't break the loop.
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
        db = get_thread_db(DOMAIN_CHAT)
        srow = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if srow:
            try:
                settings = json.loads(srow[0] or '{}')
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[autopilot] _start_followup_task caught %s: %s', type(_e_audit).__name__, _e_audit)
                settings = {}
            settings['activeTaskId'] = new_task_id
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (json.dumps(settings, ensure_ascii=False), conv_id),
            )
    except Exception as e:
        logger.debug('[Autopilot] activeTaskId update skipped: %s', e)

    try:
        from lib.conversations import invalidate_meta_cache
        invalidate_meta_cache()
    except Exception as e:
        logger.debug('[Autopilot] meta cache invalidation skipped: %s', e)

    return new_task_id


def maybe_run_autopilot(task: dict) -> dict | None:
    """End-of-turn hook: run the VU and spawn a follow-up task if eligible.

    Called from ``_finalize_and_emit_done`` BEFORE ``append_event(done_evt)``
    so the returned info can be embedded in the same ``done`` SSE event
    that finishes the current turn.  This eliminates the polling race
    where the SSE stream closed before the VU had time to spawn the
    follow-up task — the synthetic user message is now delivered
    in-band on the same connection.

    Returns ``{'next_task_id': str, 'vu_msg': dict}`` when a follow-up
    was spawned, ``None`` otherwise (no autopilot, no eligible context,
    VU emitted ``[VU: TASK_DONE]``, real user message queued, or any
    failure path).  The orchestrator inlines the dict into ``done_evt``
    as ``autopilotNextTaskId`` + ``autopilotVuMessage``.
    """
    tid = task['id'][:8]

    if not is_autopilot_enabled(task):
        # Log at debug level so silencing is invisible in normal mode
        # but findable when someone wonders "why didn't it take over?".
        cfg = task.get('config') or {}
        logger.debug('[Autopilot %s] Skip — not enabled '
                     '(autopilot=%s, endpointMode=%s, _endpoint_managed=%s)',
                     tid, cfg.get('autopilot'), cfg.get('endpointMode'),
                     task.get('_endpoint_managed'))
        return None

    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        logger.debug('[Autopilot %s] Skip — no DB-backed conversation', tid)
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted before VU could run', tid)
        return None
    if task.get('error'):
        logger.info('[Autopilot %s] Skip — task ended in error: %.120s',
                    tid, str(task.get('error')))
        return None
    if task.get('finishReason') == 'tool_rounds_exhausted':
        logger.info('[Autopilot %s] Skip — tool rounds exhausted', tid)
        return None

    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Skip — real user message queued '
                    '(it takes priority)', tid)
        return None
    # ``_successor_already_running`` is largely redundant in the new
    # ordering (queue dispatch happens AFTER us via persist_task_result),
    # but keep it as defense-in-depth for endpoint-mode / branch flows
    # that may have already advanced the latest-task registry for this
    # conversation.
    if _successor_already_running(task, conv_id):
        logger.info('[Autopilot %s] Skip — another task already took over '
                    'for conv=%s', tid, conv_id[:8])
        return None

    from lib.tasks_pkg.manager import append_event

    # Mint the VU message id up front and EAGERLY emit `autopilot_vu_start`
    # so the frontend creates the simulated-user bubble in the USER lane
    # the moment the worker stops — showing "Autopilot · composing…" with
    # the Autopilot avatar, exactly like a real pending user turn.  The
    # VU's thinking / tool calls / reply then stream INTO that bubble via
    # the wrapped `autopilot_vu_event` frames (see _VUEventForwarder).
    #
    # IMPORTANT — the start event is IN-MEMORY ONLY: it does NOT write
    # anything to the conv DB.  Persistence happens exactly once, on
    # success, in `_append_vu_message_to_conv` (fired right before
    # `autopilot_vu_done`).  Failure paths (TASK_DONE / abort / queued
    # real user msg) emit `autopilot_vu_cancel`, which removes the
    # in-memory bubble and leaves NO trace on disk — preserving the
    # "no ghost empty VU at the bottom" guarantee.
    vu_msg_id = str(uuid.uuid4())

    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_START,
            vuMsgId=vu_msg_id,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] vu_start emit failed: %s', tid, e)

    vu_result = run_virtual_user(task, vu_msg_id=vu_msg_id)
    if vu_result is None:
        # VU emitted [VU: TASK_DONE], errored, or task was aborted.
        # On a graceful TASK_DONE, disarm the persistent marker so the loop
        # ends and the queue-bar sentinel disappears.  (Abort/error leave the
        # marker intact — a transient failure shouldn't silently disarm.)
        if task.get('_vu_emitted_done'):
            try:
                from lib.message_queue import clear_autopilot_marker
                clear_autopilot_marker(conv_id)
            except Exception as e:
                logger.debug('[Autopilot %s] marker clear failed: %s', tid, e)
        # Tell the frontend to discard any in-memory bubble it may
        # have lazily created from inner stream events; nothing was
        # ever persisted.
        try:
            append_event(task, build_event(
                EventType.AUTOPILOT_VU_CANCEL,
                vuMsgId=vu_msg_id,
            ))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None
    vu_text = vu_result['text']
    vu_rounds = vu_result.get('rounds') or []

    # Race-close: a real user may have submitted a message while the VU
    # LLM call was running.  If so, defer to that real message instead
    # of clobbering it with a synthetic VU turn.
    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Real user message arrived during VU '
                    'call — deferring to queue', tid)
        try:
            append_event(task, build_event(EventType.AUTOPILOT_VU_CANCEL,
                                 vuMsgId=vu_msg_id))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted while VU was running — stopping', tid)
        try:
            append_event(task, build_event(EventType.AUTOPILOT_VU_CANCEL,
                                 vuMsgId=vu_msg_id))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None

    # VU produced a reply — NOW commit it to the conv DB.  But FIRST make
    # sure the parent's final assistant reply is committed: on the
    # runtime-arm path (autopilot flipped on mid-stream) the orchestrator's
    # pre-hook sync may have been skipped (it gates on is_autopilot_enabled
    # evaluated a few lines earlier), so do it here too — idempotent.
    _presync_parent_reply(task)
    vu_msg = _append_vu_message_to_conv(
        conv_id, vu_msg_id, vu_text, rounds=vu_rounds,
    )
    if vu_msg is None:
        return None

    # Tell the frontend the VU bubble is fully baked.  Carries the
    # final content + rounds so a client that lazily built the bubble
    # from streaming deltas — or one that missed them entirely (cold
    # replay, late connect) — can reconcile in one shot.
    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_DONE,
            vuMsgId=vu_msg_id,
            vuMessage=vu_msg,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] vu_done emit failed: %s', tid, e)

    next_task_id = _start_followup_task(task, conv_id)
    if next_task_id is None:
        return None

    # Tell ``_dispatch_queued_message`` (which runs slightly after us
    # inside ``persist_task_result``) that autopilot already spawned a
    # successor for this conversation.  Otherwise a real user message
    # that landed in the tiny window between our post-VU queue re-check
    # and now would race-spawn its own task and abort our follow-up.
    # The queued message will be picked up when the autopilot follow-up
    # itself completes.
    task['_autopilot_spawned_followup'] = next_task_id

    return {'next_task_id': next_task_id, 'vu_msg': vu_msg}


# ──────────────────────────────────────────────────────────────────
#  Kick from idle — start the VU loop on a FINISHED conversation
# ──────────────────────────────────────────────────────────────────

def _run_autopilot_kick(task: dict) -> None:
    """Carrier-task entry: run the VU hook directly, with NO worker turn.

    Used by the "push the conversation forward" gesture (empty-Enter on a
    finished conversation with autopilot ON).  Unlike a normal task, this
    carrier never calls the LLM as the assistant — the conversation already
    ended and the last message is the agent's reply, so the virtual user
    should answer it straight away.  We reuse the SAME end-of-turn hook the
    natural-stop path runs (``maybe_run_autopilot``): it emits the
    ``autopilot_vu_*`` stream, appends the synthetic user message, spawns the
    follow-up worker task, and returns the ``next_task_id`` / ``vu_msg``
    baton.  The baton rides out on this carrier's ``done`` event (and on
    ``task['_autopilot_followup']`` for the poll path) exactly as it does at
    a natural stop, so the frontend attaches to the follow-up with no extra
    plumbing.

    Invoked from ``orchestrator.run_task`` when ``task['_autopilot_kick']``
    is set.
    """
    from lib.tasks_pkg.manager import append_event, persist_task_result

    tid = task['id'][:8]
    # The carrier produces no assistant content of its own; flip to 'done'
    # immediately so the SSE generator / poll treat the (in-flight) autopilot
    # decision window correctly via the _autopilot_deciding latch below.
    task['status'] = 'done'

    done_evt = build_event(EventType.DONE)
    if task.get('model'):
        done_evt['model'] = task['model']

    task['_autopilot_deciding'] = True
    try:
        ap_result = maybe_run_autopilot(task)
        if ap_result:
            done_evt['autopilotNextTaskId'] = ap_result['next_task_id']
            done_evt['autopilotVuMessage'] = ap_result['vu_msg']
            # Same transport-agnostic stash as the natural-stop path so a
            # client that fell back to /api/chat/poll still gets the baton.
            task['_autopilot_followup'] = ap_result
            logger.info('[Autopilot kick %s] VU took over conv=%s → follow-up %s',
                        tid, task.get('convId', '')[:8],
                        ap_result['next_task_id'][:8])
        else:
            logger.info('[Autopilot kick %s] VU declined to take over conv=%s '
                        '(TASK_DONE / no eligible context)', tid,
                        task.get('convId', '')[:8])
    except Exception as e:
        logger.error('[Autopilot kick %s] hook raised: %s', tid, e, exc_info=True)
    finally:
        task['_autopilot_deciding'] = False

    append_event(task, done_evt)
    persist_task_result(task)


def kick_autopilot(conv_id: str, config: dict | None = None) -> dict:
    """Start the virtual-user loop on a conversation whose reply has finished.

    The "push it forward for me" gesture: the user chatted with autopilot ON,
    the turn ended, and they want the virtual user to keep the conversation
    going WITHOUT typing anything.  Because ``maybe_run_autopilot`` only runs
    as an end-of-turn hook (there is no live task to hang it on once the reply
    finished), we spawn a thin carrier task whose ``run_task`` short-circuits
    straight to :func:`_run_autopilot_kick`.

    Refuses (``taskId=None``) when a non-VU task is already ``running`` for the
    conversation — in that case the caller should ARM the live task instead
    (see :func:`arm_autopilot`), so we never double-drive the loop.

    Also persists ``settings.autopilotEnabled=true`` so subsequent manual
    sends keep looping, mirroring the arm route.

    Returns ``{'taskId': str}`` on success, or ``{'taskId': None, 'error':
    str}`` when there is nothing to kick (no conversation, empty history, or a
    task is already running).
    """
    if not conv_id:
        return {'taskId': None, 'error': 'conv_id is required'}

    # Refuse if a live (non-VU) task is already running — arm it instead.
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        for t in tasks.values():
            if (t.get('convId') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('_vu_subtask')):
                logger.info('[Autopilot kick] conv=%s already has a running '
                            'task %s — refusing kick (arm instead)',
                            conv_id[:8], t.get('id', '?')[:8])
                return {'taskId': None, 'error': 'task_already_running'}

    cfg = dict(config or {})
    cfg['autopilot'] = True
    cfg['endpointMode'] = False
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        cfg.pop(stale_key, None)

    from lib.tasks_pkg import create_task, spawn_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db

    api_messages = build_api_messages_from_db(conv_id, cfg)
    if api_messages is None:
        return {'taskId': None, 'error': 'conversation_not_found'}
    if not api_messages:
        return {'taskId': None, 'error': 'conversation_empty'}

    task = create_task(conv_id, api_messages, cfg)
    task['_autopilot_kick'] = True

    # Persist the setting so the loop keeps going on any later manual send.
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
        )
        db = get_thread_db(DOMAIN_CHAT)
        srow = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if srow:
            try:
                settings = json.loads(srow[0] or '{}')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Autopilot kick] settings parse failed conv=%s: %s',
                             conv_id[:8], e)
                settings = {}
            settings['autopilotEnabled'] = True
            settings['activeTaskId'] = task['id']
            db_execute_with_retry(
                db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (json.dumps(settings, ensure_ascii=False), conv_id),
            )
    except Exception as e:
        logger.warning('[Autopilot kick] persist autopilotEnabled failed '
                       'conv=%s: %s', conv_id[:8], e)

    logger.info('[Autopilot kick] conv=%s spawning carrier task %s',
                conv_id[:8], task['id'][:8])
    audit_log('autopilot_kick', conv_id=conv_id, task_id=task['id'])
    spawn_task(task)
    return {'taskId': task['id']}


# ──────────────────────────────────────────────────────────────────
#  Runtime arming — turn autopilot on for an ALREADY-RUNNING task
# ──────────────────────────────────────────────────────────────────

def arm_autopilot(conv_id: str) -> dict:
    """Arm autopilot for a conversation whose task is already in flight.

    Use case: the user chatted with autopilot OFF, then decides to step
    away mid-reply and wants the virtual user to take over at the next
    natural stop.  Toggling the frontend button only affects the NEXT
    task — the in-flight task's ``config['autopilot']`` was frozen at
    creation time, so its end-of-turn hook would never fire.

    This flips ``config['autopilot'] = True`` on every live (status=
    ``running``) task for the conversation.  Because ``_finalize_and_emit_done``
    re-reads ``is_autopilot_enabled(task)`` at finalize, the running task
    will now run the VU hook when it stops.  Mutating ``config`` (rather
    than a side flag) also means the value propagates to autopilot
    follow-ups via ``_start_followup_task``'s ``dict(task['config'])``,
    so the loop continues until the VU emits ``[VU: TASK_DONE]``.

    Endpoint-managed tasks are skipped — autopilot and endpoint mode are
    mutually exclusive (they share the same termination boundary).

    Returns ``{'armed': bool, 'taskIds': [...]}`` — ``armed`` is True iff
    at least one live task was flipped.  When no task is live (the reply
    already finished), ``armed`` is False and the caller should rely on
    the persisted ``autopilotEnabled`` setting to kick off the loop on the
    user's next send.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    armed_ids: list[str] = []
    marker_cfg: dict = {}
    endpoint_blocked = False
    with tasks_lock:
        # Pass 1 — mutual exclusion: if ANY live task for the conv is endpoint
        # mode, refuse to arm autopilot (they share the same termination
        # boundary; running both double-loops).
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('status') != 'running':
                continue
            if t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if t.get('_endpoint_managed') or (isinstance(cfg, dict) and cfg.get('endpointMode')):
                endpoint_blocked = True
                break
        # Pass 2 — flip config.autopilot on live non-endpoint tasks + capture
        # a config to seed the marker.
        if not endpoint_blocked:
            for tid, t in tasks.items():
                if t.get('convId') != conv_id or t.get('status') != 'running':
                    continue
                if t.get('_endpoint_managed') or t.get('_vu_subtask'):
                    continue
                cfg = t.get('config')
                if not isinstance(cfg, dict):
                    continue
                if not marker_cfg:
                    marker_cfg = dict(cfg)
                if not cfg.get('autopilot'):
                    cfg['autopilot'] = True
                    armed_ids.append(tid)

    if endpoint_blocked:
        logger.info('[Autopilot] Arm refused for conv=%s — endpoint mode is '
                    'live (mutually exclusive)', conv_id[:8])
        return {'armed': False, 'taskIds': [], 'markerAdded': False}

    # Persist the armed-marker sentinel in the queue so the arm survives a
    # page reload, shows in the queue bar (cancellable), and — critically —
    # keeps autopilot armed even when no task is live (the "I'll step away,
    # take over when the current reply finishes" gesture works whether or not
    # a reply is still streaming).  Idempotent: at most one marker per conv.
    marker_added = False
    try:
        from lib.message_queue import arm_autopilot_marker
        res = arm_autopilot_marker(conv_id, marker_cfg)
        marker_added = res.get('armed', False)
    except Exception as e:
        logger.warning('[Autopilot] failed to persist armed-marker for '
                       'conv=%s: %s', conv_id[:8], e)

    if armed_ids:
        logger.info('[Autopilot] Armed %d live task(s) for conv=%s: %s '
                    '(marker_added=%s)', len(armed_ids), conv_id[:8],
                    [t[:8] for t in armed_ids], marker_added)
    else:
        logger.info('[Autopilot] Arm requested for conv=%s — no live task to '
                    'flip; persistent marker now governs (marker_added=%s)',
                    conv_id[:8], marker_added)
    audit_log('autopilot_armed', conv_id=conv_id, task_ids=armed_ids,
              marker_added=marker_added)

    # ``armed`` reflects whether autopilot is now armed for the conv — True if
    # a live task was flipped OR a marker is in place.
    armed = bool(armed_ids) or marker_added or _marker_exists(conv_id)
    return {'armed': armed, 'taskIds': armed_ids, 'markerAdded': marker_added}


def _marker_exists(conv_id: str) -> bool:
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception:
        return False


def disarm_autopilot(conv_id: str) -> dict:
    """Cancel autopilot for a conversation: clear the marker + live config.

    The inverse of :func:`arm_autopilot`.  Removes the persistent armed-marker
    sentinel AND flips ``config['autopilot']=False`` on any live task so the
    loop stops at the current turn's natural end.  Used by the queue-bar
    cancel button and the toggle-OFF gesture.

    Returns ``{disarmed, markerCleared, taskIds}``.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    marker_cleared = False
    try:
        from lib.message_queue import clear_autopilot_marker
        marker_cleared = clear_autopilot_marker(conv_id)
    except Exception as e:
        logger.warning('[Autopilot] disarm: marker clear failed for conv=%s: %s',
                       conv_id[:8], e)

    cleared_ids: list[str] = []
    with tasks_lock:
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if isinstance(cfg, dict) and cfg.get('autopilot'):
                cfg['autopilot'] = False
                cleared_ids.append(tid)

    logger.info('[Autopilot] Disarmed conv=%s (markerCleared=%s, tasks=%s)',
                conv_id[:8], marker_cleared, [t[:8] for t in cleared_ids])
    audit_log('autopilot_disarmed', conv_id=conv_id,
              marker_cleared=marker_cleared, task_ids=cleared_ids)
    return {'disarmed': marker_cleared or bool(cleared_ids),
            'markerCleared': marker_cleared, 'taskIds': cleared_ids}
