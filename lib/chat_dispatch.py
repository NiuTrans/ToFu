"""lib/chat_dispatch.py — chat_send + chat_stream business-logic sinks (pt_04686ac6 slices 5-7).

**Extraction context** (board epic ``pt_04686ac6054a451e``):

  * Slice 5 — ``classify_send_intent``: chat_send's ~175-line queue-
    classification + steer + abort-on-send pipeline.
  * Slice 6 — ``build_cold_replay_response``: chat_stream's ~170-line
    cold-path (task not in memory) resume-serviceability + snapshot
    generation for both the persisted-events replay and the DB-only
    synthetic state+done paths.
  * Slice 7 — ``plan_warm_resume`` + ``build_fresh_state_snapshot``:
    chat_stream's warm-path Last-Event-ID parse, ``_warm_resume_serviceable``
    verdict + resume-snapshot builder AND the fresh-connection full-state
    snapshot builder. Two pure(-ish) helpers so the chat_stream generate()
    closure shrinks to a thin cursor+yield driver.

``routes/chat.py::chat_send`` used to inline ~175 lines of business
logic between "user_msg was built with auto-translate" and "no active
task, dispatch immediately": abort-during-translate check, abort-on-send
race (frontend-reported recently-stopped task), running-task
classification, autopilot-followup supersede, inject-mode steer path,
and the queue path with cross-device pending-row mirror.

That block is EXACTLY the "queue classification + autopilot-followup
detection + abort-on-send race" bundle the epic description called out
as the fat handler's business logic. Extracted here so:

  * ``chat_send`` becomes a ~30-line facade around the parse → classify
    → dispatch pipeline
  * The classification lives in an importable, unit-testable pure(-ish)
    function that takes explicit arguments and returns a small
    ``SendIntent`` value object
  * Future slices can add ``dispatch_regen`` / ``dispatch_continue`` /
    ``dispatch_branch`` next to it under the same module

**Contract**:

  ``classify_send_intent(...) -> SendIntent | None``

    Returns ``None`` when the caller MUST fall through to the
    "immediate start" path (append user_msg, persist, start task).

    Returns a ``SendIntent`` when the caller must SHORT-CIRCUIT and
    return the intent's ``response`` dict as the API response. The
    intent's ``kind`` is one of:
      * ``'aborted'`` — abort landed during auto-translate; drop the
        message entirely (do NOT persist, enqueue, or dispatch).
      * ``'steered'`` — steer-injected into the currently-running turn.
      * ``'queued'`` — enqueued for dispatch after the running turn ends.

Side effects (preserved byte-for-byte from the pre-slice inline code):

  * Marks any ``abortTaskId``-referenced task as aborted
    (`_abort_reason='superseded_by_send'`) BEFORE the running-task scan.
  * Supersedes invisible autopilot follow-up runs when the ONLY running
    tasks are followups AND autopilot is armed (aborts them, disarms
    autopilot, falls through so has_running_task=False).
  * On 'steer' with a drainable inbox: enqueues into agent_inbox at
    priority=next/mode=user-steer, persists title-only for new convs,
    emits notify_conv_changed(rev=None).
  * On 'queue' path: enqueues into message_queue with pre-built
    user_msg (so dispatch_next_queued can append without re-translate),
    persists title-only for new convs, attempts the cross-device
    pending-row mirror (best-effort), emits notify_conv_changed with
    the real rev.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


@dataclass
class SendIntent:
    """Result of classify_send_intent: what chat_send should return NOW.

    Attributes:
        kind: One of 'aborted', 'steered', 'queued' — the branch the
            classifier picked. Callers use this only for logging /
            metrics; the response body is authoritative.
        response: The JSON body the caller should hand back verbatim.
    """
    kind: str
    response: dict[str, Any]


def classify_send_intent(
    db: Any,
    conv_id: str,
    config: dict[str, Any],
    payload: dict[str, Any],
    data: dict[str, Any],
    messages: list,
    is_new: bool,
    title: str,
    user_msg: dict[str, Any],
    settings_patch: Any,
    text: str,
    send_started_at: float,
) -> SendIntent | None:
    """Business-logic pipeline of chat_send. See module docstring.

    Returns None to signal "proceed with immediate start"; returns a
    SendIntent to short-circuit chat_send with its response body.
    """
    # Late imports (routes/chat.py used lazy imports throughout — preserve
    # that style so the extraction cannot introduce a new import-time
    # circular-dependency edge with lib.tasks_pkg / lib.message_queue).
    from lib.tasks_pkg import tasks, tasks_lock
    from lib.chat import (
        append_pending_user_msg as _append_pending_user_msg,
        persist_conv_messages as _persist_conv_messages,
    )
    from routes.chat_state import _was_aborted_after
    # notify_conv_changed lives in routes.common; wrap in a try-broken
    # local so the extraction remains testable without a Flask app ctx.
    try:
        from routes.common import _notify_conv_changed, _request_user_id
    except Exception as _e:
        logger.debug('[chat_dispatch] routes.common._notify_conv_changed '
                     'unavailable: %s (test path)', _e)
        _notify_conv_changed = lambda *a, **kw: None  # noqa: E731
        _request_user_id = lambda: 1  # noqa: E731

    # 2a. If the user clicked Stop while we were inside the auto-
    #     translate call, drop this message entirely — do NOT persist,
    #     enqueue, or dispatch. This prevents the 'translation finishes
    #     after abort → enqueue → fires after regen completes' double-
    #     send bug.
    if _was_aborted_after(conv_id, send_started_at):
        logger.info('[Send] conv=%s ⚠️ Aborted during translate — dropping message '
                    '(translated=%s)',
                    conv_id[:8], bool(user_msg.get('originalContent')))
        return SendIntent(kind='aborted', response={
            'aborted': True,
            'convId': conv_id,
        })

    # ★ 3a. If the frontend reports a recently-aborted task, mark it
    #   as aborted NOW — this handles the race where the user clicks
    #   Stop and immediately sends a new message, and the fire-and-
    #   forget abort fetch hasn't arrived yet.
    abort_task_id = data.get('abortTaskId')
    if abort_task_id:
        with tasks_lock:
            abort_target = tasks.get(abort_task_id)
            if (abort_target
                    and not abort_target.get('aborted')
                    and abort_target.get('convId') == conv_id):
                abort_target['aborted'] = True
                abort_target['_abort_timestamp'] = time.time()
                abort_target['_abort_reason'] = 'superseded_by_send'
                logger.info('[Send] conv=%s ⚠️ Abort-on-send: task %s marked aborted '
                            '(frontend reported recently stopped task)',
                            conv_id[:8], abort_task_id[:8])

    # ★ 3b. Check if a task is already running for this conversation.
    #   If so, enqueue instead of starting — the backend dispatches
    #   automatically when the current task finishes.
    running_tasks = []
    with tasks_lock:
        for t in tasks.values():
            if (t.get('convId') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('aborted')):
                running_tasks.append(t)

    from lib.message_queue import has_autopilot_marker

    def _is_autopilot_followup(t):
        return bool(t.get('_autopilotParent') or t.get('_vu_subtask')
                    or t.get('_autopilot_kick'))

    has_running_task = bool(running_tasks)
    only_autopilot_followups = (
        has_running_task
        and all(_is_autopilot_followup(t) for t in running_tasks))

    if (has_running_task and only_autopilot_followups
            and has_autopilot_marker(conv_id)):
        # Supersede: abort the invisible autopilot follow-up(s) for real
        # (backend stop, so the zombie is reclaimed), disarm autopilot, and
        # fall through to start the human message immediately.
        for t in running_tasks:
            t['aborted'] = True
            t['_abort_timestamp'] = time.time()
            t['_abort_reason'] = 'superseded_by_user_send'
        logger.info('[Send] conv=%s ⚡ superseding %d in-flight autopilot '
                    'follow-up turn(s) for a real user send',
                    conv_id[:8], len(running_tasks))
        try:
            from lib.tasks_pkg.autopilot import disarm_autopilot
            disarm_autopilot(conv_id)
        except Exception as e:
            logger.warning('[Send] conv=%s disarm_autopilot on supersede '
                           'failed (non-fatal): %s', conv_id[:8], e)
        has_running_task = False

    # ★ Inject-mode: 'queue' (default) or 'steer'.
    # injectMode is a PER-SEND decision from the post-send dialog
    # (_promptInjectMode), carried at the top level of the request body — it
    # is NOT a persisted conversation setting. Read `data` FIRST: reading
    # `config` first would be shadowed by resolve_conv_config's 'queue'
    # default (truthy), so a 'steer' choice could never win.
    _inject_mode = (data.get('injectMode') or '').strip().lower()
    if has_running_task and _inject_mode == 'steer':
        from lib.agent_inbox import _tombstones as _inbox_tombstones
        from lib.agent_inbox import _lock as _inbox_lock
        from lib.agent_inbox import enqueue as _inbox_enqueue
        # The inbox key is conversation-scoped (swarm_key_for → convId).
        _steer_key = conv_id
        with _inbox_lock:
            _drainable = _steer_key not in _inbox_tombstones
        if _drainable:
            # value = the wire text the model sees; _user_msg carries the
            # pre-built/translated dict so the finalize salvage can re-queue
            # it verbatim on an abort (exactly-once, never re-translated).
            _steer_text = user_msg.get('content', '') or text
            _inbox_enqueue(
                _steer_key, _steer_text,
                priority='next', mode='user-steer',
                extra={'_user_msg': user_msg, 'config': config})
            logger.info('[Send] conv=%s ➡ STEER (injected into running turn) '
                        'text=%d chars', conv_id[:8], len(_steer_text))
            if is_new:
                _persist_conv_messages(db, conv_id, messages, title, settings_patch)
            _notify_conv_changed(conv_id, rev=None, user_id=_request_user_id())
            return SendIntent(kind='steered', response={
                'steered': True,
                'convId': conv_id,
                'title': title,
                'userMessage': user_msg,
                'isNew': is_new,
                'msgCount': len(messages),  # excludes the steer msg
            })
        # Not drainable → fall through to the durable-queue path below so
        # the steer is delivered as a fresh turn instead of being dropped.
        logger.info('[Send] conv=%s steer requested but inbox slot not '
                    'drainable (task finalizing) — falling back to queue',
                    conv_id[:8])

    if has_running_task:
        from lib.message_queue import enqueue_message, get_queue_depth
        # ★ Enqueue for later dispatch. The durable queue is the source of
        #   truth for WHEN this turn runs. Store the pre-built user_msg so
        #   dispatch_next_queued can append it without re-translating.
        queue_payload = dict(payload)
        queue_payload['_user_msg'] = user_msg
        queue_result = enqueue_message(conv_id, queue_payload, config)
        logger.info('[Send] conv=%s ➡ QUEUED (active task running) queueId=%s position=%d',
                    conv_id[:8], queue_result['queueId'][:8], queue_result['position'])

        # Persist title update for new conversations (but NOT the user message)
        if is_new:
            _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # ★ Cross-device visibility (Fix 2a): land the queued user message
        #   in the conversation body NOW as a display-only _pendingQueued
        #   row + push the REAL rev, so another device sees it immediately
        #   instead of only after the current turn replies. Two guards keep
        #   this safe: (1) ONLY the FIRST queued turn (depth==1 after this
        #   enqueue) may pre-persist — a 2nd pending row would misorder
        #   against the eventual replies; (2) the helper itself declines
        #   unless the DB tail is the running turn's assistant slot (so the
        #   row lands correctly ordered). On decline we fall back to the
        #   original queue-only behaviour (rev=None sidebar nudge). The
        #   later dispatch_next_queued reconciles this row in place by
        #   timestamp (never a duplicate).
        _pending_rev = None
        try:
            _running_amids = {t.get('_assistantMsgId') for t in running_tasks
                              if t.get('_assistantMsgId')}
            if get_queue_depth(conv_id) == 1:
                _appended, _pending_rev = _append_pending_user_msg(
                    db, conv_id, user_msg, valid_assistant_ids=_running_amids)
                if _appended:
                    logger.info('[Send] conv=%s queued user msg mirrored as '
                                'pending row (rev=%s) — cross-device visible',
                                conv_id[:8], _pending_rev)
        except Exception as e:
            logger.warning('[Send] conv=%s pending-row mirror failed (non-fatal, '
                           'queue-only fallback): %s', conv_id[:8], e)
            _pending_rev = None

        _notify_conv_changed(conv_id, rev=_pending_rev, user_id=_request_user_id())

        return SendIntent(kind='queued', response={
            'queued': True,
            'queueId': queue_result['queueId'],
            'position': queue_result['position'],
            'convId': conv_id,
            'title': title,
            'userMessage': user_msg,
            'isNew': is_new,
            'msgCount': len(messages),  # excludes the queued user msg
        })

    # No active task, no steer, no abort → caller falls through to
    # immediate-start (append user_msg + persist + start task).
    return None


async def build_cold_replay_response(task_id: str, last_event_id_header: str):
    """chat_stream cold-path handler (pt_04686ac6 slice 6).

    Called by ``routes/chat.py::chat_stream`` when the task is not in
    the in-memory ``tasks`` dict (crashed / cleaned up / restarted /
    reconnecting past cleanup_old_tasks). Two sub-paths:

    1. **Persisted event replay** — if the client sends a valid
       ``Last-Event-ID`` header AND ``lib.tasks_pkg.event_log`` has
       persisted events since that cursor, replay every event from the
       log. On no persisted 'done' event, synthesize a state+done pair
       from the ``task_results`` row folded with ``event_fold``.

    2. **DB snapshot** — if no valid Last-Event-ID (or the log had
       nothing since it) but ``task_results`` still has the row,
       emit a single ``state`` event (content + thinking, folded via
       event_fold; toolRounds either from the DB column or rebuilt from
       the conversation) followed by a ``done`` event with all the
       expected metadata keys (finishReason / usage / preset / model /
       provider_id / thinkingDepth / apiRounds / modifiedFiles /
       modifiedFileList / fallbackModel/from/reason/kind).

    3. **Not found** — no persisted events AND no ``task_results`` row
       → return ``api_not_found('Task not found')``.

    Args:
        task_id: The task id from the URL path.
        last_event_id_header: The raw ``Last-Event-ID`` HTTP header
            value (empty string when the client didn't send one).

    Returns:
        A Flask/Quart response ready to hand back from chat_stream, OR
        ``None`` in the degenerate case that a caller should fall through
        (kept as a sentinel; not currently used — the three sub-paths
        above always produce a real response).
    """
    import asyncio
    import json

    from lib.agent_core.events import EventType, build_event
    from lib.api_response import api_not_found, sse_response
    from lib.database import DOMAIN_CHAT, get_db

    # Late imports (matches routes/chat.py's late-import style; keeps
    # this module import-lightweight for tests).
    from routes.chat import _dumps_yielding, _loads_yielding
    from lib.chat.persistence import extract_db_meta as _extract_db_meta

    # ── Persisted event replay path ──
    _replay_cursor_hdr = (last_event_id_header or '').strip()
    if _replay_cursor_hdr:
        try:
            _replay_cursor = int(_replay_cursor_hdr)
        except (ValueError, TypeError) as _e_audit:
            logger.debug('[chat_dispatch] cold-replay caught %s: %s',
                         type(_e_audit).__name__, _e_audit)
            _replay_cursor = None
        if _replay_cursor is not None and _replay_cursor >= 0:
            from lib.tasks_pkg.event_log import read_events as _read_events
            _persisted = await asyncio.to_thread(
                _read_events, task_id, since_event_id=_replay_cursor)
            if _persisted:
                logger.info('[Chat] Stream %s cold replay from event_log: '
                            '%d event(s) since id=%d',
                            task_id[:8], len(_persisted), _replay_cursor)

                def gen_persisted():
                    # SSE preamble: 4 large-comment lines force any
                    # buffering proxy to flush headers immediately.
                    for _ in range(4):
                        yield ':' + ' ' * 2048 + '\n\n'
                    for ev in _persisted:
                        eid = ev['event_id']
                        payload = ev['payload']
                        yield f'id: {eid}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                        if isinstance(payload, dict) and payload.get('type') == 'done':
                            return
                    # No persisted 'done' — synthesize state+done from
                    # task_results.  We MUST emit a 'state' event before
                    # 'done' here: a client whose Last-Event-ID points
                    # past the end of the persisted log (e.g. TTL prune
                    # ran, or the client's last cursor was very recent)
                    # would otherwise see only metadata and lose all
                    # text. Mirrors the warm-fallback shape further down.
                    try:
                        db_local = get_db(DOMAIN_CHAT)
                        row_local = db_local.execute(
                            'SELECT conv_id,content,thinking,error,status,tool_rounds,metadata '
                            'FROM task_results WHERE task_id=?',
                            (task_id,)
                        ).fetchone()
                        if row_local:
                            # ★ Close the 5s cold-replay window: fold
                            #   the lossless per-delta task_events log
                            #   instead of trusting the (up to 5s stale)
                            #   task_results checkpoint. The fold
                            #   reconstructs the EXACT text the client
                            #   saw; on an empty/failed log it returns
                            #   the checkpoint pair unchanged.
                            from lib.tasks_pkg.event_fold import fold_cold_state_text
                            _fold_c, _fold_t = fold_cold_state_text(
                                task_id, row_local['content'] or '',
                                row_local['thinking'] or '')
                            state_local = build_event(
                                EventType.STATE,
                                content=_fold_c,
                                thinking=_fold_t,
                                status=row_local['status'],
                            )
                            if row_local['tool_rounds']:
                                try:
                                    state_local['toolRounds'] = _loads_yielding(row_local['tool_rounds'])
                                except (json.JSONDecodeError, ValueError, TypeError) as _e:
                                    logger.debug('[Chat] cold-replay tool_rounds parse failed: %s', _e)
                            else:
                                from lib.tasks_pkg import load_tool_rounds_from_conversation
                                _tr = load_tool_rounds_from_conversation(row_local['conv_id'])
                                if _tr:
                                    state_local['toolRounds'] = _tr
                            if row_local['error']:
                                from lib.error_envelope import from_json as _err_from_json
                                state_local['error'] = _err_from_json(row_local['error'])
                            yield f'data: {_dumps_yielding(state_local)}\n\n'
                        done_evt_local = build_event(EventType.DONE)
                        if row_local:
                            if row_local['metadata']:
                                try:
                                    m = json.loads(row_local['metadata'])
                                    # Field list MUST mirror
                                    # _extract_task_meta / _extract_db_meta
                                    # / chat_poll's DB-path loop.  See
                                    # _extract_task_meta docstring for why.
                                    for k in ('finishReason', 'usage', 'preset', 'toolSummary',
                                              'model', 'provider_id', 'thinkingDepth',
                                              'apiRounds', 'modifiedFiles', 'modifiedFileList',
                                              'fallbackModel', 'fallbackFrom',
                                              'fallbackReason', 'fallbackKind'):
                                        if m.get(k):
                                            done_evt_local[k] = m[k]
                                except (json.JSONDecodeError, TypeError) as _e_audit:
                                    logger.debug('[chat_dispatch] gen_persisted caught %s: %s',
                                                 type(_e_audit).__name__, _e_audit)
                            if row_local['error']:
                                from lib.error_envelope import from_json as _err_from_json
                                done_evt_local['error'] = _err_from_json(row_local['error'])
                        yield f'data: {_dumps_yielding(done_evt_local)}\n\n'
                    except Exception as _e:
                        logger.debug('[Chat] cold-replay synthetic done failed: %s', _e)

                return sse_response(gen_persisted())

    # ── DB snapshot path (no persisted events / no valid cursor) ──
    db = get_db(DOMAIN_CHAT)
    row = await asyncio.to_thread(
        lambda: db.execute(
            'SELECT conv_id,content,thinking,error,status,tool_rounds,metadata '
            'FROM task_results WHERE task_id=?',
            (task_id,)
        ).fetchone())
    if row:
        # ★ Close the 5s cold-replay window (see gen_persisted above):
        #   fold the lossless per-delta task_events log; falls back to
        #   the checkpoint pair on an empty/failed log.
        from lib.tasks_pkg.event_fold import fold_cold_state_text
        _fold_c, _fold_t = fold_cold_state_text(
            task_id, row['content'] or '', row['thinking'] or '')
        state = build_event(
            EventType.STATE, content=_fold_c,
            thinking=_fold_t, status=row['status'],
        )
        if row['error']:
            from lib.error_envelope import from_json as _err_from_json
            state['error'] = _err_from_json(row['error'])
        if row['tool_rounds']:
            try:
                state['toolRounds'] = await asyncio.to_thread(_loads_yielding, row['tool_rounds'])
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.warning('[Chat] Failed to parse tool_rounds for task %s: %s',
                               task_id, e, exc_info=True)
        else:
            from lib.tasks_pkg import load_tool_rounds_from_conversation
            _tr = await asyncio.to_thread(load_tool_rounds_from_conversation, row['conv_id'])
            if _tr:
                state['toolRounds'] = _tr
        meta = _extract_db_meta(row)
        # Field lists MUST stay aligned with _extract_task_meta and the
        # chat_poll DB-path loop. See _extract_task_meta docstring.
        for key in ('finishReason', 'usage', 'preset', 'model',
                    'provider_id', 'thinkingDepth',
                    'apiRounds', 'modifiedFiles', 'modifiedFileList'):
            if meta.get(key):
                state[key] = meta[key]
        done_evt = build_event(EventType.DONE)
        for key in ('finishReason', 'usage', 'preset', 'toolSummary',
                    'model', 'provider_id', 'thinkingDepth',
                    'apiRounds', 'modifiedFiles', 'modifiedFileList'):
            if meta.get(key):
                done_evt[key] = meta[key]
        if meta.get('fallbackModel'):
            done_evt['fallbackModel'] = meta['fallbackModel']
            done_evt['fallbackFrom'] = meta.get('fallbackFrom', '')
            if meta.get('fallbackReason'):
                done_evt['fallbackReason'] = meta['fallbackReason']
            if meta.get('fallbackKind'):
                done_evt['fallbackKind'] = meta['fallbackKind']
        if row['error']:
            from lib.error_envelope import from_json as _err_from_json
            done_evt['error'] = _err_from_json(row['error'])

        logger.info('[Chat] Stream %s served from DB — status=%s content=%dchars '
                    'finishReason=%s model=%s error=%s',
                    task_id[:8], row['status'], len(row['content'] or ''),
                    meta.get('finishReason', '?'), meta.get('model', '?'),
                    row['error'] or 'none')

        def gen_done():
            for _ in range(4):
                yield ':' + ' ' * 2048 + '\n\n'
            yield f'data: {_dumps_yielding(state)}\n\n'
            yield f'data: {_dumps_yielding(done_evt)}\n\n'

        return sse_response(gen_done())

    # ── Not found ──
    logger.warning('[Chat] Task %s not found (stream)', task_id)
    return api_not_found('Task not found')


@dataclass
class WarmResumePlan:
    """Result of ``plan_warm_resume``: enough state to drive the warm
    resume yields (pt_04686ac6 slice 7).

    Attributes:
        resume_from: The event-log index the caller resumes streaming
            from (per the SSE spec, ``cursor + 1`` — Last-Event-ID is the
            id of the last RECEIVED event).
        replay_events: Copy of ``task['events'][resume_from:]`` taken
            under ``events_lock``; the caller yields these back to the
            client as ``id:``-tagged replay frames.
        resume_state: The leading ``state`` event the caller emits BEFORE
            the delta replay (see the ★ comment about the frontend
            keep-longer guard adopting a full snapshot on a fresh
            placeholder). Carries ``content`` / ``thinking`` /
            ``status`` / ``toolRounds`` + optional ``createdAt``,
            ``error`` copied from the task under the lock.
        serviceable: Always True on a returned plan — kept as an explicit
            attribute so a False-y check on the plan itself never fires
            by mistake (a plan with 0 replay events is still valid — it
            triggers the terminal ``late_done`` synth in the caller).
    """
    resume_from: int
    replay_events: list
    resume_state: dict[str, Any]
    serviceable: bool = True


def plan_warm_resume(
    task: dict[str, Any],
    last_event_id_hdr: str,
    task_id_short: str,
) -> WarmResumePlan | None:
    """chat_stream warm-path resume planner (pt_04686ac6 slice 7).

    Called by ``routes/chat.py::chat_stream`` inside its generate()
    closure. Handles the two-part decision the pre-slice inline code
    made:

      1. Parse ``Last-Event-ID`` to an int cursor. Empty / non-numeric
         → cursor=None → no valid resume → return None (caller falls
         through to ``build_fresh_state_snapshot``).
      2. Check ``_warm_resume_serviceable(cursor, len(task['events']))``
         under ``events_lock``. When True: slice ``task['events']`` from
         cursor+1, capture the resume state, return a ``WarmResumePlan``.
         When False (cursor ahead of buffer / trimmed): return None so
         the caller emits a full-state resync snapshot.

    Args:
        task: The in-memory task dict (must have ``events``,
            ``events_lock``, ``content``, ``thinking``, ``status``,
            ``error``, ``toolRounds``).
        last_event_id_hdr: Raw ``Last-Event-ID`` HTTP header value
            (empty string when absent).
        task_id_short: An 8-char task id for log lines (matches the
            pre-slice inline ``task_id[:8]`` style).

    Returns:
        A ``WarmResumePlan`` when the cursor is in-buffer; ``None``
        when the caller should build a fresh snapshot instead.
    """
    _cursor_hdr = (last_event_id_hdr or '').strip()
    if not _cursor_hdr:
        return None
    try:
        _cursor = int(_cursor_hdr)
    except (ValueError, TypeError):
        logger.debug('[Chat] SSE stream %s ignoring invalid Last-Event-ID: %s',
                     task_id_short, _cursor_hdr)
        return None
    logger.info('[Chat] SSE stream %s reconnecting with Last-Event-ID=%d',
                task_id_short, _cursor)

    # Late import to keep chat_dispatch import-lightweight for tests
    # (routes.chat_helpers re-exports _warm_resume_serviceable, which
    # is a 4-line pure function so the extra module load is cheap).
    from routes.chat_helpers import _warm_resume_serviceable
    from lib.agent_core.events import EventType, build_event

    with task['events_lock']:
        if not _warm_resume_serviceable(_cursor, len(task['events'])):
            if _cursor >= 0:
                logger.info('[Chat] SSE stream %s Last-Event-ID=%d is ahead of '
                            'buffer (len=%d) — full-snapshot resync',
                            task_id_short, _cursor, len(task['events']))
            return None
        _resume_from = _cursor + 1
        _replay = task['events'][_resume_from:]
        # ★ VU carrier (2026-07-26 contract): a warm resume keeps the
        #   client's JS state, so the VU bubble still holds every frame up
        #   to the cursor — replaying the MISSED frames is the whole job.
        #   Echoing the agent `state` (content/thinking) would paint a
        #   phantom Agent bubble; echoing a vu replaySnapshot would
        #   reset-then-reappend (duplication). resume_state=None → the
        #   caller yields nothing before the replay.
        if task.get('_vu_subtask'):
            return WarmResumePlan(
                resume_from=_resume_from,
                replay_events=_replay,
                resume_state=None,
                serviceable=True,
            )
        # ★ Build the resume state under the SAME lock that sliced the
        #   replay list, so the state and the deltas are internally
        #   consistent (a mid-lock append to task['content'] on the
        #   producer side otherwise races).  Mirrors the pre-slice
        #   inline block's second ``with task['events_lock']:`` — kept
        #   as ONE lock hold for slice 7 since the two blocks were
        #   trivially adjacent and the extra release+reacquire was
        #   pure overhead (no other awaits between them).
        _state = build_event(
            EventType.STATE, content=task['content'],
            thinking=task['thinking'], status=task['status'],
        )
        _created = task.get('created_at')
        if _created:
            _state['createdAt'] = int(_created * 1000)
        if task['error']:
            _state['error'] = task['error']
        _state['toolRounds'] = task['toolRounds']

    return WarmResumePlan(
        resume_from=_resume_from,
        replay_events=_replay,
        resume_state=_state,
        serviceable=True,
    )


def build_fresh_state_snapshot(task: dict[str, Any]):
    """chat_stream fresh-connection snapshot builder (pt_04686ac6 slice 7).

    Called by ``routes/chat.py::chat_stream`` when ``plan_warm_resume``
    returned None (no cursor / cursor ahead of buffer). Builds the full
    state event + meta dict + cursor exactly as the pre-slice inline
    block did.

    Runs the entire snapshot compose under ``events_lock`` so a
    concurrent producer append cannot split content across the state
    read and the cursor snapshot.

    Args:
        task: The in-memory task dict.

    Returns:
        (state, meta, cursor):
          * state: the ``state`` event dict to encode + yield
            (via ``asyncio.to_thread(_dumps_yielding, state)`` — the
            caller does the offload since this fn stays synchronous).
          * meta: the raw ``_extract_task_meta(task)`` dict; caller
            reuses it if the task is already terminal to synthesize
            the trailing ``done`` event.
          * cursor: ``len(task['events'])`` — where the live-stream
            loop should start reading from.
    """
    from lib.agent_core.events import EventType, build_event
    from lib.chat.persistence import extract_task_meta as _extract_task_meta

    with task['events_lock']:
        state = build_event(
            EventType.STATE, content=task['content'],
            thinking=task['thinking'], status=task['status'],
        )
        _created = task.get('created_at')
        if _created:
            state['createdAt'] = int(_created * 1000)
        if task['error']:
            state['error'] = task['error']
        if task['toolRounds']:
            state['toolRounds'] = task['toolRounds']
        meta = _extract_task_meta(task)
        for key in ('finishReason', 'usage', 'model', 'thinkingDepth'):
            if meta.get(key):
                state[key] = meta[key]
        if task.get('preset'):
            state['preset'] = task['preset']
        if task.get('_memoryPrefetch'):
            state['memoryPrefetch'] = task['_memoryPrefetch']
        if task.get('_preferencesApplied'):
            state['preferencesApplied'] = task['_preferencesApplied']
        if task.get('_relatedConversations'):
            state['relatedConversations'] = task['_relatedConversations']
        if task.get('_preferencesLearned'):
            state['preferencesLearned'] = task['_preferencesLearned']
        # inbox-inject sidecars (swarm/peer/user-steer) — survive an
        # SSE-broken resume so the in-timeline inject chips repaint.
        if task.get('_inboxInjects'):
            state['inboxInjects'] = task['_inboxInjects']
        if task.get('_peerInjects'):
            state['peerInjects'] = task['_peerInjects']
        if task.get('_userSteerInjects'):
            state['userSteerInjects'] = task['_userSteerInjects']
        # ★ Model-fallback early-notification sidecar: llm_fallback/_call.py
        #   stamps these AT THE DECISION MOMENT (before the fallback stream
        #   starts), so a cold reload DURING the (long) fallback round still
        #   repaints the in-bubble banner instead of waiting for done.
        if task.get('_fallback_model'):
            state['fallbackModel'] = task['_fallback_model']
            state['fallbackFrom'] = task.get('_fallback_from', '')
            state['fallbackReason'] = task.get('_fallback_reason', '')
            state['fallbackKind'] = task.get('_fallback_kind', '')
        if task.get('endpoint_mode'):
            state['endpointMode'] = True
            state['endpointPhase'] = task.get('_endpoint_phase', 'planning')
            state['endpointIteration'] = task.get('_endpoint_iteration', 0)
            ep_turns = task.get('_endpoint_turns')
            if ep_turns:
                state['endpointTurns'] = ep_turns
            if task.get('_endpoint_stop_reason'):
                state['endpointStopReason'] = task['_endpoint_stop_reason']
        cursor = len(task['events'])

    return state, meta, cursor


def build_connect_snapshot(task: dict[str, Any]):
    """Fresh-connection snapshot selector (2026-07-26, VU-carrier contract).

    A ``_vu_subtask`` carrier must NOT greet the client with the agent
    ``state`` frame that :func:`build_fresh_state_snapshot` composes —
    that frame renders the VU's in-flight reply as a second "Agent"
    bubble (conv ms1rrjchpa5pqw: "user agent agent" instead of
    "user agent Autopilot").  The carrier instead opens with an
    ``autopilot_vu_start`` frame carrying a ``replaySnapshot`` of the
    VU's current content/thinking/toolRounds; the frontend applies it
    with RESET semantics (dedupes the frames already seen on the parent
    stream before the hop), then continues from the live tail
    (``cursor == len(events)`` — replaying history would double-append).

    Everything else defers to :func:`build_fresh_state_snapshot`
    byte-identically.

    Returns:
        ``(state, meta, cursor)`` — same triple shape as
        :func:`build_fresh_state_snapshot` so the caller's yield /
        terminal-synth plumbing is flavor-agnostic.
    """
    if task.get('_vu_subtask'):
        from lib.agent_core.events import EventType, build_event
        from lib.chat.persistence import extract_task_meta as _extract_task_meta
        with task['events_lock']:
            state = build_event(
                EventType.AUTOPILOT_VU_START,
                vuMsgId=task.get('_vu_msg_id') or '',
            )
            state['replaySnapshot'] = {
                'content': task.get('content') or '',
                'thinking': task.get('thinking') or '',
                'toolRounds': list(task.get('toolRounds') or []),
            }
            meta = _extract_task_meta(task)
            cursor = len(task['events'])
        return state, meta, cursor
    return build_fresh_state_snapshot(task)


def build_carrier_terminal_done(task: dict[str, Any]) -> dict[str, Any]:
    """The MINIMAL done frame that closes a ``_vu_subtask`` carrier stream.

    The VU bubble is settled by ``autopilot_vu_done`` / removed by
    ``autopilot_vu_cancel`` — the closing frame's only jobs are (a) flip
    the frontend's ``streamDone`` latch so the stream ends cleanly (no
    resume-retry / poll-fallback dance), and (b) ship the supersede
    successor when one is already registered (the autopilot follow-up
    worker), so the client hops VU → follow-up without polling.  It
    deliberately carries NO agent meta (usage / finishReason / …): the
    frontend binds a detached dummy assistant on this stream, and meta
    would paint a phantom agent finish bar.

    ``latestLiveTaskIsVu`` is stamped when the successor is itself a VU
    carrier (kick → VU → VU chains), so the frontend routes the next hop
    through the VU connector too.
    """
    from lib.agent_core.events import EventType, build_event
    evt = build_event(EventType.DONE)
    try:
        from lib.tasks_pkg.manager import _live_successor_info
        _succ_tid, _succ_is_vu = _live_successor_info(
            task.get('convId') or '', exclude_task_id=task.get('id', ''))
        if _succ_tid:
            evt['latestLiveTaskId'] = _succ_tid
            if _succ_is_vu:
                evt['latestLiveTaskIsVu'] = True
    except Exception as _stamp_err:
        logger.debug('[Chat] carrier-done successor stamp failed: %s',
                     _stamp_err)
    return evt


def is_task_terminal(task: dict[str, Any]) -> bool:
    """Predicate: is the task truly finished from the SSE stream's POV?
    (pt_04686ac6 slice 8; moved from a chat_stream local closure).

    True iff ``task['status'] != 'running'``.  (pt_8dc03017 cutover removed
    the ``_autopilot_deciding`` withhold — the VU now runs as an independent
    task on its own stream, so a done parent is genuinely terminal and the
    SSE stream closes promptly; the client discovers the successor via the
    conv→latest-task supersede index, not via a withheld baton.)
    """
    return task['status'] != 'running'


def apply_autopilot_baton(task: dict[str, Any], evt: dict[str, Any]) -> dict[str, Any]:
    """Stamp the autopilot follow-up baton onto a SYNTHESIZED done
    event (pt_04686ac6 slice 8; moved from a chat_stream local closure).

    The orchestrator's REAL done event carries
    ``autopilotNextTaskId`` / ``autopilotVuMessage`` directly, but
    every late / synthetic done built by the SSE stream helpers uses
    ``extract_task_meta()``, which does NOT include them.  If a
    synthetic done is ever sent for an autopilot turn (cold replay,
    resume, or a residual status-flip race), copy the baton from the
    transport-agnostic stash so the frontend still attaches to the
    spawned follow-up instead of stranding it.  Mirrors the
    ``chat_poll`` baton surfacing.

    Returns the (mutated) ``evt`` for call-site chaining.
    """
    _ap = task.get('_autopilot_followup')
    if _ap:
        evt['autopilotNextTaskId'] = _ap['next_task_id']
        evt['autopilotVuMessage'] = _ap['vu_msg']
    return evt


@dataclass
class LiveTickAction:
    """Result of ``next_live_tick``: what the LIVE-stream driver should
    do THIS tick (pt_04686ac6 slice 8).

    Every field except ``kind`` is optional; the driver reads only the
    fields relevant to ``kind``. This keeps the dataclass a flat, easy-
    to-inspect value object (no discriminated-union machinery) while
    still steering the driver to the right branch.

    Attributes:
        kind: One of
          * ``'sse_timeout'`` — driver yields ``timeout_evt`` (JSON
            SSE frame WITHOUT ``id:`` — synthetic) then RETURNS
            (closes the stream so the frontend switches to polling).
          * ``'events'`` — driver yields each ``(event_id, event)``
            in ``frames`` as ``id: <n>\\ndata: <json>\\n\\n``, stamps
            ``_events_sent`` up, sets ``last_t = now``, and if a
            frame's type is ``'done'`` RETURNS immediately (the
            orchestrator's real done). Otherwise advances ``cursor``
            to ``next_cursor`` and continues.
          * ``'late_done'`` — driver yields ``late_done_evt`` as
            ``id: <next_cursor>\\ndata: <json>\\n\\n`` then RETURNS.
          * ``'superseded'`` — driver just RETURNS (the newer reader
            takes over).
          * ``'keepalive'`` — driver yields ``: keepalive\\n\\n``,
            updates ``last_t = now``, refreshes the SSE slot lease.
          * ``'sleep'`` — driver does ``await asyncio.sleep(0.05)``
            and loops.
        timeout_evt: SSE_TIMEOUT event dict (set only when
            kind='sse_timeout').
        frames: List of ``(event_id, event)`` tuples (set only when
            kind='events').
        next_cursor: Advance cursor value; set for 'events'
            (post-drain new length) and 'late_done' (== len(events)
            at time of the check — used as the ``id:`` on the
            synthesized frame).
        late_done_evt: Fully-populated DONE event (set only when
            kind='late_done'); baton already applied.
    """
    kind: str
    timeout_evt: dict[str, Any] | None = None
    frames: list | None = None
    next_cursor: int | None = None
    late_done_evt: dict[str, Any] | None = None


def next_live_tick(
    task: dict[str, Any],
    cursor: int,
    sse_gen: int,
    stream_start: float,
    sse_max_duration: float,
    last_t: float,
    now: float,
    task_id_short: str,
) -> LiveTickAction:
    """LIVE-path tick planner (pt_04686ac6 slice 8).

    Called once per iteration of chat_stream's ``while True`` loop
    with the caller's current time, cursor, and stream-start.
    Deterministic — the same inputs always yield the same
    ``LiveTickAction``. Reads ``task`` under ``events_lock`` when
    slicing new events; otherwise no side effects except logging.

    Decision order (matches the pre-slice inline block byte-for-byte):
      1. Elapsed > SSE_MAX_DURATION → ``sse_timeout``.
      2. Drain new events past ``cursor``. If any → ``events``.
      3. Task terminal AND drain was empty → ``late_done``.
      4. Newer SSE reader present → ``superseded``.
      5. Gap since last emit > 15s → ``keepalive``.
      6. Else → ``sleep``.

    Args:
        task: The in-memory task dict.
        cursor: The current SSE-cursor (index into task['events']).
        sse_gen: The generation id captured when THIS reader opened
            (for supersede detection).
        stream_start: Wall-clock time (seconds) when the SSE stream
            opened; used for the max-duration guard.
        sse_max_duration: Max SSE stream lifetime in seconds
            (default 7200 in the caller; parameterized so tests can
            drive tighter bounds).
        last_t: Wall-clock time of the last emit (event/keepalive/
            preamble). Used for the 15s keepalive gap.
        now: Wall-clock time NOW (passed in so tests are
            deterministic; production uses ``time.time()``).
        task_id_short: 8-char task id for log lines.

    Returns:
        A ``LiveTickAction`` with ``kind`` naming the branch and
        exactly the fields that branch needs populated.
    """
    from lib.agent_core.events import EventType, build_event
    from lib.chat.persistence import extract_task_meta as _extract_task_meta

    # 1. SSE max-duration guard.
    _elapsed = now - stream_start
    if _elapsed > sse_max_duration:
        _conv_id = task.get('convId', '?')
        logger.warning(
            '[Chat] SSE stream %s conv=%s closing after %.0fs (max %.0fs) — '
            'task still running (status=%s). '
            'Frontend will switch to polling to pick up the result.',
            task_id_short, _conv_id, _elapsed, sse_max_duration,
            task.get('status', '?'))
        timeout_evt = build_event(
            EventType.SSE_TIMEOUT,
            message='SSE connection reached maximum duration. Switching to '
                    'polling — task is still running.')
        return LiveTickAction(kind='sse_timeout', timeout_evt=timeout_evt)

    # 2. Drain new events past cursor (under lock).
    with task['events_lock']:
        new_evts = task['events'][cursor:]
        _cursor_before = cursor
        _new_cursor = len(task['events'])
    if new_evts:
        frames = [(_cursor_before + idx, ev) for idx, ev in enumerate(new_evts)]
        return LiveTickAction(kind='events', frames=frames,
                              next_cursor=_new_cursor)

    # 3. Task terminal + no new events → synthesize LATE done.
    if is_task_terminal(task):
        # ★ VU carrier (2026-07-26 contract): the endpoint-managed
        #   finalize never emits a done for the carrier, so its stream
        #   closes HERE — with a MINIMAL done (no agent meta; the VU
        #   bubble was already settled/removed by the lifecycle frame)
        #   carrying any registered successor for the next hop.  The
        #   lifecycle frame is ALWAYS appended before the terminal flip
        #   (autopilot._close_vu_carrier_stream runs after the dual-emit),
        #   so the drain above already delivered it.
        if task.get('_vu_subtask'):
            logger.info('[Chat] SSE stream %s emitting carrier done '
                        '(VU sub-task terminal) — closing stream',
                        task_id_short)
            return LiveTickAction(
                kind='late_done',
                late_done_evt=build_carrier_terminal_done(task),
                next_cursor=_new_cursor)
        # Finalize-window latch: the orchestrator flips status to terminal
        # BEFORE the real done is appended (the autopilot hook + pre-emit
        # sync run in between). A tick landing in that window would emit a
        # LATE done MISSING the latestLiveTaskId stamp (the supersede index
        # is advanced only inside the hook) and close the stream with the
        # client deaf for the whole VU window. Hold while the latch is
        # fresh; the 30s ceiling self-expires a crashed finalize so the
        # stream can never wedge on a stuck flag.
        _fin_started = task.get('_finalize_started_at') or 0
        if _fin_started and (now - _fin_started) < 30:
            return LiveTickAction(kind='sleep')
        late_done = build_event(EventType.DONE)
        late_meta = _extract_task_meta(task)
        late_done.update(late_meta)
        if task.get('error'):
            late_done['error'] = task['error']
        # Severity split: an aborted/interrupted/normally-stopped
        # task that needs a LATE done synthesis is expected:
        #   - aborted/interrupted: user hit Stop after orchestrator
        #     already flushed its own done event.
        #   - stop: task finished normally between the queue poll and
        #     this status check (the common 157-line/day pattern).
        # Anything else (length, content_filter, error, etc.) still
        # signals a missed done-event — keep as warning.
        _late_fr = late_meta.get('finishReason', '?')
        _is_benign = _late_fr in ('aborted', 'interrupted', 'stop')
        _log_fn = logger.info if _is_benign else logger.warning
        _err_obj = task.get('error')
        _err_summary = (
            _err_obj.get('detail') or _err_obj.get('message') or _err_obj.get('kind')
            if isinstance(_err_obj, dict) else (_err_obj or 'none')
        )
        _log_fn('[Chat] SSE stream %s emitting LATE done '
                '(task finished but no done event in queue) — '
                'finishReason=%s model=%s error=%s',
                task_id_short, _late_fr,
                late_meta.get('model', '?'), _err_summary)
        apply_autopilot_baton(task, late_done)
        # ── Supersede-successor stamp (pt_8dc03017 wire completion) ──
        # The cutover made the parent close PROMPTLY at status-flip, with the
        # client discovering any successor (autopilot VU / follow-up) via the
        # conv→latest-task index. But that index never reached the wire: the
        # LATE done carried no baton (the VU was still deciding) and no
        # successor id, and the VU is a carrier hidden from /api/chat/active
        # — the client went deaf for the whole VU window, so an auto-
        # dispatched queued turn started invisibly (production 2026-07-25:
        # six minutes of silence until a manual refresh). Ship the index.
        try:
            from lib.tasks_pkg.manager import _live_successor_info
            _succ_tid, _succ_is_vu = _live_successor_info(
                task.get('convId') or '', exclude_task_id=task.get('id', ''))
            if _succ_tid:
                late_done['latestLiveTaskId'] = _succ_tid
                if _succ_is_vu:
                    late_done['latestLiveTaskIsVu'] = True
        except Exception as _stamp_err:
            logger.debug('[Chat] LATE done successor stamp failed: %s', _stamp_err)
        return LiveTickAction(kind='late_done', late_done_evt=late_done,
                              next_cursor=_new_cursor)

    # 4. Newer SSE reader → close this stale one.
    _gen_now = task.get('_sse_gen_id', sse_gen)
    if _gen_now != sse_gen:
        logger.info('[Chat] SSE stream %s superseded by newer reader (gen %d→%d) — '
                    'closing stale reader',
                    task_id_short, sse_gen, _gen_now)
        return LiveTickAction(kind='superseded')

    # 5. Keepalive on >15s gap since last emit.
    if now - last_t > 15:
        return LiveTickAction(kind='keepalive')

    # 6. Nothing to do → sleep.
    return LiveTickAction(kind='sleep')


# ── Slice 9 (pt_04686ac6): dispatch_prefill_continue ─────────────────
#
# Extracts routes/chat.py::_continue_via_prefill_only — the last ~78-line
# independent closure in the file (per the epic's ordered plan). Case 3
# of the Continue flow: no tool-call checkpoint but a resumable prefill
# was extracted from segments; feed the model its own half-written string
# and let it continue the SAME tokens.
#
# The route helper previously wrapped ALL of: assistant_msg mutation
# (content ← orig_full, thinking → priorThinking iff non-empty, stale
# key strip), persist, cfg_payload build (excludeLast / resumePrefill /
# contentPrefix), dispatch through _start_task_for_conv, stamp
# activeTaskId, notify_conv_changed, audit_log, and build the
# checkpoint jsonify payload.
#
# After extraction: chat_continue's inline call becomes:
#
#   res = dispatch_prefill_continue(...)
#   if res.kind == 'passthrough':
#       return res.err_resp
#   return jsonify(res.payload)
#
# The collaborators (_start_task_for_conv, _persist_conv_messages,
# set_conversation_settings, _notify_conv_changed, _audit_log) are
# passed as callables so the extraction is unit-testable without a
# Flask app context and without the routes.* / lib.conversations /
# lib.log import graph. Real callers hand in the module bindings.


@dataclass
class PrefillContinueResult:
    """Discriminated union — either the caller wraps `payload` in
    jsonify(...) OR returns `err_resp` verbatim (task-start failure).
    """
    kind: str  # 'payload' | 'passthrough'
    payload: dict[str, Any] | None = None
    err_resp: Any = None


def dispatch_prefill_continue(
    db: Any,
    conv_id: str,
    messages: list,
    assistant_msg: dict[str, Any],
    title: Any,
    config: dict[str, Any],
    settings_patch: Any,
    resume_prefill: str,
    orig_full_content: str,
    data: dict[str, Any],
    *,
    _start_task_for_conv,
    _persist_conv_messages,
    _set_conversation_settings,
    _notify_conv_changed,
    _audit_log,
    user_id: Any = 1,
) -> PrefillContinueResult:
    """Case 3 continue: resume a NO-TOOL mid-answer turn via assistant prefill.

    See the module docstring above for the full contract. The route
    layer supplies its module-scoped collaborators as keyword-only
    callables — this keeps the extraction unit-testable without any
    routes.* / lib.conversations / lib.log side imports.
    """
    # Keep the full prior answer as the continuation base. Clear the
    # live thinking tail (prefill carries content only on the wire) but
    # stash it display-only when non-empty, mirroring case 2.
    assistant_msg['content'] = orig_full_content
    _prior_think = assistant_msg.get('thinking') or ''
    assistant_msg['thinking'] = ''
    if _prior_think:
        assistant_msg['priorThinking'] = _prior_think
    for stale_key in ('finishReason', 'toolSummary', 'error'):
        assistant_msg.pop(stale_key, None)

    _persist_conv_messages(db, conv_id, messages, title, settings_patch)

    cfg_payload = dict(config)
    cfg_payload['excludeLast'] = True
    cfg_payload['resumePrefill'] = resume_prefill
    # Seed task['content'] with the full pre-rollback answer so the
    # resumed turn displays everything the user already saw plus the
    # continuation.
    cfg_payload['contentPrefix'] = orig_full_content

    task_id, err_resp = _start_task_for_conv(conv_id, cfg_payload, data)
    if err_resp is not None:
        return PrefillContinueResult(kind='passthrough', err_resp=err_resp)

    try:
        # notify=False: _notify_conv_changed is emitted below (no double push).
        _set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                   db=db, notify=False)
    except Exception as e:
        logger.warning('[Continue] Failed to update activeTaskId (prefill-only): %s', e)

    _notify_conv_changed(conv_id, rev=None, user_id=user_id)
    try:
        _audit_log('continue_prefill_only', conv_id=conv_id,
                   prefillChars=len(resume_prefill),
                   origContentChars=len(orig_full_content))
    except Exception as e:
        logger.debug('[Continue] audit_log (prefill-only) failed (non-fatal): %s', e)

    return PrefillContinueResult(kind='payload', payload={
        'taskId': task_id,
        'convId': conv_id,
        'checkpoint': {
            'keptRounds': 0,
            'discardedRounds': 0,
            'preservedContentLen': len(orig_full_content),
            'discardedContentLen': 0,
            'preservedThinkingChars': 0,
            'discardedThinking': 0,
            'resumeMode': 'prefill',
            # Prefill CONTINUES the same string — nothing discarded, so
            # the full prior answer is the continuation base and there
            # is NO priorContent block. The reasoning tail is display-
            # only.
            'contentPrefix': orig_full_content,
            'priorContent': '',
            'priorThinking': assistant_msg.get('priorThinking') or '',
        },
    })


@dataclass
class ContinueOutcome:
    """Discriminated union for :func:`execute_chat_continue`.

    kind:
      * ``'started'``  — a resume task was started; ``payload`` is the
        continue response body (``taskId`` / ``convId`` / ``checkpoint``).
      * ``'fallback'`` — nothing recoverable; ``payload`` is
        ``{'fallback': 'regenerate', 'reason': ...}`` and the caller
        decides how to regenerate (the HTTP route ships it verbatim;
        chat_start's interrupted-tail guard pops the stub and starts
        fresh instead).
      * ``'error'``    — passthrough error response (400/404/500 or a
        task-start failure); the caller returns ``err_resp`` verbatim.
    """
    kind: str
    payload: dict[str, Any] | None = None
    err_resp: Any = None


def execute_chat_continue(
    conv_id: str,
    config: dict[str, Any],
    settings_patch: Any,
    data: dict[str, Any],
    *,
    user_id: Any,
    _start_task_for_conv,
    _persist_conv_messages,
    _set_conversation_settings,
    _notify_conv_changed,
    _audit_log,
) -> ContinueOutcome:
    """Atomic continue: roll back the last assistant message to its last
    complete tool-call checkpoint, persist the rolled-back state to DB,
    then start a new task that resumes from that checkpoint.

    Single source of truth for the continue contract, shared by:

      * ``routes/chat.py::chat_continue`` — the HTTP entry (thin wrapper
        that parses the body and maps the outcome to responses), and
      * ``routes/chat.py::chat_start``'s interrupted-tail guard — a bare
        start whose DB tail is an interrupted assistant stub must RESUME
        it via this same contract instead of appending a twin turn (the
        ms43foj3 ``U A U A(stub) A(answer)`` doubled-bubble incident:
        the frontend's pop-and-regenerate escape hatch reached
        ``/chat/start`` with the rolled-back stub still at the tail, and
        the fresh task appended a sibling answer instead of continuing
        the stub).

    If no recoverable checkpoint is found (no complete tool rounds) the
    outcome is ``{'fallback': 'regenerate'}`` and the caller decides how
    to regenerate.

    The collaborators (``_start_task_for_conv``, ``_persist_conv_messages``,
    ``_set_conversation_settings``, ``_notify_conv_changed``,
    ``_audit_log``) are passed as callables — same seam pattern as
    ``dispatch_prefill_continue`` — so the extraction is unit-testable
    without a Flask app context, and monkeypatches of the routes.chat
    module bindings keep steering the real route exactly as before.
    """
    # Late imports (matches this module's established style — keeps the
    # import graph acyclic with routes.* / lib.tasks_pkg).
    import json as _json

    from lib.api_response import api_bad_request, api_internal_error, api_not_found
    from lib.chat import scan_continue_checkpoint
    from lib.database import DOMAIN_CHAT, get_thread_db
    from routes.common import DEFAULT_USER_ID

    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages, title FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID)
    ).fetchone()

    if not row:
        return ContinueOutcome(kind='error', err_resp=api_not_found('Conversation not found'))

    try:
        messages = _json.loads(row['messages'] or '[]')
    except (_json.JSONDecodeError, TypeError) as e:
        logger.warning('[Continue] Failed to parse messages for conv=%s: %s',
                       conv_id[:8], e)
        return ContinueOutcome(kind='error',
                               err_resp=api_internal_error('Failed to parse conversation'))

    title = row['title']

    if not messages:
        return ContinueOutcome(kind='error',
                               err_resp=api_bad_request('Conversation has no messages'))
    if messages[-1].get('role') != 'assistant':
        return ContinueOutcome(
            kind='error',
            err_resp=api_bad_request('Last message is not an assistant message'))

    assistant_msg = messages[-1]
    # Trivial case: empty content & thinking → no checkpoint needed; ask
    # the caller to fall back to pop-and-resend (full regeneration).
    if not assistant_msg.get('content') and not assistant_msg.get('thinking') \
            and not (assistant_msg.get('toolRounds') or []):
        logger.info('[Continue] conv=%s last assistant is empty — fallback to regenerate',
                    conv_id[:8])
        return ContinueOutcome(kind='fallback',
                               payload={'fallback': 'regenerate', 'reason': 'empty_assistant'})

    # ★ Resume-prefill extraction (epic pt_cb8f98b0cb9b47fb) — MUST happen
    #   BEFORE any rollback mutates assistant_msg. The segment list is the
    #   SoT: resume_prefill_from_segments reads the terminal deliverable
    #   (the tail the model was mid-writing) from assistant_msg['segments'],
    #   gated on model prefill capability + a resumable finish reason. This
    #   is the segments-first replacement for tail-diffing. Claude → None
    #   (fail closed). The full pre-rollback content is captured too, to
    #   seed task['content'] so nothing the user saw vanishes on resume.
    _model = (config.get('model') or '').strip()
    _finish_reason = assistant_msg.get('finishReason') or ''
    _orig_full_content = assistant_msg.get('content') or ''
    _resume_prefill = None
    try:
        from lib.tasks_pkg.segments import resume_prefill_from_segments
        _resume_prefill = resume_prefill_from_segments(
            assistant_msg.get('segments'), _model, finish_reason=_finish_reason,
            content=_orig_full_content)
    except Exception as _rp_e:
        logger.debug('[Continue] resume-prefill extraction failed (non-fatal): %s', _rp_e)

    scan = scan_continue_checkpoint(assistant_msg)
    if scan is None:
        # No tool-call checkpoint. If we DO have a resumable prefill (a
        # no-tool mid-answer turn — case 3), resume via prefill alone rather
        # than regenerating from scratch. Otherwise fall back to regenerate.
        if _resume_prefill:
            logger.info('[Continue] conv=%s no tool checkpoint but resumable '
                        'prefill (%d chars) — resuming via assistant prefill (case 3)',
                        conv_id[:8], len(_resume_prefill))
            _res = dispatch_prefill_continue(
                db=db, conv_id=conv_id, messages=messages,
                assistant_msg=assistant_msg, title=title, config=config,
                settings_patch=settings_patch,
                resume_prefill=_resume_prefill,
                orig_full_content=_orig_full_content, data=data,
                _start_task_for_conv=_start_task_for_conv,
                _persist_conv_messages=_persist_conv_messages,
                _set_conversation_settings=_set_conversation_settings,
                _notify_conv_changed=_notify_conv_changed,
                _audit_log=_audit_log,
                user_id=user_id,
            )
            if _res.kind == 'passthrough':
                return ContinueOutcome(kind='error', err_resp=_res.err_resp)
            return ContinueOutcome(kind='started', payload=_res.payload)
        logger.info('[Continue] conv=%s no tool-call checkpoint available — fallback to regenerate',
                    conv_id[:8])
        return ContinueOutcome(kind='fallback',
                               payload={'fallback': 'regenerate', 'reason': 'no_checkpoint'})

    # ── Apply rollback in place on the assistant message ──
    preserved_content = scan['preserved_content']
    assistant_msg['toolRounds'] = scan['kept_rounds']
    if _resume_prefill:
        # ★ case-2 prefill (P5, LOSSLESS): the trailing prose is CONTINUED
        #   (shipped as resumePrefill below), NOT discarded. Keep the FULL
        #   deliverable content and drop nothing into priorContent — the
        #   checkpoint-regenerate branch (else) discards the tail instead.
        assistant_msg['content'] = _orig_full_content
        assistant_msg.pop('priorContent', None)
    else:
        assistant_msg['content'] = preserved_content
    # Strip live thinking — any replay-worthy thinking already lives on
    # keptRounds[i].thinking and is carried forward via toolHistory.
    # If there was trailing message-level thinking (reasoning emitted
    # after the last completed tool batch) we can't replay it on the
    # wire, but we stash it on a display-only field so the UI can
    # render it as a collapsed "earlier thinking" block.  Stripped by
    # _strip_non_api_fields before any LLM call (not in _API_MESSAGE_FIELDS).
    assistant_msg['thinking'] = ''
    if scan.get('discarded_thinking_text'):
        # Replace rather than append — a Continue cycle that produced new
        # trailing thinking is the freshest signal of "what the model was
        # reasoning about right before we resumed."  Older priorThinking
        # from a prior Continue is no longer the immediate context.
        assistant_msg['priorThinking'] = scan['discarded_thinking_text']
    # else: leave any existing priorThinking from a previous Continue cycle
    # in place — streaming this turn produced no extra trailing thinking,
    # so the prior "earlier thinking" remains the most recent discard.
    # Same treatment for the discarded prose tail (display-only priorContent)
    # so a post-Continue page refresh (DB reload) doesn't lose the visible
    # record of what was rolled back — keeping the content area honest
    # rather than silently empty beside an unchanged tool panel.
    if scan.get('discarded_content_text') and not _resume_prefill:
        assistant_msg['priorContent'] = scan['discarded_content_text']
    for stale_key in ('finishReason', 'toolSummary', 'error'):
        assistant_msg.pop(stale_key, None)

    # Stash pre-checkpoint metadata on cfg for the task + for DB merge.
    kept_usage = assistant_msg.get('usage') or None
    kept_api_rounds = assistant_msg.get('apiRounds') or []
    kept_modified_files = assistant_msg.get('modifiedFiles') or None
    kept_modified_file_list = assistant_msg.get('modifiedFileList') or []

    # Persist rolled-back state BEFORE starting the task — mirrors the
    # order used in chat_regenerate to avoid the streaming task
    # overwriting the rollback in ``_sync_result_to_conversation``.
    _persist_conv_messages(db, conv_id, messages, title, settings_patch)

    logger.info(
        '[Continue] conv=%s kept=%d rounds discarded=%d rounds preservedContent=%d '
        'discardedContent=%d preservedThinking=%d discardedThinking=%d priorThinking=%s',
        conv_id[:8], len(scan['kept_rounds']), scan['discarded_rounds'],
        len(preserved_content), scan['discarded_content'],
        scan['preserved_thinking_chars'], scan['discarded_thinking'],
        'preserved' if scan.get('discarded_thinking_text') else 'none',
    )

    # Build cfg payload — same shape the frontend used to build.
    cfg_payload = dict(config)
    cfg_payload['excludeLast'] = True
    if scan['tool_history']:
        cfg_payload['toolHistory'] = scan['tool_history']
    if preserved_content:
        cfg_payload['contentPrefix'] = preserved_content
    # ★ Resume-prefill (case 2 — mid-prose AFTER a completed tool batch).
    #   When the provider tolerates prefill AND the tail is resumable, ship
    #   the terminal deliverable tail so the model continues the SAME tokens
    #   (inject_tool_history replays the tool batch; the prefill is appended
    #   as the trailing assistant turn by the orchestrator). Seed
    #   task['content'] with the FULL pre-rollback content so the resumed
    #   turn displays [everything the user saw] + [continuation] with no
    #   duplication — the continuation the model returns is ONLY the new
    #   tokens after the prefill. Claude / clean stop → _resume_prefill is
    #   None → contentPrefix (preserved_content) drives the universal path.
    if _resume_prefill:
        cfg_payload['resumePrefill'] = _resume_prefill
        cfg_payload['contentPrefix'] = _orig_full_content
    if scan['kept_rounds']:
        cfg_payload['checkpointToolRounds'] = scan['kept_rounds']
    if kept_usage:
        cfg_payload['checkpointUsage'] = kept_usage
    if kept_api_rounds:
        cfg_payload['checkpointApiRounds'] = kept_api_rounds
    if kept_modified_files:
        cfg_payload['checkpointModifiedFiles'] = kept_modified_files
    if kept_modified_file_list:
        cfg_payload['checkpointModifiedFileList'] = kept_modified_file_list

    # Start the task.
    # Anchor the turn-ctx capsule reconcile: continue resumes the assistant
    # for the last user BEFORE it. Walk back from the assistant tail to find
    # that user's stable _msgId; ships as done_evt.userMsgId.
    _continue_user_msg_id = ''
    for _m in reversed(messages[:-1]):
        if isinstance(_m, dict) and _m.get('role') == 'user':
            _continue_user_msg_id = _m.get('_msgId') or ''
            break
    task_id, err_resp = _start_task_for_conv(
        conv_id, cfg_payload, data,
        user_msg_id=_continue_user_msg_id)
    if err_resp is not None:
        return ContinueOutcome(kind='error', err_resp=err_resp)

    # Persist activeTaskId (settings-only — same rationale as chat_send:
    #    a full-row rewrite would clobber a task-thread checkpoint).
    try:
        # notify=False: the _notify_conv_changed below emits the push; the
        # settings write must invalidate the local cache (structural
        # guarantee) WITHOUT a second cross-device push.
        _set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                   db=db, notify=False)
    except Exception as e:
        logger.warning('[Continue] Failed to update activeTaskId: %s', e)

    _notify_conv_changed(conv_id, rev=None, user_id=user_id)
    try:
        _audit_log(
            'continue_checkpoint',
            conv_id=conv_id,
            kept=len(scan['kept_rounds']),
            discarded=scan['discarded_rounds'],
            preservedContentLen=len(preserved_content),
            discardedContentLen=scan['discarded_content'],
            preservedThinking=scan['preserved_thinking_chars'],
            discardedThinking=scan['discarded_thinking'],
            priorThinkingChars=len(scan.get('discarded_thinking_text') or ''),
        )
    except Exception as e:
        logger.debug('[Continue] audit_log failed (non-fatal): %s', e)

    return ContinueOutcome(kind='started', payload={
        'taskId': task_id,
        'convId': conv_id,
        'checkpoint': {
            'keptRounds': len(scan['kept_rounds']),
            'discardedRounds': scan['discarded_rounds'],
            'preservedContentLen': len(preserved_content),
            'discardedContentLen': scan['discarded_content'],
            'preservedThinkingChars': scan['preserved_thinking_chars'],
            'discardedThinking': scan['discarded_thinking'],
            # ── Authoritative anchor DATA (typed fact) ──────────────
            # The rollback the server ALREADY computed + persisted. The
            # frontend is a pure reducer over these: it slices its local
            # rounds to `keptRounds` and adopts these strings verbatim,
            # rather than re-deriving the checkpoint by scanning
            # status==='done' (which duplicated this exact logic).
            'resumeMode': 'prefill' if _resume_prefill else 'checkpoint',
            'contentPrefix': _orig_full_content if _resume_prefill else preserved_content,
            'priorContent': '' if _resume_prefill else (scan.get('discarded_content_text') or ''),
            'priorThinking': scan.get('discarded_thinking_text') or '',
        },
    })


__all__ = [
    'SendIntent', 'classify_send_intent', 'build_cold_replay_response',
    'WarmResumePlan', 'plan_warm_resume', 'build_fresh_state_snapshot',
    'LiveTickAction', 'next_live_tick', 'is_task_terminal',
    'apply_autopilot_baton',
    'PrefillContinueResult', 'dispatch_prefill_continue',
    'ContinueOutcome', 'execute_chat_continue',
]
