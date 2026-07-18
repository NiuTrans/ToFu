"""routes/chat.py — Chat start, streaming, polling, abort."""

import json
import threading
import time

import orjson

from flask import Blueprint, jsonify, request

from lib.agent_core.events import EventType, build_event
from lib.database import DOMAIN_CHAT, get_db
from lib.log import audit_log, get_logger
from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok, sse_response
from lib.request_parser import parse_body
from routes.api_v1.auth import current_auth, require_scope

from lib.tasks_pkg import cleanup_old_tasks, create_task, tasks, tasks_lock

import re

from lib.database import get_thread_db
# Back-compat aliases — the implementations moved to lib/chat/messages.py
# (2026-06) to break the lib→routes circular import. Internal callers below
# and external importers (lib.message_queue) keep using these private names.
from lib.chat import (  # noqa: F401
    append_user_msg_idempotent as _append_user_msg_idempotent,
    auto_translate_user as _auto_translate_user,
    build_tool_history_round as _build_tool_history_round,
    append_pending_user_msg as _append_pending_user_msg,
    build_user_msg_from_payload as _build_user_msg_from_payload,
    extract_db_meta as _extract_db_meta,
    extract_task_meta as _extract_task_meta,
    get_send_translate_status,
    load_or_create_conv as _load_or_create_conv,
    persist_conv_messages as _persist_conv_messages,
    resolve_conv_refs as _resolve_conv_refs,
    scan_continue_checkpoint as _scan_continue_checkpoint,
)
from lib.idempotency import idempotent_post
from routes.common import DEFAULT_USER_ID, _notify_conv_changed

logger = get_logger(__name__)

chat_bp = Blueprint('chat', __name__)


def _dumps_yielding(obj) -> str:
    """Serialize a (potentially multi-MB) SSE snapshot off the event loop.

    Background: the C accelerator behind ``json.dumps`` holds the GIL for the
    *entire* call and never releases it mid-encode, so wrapping plain
    ``json.dumps`` in ``asyncio.to_thread`` does NOT free the loop — a 10 MB
    conversation snapshot still stalls ``accept()`` for ~40 ms (the wedge
    behind the 15000 incident).

    ``orjson.dumps`` encodes the same 10 MB in ~5 ms — fast enough that the
    loop stall drops to ~4 ms even though it, too, holds the GIL; the encode
    is simply over before it matters, and it also tames the pathological
    "one huge string field" shape that ``iterencode`` (one atomic chunk)
    cannot. It is the primary path.

    orjson rejects a handful of inputs the stdlib tolerates (notably non-str
    dict keys → ``JSONEncodeError``/``TypeError``). For those rare snapshots
    we fall back to ``JSONEncoder.iterencode``, which yields to the
    interpreter between chunks so the loop can still breathe.

    The two encoders differ only in item separators (orjson is compact:
    ``,``/``:`` vs stdlib ``, ``/``: ``); both are valid JSON the frontend
    parses identically.
    """
    try:
        return orjson.dumps(obj).decode('utf-8')
    except (TypeError, ValueError) as e:
        logger.warning('[Chat] orjson snapshot encode failed (%s); '
                       'falling back to stdlib iterencode', e)
        return ''.join(json.JSONEncoder(ensure_ascii=False).iterencode(obj))


def _running_checkpoint_verdict(sharded: bool):
    """Decide how to report a DB checkpoint with status='running' whose task is
    ABSENT from this replica's memory (Epic C §4.1 / §6.4).

    Returns ``(effective_status, reconnect_hint)``:
      * sharded (redis, multi-replica): ``('running', True)`` — the task is
        (probably) alive on another replica; the client re-routes via taskId
        affinity. NO cross-replica liveness probe, NO DB flip to interrupted.
      * single-process (inproc): ``('interrupted', False)`` — absent genuinely
        means the server crashed mid-task; keep the crash-recovery behaviour
        byte-identical to before Epic C.
    """
    if sharded:
        return ('running', True)
    return ('interrupted', False)


def _log_poll_task_id_mismatch(db, conv_id, polled_task_id, db_meta):
    """P0 observability: log the activeTaskId ↔ message _taskId inconsistency
    behind an empty-metadata interrupted poll.

    When a poll serves an ``interrupted`` result whose metadata is EMPTY (no
    finishReason/usage/apiRounds — the finish-bar shows only the model name),
    the underlying cause is almost always an ID desync: the conversation's
    ``settings.activeTaskId`` no longer matches the task the client polled OR
    the trailing assistant message's ``_taskId``. Surfacing that mismatch here
    means the empty finish-bar is diagnosable from ``app.log`` alone, without a
    post-hoc DB query. Best-effort — never raises into the poll response.
    """
    try:
        has_meta = any(db_meta.get(k) for k in ('finishReason', 'usage', 'apiRounds'))
        if has_meta or not conv_id:
            return
        row = db.execute(
            'SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return
        try:
            settings = json.loads(row['settings'] or '{}') or {}
        except (json.JSONDecodeError, TypeError):
            settings = {}
        active_task_id = settings.get('activeTaskId')
        reconciled_at = settings.get('_reconciledAt')
        msg_task_id = None
        try:
            messages = json.loads(row['messages'] or '[]')
            for m in reversed(messages):
                if m.get('role') == 'assistant':
                    msg_task_id = m.get('_taskId')
                    break
        except (json.JSONDecodeError, TypeError):
            pass
        logger.warning(
            '[Chat] Poll %s — EMPTY-metadata interrupted result; ID inconsistency: '
            'polled=%s activeTaskId=%s msg_taskId=%s _reconciledAt=%s. '
            'Finish-bar will show only the model name (finishReason/usage/apiRounds never persisted). '
            'This is the interrupted-turn ID desync (empty finish-bar + flicker) class.',
            polled_task_id[:8], polled_task_id[:8],
            (active_task_id[:8] if active_task_id else 'none'),
            (msg_task_id[:8] if msg_task_id else 'none'),
            reconciled_at or 'none')
    except Exception as _e:
        logger.debug('[Chat] poll ID-mismatch probe failed conv=%s: %s',
                     conv_id[:8] if conv_id else '?', _e)


def _loads_yielding(raw):
    """Parse a (potentially multi-MB) JSON snapshot with minimal GIL-hold.

    The mirror of :func:`_dumps_yielding` for the DECODE direction. The
    stdlib ``json.loads`` C accelerator holds the GIL for the whole parse,
    so a multi-MB ``tool_rounds`` blob decoded inside the sync SSE fallback
    generators (``gen_done`` / ``gen_persisted``) stalls the event loop just
    as an on-loop encode would — those generators run each ``next()`` in the
    executor via Quart's ``run_sync_iterable``, but the GIL is still held for
    the whole call so the loop thread is starved regardless (the same trap
    documented for ``to_thread(json.dumps)``).

    ``orjson.loads`` parses the same blob several times faster and releases
    the GIL far sooner, dropping the stall below the danger threshold. It
    accepts ``str`` or ``bytes``. On the rare input orjson rejects we fall
    back to stdlib ``json.loads`` so behaviour is never worse than before.
    """
    try:
        return orjson.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning('[Chat] orjson snapshot parse failed (%s); '
                       'falling back to stdlib json.loads', e)
        return json.loads(raw)
# v1 blueprint for the JSON routes (the carve-out /api/chat/stream/<id> stays on chat_bp).
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402


@api_v1_chat_bp.route('/api/v1/chat/active', methods=['GET'], endpoint='ui_chat_active')
@require_scope('chat')
def chat_active():
    """List in-memory tasks (id, conv, status, abort flag).

    Used by the frontend on reload to decide whether to resume polling
    a task it knew about before navigation. Cleans up stale finished
    tasks as a side effect.
    """
    cleanup_old_tasks()
    with tasks_lock:
        # ``tasks`` is a process-global registry mutated by many code paths
        # (the orchestrator, external/eval harnesses, tests). A malformed
        # entry missing optional keys must not 500 this status endpoint, so
        # read every field defensively.
        #
        # ★ Exclude non-streaming CARRIER/HOLDER tasks (``_inline_messages`` /
        #   ``_vu_subtask``): the autopilot VU + reporter sub-turns and the
        #   summarize carrier use ``create_task`` purely as a message container
        #   and never stream a ``done`` event. If the frontend sees one here it
        #   runs orphan-recovery (initActiveTasks Case C / cross-tab reconcile),
        #   pushes an empty assistant placeholder, and connects an SSE that
        #   never completes → a permanently-stuck "Waiting…" bubble. Reconnect
        #   only ever makes sense for real UI-streaming tasks, so hide carriers.
        #   (The autopilot-kick carrier sets ``_autopilot_kick``, NOT these
        #   flags, so it is still reported and reconnectable.)
        result = [{'id': t.get('id', ''), 'convId': t.get('convId', ''),
                   'status': t.get('status', ''),
                   'aborted': bool(t.get('aborted'))}
                  for t in tasks.values()
                  if not t.get('_inline_messages') and not t.get('_vu_subtask')]
    return jsonify(result)


@api_v1_chat_bp.route('/api/v1/chat/start', methods=['POST'], endpoint='ui_chat_start')
@require_scope('chat')
@idempotent_post()
def chat_start():
    """Start a chat task. Body is ``{convId, config[, messages]}``.

    Default flow: load messages from the DB via ``build_api_messages_from_db``,
    then dispatch to the built-in orchestrator. External callers (SWE-bench,
    eval harnesses) may pass ``messages`` inline, which sets ``_inline_messages``
    so :func:`_sync_result_to_conversation` skips DB write-back.

    Returns ``{taskId}``; the client polls ``/api/v1/chat/poll/<taskId>``.
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    cfg = data.get('config', {})

    # ── Server-side message building ──
    # The frontend now sends {convId, config} only.
    # Messages are loaded from the DB and transformed server-side.
    # Legacy / external-caller path: if the POST body ships 'messages' inline
    # (SWE-bench harness, eval tools, external backends), use them as-is and
    # skip the DB-backed conversation write-back later — see `_inline_messages`
    # flag below which is consumed by _sync_result_to_conversation().
    messages = data.get('messages')
    inline_messages = bool(messages)
    if not messages:
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        exclude_last = cfg.get('excludeLast', False)
        messages = build_api_messages_from_db(conv_id, cfg, exclude_last=exclude_last)
        if messages is None:
            return api_not_found('Conversation not found')
        if not messages:
            return api_bad_request('No messages')
        logger.info('[Chat] Built %d API messages from DB for conv %s',
                    len(messages), conv_id[:8])

    cleanup_old_tasks()

    # ★ Supersede (abort any stale running task for this conv) is now an
    #   invariant of create_task(supersede=True, the default) — no explicit
    #   pre-abort needed here. The ordering-critical explicit sweeps in
    #   _start_task_for_conv (before build_api_messages_from_db) and
    #   chat_abort_conv remain, because those must run BEFORE their DB read.
    task = create_task(conv_id, messages, cfg)
    # Human-attended UI task: a user is watching and can answer the write-
    # approval prompt, so Manual mode actually gates here (see
    # execute_tool_pipeline's attendance-aware default).
    task['_attended'] = True
    # Tag tasks that were started with inline messages (no DB-backed
    # conversation row). These tasks skip _sync_result_to_conversation()
    # entirely — external callers read results from task_results directly.
    if inline_messages:
        task['_inline_messages'] = True

    # ── Admission control (backpressure) ──
    # Cap concurrent in-flight tasks so a UI spawn storm can't exhaust the
    # agent-worker pool — the SAME ceiling the headless paths enforce
    # (controller.try_acquire → 503 + retry_after when full). The slot is
    # released on the task's terminal event via on_terminal (fires once from
    # the orchestrator worker thread), so a UI spawn can never permanently
    # consume capacity. Registered BEFORE spawn so no terminal signal is
    # missed.
    from lib.agent_core.admission import controller as _admission, on_terminal
    if not _admission.try_acquire():
        _stats = _admission.stats()
        logger.warning('[Chat] /chat/start refused for conv=%s — at inflight '
                       'capacity (%d/%d)', conv_id[:8],
                       _stats['in_flight'], _stats['capacity'])
        return jsonify({
            'ok': False,
            'error': {'kind': 'capacity',
                      'detail': 'Server is at task capacity. Retry shortly.',
                      'retry_after_s': 3},
        }), 503
    on_terminal(task['id'], lambda _tid: _admission.release())

    from lib.tasks_pkg import spawn_task
    _cfg_model = cfg.get('model', '?')
    _cfg_preset = cfg.get('preset', cfg.get('effort', '?'))
    logger.info('[Chat] Starting task %s for conv %s model=%s preset=%s',
                task['id'], task['convId'], _cfg_model, _cfg_preset)
    try:
        spawn_task(task)
    except Exception as _spawn_err:
        logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                         task['id'], task['convId'])
        # spawn failed → no terminal event will fire, so the on_terminal
        # release above never runs. Release the admission slot here to avoid
        # permanently leaking capacity on a spawn error.
        _admission.release()
        from lib.error_envelope import make_envelope as _make_env
        task['status'] = 'error'
        task['error'] = _make_env(
            'internal',
            detail='Server failed to start task thread.',
            model=cfg.get('model', ''),
            context='chat-start',
            source='routes.chat',
            raw=str(_spawn_err),
        )
        return api_internal_error('Failed to start task')

    return jsonify({'taskId': task['id']})


# ══════════════════════════════════════════════════════════
#  Atomic send: user message creation + task start
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#  Per-conv abort marker for in-flight /api/chat/send.
#
#  When the user clicks Stop while a /api/chat/send is still inside the
#  synchronous auto-translate (and no task has been started yet), the
#  frontend hits /api/chat/abort-conv but there is nothing to abort —
#  the send handler will keep blocking, finish translating, then either
#  start a task or enqueue the message. We record the abort wall-clock
#  here so the send handler can detect 'this conv was aborted AFTER my
#  request started' and bail out before persisting / enqueueing /
#  dispatching anything.
# ══════════════════════════════════════════════════════════
_send_abort_marker = {}               # conv_id -> abort_wall_clock_seconds
_send_abort_marker_lock = threading.Lock()


def _mark_conv_aborted(conv_id):
    """Record that this conv was aborted at wall clock ``time.time()``."""
    if not conv_id:
        return
    with _send_abort_marker_lock:
        _send_abort_marker[conv_id] = time.time()


def _was_aborted_after(conv_id, since_ts):
    """Return True if /api/chat/abort-conv ran for this conv after ``since_ts``."""
    if not conv_id or since_ts is None:
        return False
    with _send_abort_marker_lock:
        ts = _send_abort_marker.get(conv_id)
    return ts is not None and ts >= since_ts


@api_v1_chat_bp.route('/api/v1/chat/translate-status/<conv_id>', methods=['GET'], endpoint='ui_chat_send_translate_status')
@require_scope('chat')
def chat_send_translate_status(conv_id):
    """Return the current send-path translate retry status for a conv.

    Returns ``{statusMessage, statusKind, updatedAt}`` or ``{}`` if no
    translate is currently in flight (or hasn't yet hit its first retry).
    """
    return jsonify(get_send_translate_status(conv_id) or {})


def _truncate_conv_history(conv_id):
    """Discharge every server-side obligation that follows truncating a conv.

    Any route that rewrites a conversation's history to a shorter prefix
    (regenerate, edit-and-resend) MUST clear the two in-memory side-channels
    that outlive the DB write, or the next task replays stale state:

      * the message QUEUE — a previous /api/chat/send aborted mid-translate
        may have left an enqueued message; without clearing it the queue
        auto-dispatches a phantom turn after this run completes;
      * the server-side tool-history STORE
        (lib/tasks_pkg/server_message_store) — a full-fidelity in-memory copy
        of the prior turns' tool_use/tool_result rounds, keyed by conv_id and
        driven by keepToolHistory (default ON). On the next task the
        orchestrator's rebuild_messages_with_history REPLACES the DB-built
        (now-truncated) messages with that stored copy, which still holds the
        rounds we just truncated away — so every regen/edit would replay an
        ever-growing stale context instead of the truncated one. Clearing it
        forces a clean rebuild from the truncated DB state; the preserved
        turns' tool history is reconstructed from their stored toolRounds by
        conv_message_builder, so no real context is lost.

    Folding both into one helper makes the invariant impossible to
    half-apply: a future truncating route calls this once instead of
    re-deriving (and forgetting one of) the two clears. Best-effort — each
    failure is logged, never raised.

    Args:
        conv_id: The conversation whose history was just truncated.
    """
    try:
        from lib.message_queue import clear_queue
        _cleared = clear_queue(conv_id)
        if _cleared:
            logger.info('[Regen] conv=%s cleared %d stale queued message(s) before regen',
                        conv_id[:8], _cleared)
    except Exception as e:
        logger.warning('[Regen] Failed to clear queue for conv=%s: %s', conv_id[:8], e)

    try:
        from lib.tasks_pkg.server_message_store import clear as _clear_msg_store
        _clear_msg_store(conv_id)
    except Exception as e:
        logger.warning('[Regen] Failed to clear message store for conv=%s: %s', conv_id[:8], e)


def _start_task_for_conv(conv_id, config, data=None):
    """Build API messages from DB and start a task. Returns (taskId, error_response).

    Automatically routes to endpoint mode (planner → worker → critic loop)
    when ``config['endpointMode']`` is truthy, so callers (chat_send,
    chat_regenerate, etc.) don't need separate routing logic.

    ★ CRITICAL: Before starting a new task, all existing running tasks for
    this conversation are auto-aborted. This prevents the "stale task
    overwrites regeneration" bug where an old task's _sync_result_to_conversation
    races with the new task and corrupts the conversation DB.
    """
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    from lib.tasks_pkg import abort_running_tasks_for_conv

    # ★ CRITICAL: abort any stale running tasks for this conversation BEFORE
    #   building the new API messages. Without this, the old task's background
    #   thread may still be running (abort is cooperative) and its persist/sync
    #   writes can land BETWEEN the regen truncation and our DB read here —
    #   resurrecting the just-truncated assistant turn (the "U1 A1 U1 A2"
    #   doubled-context bug). Aborting first stamps `_abort_reason` so the
    #   freshness guard in _sync_result_to_conversation rejects those late
    #   writes; building messages afterwards reads a settled DB state.
    _aborted_count = abort_running_tasks_for_conv(conv_id)
    if _aborted_count:
        logger.info('[Chat] conv=%s Auto-aborted %d stale task(s) before new task',
                    conv_id[:8], _aborted_count)

    cleanup_old_tasks()

    # ``excludeLast`` is honored so /api/chat/continue can rebuild messages
    # without the assistant message that is about to be regenerated.
    _exclude_last = bool(config.get('excludeLast', False))
    api_messages = build_api_messages_from_db(conv_id, config, exclude_last=_exclude_last)
    if api_messages is None:
        return None, (jsonify({'error': 'Conversation not found after save'}), 500)
    if not api_messages:
        return None, (jsonify({'error': 'No messages to process'}), 400)

    task = create_task(conv_id, api_messages, config)
    task['_attended'] = True
    task_id = task['id']
    _cfg_model = config.get('model', '?')

    # ★ A user-SELECTED orchestration flow (Mode dropdown) is mutually
    #   exclusive with the endpoint/autopilot toggles — the flow IS the
    #   execution mode. Drop the toggles so we never double-loop, and so the
    #   resolver's flow-wins precedence isn't masked by a stale endpoint flag.
    _flow_selected = bool(config.get('flowDefinition') or config.get('flowBuiltin')
                          or config.get('flowId'))
    if _flow_selected and (config.get('endpointMode') or config.get('autopilot')):
        logger.info('[Chat] conv=%s endpointMode/autopilot dropped — '
                    'an orchestration flow is selected (flow takes precedence)',
                    conv_id[:8])
        config = dict(config)
        config['endpointMode'] = False
        config['autopilot'] = False
        task['config'] = config

    # ★ Endpoint mode: route to the autonomous planner → worker → critic loop
    is_endpoint = config.get('endpointMode', False)

    # ★ Autopilot is mutually exclusive with endpoint mode — both share the
    #   same "model stopped → loop again" boundary, so running them together
    #   would produce a double-loop with confusing semantics.  Endpoint
    #   wins; the autopilot flag is silently dropped.
    if is_endpoint and config.get('autopilot'):
        logger.warning('[Chat] conv=%s autopilot=True dropped — '
                       'endpointMode=True takes precedence',
                       conv_id[:8])
        config = dict(config)
        config['autopilot'] = False
        task['config'] = config

    # ★ FlowExecutor dispatch (the orchestration-engine convergence point):
    #   a user-SELECTED flow (flowDefinition / flowBuiltin / flowId) is always
    #   honored; endpointMode / autopilot route through the engine only when
    #   their respective flags are on (TOFU_ENDPOINT_VIA_FLOW /
    #   TOFU_AUTOPILOT_VIA_FLOW). Returns None when no engine path applies, so
    #   endpoint mode falls back to the live loop and everything else to a
    #   normal task. All flagging/precedence lives in resolve_chat_flow_entry.
    from lib.orchestration_endpoint_runner import resolve_chat_flow_entry
    _flow_entry = resolve_chat_flow_entry(config)

    if _flow_entry is not None or is_endpoint:
        # Endpoint mode without a flow entry → the live planner→worker→critic
        # loop (default + authoritative). Otherwise the chosen engine entry.
        if _flow_entry is None:
            from lib.tasks_pkg.endpoint import run_endpoint_task
            _flow_entry = run_endpoint_task
        task['endpoint_mode'] = True
        # Seed the phase that the FIRST SSE `state` snapshot will report. A
        # user-selected flow may open on a worker / verifier rather than a
        # planner; advertising 'planning' for a plannerless flow makes the
        # frontend stand up a Planner bubble that never streams (hangs at
        # "Waiting…"). Live endpoint mode (no flow def) keeps 'planning'.
        _initial_phase = 'planning'
        try:
            from lib.orchestration_endpoint_runner import resolve_chat_flow_definition
            _sel_defn, _ = resolve_chat_flow_definition(config)
            if _sel_defn is not None:
                from lib.orchestration import initial_phase_for_flow
                _initial_phase = initial_phase_for_flow(_sel_defn)
        except Exception as _phase_err:
            logger.debug('[Chat] initial-phase derivation failed, defaulting to '
                         'planning: %s', _phase_err)
        task['_endpoint_phase'] = _initial_phase
        task['_endpoint_iteration'] = 0
        logger.info('[Chat] Starting FLOW task %s for conv %s model=%s via=%s',
                    task_id[:8], conv_id[:8], _cfg_model, _flow_entry.__name__)
        try:
            threading.Thread(target=_flow_entry, args=(task,), daemon=True).start()
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start flow/endpoint thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start endpoint task thread.',
                model=config.get('model', ''),
                context='endpoint-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return None, (jsonify({'error': 'Failed to start task'}), 500)
    else:
        logger.info('[Chat] Starting task %s for conv %s model=%s',
                    task_id[:8], conv_id[:8], _cfg_model)
        try:
            from lib.tasks_pkg import spawn_task
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Chat] Failed to start thread for task %s conv=%s',
                             task_id[:8], conv_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start task thread.',
                model=config.get('model', ''),
                context='task-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return None, (jsonify({'error': 'Failed to start task'}), 500)

    return task_id, None


@api_v1_chat_bp.route('/api/v1/chat/send', methods=['POST'], endpoint='ui_chat_send')
@require_scope('chat')
@idempotent_post()
def chat_send():
    """Atomic send: create user message + auto-translate + persist + start task.

    If a task is already running for this conversation, the message is
    auto-translated, persisted to the user-visible conversation (so it
    appears instantly on the frontend), and enqueued to ``message_queue``
    for automatic dispatch when the current task finishes.  The frontend
    receives ``{queued: true}`` and renders a queue indicator — it never
    needs to decide whether to queue or send.

    Body: {
        convId: str,
        message: { text, images?, pdfTexts?, replyQuotes?, convRefs?, convRefTexts?, folderId? },
        config: { model, searchMode, ... all tool settings },
        settings?: { per-conv tool state to persist }
    }

    Returns on immediate start:
        { taskId, convId, title, userMessage, isNew, msgCount }
    Returns on queue:
        { queued: true, queueId, position, convId, title, userMessage, isNew, msgCount }
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    payload = data.get('message', {})
    config = data.get('config', {})
    settings_patch = data.get('settings')

    text = payload.get('text', '')
    if not text and not payload.get('images') and not payload.get('pdfTexts'):
        return api_bad_request('Empty message')

    # Snapshot the request start wall-clock BEFORE the synchronous
    # auto-translate call. If /api/chat/abort-conv runs while we are
    # blocked in translation, _was_aborted_after() will catch it.
    _send_started_at = time.time()

    try:
        db = get_thread_db(DOMAIN_CHAT)

        # 1. Load or create conversation
        messages, is_new, title = _load_or_create_conv(db, conv_id, config, payload)

        # 2. Build user message (with auto-translate).
        #    Pass conv_id so send-path retries surface via
        #    /api/chat/send-translate-status/<conv_id>.
        user_msg = _build_user_msg_from_payload(payload, config, conv_id=conv_id)

        # 2a. If the user clicked Stop while we were inside the auto-
        #     translate call, drop this message entirely — do NOT persist,
        #     enqueue, or dispatch. This prevents the 'translation finishes
        #     after abort → enqueue → fires after regen completes' double-
        #     send bug.
        if _was_aborted_after(conv_id, _send_started_at):
            logger.info('[Send] conv=%s ⚠️ Aborted during translate — dropping message '
                        '(translated=%s)',
                        conv_id[:8], bool(user_msg.get('originalContent')))
            return jsonify({
                'aborted': True,
                'convId': conv_id,
            })

        # 3. Compute title for first user message
        user_msgs = [m for m in messages if m.get('role') == 'user']
        if len(user_msgs) == 0 and text:
            title_text = re.sub(r'</?(?:notranslate|nt)>', '', text, flags=re.IGNORECASE)
            title = title_text[:60] + ('...' if len(title_text) > 60 else '')

        logger.info('[Send] conv=%s msgs=%d title=%.50s isNew=%s translated=%s',
                    conv_id[:8], len(messages), title, is_new,
                    bool(user_msg.get('originalContent')))

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
        #   ★ CRITICAL: exclude aborted tasks — when the user clicks Stop
        #   and immediately sends a new message, the old task may still
        #   have status='running' (abort is cooperative) but should NOT
        #   cause the new message to be enqueued.
        #   ★ Classify the running tasks: a genuine (normal) worker turn must
        #   still make the human wait, but an INVISIBLE autopilot follow-up
        #   turn (a VU-spawned background turn carrying ``_autopilotParent`` /
        #   ``_vu_subtask``) must NOT — the human once sat "QUEUED" for minutes
        #   behind a background autopilot reply. So: if the ONLY running tasks
        #   are autopilot follow-ups (and autopilot is armed), supersede them.
        #   Keyed on the background MARKER, not the bare ``config.autopilot``
        #   flag — the armed PRIMARY turn the user is watching carries the flag
        #   but no marker, and must be queued behind, never aborted.
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

        # ★ Inject-mode: the composer's per-conversation toggle. Two lanes when
        #   a task is already running for this conversation:
        #     • 'queue' (default) — enqueue for dispatch as a FRESH turn after
        #       the current reply ends (persistent, survives reload).
        #     • 'steer' — inject into the CURRENTLY-RUNNING turn at its next
        #       clean round boundary (after any open tool_result closes), so the
        #       human can course-correct mid-generation. Delivered via the
        #       model-facing agent_inbox under mode='user-steer'.
        #   Steer has an exactly-once fallback: if the running task is NOT
        #   drainable (its inbox slot is tombstoned — it is finalizing and will
        #   run no further round-boundary drain), we DO NOT enqueue into the
        #   inbox (it would be silently dropped) — we fall back to the durable
        #   message_queue so the steer becomes the next turn instead. Never zero.
        # injectMode is a PER-SEND decision from the post-send dialog
        # (_promptInjectMode), carried at the top level of the request body — it
        # is NOT a persisted conversation setting. Read `data` FIRST: reading
        # `config` first would be shadowed by resolve_conv_config's 'queue'
        # default (truthy), so a 'steer' choice could never win.
        _inject_mode = (data.get('injectMode') or '').strip().lower()
        if has_running_task and _inject_mode == 'steer':
            from lib.agent_inbox import has_pending as _inbox_has_pending  # noqa: F401
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
                _notify_conv_changed(conv_id, rev=None)
                return jsonify({
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

            _notify_conv_changed(conv_id, rev=_pending_rev)

            return jsonify({
                'queued': True,
                'queueId': queue_result['queueId'],
                'position': queue_result['position'],
                'convId': conv_id,
                'title': title,
                'userMessage': user_msg,
                'isNew': is_new,
                'msgCount': len(messages),  # excludes the queued user msg
            })

        # 4. Append user message and persist (only for immediate start).
        #    Idempotent: if a racing sync already planted the optimistic copy
        #    as the tail (matching timestamp), reconcile in place instead of
        #    appending a duplicate. This is the root-cause guard — the
        #    frontend _sendInFlight flag merely avoids triggering the race.
        _append_user_msg_idempotent(messages, user_msg)
        _send_rev = _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # 5. Start task (no active task — send immediately)
        task_id, err_resp = _start_task_for_conv(conv_id, config, data)
        if err_resp is not None:
            if isinstance(err_resp, tuple):
                return err_resp
            return err_resp

        # 6. Update activeTaskId in settings.
        #    ★ Settings-ONLY write (not a full-row _persist_conv_messages):
        #    the only new information here is activeTaskId. Rewriting the whole
        #    `messages` array with this route's stale user-only tail (and
        #    bumping updated_at) races the task thread, which may have ALREADY
        #    checkpointed the assistant slot via _sync_partial_to_conversation —
        #    clobbering the streamed content back to the pre-start snapshot
        #    ("Waiting…" on reload). set_conversation_settings does a per-conv
        #    serialized merge of just this key and never touches messages.
        try:
            from lib.conversations import set_conversation_settings
            # notify=False: each of these paths emits its own
            # _notify_conv_changed below, so the gate must invalidate the local
            # cache (structural guarantee) WITHOUT a second cross-device push.
            set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                      db=db, notify=False)
        except Exception as e:
            logger.warning('[Send] Failed to update activeTaskId: %s', e)

        # ★ Carry the REAL post-write rev (not None): the user message was just
        #   persisted (step 4) and the rev trigger bumped it. A rev-bearing
        #   frame makes a sibling device's rev-gate refetch the body — so the
        #   just-sent user message appears on the other device immediately,
        #   instead of a rev=None frame that only nudges the sidebar and leaves
        #   the message invisible until the assistant reply lands.
        _notify_conv_changed(conv_id, rev=_send_rev)

        return jsonify({
            'taskId': task_id,
            'convId': conv_id,
            'title': title,
            'userMessage': user_msg,
            'isNew': is_new,
            'msgCount': len(messages),
        })

    except Exception as e:
        logger.error('[Send] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')



@api_v1_chat_bp.route('/api/v1/chat/branch', methods=['POST'], endpoint='ui_chat_branch_start')
@require_scope('chat')
def chat_branch_start():
    """Start a branch task with server-side message building.

    The backend loads the conversation from DB, extracts the main chat context
    up to the branch anchor point, appends the branch's own messages (decorated
    with topic + selection context), and runs the full message transform pipeline.
    This eliminates the frontend ``_buildBranchApiMessages`` → ``buildApiMessages``
    code path that could diverge from the backend builder.

    Body: {
        convId: str,
        msgIdx: int,           // index of the parent message the branch is attached to
        branchIdx: int,        // index of the branch within parent.branches[]
        config: { model, searchMode, branchKey, ... }
    }

    Returns: { taskId }
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    msg_idx = data.get('msgIdx')
    branch_idx = data.get('branchIdx')
    if msg_idx is None or branch_idx is None:
        return api_bad_request('msgIdx and branchIdx required')

    cfg = data.get('config', {})

    try:
        from lib.tasks_pkg.conv_message_builder import build_branch_api_messages

        api_messages = build_branch_api_messages(conv_id, msg_idx, branch_idx, cfg)
        if api_messages is None:
            return api_not_found('Branch not found')
        if not api_messages:
            return api_bad_request('No messages to process')

        cleanup_old_tasks()

        # ★ supersede=False: a branch is a DELIBERATE concurrency axis — it must
        #   run alongside the main task and sibling branches, so it must NOT
        #   abort other running tasks for this conv (see create_task docstring).
        task = create_task(conv_id, api_messages, cfg, supersede=False)
        task['_attended'] = True
        task_id = task['id']
        _cfg_model = cfg.get('model', '?')
        logger.info('[Branch] Starting task %s for conv %s msg=%d branch=%d model=%s',
                    task_id[:8], conv_id[:8], msg_idx, branch_idx, _cfg_model)

        from lib.tasks_pkg import spawn_task
        try:
            spawn_task(task)
        except Exception as _spawn_err:
            logger.exception('[Branch] Failed to start thread for task %s', task_id[:8])
            from lib.error_envelope import make_envelope as _make_env
            task['status'] = 'error'
            task['error'] = _make_env(
                'internal',
                detail='Server failed to start branch task thread.',
                model=cfg.get('model', ''),
                context='branch-start',
                source='routes.chat',
                raw=str(_spawn_err),
            )
            return api_internal_error('Failed to start task')

        return jsonify({'taskId': task_id})

    except Exception as e:
        logger.error('[Branch] Failed for conv=%s msg=%d branch=%d: %s',
                     conv_id[:8], msg_idx, branch_idx, e, exc_info=True)
        return api_internal_error('internal_error')


@api_v1_chat_bp.route('/api/v1/chat/regenerate', methods=['POST'], endpoint='ui_chat_regenerate')
@require_scope('chat')
def chat_regenerate():
    """Atomic regenerate/edit: truncate messages + optional edit + auto-translate + start task.

    Body: {
        convId: str,
        truncateToIndex: int,         // keep messages[0..truncateToIndex] inclusive
        editedContent?: str,          // if provided, replace the message at truncateToIndex
        editedImages?: [],            // replacement images (optional)
        editedPdfTexts?: [],          // replacement pdfTexts (optional)
        config: { model, searchMode, ... },
        settings?: { per-conv tool state to persist }
    }

    Returns: { taskId, convId, title, msgCount, userMessage? }
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    truncate_to = data.get('truncateToIndex')
    if truncate_to is None:
        return api_bad_request('truncateToIndex required')

    config = data.get('config', {})
    edited_content = data.get('editedContent')
    edited_images = data.get('editedImages')
    edited_pdf_texts = data.get('editedPdfTexts')
    settings_patch = data.get('settings')

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, title FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()

        if not row:
            return api_not_found('Conversation not found')

        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Regen] Failed to parse messages for conv=%s: %s', conv_id[:8], e)
            return api_internal_error('Failed to parse conversation')

        title = row['title']

        # ── Phase 3: msgId is authoritative; index is the fallback ──
        # The client sends ``truncateToMsgId`` (the stable id of the user
        # message to keep-and-resend-from) alongside the legacy
        # ``truncateToIndex``. If the id resolves in the freshly-loaded
        # messages, its CURRENT index wins — this is index-drift-proof if any
        # writer reordered messages between the client's read and this request.
        # When the id is absent (older client) or doesn't resolve (message
        # since deleted), we fall back to the supplied index unchanged, so the
        # behaviour is strictly additive.
        _truncate_msg_id = data.get('truncateToMsgId')
        if _truncate_msg_id:
            from lib.tasks_pkg.manager import find_message_by_id
            _resolved_idx, _ = find_message_by_id(messages, _truncate_msg_id)
            if _resolved_idx is not None:
                if _resolved_idx != truncate_to:
                    logger.info('[Regen] conv=%s truncateToMsgId resolved to index '
                                '%d (client sent index %d — drift corrected)',
                                conv_id[:8], _resolved_idx, truncate_to)
                truncate_to = _resolved_idx
            else:
                logger.debug('[Regen] conv=%s truncateToMsgId=%s did not resolve '
                             '— using index %d', conv_id[:8],
                             str(_truncate_msg_id)[:12], truncate_to)

        if truncate_to < 0 or truncate_to >= len(messages):
            return api_bad_request(f'truncateToIndex {truncate_to} out of range (0..{len(messages)-1})')

        # 1. Truncate
        messages = messages[:truncate_to + 1]

        # 2. Apply edit if provided
        user_msg = messages[truncate_to]
        if edited_content is not None:
            user_msg['content'] = edited_content
            user_msg.pop('originalContent', None)
            user_msg['timestamp'] = int(time.time() * 1000)
        if edited_images is not None:
            user_msg['images'] = edited_images
        if edited_pdf_texts is not None:
            user_msg['pdfTexts'] = edited_pdf_texts
        # Refresh the per-turn context snapshot when the client sends a fresh
        # one (edit-and-resend re-runs with possibly new model/tools). A plain
        # regenerate omits ``ctx`` so the original snapshot is preserved.
        # See static/js/info-rail.js.
        if data.get('ctx'):
            user_msg['_ctx'] = data['ctx']

        # 3. Auto-translate if needed
        text = user_msg.get('content', '')
        from lib.conv_config import resolve_auto_translate
        auto_translate = resolve_auto_translate(config)
        # Track whether we mutated user_msg so the response tells the frontend
        # to refresh its local copy (otherwise a later full-conv sync would PUT
        # the stale pre-restore content back into the DB).
        restored_original = False

        # ── Autopilot (virtual-user) / endpoint-critic messages ──
        # These are role='user' but DISPLAY-translated: `content` is the
        # model-language original (shown in the 原文 toggle) and
        # `translatedContent` is the UI-language rendering shown in the OUTER
        # bubble. This is the OPPOSITE wiring of a normal user message.
        # An edit changes `content`, which invalidates the cached
        # `translatedContent` — drop it (+ `_translatedCache`) so the outer
        # bubble re-renders the edited `content` instead of the stale 译文
        # (the reported "edit only shows in the toggle" bug). They must NOT go
        # through the normal user→English auto-translate path below, which
        # would set `originalContent` and corrupt the VU/critic structure into
        # a double-translated mess.
        _is_vu_critic = bool(user_msg.get('_isVirtualUser') or user_msg.get('_isEndpointReview'))
        if _is_vu_critic and edited_content is not None:
            for _k in ('translatedContent', '_translatedCache', '_translateDone',
                       '_translateModel', '_translateField', '_translateError'):
                user_msg.pop(_k, None)
            restored_original = True  # tell the client to refresh its local copy
            logger.info('[Regen] conv=%s VU/critic edit — cleared stale display '
                        'translation (translatedContent) before regenerate',
                        conv_id[:8])

        if _is_vu_critic:
            # Skip the normal user-message translate paths entirely for VU /
            # critic messages — their `content` is fed to the model as-is.
            auto_translate = False
        if not auto_translate and text and user_msg.get('originalContent'):
            # Auto-translate is OFF, but this message carries a translation from
            # an earlier auto-translate-ON turn: content=English (sent to the
            # model), originalContent=the user's original (what they see). A
            # plain regenerate sends no editedContent, so without this the model
            # would silently receive the stale English instead of the original.
            # Restore the original and drop the translation metadata so the run
            # honours the current (OFF) setting and matches the on-screen text.
            # (Edit-and-resend already pops originalContent in step 2, so this
            # only triggers for the no-edit regenerate path.)
            user_msg['content'] = user_msg['originalContent']
            user_msg.pop('originalContent', None)
            user_msg.pop('_translateDone', None)
            user_msg.pop('_translateModel', None)
            user_msg.pop('_translateFailed', None)
            text = user_msg['content']
            restored_original = True
            logger.info('[Regen] conv=%s auto-translate OFF — restored original '
                        'text (dropped stale translation) before regenerate',
                        conv_id[:8])
        if auto_translate and text:
            # Translate ANY non-English input to English (English is the
            # model's strongest language). The actual "is this already
            # English / what is the source language" decision lives inside
            # _auto_translate_user (it honours config.translateSourceLang and
            # falls back to a heuristic only when the source is unknown), so
            # we don't pre-gate on a Latin-script check here — that would
            # wrongly skip German/Spanish/etc. which are also Latin-script.
            # If the message already has originalContent and _translateDone, skip re-translation
            # (user didn't edit the text, just regenerating)
            already_translated = (user_msg.get('originalContent')
                                  and user_msg.get('_translateDone')
                                  and edited_content is None)
            if not already_translated:
                translated, original, model, fail_reason = _auto_translate_user(
                    text, config, conv_id=conv_id)
                if original:
                    user_msg['content'] = translated
                    user_msg['originalContent'] = original
                    user_msg['_translateDone'] = True
                    user_msg.pop('_translateFailed', None)
                    if model:
                        user_msg['_translateModel'] = model
                elif fail_reason:
                    # Translation attempted but failed/timed out — original text
                    # was sent. Flag it so the frontend shows a non-silent notice.
                    user_msg['_translateFailed'] = fail_reason

        # 4. Update title if this is the only user message
        user_msgs = [m for m in messages if m.get('role') == 'user']
        if len(user_msgs) == 1 and text:
            original_text = user_msg.get('originalContent') or text
            title_text = re.sub(r'</?(?:notranslate|nt)>', '', original_text, flags=re.IGNORECASE)
            title = title_text[:60] + ('...' if len(title_text) > 60 else '')

        # 5. Persist truncated messages to DB
        _persist_conv_messages(db, conv_id, messages, title, settings_patch)

        # 5a+5b. Discharge the post-truncation obligations (clear the message
        #        queue + the in-memory server_message_store) via the single
        #        helper so the invariant can't be half-applied. See
        #        _truncate_conv_history for the full rationale.
        _truncate_conv_history(conv_id)

        logger.info('[Regen] conv=%s truncated to idx=%d msgs=%d edited=%s title=%.50s',
                    conv_id[:8], truncate_to, len(messages),
                    edited_content is not None, title)

        # 6. Start task
        task_id, err_resp = _start_task_for_conv(conv_id, config, data)
        if err_resp is not None:
            if isinstance(err_resp, tuple):
                return err_resp
            return err_resp

        # 7. Update activeTaskId (settings-only — see the note in chat_send:
        #    a full-row rewrite here would clobber a task-thread checkpoint).
        try:
            from lib.conversations import set_conversation_settings
            # notify=False: each of these paths emits its own
            # _notify_conv_changed below, so the gate must invalidate the local
            # cache (structural guarantee) WITHOUT a second cross-device push.
            set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                      db=db, notify=False)
        except Exception as e:
            logger.warning('[Regen] Failed to update activeTaskId: %s', e)

        _notify_conv_changed(conv_id, rev=None)

        return jsonify({
            'taskId': task_id,
            'convId': conv_id,
            'title': title,
            'msgCount': len(messages),
            'userMessage': (user_msg if (edited_content is not None
                                         or restored_original) else None),
            # Tells the frontend to DROP its stale local translation fields
            # (originalContent / _translateDone / …). Object.assign-merging
            # the returned user_msg alone wouldn't remove keys that the server
            # dropped, leaving a ghost bilingual block + a re-PUT risk.
            'restoredOriginal': restored_original,
        })

    except Exception as e:
        logger.error('[Regen] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


# ══════════════════════════════════════════════════════════
#  Continue: checkpoint-based resumption of an assistant turn
#  (_build_tool_history_round + _scan_continue_checkpoint moved to
#   lib/chat/turn_builder.py; re-exported at the top of this module)
# ══════════════════════════════════════════════════════════


def _continue_via_prefill_only(db, conv_id, messages, assistant_msg, title,
                               config, settings_patch, resume_prefill,
                               orig_full_content, data):
    """Case 3 — resume a NO-TOOL mid-answer turn via assistant prefill.

    There is no tool-call checkpoint (``scan is None``), so the classic
    Continue path would fall back to regenerate-from-scratch. But the provider
    tolerates a trailing assistant prefill and the turn ended mid-answer, so we
    feed the model its own half-written string and let it continue the SAME
    tokens instead of restarting.

    Unlike the tool-checkpoint rollback (which DISCARDS the mid-prose tail and
    regenerates), prefill mode CONTINUES the same string — nothing is discarded.
    So the message content stays as the full prior answer (the base the
    continuation extends); there is NO ``priorContent`` block. The streaming
    checkpoint grows ``content`` as continuation tokens arrive, so a mid-stream
    reload shows [full prior answer] + [partial continuation]. Persist BEFORE
    starting so a streaming checkpoint can't race the rollback.
    """
    # Keep the full prior answer as the continuation base (content is already
    # orig_full_content; assert it explicitly for clarity). Clear the live
    # thinking tail (prefill carries content only — the reasoning trace can't
    # be replayed on the wire) but stash it display-only, mirroring case 2.
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
    # Seed task['content'] with the full pre-rollback answer so the resumed
    # turn displays everything the user already saw plus the continuation.
    cfg_payload['contentPrefix'] = orig_full_content

    task_id, err_resp = _start_task_for_conv(conv_id, cfg_payload, data)
    if err_resp is not None:
        return err_resp if not isinstance(err_resp, tuple) else err_resp

    try:
        from lib.conversations import set_conversation_settings
        # notify=False: _notify_conv_changed is emitted below (no double push).
        set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                  db=db, notify=False)
    except Exception as e:
        logger.warning('[Continue] Failed to update activeTaskId (prefill-only): %s', e)

    _notify_conv_changed(conv_id, rev=None)
    try:
        from lib.log import audit_log as _audit_log
        _audit_log('continue_prefill_only', conv_id=conv_id,
                   prefillChars=len(resume_prefill),
                   origContentChars=len(orig_full_content))
    except Exception as e:
        logger.debug('[Continue] audit_log (prefill-only) failed (non-fatal): %s', e)

    return jsonify({
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
            # Prefill CONTINUES the same string — nothing discarded, so the
            # full prior answer is the continuation base and there is NO
            # priorContent block. The reasoning tail is display-only.
            'contentPrefix': orig_full_content,
            'priorContent': '',
            'priorThinking': assistant_msg.get('priorThinking') or '',
        },
    })


@api_v1_chat_bp.route('/api/v1/chat/continue', methods=['POST'], endpoint='ui_chat_continue')
@require_scope('chat')
def chat_continue():
    """Atomic continue: roll back the last assistant message to its last
    complete tool-call checkpoint, persist the rolled-back state to DB,
    then start a new task that resumes from that checkpoint.

    Body: {
        convId: str,
        config: { model, ... },
        settings?: { per-conv tool state to persist }
    }

    Returns on success:
        { taskId, convId, checkpoint: {
            keptRounds, discardedRounds,
            preservedContentLen, discardedContentLen,
            preservedThinkingChars, discardedThinking,
        }}

    If no recoverable checkpoint is found (no complete tool rounds), returns
    ``{fallback: "regenerate"}`` and the frontend should pop-and-resend.
    """
    data = parse_body()
    conv_id = data.get('convId', '')
    if not conv_id:
        return api_bad_request('convId required')

    config = data.get('config') or {}
    settings_patch = data.get('settings')

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, title FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()

        if not row:
            return api_not_found('Conversation not found')

        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Continue] Failed to parse messages for conv=%s: %s',
                           conv_id[:8], e)
            return api_internal_error('Failed to parse conversation')

        title = row['title']

        if not messages:
            return api_bad_request('Conversation has no messages')
        if messages[-1].get('role') != 'assistant':
            return api_bad_request('Last message is not an assistant message')

        assistant_msg = messages[-1]
        # Trivial case: empty content & thinking → no checkpoint needed; ask
        # the frontend to fall back to pop-and-resend (full regeneration).
        if not assistant_msg.get('content') and not assistant_msg.get('thinking') \
                and not (assistant_msg.get('toolRounds') or []):
            logger.info('[Continue] conv=%s last assistant is empty — fallback to regenerate',
                        conv_id[:8])
            return jsonify({'fallback': 'regenerate', 'reason': 'empty_assistant'})

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
                assistant_msg.get('segments'), _model, finish_reason=_finish_reason)
        except Exception as _rp_e:
            logger.debug('[Continue] resume-prefill extraction failed (non-fatal): %s', _rp_e)

        scan = _scan_continue_checkpoint(assistant_msg)
        if scan is None:
            # No tool-call checkpoint. If we DO have a resumable prefill (a
            # no-tool mid-answer turn — case 3), resume via prefill alone rather
            # than regenerating from scratch. Otherwise fall back to regenerate.
            if _resume_prefill:
                logger.info('[Continue] conv=%s no tool checkpoint but resumable '
                            'prefill (%d chars) — resuming via assistant prefill (case 3)',
                            conv_id[:8], len(_resume_prefill))
                return _continue_via_prefill_only(
                    db, conv_id, messages, assistant_msg, title, config,
                    settings_patch, _resume_prefill, _orig_full_content, data)
            logger.info('[Continue] conv=%s no tool-call checkpoint available — fallback to regenerate',
                        conv_id[:8])
            return jsonify({'fallback': 'regenerate', 'reason': 'no_checkpoint'})

        # ── Apply rollback in place on the assistant message ──
        preserved_content = scan['preserved_content']
        assistant_msg['toolRounds'] = scan['kept_rounds']
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
        if scan.get('discarded_content_text'):
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
        task_id, err_resp = _start_task_for_conv(conv_id, cfg_payload, data)
        if err_resp is not None:
            return err_resp if not isinstance(err_resp, tuple) else err_resp

        # Persist activeTaskId (settings-only — same rationale as chat_send:
        #    a full-row rewrite would clobber a task-thread checkpoint).
        try:
            from lib.conversations import set_conversation_settings
            # notify=False: each of these paths emits its own
            # _notify_conv_changed below, so the gate must invalidate the local
            # cache (structural guarantee) WITHOUT a second cross-device push.
            set_conversation_settings(conv_id, {'activeTaskId': task_id},
                                      db=db, notify=False)
        except Exception as e:
            logger.warning('[Continue] Failed to update activeTaskId: %s', e)

        _notify_conv_changed(conv_id, rev=None)
        try:
            from lib.log import audit_log as _audit_log
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

        return jsonify({
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
                'resumeMode': 'checkpoint',
                'contentPrefix': preserved_content,
                'priorContent': scan.get('discarded_content_text') or '',
                'priorThinking': scan.get('discarded_thinking_text') or '',
            },
        })

    except Exception as e:
        logger.error('[Continue] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return api_internal_error('internal_error')


# ══════════════════════════════════════════════════════════
#  Tool-state sync endpoint
#  → moved to routes/chat_tool_state.py (kept on the same chat_bp)
# ══════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════
#  Server-side message queue endpoints
#  → moved to routes/chat_queue.py (kept on the same chat_bp)
# ══════════════════════════════════════════════════════════


def _warm_resume_serviceable(resume_cursor, n_events):
    """Decide whether a warm (in-memory) Last-Event-ID resume is serviceable.

    Returns True iff ``resume_cursor`` names a position the in-memory event
    buffer can actually replay from — i.e. the next event to send
    (``resume_cursor + 1``) is at or before the current buffer length.

    When False the caller MUST fall back to a full state-snapshot
    (a "resync"), exactly as the cold path does, instead of slicing
    ``events[resume_from:]`` into an empty list. An empty slice on an
    ahead-of-buffer cursor used to leave the warm stream sending nothing
    until the next live event (a silent stall) and mis-index the live loop.
    A cursor that is plausibly behind the buffer (``>= -1``, in range) stays
    serviceable; only an out-of-range-ahead cursor forces resync.

    ``resume_cursor`` is the SSE ``Last-Event-ID`` (id of the last RECEIVED
    event); ``-1``/``0`` etc. are normal early cursors. The boundary case
    ``resume_from == n_events`` IS serviceable (empty replay, then live
    streaming continues from exactly that index).
    """
    if resume_cursor is None or resume_cursor < 0:
        return False  # no/invalid cursor → fresh snapshot (caller's else-branch)
    resume_from = resume_cursor + 1
    return resume_from <= n_events


@chat_bp.route('/api/chat/stream/<task_id>', methods=['GET'])
async def chat_stream(task_id):
    import asyncio

    # ── Per-principal concurrent-SSE cap (backpressure) ──
    # A single process serves every LIVE SSE stream as a long-lived
    # connection; with no per-principal ceiling one client/IP can open
    # unbounded streams and exhaust the process. We acquire a slot keyed by
    # the request principal (user_id → key_id → client IP) ONLY for the live
    # streaming path (generate_with_disconnect_log) — the transient
    # DB-snapshot / cold-replay generators below finish in microseconds and
    # are the reconnect/replay safety valve, so they are intentionally NOT
    # counted against the cap. The live path acquires just before opening the
    # stream and releases in its finally, so a dropped/aborted/errored stream
    # can never leak a slot.
    from lib.agent_core.principal import principal_key
    from lib.agent_core.sse_limit import limiter as _sse_limiter
    _sse_principal = principal_key(current_auth())

    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        # ★ Cold-path resumption: if the client provides Last-Event-ID and we
        #   still have persisted events for this task, replay from the table.
        #   This makes Last-Event-ID resumption durable across cleanup_old_tasks
        #   and server restart — without it, the stale reader would receive
        #   only a synthetic `state`+`done` snapshot and lose every intermediate
        #   tool/phase event the user saw before the disconnect.
        _replay_cursor_hdr = request.headers.get('Last-Event-ID', '').strip()
        if _replay_cursor_hdr:
            try:
                _replay_cursor = int(_replay_cursor_hdr)
            except (ValueError, TypeError) as _e_audit:
                logger.debug('[chat] chat_stream caught %s: %s', type(_e_audit).__name__, _e_audit)
                _replay_cursor = None
            if _replay_cursor is not None and _replay_cursor >= 0:
                from lib.tasks_pkg.event_log import read_events as _read_events
                _persisted = await asyncio.to_thread(
                    _read_events, task_id, since_event_id=_replay_cursor)
                if _persisted:
                    logger.info('[Chat] Stream %s cold replay from event_log: %d event(s) since id=%d',
                                task_id[:8], len(_persisted), _replay_cursor)

                    def gen_persisted():
                        for _ in range(4):
                            yield ':' + ' ' * 2048 + '\n\n'
                        for ev in _persisted:
                            eid = ev['event_id']
                            payload = ev['payload']
                            yield f'id: {eid}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                            if isinstance(payload, dict) and payload.get('type') == 'done':
                                return
                        # No persisted 'done' — synthesize state+done from task_results.
                        # We MUST emit a 'state' event before 'done' here: a
                        # client whose Last-Event-ID points past the end of
                        # the persisted log (e.g. TTL prune ran, or the
                        # client's last cursor was very recent) would
                        # otherwise see only metadata and lose all text.
                        # Mirrors the warm-fallback shape further down.
                        try:
                            db_local = get_db(DOMAIN_CHAT)
                            row_local = db_local.execute(
                                'SELECT conv_id,content,thinking,error,status,tool_rounds,metadata '
                                'FROM task_results WHERE task_id=?',
                                (task_id,)
                            ).fetchone()
                            if row_local:
                                # ★ Close the 5s cold-replay window: fold the
                                #   lossless per-delta task_events log instead of
                                #   trusting the (up to 5s stale) task_results
                                #   checkpoint. The fold reconstructs the EXACT
                                #   text the client saw; on an empty/failed log
                                #   it returns the checkpoint pair unchanged.
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
                                        # Field list MUST mirror _extract_task_meta /
                                        # _extract_db_meta / chat_poll's DB-path loop.
                                        # See _extract_task_meta docstring for why.
                                        for k in ('finishReason', 'usage', 'preset', 'toolSummary',
                                                  'model', 'provider_id', 'thinkingDepth',
                                                  'apiRounds', 'modifiedFiles', 'modifiedFileList',
                                                  'fallbackModel', 'fallbackFrom',
                                                  'fallbackReason', 'fallbackKind'):
                                            if m.get(k):
                                                done_evt_local[k] = m[k]
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[chat] gen_persisted caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                if row_local['error']:
                                    from lib.error_envelope import from_json as _err_from_json
                                    done_evt_local['error'] = _err_from_json(row_local['error'])
                            yield f'data: {_dumps_yielding(done_evt_local)}\n\n'
                        except Exception as _e:
                            logger.debug('[Chat] cold-replay synthetic done failed: %s', _e)

                    return sse_response(gen_persisted())

        db = get_db(DOMAIN_CHAT)
        row = await asyncio.to_thread(
            lambda: db.execute(
                'SELECT conv_id,content,thinking,error,status,tool_rounds,metadata FROM task_results WHERE task_id=?',
                (task_id,)
            ).fetchone())
        if row:
            # ★ Close the 5s cold-replay window (see gen_persisted above):
            #   fold the lossless per-delta task_events log; falls back to the
            #   checkpoint pair on an empty/failed log.
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
                    logger.warning('[Chat] Failed to parse tool_rounds for task %s: %s', task_id, e, exc_info=True)
            else:
                from lib.tasks_pkg import load_tool_rounds_from_conversation
                _tr = await asyncio.to_thread(load_tool_rounds_from_conversation, row['conv_id'])
                if _tr:
                    state['toolRounds'] = _tr
            meta = _extract_db_meta(row)
            # Field lists MUST stay aligned with _extract_task_meta and
            # the chat_poll DB-path loop. See _extract_task_meta docstring.
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
        logger.warning('[Chat] Task %s not found (stream)', task_id)
        return api_not_found('Task not found')

    # ★ SSE reader dedup: supersede any previous SSE reader for this task.
    #   When a client reconnects (proxy timeout, page switch), the old reader
    #   should detect it's been replaced and exit, freeing the thread.
    _sse_gen = task.get('_sse_gen_id', 0) + 1
    task['_sse_gen_id'] = _sse_gen

    # ★ Item 6: Last-Event-ID reconnection — if the client provides a cursor,
    #   skip the full state snapshot and resume from that event index.
    _last_event_id = request.headers.get('Last-Event-ID', '').strip()
    _resume_cursor = None
    if _last_event_id:
        try:
            _resume_cursor = int(_last_event_id)
            logger.info('[Chat] SSE stream %s reconnecting with Last-Event-ID=%d',
                        task_id[:8], _resume_cursor)
        except (ValueError, TypeError):
            logger.debug('[Chat] SSE stream %s ignoring invalid Last-Event-ID: %s',
                         task_id[:8], _last_event_id)

    _stream_start = time.time()
    _events_sent = 0
    def _task_terminal():
        """True iff the task is finished AND not mid-autopilot-decision.

        ``task['status']`` flips to 'done' (orchestrator _run_loop tail)
        BEFORE the autopilot end-of-turn hook runs its multi-second VU LLM
        call; the baton-carrying ``done`` event is only appended AFTER.
        Synthesizing a late ``done`` during that window closes the SSE
        stream without the ``autopilotNextTaskId``/``autopilotVuMessage``
        handoff, stranding the already-spawned follow-up — the conv goes
        idle (sidebar dot off, pause→send, translation fires) until a
        manual refresh.  Treat the decision window as still-running so the
        loop keeps the stream open until the real done event arrives.
        """
        return task['status'] != 'running' and not task.get('_autopilot_deciding')

    def _apply_autopilot_baton(evt):
        """Stamp the autopilot follow-up baton onto a SYNTHESIZED done event.

        The orchestrator's REAL done event carries
        ``autopilotNextTaskId``/``autopilotVuMessage`` directly, but every
        late/synthetic done built here uses ``extract_task_meta()`` which does
        NOT include them.  If a synthetic done is ever sent for an autopilot
        turn (cold replay, resume, or a residual status-flip race), copy the
        baton from the transport-agnostic stash so the frontend still attaches
        to the spawned follow-up instead of stranding it.  Mirrors the
        ``chat_poll`` baton surfacing below.
        """
        _ap = task.get('_autopilot_followup')
        if _ap:
            evt['autopilotNextTaskId'] = _ap['next_task_id']
            evt['autopilotVuMessage'] = _ap['vu_msg']
        return evt

    async def generate():
        nonlocal _events_sent
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'

        with task['events_lock']:
            # ★ If resuming via Last-Event-ID, skip the state snapshot and
            #   replay only events AFTER the cursor. Per the SSE spec,
            #   Last-Event-ID is the id of the last *received* event, so
            #   we resume from cursor + 1 to avoid re-sending it.
            # ★ Resync guard: if the cursor is AHEAD of the in-memory buffer
            #   (e.g. the buffer was trimmed, or a stale/over-eager client),
            #   _warm_resume_serviceable() is False → fall through to a full
            #   state snapshot instead of slicing an empty list (which would
            #   silently stall the stream and mis-index the live loop).
            if _warm_resume_serviceable(_resume_cursor, len(task['events'])):
                resume_from = _resume_cursor + 1
                missed_evts = task['events'][resume_from:]
            else:
                if _resume_cursor is not None and _resume_cursor >= 0:
                    logger.info('[Chat] SSE stream %s Last-Event-ID=%d is ahead of '
                                'buffer (len=%d) — full-snapshot resync',
                                task_id[:8], _resume_cursor, len(task['events']))
                missed_evts = None
                resume_from = None
                cursor = len(task['events'])

        if resume_from is not None:
            cursor = resume_from
            # ★ Reassert a leading full-state snapshot BEFORE replaying the
            #   post-cursor deltas. A warm resume that landed on a fresh empty
            #   assistant placeholder (initActiveTasks Case-A stale-tail /
            #   connectToTask stale-turn guard → toolRounds:[]) has NO cached
            #   rounds, so a delta-only replay would render starting at whatever
            #   round the first missed tool_start carried (the round-10 strand,
            #   conv mrbf9px2g5mct3). Emit the COMPLETE task['toolRounds']
            #   (+ content/thinking) read under the lock; the frontend's
            #   _snapshotLongerRounds keep-longer guard ADOPTS it when the client
            #   cache is short and harmlessly IGNORES it when equal/longer — so a
            #   shorter buffer can never collapse a longer one. Like the fresh
            #   path it carries NO id: (synthetic; avoids a cursor collision with
            #   the first replayed event).
            with task['events_lock']:
                resume_state = build_event(
                    EventType.STATE, content=task['content'],
                    thinking=task['thinking'], status=task['status'],
                )
                # ★ Server-authoritative task start (ms) — seed the elapsed timer
                #   from the real start on a Last-Event-ID resume too.
                _created = task.get('created_at')
                if _created:
                    resume_state['createdAt'] = int(_created * 1000)
                if task['error']:
                    resume_state['error'] = task['error']
                resume_state['toolRounds'] = task['toolRounds']
            _resume_state_payload = json.dumps(resume_state, ensure_ascii=False)
            yield f'data: {_resume_state_payload}\n\n'
            _events_sent += 1
            # Resume path: replay missed events since Last-Event-ID
            for idx, ev in enumerate(missed_evts):
                eid = resume_from + idx
                yield f'id: {eid}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n'
                _events_sent += 1
                if ev.get('type') == 'done':
                    return
            # Advance cursor past replayed events for live streaming loop
            cursor = resume_from + len(missed_evts)
            if _task_terminal() and not missed_evts:
                late_done = build_event(EventType.DONE)
                late_meta = _extract_task_meta(task)
                late_done.update(late_meta)
                if task['error']:
                    late_done['error'] = task['error']
                _apply_autopilot_baton(late_done)
                yield f'id: {cursor}\ndata: {json.dumps(late_done, ensure_ascii=False)}\n\n'
                return
        else:
            # Fresh connection path: send full state snapshot
            with task['events_lock']:
                state = build_event(
                    EventType.STATE, content=task['content'],
                    thinking=task['thinking'], status=task['status'],
                )
                # ★ Server-authoritative task start (ms) so a reconnecting client
                #   seeds its elapsed timer from the REAL start, not from 0.
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
                # ★ Endpoint mode: include phase and completed turns for reconnection
                if task.get('endpoint_mode'):
                    state['endpointMode'] = True
                    state['endpointPhase'] = task.get('_endpoint_phase', 'planning')
                    state['endpointIteration'] = task.get('_endpoint_iteration', 0)
                    ep_turns = task.get('_endpoint_turns')
                    if ep_turns:
                        state['endpointTurns'] = ep_turns
                    # ★ Authoritative finished signal — when task has completed
                    #   (_finalize set _endpoint_phase='done'), propagate the
                    #   stop reason so the frontend's reconnect paths never
                    #   create a ghost worker after Critic STOP approval.
                    if task.get('_endpoint_stop_reason'):
                        state['endpointStopReason'] = task['_endpoint_stop_reason']
                cursor = len(task['events'])

            # ★ State snapshot gets NO id: field — it's synthetic, not a real
            #   event from the events array. Only real events (deltas, phases,
            #   done) get id: fields. This prevents the id collision between
            #   the state snapshot and the first live event at the same cursor.
            #   If the client only received the state snapshot and reconnects,
            #   _lastEventId will be null → fresh connection with full state.
            # ★ Robustness: the snapshot serializes the ENTIRE conversation
            #   content + thinking + toolRounds. For very large conversations
            #   this json.dumps is multi-millisecond CPU work; running it
            #   directly on the event-loop thread stalls accept()/all other
            #   connections (a single big conv could make the whole page go
            #   dark). Offload to the executor via _dumps_yielding (orjson —
            #   plain json.dumps holds the GIL for the whole call so to_thread
            #   alone does NOT free the loop). Live deltas below stay inline →
            #   streaming latency unchanged.
            _state_payload = await asyncio.to_thread(_dumps_yielding, state)
            yield f'data: {_state_payload}\n\n'

            if _task_terminal():
                done_evt = build_event(EventType.DONE)
                done_evt.update(meta)
                if task['error']:
                    done_evt['error'] = task['error']
                _apply_autopilot_baton(done_evt)
                yield f'id: {cursor}\ndata: {json.dumps(done_evt, ensure_ascii=False)}\n\n'
                return

        _MAX_SSE_DURATION = 7200  # 2 hours — absolute max SSE stream lifetime
        last_t = time.time()
        while True:
            # ── Guard: absolute SSE stream duration limit ──
            _elapsed = time.time() - _stream_start
            if _elapsed > _MAX_SSE_DURATION:
                _conv_id = task.get('convId', '?')
                logger.warning('[Chat] SSE stream %s conv=%s closing after %.0fs (max %ds) — '
                               'task still running (status=%s), %d events sent so far. '
                               'Frontend will switch to polling to pick up the result.',
                               task_id[:8], _conv_id, _elapsed, _MAX_SSE_DURATION,
                               task.get('status', '?'), _events_sent)
                # ★ DO NOT abort the backend task — it's still doing useful work.
                # Send an informational event (NOT 'done') so the frontend shows a toast,
                # then close the SSE stream. The frontend detects the stream closed
                # without a 'done' event → _trySSE returns false → _pollFallback kicks in.
                timeout_notice = build_event(
                    EventType.SSE_TIMEOUT,
                    message='SSE connection reached maximum duration. Switching to polling — task is still running.')
                yield f'data: {json.dumps(timeout_notice, ensure_ascii=False)}\n\n'
                return

            with task['events_lock']:
                new_evts = task['events'][cursor:]
                _cursor_before = cursor
                cursor = len(task['events'])
            for idx, ev in enumerate(new_evts):
                eid = _cursor_before + idx
                yield f'id: {eid}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n'
                _events_sent += 1
                last_t = time.time()
                if ev.get('type') == 'done':
                    _done_fr = ev.get('finishReason', '?')
                    _done_err = ev.get('error')
                    _done_err_summary = (
                        _done_err.get('kind') if isinstance(_done_err, dict)
                        else (_done_err or 'none')
                    )
                    logger.info('[Chat] SSE stream %s finished normally — %d events sent in %.1fs '
                               'finishReason=%s error=%s',
                               task_id[:8], _events_sent, time.time() - _stream_start,
                               _done_fr, _done_err_summary)
                    return
            if _task_terminal() and not new_evts:
                late_done = build_event(EventType.DONE)
                late_meta = _extract_task_meta(task)
                late_done.update(late_meta)
                if task['error']:
                    late_done['error'] = task['error']
                # ★ Severity split: an aborted/interrupted/normally-stopped
                #   task that needs a LATE done synthesis is expected:
                #   - aborted/interrupted: user hit Stop after orchestrator
                #     already flushed its own done event.
                #   - stop: task finished normally between the queue poll and
                #     this status check (the common 157-line/day pattern).
                #   Anything else (length, content_filter, error, etc.) still
                #   signals a missed done-event — keep as warning.
                _late_fr = late_meta.get('finishReason', '?')
                _is_benign = _late_fr in ('aborted', 'interrupted', 'stop')
                _log_fn = logger.info if _is_benign else logger.warning
                _err_obj = task['error']
                _err_summary = (
                    _err_obj.get('detail') or _err_obj.get('message') or _err_obj.get('kind')
                    if isinstance(_err_obj, dict) else (_err_obj or 'none')
                )
                _log_fn('[Chat] SSE stream %s emitting LATE done '
                        '(task finished but no done event in queue) — '
                        'finishReason=%s model=%s error=%s',
                        task_id[:8], _late_fr,
                        late_meta.get('model', '?'), _err_summary)
                _apply_autopilot_baton(late_done)
                yield f'id: {cursor}\ndata: {json.dumps(late_done, ensure_ascii=False)}\n\n'
                return
            # ★ SSE reader dedup: if a newer SSE reader connected, exit this one
            if task.get('_sse_gen_id', _sse_gen) != _sse_gen:
                logger.info('[Chat] SSE stream %s superseded by newer reader (gen %d→%d) — '
                           'closing stale reader after %d events in %.1fs',
                           task_id[:8], _sse_gen, task.get('_sse_gen_id', -1),
                           _events_sent, time.time() - _stream_start)
                return
            if time.time() - last_t > 15:
                yield ': keepalive\n\n'
                last_t = time.time()
                # Heartbeat: re-arm the per-principal SSE slot lease so a
                # living long stream never expires; the lease TTL only
                # reclaims a slot whose owner crashed (design §5.2).
                _sse_limiter.refresh(_sse_token)
            await asyncio.sleep(0.05)

    async def generate_with_disconnect_log():
        """Wrap generate() to detect client disconnect (SSE premature close)."""
        done_sent = False
        try:
            async for chunk in generate():
                if '"type"' in chunk and ('"type": "done"' in chunk or '"type":"done"' in chunk):
                    done_sent = True
                yield chunk
        except (GeneratorExit, asyncio.CancelledError):
            logger.debug('[Chat] SSE stream closed by client (GeneratorExit)', exc_info=True)
        finally:
            # Release the per-principal SSE slot FIRST — a dropped / aborted /
            # errored stream must free its slot before anything else can raise.
            _sse_limiter.release(_sse_token)
            elapsed = time.time() - _stream_start
            content_len = len(task.get('content') or '')
            _fr = task.get('finishReason') or '?'
            _model = task.get('model') or '?'
            _provider = task.get('provider_id') or '?'
            _err = task.get('error')
            if not done_sent:
                # Severity-aware: zero-events = real problem (SSE opened
                # but nothing delivered); events>0 = normal client-side
                # tab-close / network-retry — client poll fallback will
                # pick up the rest.
                if _events_sent == 0:
                    logger.warning('[Chat] SSE stream %s DISCONNECTED PREMATURELY — '
                                 '%d events sent in %.1fs, task status=%s, content=%dchars, '
                                 'finishReason=%s model=%s provider=%s error=%s. '
                                 'Client may lose data if poll fallback fails!',
                                 task_id[:8], _events_sent, elapsed,
                                 task.get('status', '?'), content_len,
                                 _fr, _model, _provider, _err or 'none')
                else:
                    logger.info('[Chat] SSE stream %s DISCONNECTED PREMATURELY — '
                               '%d events sent in %.1fs, task status=%s, content=%dchars, '
                               'finishReason=%s model=%s provider=%s error=%s. '
                               'Client may lose data if poll fallback fails!',
                               task_id[:8], _events_sent, elapsed,
                               task.get('status', '?'), content_len,
                               _fr, _model, _provider, _err or 'none')
            else:
                logger.info('[Chat] SSE stream %s closed after done — %d events, %.1fs, %dchars, '
                           'finishReason=%s model=%s provider=%s',
                           task_id[:8], _events_sent, elapsed, content_len,
                           _fr, _model, _provider)

    _sse_token = _sse_limiter.try_acquire(_sse_principal)
    if _sse_token is None:
        _active = _sse_limiter.active(_sse_principal)
        logger.warning('[Chat] SSE stream refused for principal=%s task=%s — '
                       'at concurrent-stream cap (%d active, cap=%d)',
                       _sse_principal, task_id[:8], _active, _sse_limiter.cap)
        resp = jsonify({
            'ok': False,
            'error': {'kind': 'rate_limited',
                      'detail': 'Too many concurrent streams for this '
                                'principal. Close an existing stream or '
                                'retry shortly.',
                      'retry_after_s': 5},
        })
        resp.headers['Retry-After'] = '5'
        return resp, 429

    return sse_response(generate_with_disconnect_log(), timeout_none=True)


@api_v1_chat_bp.route('/api/v1/chat/abort-conv/<conv_id>', methods=['POST'], endpoint='ui_chat_abort_conv')
@require_scope('chat')
def chat_abort_conv(conv_id):
    """Abort all running tasks for a conversation by conv ID.

    Used when the frontend aborts during translation and never received a
    taskId — the server may have already started a task that needs to be
    killed.  This is the convId-based counterpart of ``/api/chat/abort/<task_id>``.

    Also records a per-conv abort marker so any /api/chat/send still
    blocked inside auto-translate can detect the abort and bail out
    before persisting / enqueueing / dispatching the message.
    """
    from lib.tasks_pkg import abort_running_tasks_for_conv
    _mark_conv_aborted(conv_id)
    aborted = abort_running_tasks_for_conv(conv_id)
    if aborted:
        logger.info('[Chat] Abort-by-conv conv=%s — aborted %d task(s)', conv_id[:8], aborted)
    else:
        logger.debug('[Chat] Abort-by-conv conv=%s — no running tasks found', conv_id[:8])
    return api_ok({'aborted': aborted})
@api_v1_chat_bp.route('/api/v1/chat/abort/<task_id>', methods=['POST'], endpoint='ui_chat_abort')
@require_scope('chat')
def chat_abort(task_id):
    """Abort a running task by id.

    Sets ``task['aborted']`` (the orchestrator polls this between rounds),
    SIGTERMs any spawned ``run_command`` subprocess, and signals the external
    backend if one is in use. Idempotent — a duplicate abort logs at WARNING
    and returns ok.

    This is the single, authoritative abort handler — it carries the real
    subprocess / external-backend kill logic. The previous duplicate stub in
    ``routes/api_v1/chat.py`` (which only flipped ``aborted``) was removed.
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return api_not_found('Not found')
    was_already_aborted = task.get('aborted', False)
    task['aborted'] = True
    task['_abort_timestamp'] = time.time()
    audit_log('api_chat_abort',
              key_id=(current_auth().key_id if current_auth() else ''),
              task_id=task_id)
    # Log comprehensive abort context
    _status = task.get('status', '?')
    _elapsed = time.time() - task.get('created_at', time.time())
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _model = task.get('model', '?')
    _conv_id = task.get('convId', '?')
    if was_already_aborted:
        logger.warning('[Chat] Task %s abort DUPLICATE — already aborted. conv=%s status=%s',
                       task_id, _conv_id, _status)
    else:
        logger.info('[Chat] Task %s ABORT RECEIVED — conv=%s model=%s status=%s '
                    'elapsed=%.1fs content=%dchars thinking=%dchars',
                    task_id, _conv_id, _model, _status, _elapsed, _content_len, _thinking_len)
    # ── Kill any running subprocess (run_command) ──
    _sub_pid = task.get('_subprocess_pid')
    if _sub_pid:
        try:
            import os as _os
            import signal as _signal
            _pgid = task.get('_subprocess_pgid')
            if _pgid:
                _os.killpg(_pgid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess process group pgid=%d',
                            task_id[:8], _pgid)
            else:
                _os.kill(_sub_pid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess pid=%d',
                            task_id[:8], _sub_pid)
        except (OSError, ProcessLookupError) as e:
            logger.debug('[Chat] Task %s — subprocess kill skipped: %s', task_id[:8], e)

    return api_ok()
@api_v1_chat_bp.route('/api/v1/chat/poll/<task_id>', methods=['GET'], endpoint='ui_chat_poll')
@require_scope('chat')
def chat_poll(task_id):
    """Poll a task by id; returns the live in-memory task or its DB checkpoint.

    Memory hit returns the full live task dict; DB hit deserialises the
    persisted ``task_results`` row. Tasks whose DB row says ``status='running'``
    but which are absent from memory are reported as ``status='interrupted'``
    (server crashed mid-task) so the frontend stops polling and recovers the
    partial content.
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if task:
        content_len = len(task.get('content') or '')
        thinking_len = len(task.get('thinking') or '')
        finish_reason = task.get('finishReason') or '?'
        model = task.get('model') or '?'
        logger.debug('[Chat] Poll %s from memory — status=%s content=%dchars thinking=%dchars '
                     'finishReason=%s model=%s error=%s',
                     task_id[:8], task['status'], content_len, thinking_len,
                     finish_reason, model, bool(task.get('error')))
        if task['status'] == 'done' and content_len == 0 and thinking_len == 0 and not task.get('error'):
            logger.warning('[Chat] Poll %s ⚠️ RETURNING EMPTY RESULT — task is done but has no content or thinking! '
                          'finishReason=%s model=%s',
                          task_id[:8], finish_reason, model)
        # ★ While the autopilot end-of-turn hook is deciding (running the
        #   multi-second VU LLM call), the task is already status='done' but
        #   the follow-up baton isn't stamped yet.  Report 'running' so a poll
        #   in this window doesn't finalize the stream without the handoff.
        _reported_status = task['status']
        if task.get('_autopilot_deciding') and _reported_status == 'done':
            _reported_status = 'running'
        r = {
            'id': task['id'], 'status': _reported_status,
            'content': task['content'], 'thinking': task['thinking'],
        }
        # ★ Server-authoritative task start (ms). Lets the frontend seed its
        #   elapsed timer from the REAL start on a reconnect/refresh instead of
        #   restarting from 0 — see health_stream_timer.js::_seedStreamTimerStart.
        _created = task.get('created_at')
        if _created:
            r['createdAt'] = int(_created * 1000)
        # Field list MUST mirror chat_poll's DB-path loop and
        # _extract_task_meta. See _extract_task_meta docstring.
        for key in ('error', 'toolRounds', 'finishReason', 'usage', 'preset',
                     'toolSummary', 'phase', 'modifiedFiles', 'modifiedFileList',
                     'model', 'provider_id', 'thinkingDepth', 'apiRounds',
                     'compactionUsage'):
            if task.get(key):
                r[key] = task[key]
        if task.get('id'):
            r['taskId'] = task['id']
        if task.get('_fallback_model'):
            r['fallbackModel'] = task['_fallback_model']
        if task.get('_fallback_from'):
            r['fallbackFrom'] = task['_fallback_from']
        if task.get('_fallback_reason'):
            r['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            r['fallbackKind'] = task['_fallback_kind']
        # ★ Memory prefetch indicator (persists through poll fallback + reload)
        if task.get('_memoryPrefetch'):
            r['memoryPrefetch'] = task['_memoryPrefetch']
        # ★ Preferences-applied chip (persists through poll fallback + reload)
        if task.get('_preferencesApplied'):
            r['preferencesApplied'] = task['_preferencesApplied']
        # ★ Related-conversations chip (persists through poll fallback + reload)
        if task.get('_relatedConversations'):
            r['relatedConversations'] = task['_relatedConversations']
        # ★ Preferences-learned moment(s) (persist through poll + reload)
        if task.get('_preferencesLearned'):
            r['preferencesLearned'] = task['_preferencesLearned']
        # ★ Inbox-inject sidecars (swarm/peer/user-steer) — persist through poll
        #   fallback + reload so the in-timeline inject chips repaint.
        if task.get('_inboxInjects'):
            r['inboxInjects'] = task['_inboxInjects']
        if task.get('_peerInjects'):
            r['peerInjects'] = task['_peerInjects']
        if task.get('_userSteerInjects'):
            r['userSteerInjects'] = task['_userSteerInjects']
        # ★ Autopilot follow-up baton — mirror the SSE done event so a client
        #   on the poll fallback path attaches to the spawned follow-up task
        #   instead of stranding it (see lib/tasks_pkg/orchestrator.py).
        _ap_followup = task.get('_autopilot_followup')
        if _ap_followup:
            r['autopilotNextTaskId'] = _ap_followup['next_task_id']
            r['autopilotVuMessage'] = _ap_followup['vu_msg']
        # ★ Include endpoint turns for endpoint mode tasks so _pollFallback
        #   can reconstruct the full multi-turn conversation.  Also surface
        #   the same authoritative terminal signals the SSE state snapshot
        #   carries (endpointPhase / endpointStopReason / endpointIteration)
        #   so BOTH transports hand the frontend the identical baton — the
        #   poll path can then suppress ghost-worker creation after Critic
        #   STOP exactly like the SSE state handler does.
        if task.get('endpoint_mode'):
            r['endpointMode'] = True
            if task.get('_endpoint_turns'):
                r['endpointTurns'] = task['_endpoint_turns']
            r['endpointPhase'] = task.get('_endpoint_phase', 'planning')
            r['endpointIteration'] = task.get('_endpoint_iteration', 0)
            if task.get('_endpoint_stop_reason'):
                r['endpointStopReason'] = task['_endpoint_stop_reason']
        return jsonify(r)

    logger.debug('[Chat] Poll %s — not in memory, checking DB', task_id[:8])
    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT task_id,conv_id,content,thinking,error,status,tool_rounds,metadata FROM task_results WHERE task_id=?',
        (task_id,)
    ).fetchone()
    if row:
        _db_content_len = len(row['content'] or '')
        _db_thinking_len = len(row['thinking'] or '')
        _db_meta = _extract_db_meta(row)
        _db_finish = _db_meta.get('finishReason', '?')
        _db_model = _db_meta.get('model', '?')
        # ★ If the DB has status='running' but the task is NOT in memory,
        #   the server crashed/restarted mid-task. Mark it as 'interrupted'
        #   so the frontend stops polling and recovers the partial content.
        effective_status = row['status']
        _reconnect_hint = False
        if effective_status == 'running':
            # ★ Epic C (§4.1 / §6.4): a running checkpoint absent from THIS
            #   replica's memory means either (a) single-process crash, or
            #   (b) multi-replica — the task is alive on ANOTHER replica and
            #   the poll landed here via a stale/misrouted request. We do NOT
            #   run a cross-replica liveness probe (ratified §6.4). Instead:
            #   under the sharded (redis) backend, report status='running' +
            #   reconnect=True so the client re-routes to the owning replica via
            #   taskId affinity (NOT the false 'interrupted' that would strand a
            #   live task). Under the single-process (inproc) backend, absent
            #   genuinely means crashed → keep today's 'interrupted'
            #   crash-recovery behaviour byte-identical.
            _sharded = False
            try:
                from lib.env_compat import getenv_compat as _ge
                _sharded = (_ge('TOFU_RUNTIME_STATE_BACKEND') or 'inproc').strip().lower() == 'redis'
            except Exception as _e_be:
                logger.debug('[Chat] backend probe failed: %s', _e_be)
            _verdict_status, _reconnect_hint = _running_checkpoint_verdict(_sharded)
            effective_status = _verdict_status
            if _reconnect_hint:
                logger.info('[Chat] Poll %s — running checkpoint absent locally under sharded backend; '
                            'reporting running+reconnect (task likely on another replica — affinity re-route). '
                            '%dchars content, %dchars thinking.',
                            task_id[:8], _db_content_len, _db_thinking_len)
                # Do NOT flip the DB to interrupted — the task is (probably)
                # alive on its owning replica; flipping would corrupt its state.
            else:
                logger.warning('[Chat] Poll %s — found stale checkpoint (status=running) in DB but task is NOT in memory. '
                               'Server likely crashed mid-task. Returning status=interrupted with %dchars content, %dchars thinking.',
                               task_id[:8], _db_content_len, _db_thinking_len)
                # effective_status already 'interrupted' from the verdict.
                # ★ Update DB so future polls don't re-trigger this warning
                try:
                    db.execute("UPDATE task_results SET status='interrupted' WHERE task_id=?", (task_id,))
                    db.commit()
                except Exception as e:
                    logger.warning('[Chat] Failed to update stale task %s to interrupted: %s', task_id[:8], e)
                # ★ P0 observability: surface the activeTaskId ↔ msg _taskId
                #   desync behind an empty finish-bar (no persisted metadata).
                _log_poll_task_id_mismatch(db, row['conv_id'], task_id, _db_meta)
        else:
            logger.debug('[Chat] Poll %s from DB — status=%s content=%dchars thinking=%dchars '
                         'finishReason=%s model=%s error=%s',
                         task_id[:8], row['status'], _db_content_len, _db_thinking_len,
                         _db_finish, _db_model, bool(row['error']))
        # ★ Close the 5s cold-replay window on the POLL fallback too (the
        #   sse_poll_fallback.js path): the task is not in memory, so row
        #   content/thinking is the (up to 5s stale) task_results checkpoint.
        #   Fold the lossless per-delta task_events log — identical to the two
        #   SSE cold emits above — so a poll-fallback reconnect mid-stream sees
        #   the full buffer, not a short checkpoint. Falls back to the row pair
        #   on an empty/failed log.
        from lib.tasks_pkg.event_fold import fold_cold_state_text
        _poll_c, _poll_t = fold_cold_state_text(
            task_id, row['content'] or '', row['thinking'] or '')
        r = {
            'id': row['task_id'], 'status': effective_status,
            'content': _poll_c, 'thinking': _poll_t,
        }
        if _reconnect_hint:
            # Tell the client to re-open the stream (it will land on the
            # owning replica via taskId affinity) rather than treat this as a
            # terminal interrupted state.
            r['reconnect'] = True

        if row['error']:
            from lib.error_envelope import from_json as _err_from_json
            r['error'] = _err_from_json(row['error'])
        if row['tool_rounds']:
            try:
                r['toolRounds'] = _loads_yielding(row['tool_rounds'])
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.warning('[Chat] Failed to parse tool_rounds in poll for task %s: %s', task_id, e, exc_info=True)
        else:
            from lib.tasks_pkg import load_tool_rounds_from_conversation
            _tr = load_tool_rounds_from_conversation(row['conv_id'])
            if _tr:
                r['toolRounds'] = _tr
        # Field list MUST mirror chat_poll's in-memory loop and
        # _extract_task_meta. provider_id was previously dropped here
        # even though persist_task_result writes it into meta_json,
        # silently round-tripping through the DB.
        for key in ('finishReason', 'usage', 'preset', 'toolSummary',
                     'model', 'provider_id', 'thinkingDepth', 'apiRounds',
                     'modifiedFiles', 'modifiedFileList'):
            if _db_meta.get(key):
                r[key] = _db_meta[key]
        # ★ Endpoint mode: the in-memory task that held _endpoint_turns has
        #   been evicted (past TTL) or lost to a server restart, so reconstruct
        #   the multi-turn structure from the durable conversation messages
        #   (the authoritative store — endpoint.py syncs every turn there).
        #   Without this, a poll-fallback that outlives the in-memory task
        #   returns no endpointMode → the frontend overwrites the multi-turn
        #   endpoint render with the last single-turn content blob, a
        #   display-state desync that only a manual refresh repaired.  Covers
        #   BOTH done and interrupted statuses (the interrupted server-crash
        #   path is exactly when this reconstruction matters most).
        if _db_meta.get('endpointMode'):
            r['endpointMode'] = True
            from lib.tasks_pkg import load_endpoint_turns_from_conversation
            _ep_turns = load_endpoint_turns_from_conversation(row['conv_id'])
            if _ep_turns:
                r['endpointTurns'] = _ep_turns
            if _db_meta.get('endpointStopReason'):
                r['endpointStopReason'] = _db_meta['endpointStopReason']
        if _db_meta.get('fallbackModel'):
            r['fallbackModel'] = _db_meta['fallbackModel']
            r['fallbackFrom'] = _db_meta.get('fallbackFrom', '')
            if _db_meta.get('fallbackReason'):
                r['fallbackReason'] = _db_meta['fallbackReason']
            if _db_meta.get('fallbackKind'):
                r['fallbackKind'] = _db_meta['fallbackKind']
        return jsonify(r)

    logger.warning('[Chat] Poll %s — NOT FOUND in memory or DB! Task may have been cleaned up. '
                   'Client will receive 404 and may lose accumulated content.',
                   task_id[:8])
    return api_not_found('Task not found')


@api_v1_chat_bp.route('/api/v1/chat/flow-trace/<task_id>', methods=['GET'],
                      endpoint='ui_chat_flow_trace')
@require_scope('chat')
def chat_flow_trace(task_id):
    """Return the per-node run trace for an orchestration-flow chat task.

    The trace is the traceability record FlowExecutor accumulates: one entry
    per executed node carrying the RESOLVED delegation brief (the rendered
    role prompt — "what is this role doing?"), a bounded copy of its effective
    input context, its full bounded output, the message axis / isolation /
    loop iteration, and deliverable counts + timing. Powers the Studio
    canvas/inspector overlay.

    Served from the live in-memory task first (mid-run / just-finished), then
    the persisted ``task_results.metadata.flowTrace`` (survives reload /
    restart). Returns ``{ok, taskId, flowLabel, trace: [...]}``.
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if task is not None:
        return api_ok({
            'taskId': task_id,
            'flowLabel': task.get('_flow_label', ''),
            'trace': task.get('_flow_trace') or [],
        })

    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT metadata FROM task_results WHERE task_id=?', (task_id,)
    ).fetchone()
    if row and row['metadata']:
        try:
            meta = json.loads(row['metadata'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Chat] flow-trace %s — metadata parse failed: %s',
                           task_id[:8], e)
            meta = {}
        return api_ok({
            'taskId': task_id,
            'flowLabel': meta.get('flowLabel', ''),
            'trace': meta.get('flowTrace') or [],
        })
    return api_not_found('Task not found')


# ══════════════════════════════════════════════════════════
#  Stdin / human-guidance response endpoints
#  → moved to routes/chat_human_io.py (kept on the same chat_bp)
# ══════════════════════════════════════════════════════════
