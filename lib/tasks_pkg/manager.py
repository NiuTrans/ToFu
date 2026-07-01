"""Task lifecycle management — creation, events, persistence, cleanup, streaming.

★ Migrated to ``lib.task_runtime.TaskRuntime`` 2026-05-22 (last of the
five legacy registries). The module-level ``tasks`` / ``tasks_lock``
names remain exported because 47 import sites across routes/, lib/, and
tests/ reference them directly. They now alias the runtime's internal
storage. All custom behaviour (phase tracking, persistent event log,
freshness-guard `_conv_latest_task` index, content_lock, etc.) is
preserved on top of the runtime by augmenting the task dict after
``runtime.create()``.
"""

import json
import threading
import time
import uuid
from datetime import datetime

from lib.agent_core.events import EventType, build_event
from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
from lib.error_envelope import to_json as _err_to_json
from lib.llm_dispatch import dispatch_stream
from lib.log import get_logger
from lib.tasks_pkg.auto_translate import (  # noqa: F401  (re-export)
    _maybe_auto_translate_assistant,
    _maybe_auto_translate_critic,
)
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)

# Gateway/provider routing prefixes that are an internal dispatch detail, not
# something the user picked. Mirrors the canonical list in
# lib/llm_dispatch/discovery.py so the user-facing model name (e.g.
# "claude-opus-4.8") never leaks "aws.claude-opus-4.8" into the UI.
_GATEWAY_PREFIXES = ('aws.', 'vertex.', 'gcp.', 'azure.', 'bedrock.')


def _display_model_name(model: str) -> str:
    """Strip internal gateway/provider prefixes for a user-facing label."""
    name = model or 'the model'
    for prefix in _GATEWAY_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name


# ── Backing runtime ──────────────────────────────────────────────
# kind='chat'. push_channel='chat' (matches the existing /api/push routes
# and the frontend ``pushSubscribe('chat', taskId)`` consumer).
# ttl=3600 matches the legacy cleanup_old_tasks threshold.
_chat_runtime = TaskRuntime(
    'chat', ttl=3600,
    push_channel='chat',
    error_source='lib.tasks_pkg.manager',
)

# ── Module-level compatibility exports ─────────────────────────────
# Both names alias the runtime's internals so the 47 existing call sites
# (routes/chat.py, routes/endpoint.py, lib/agent_backends/builtin.py, etc.)
# continue to work without modification.
tasks = _chat_runtime._tasks  # type: ignore[attr-defined]
tasks_lock = _chat_runtime._lock  # type: ignore[attr-defined]

# ── Conversation → latest task_id mapping for freshness guard ──
# When a new task starts for a conv, the old task becomes stale and its
# _sync_result_to_conversation writes should be rejected.
_conv_latest_task = {}   # conv_id → task_id
_conv_latest_task_lock = threading.Lock()

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
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task_id
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


def _assign_message_ids(messages):
    """Ensure every message has a stable ``_msgId`` (UUID).

    Idempotent: messages that already have an id keep theirs.  Returns True
    if any id was newly assigned, so callers can decide whether to write back.

    Stable per-message IDs are the foundation for index-free addressing
    (translate, edit, regenerate, branches).  See docs/ARCHITECTURE.md
    \u00a76 \"Messages-as-Rows roadmap\" \u2014 this is the bridge from JSONB
    array to the per-message-row schema.
    """
    if not isinstance(messages, list):
        return False
    changed = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        if not m.get('_msgId'):
            m['_msgId'] = str(uuid.uuid4())
            changed = True
    return changed


def find_message_by_id(messages, msg_id):
    """Locate a message by ``_msgId``. Returns (idx, msg) or (None, None)."""
    if not msg_id or not isinstance(messages, list):
        return None, None
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get('_msgId') == msg_id:
            return i, m
    return None, None


def _strip_base64_for_snapshot(messages):
    """Strip large base64 data from messages for debug snapshot (keep structure, save bandwidth)."""
    stripped = []
    for msg in messages:
        m = dict(msg)
        content = m.get('content')
        if isinstance(content, list):
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'image_url':
                    url = block.get('image_url', {}).get('url', '')
                    size = len(url)
                    # Replace base64 data with placeholder showing size
                    new_blocks.append({'type': 'image_url', 'image_url': {'url': f'[base64 image, {size:,} chars]'}})
                else:
                    new_blocks.append(block)
            m['content'] = new_blocks
        elif isinstance(content, str) and len(content) > 100000:
            m['content'] = content[:1000] + f'\n... [{len(content):,} chars total]'
        # Strip tool call arguments that are too large (e.g. write_file content)
        if 'tool_calls' in m:
            new_tcs = []
            for tc in m['tool_calls']:
                tc2 = dict(tc)
                fn = tc2.get('function', {})
                args_str = fn.get('arguments', '')
                if isinstance(args_str, str) and len(args_str) > 50000:
                    fn2 = dict(fn)
                    fn2['arguments'] = args_str[:2000] + f'\n... [{len(args_str):,} chars total]'
                    tc2['function'] = fn2
                new_tcs.append(tc2)
            m['tool_calls'] = new_tcs
        stripped.append(m)
    return stripped

def append_event(task, event):
    """Append an event to the task's event log (chat-specific behaviour).

    Chat extends the runtime's plain append with:
      1. Phase tracking on task['phase'] (polling fallback consumer).
      2. Persistent event_log row for durable Last-Event-ID replay across
         cleanup_old_tasks + server restart.

    The runtime takes care of ``events`` append, the ``events_lock``, and
    pushing to the 'chat' WebSocket channel.

    ★ Sub-agent proxy tasks (lib/swarm/agent.py::_dispatch_tool) set
    ``_suppressEvents`` so their inner tool executions (which call
    ``_finalize_tool_round`` → ``append_event``) never leak ``tool_start`` /
    ``tool_result`` SSE events onto the PARENT's stream. Those events carry
    the sub-agent's own small roundNum and an empty toolCallId, so the
    frontend's roundNum fallback would graft them onto a same-numbered
    parent round (e.g. a run_command). The sub-agent's progress is surfaced
    separately via the master orchestrator's on_event callback
    (swarm_agent_* events), not through this path.
    """
    if task.get('_suppressEvents'):
        return
    seq = _chat_runtime.append_event(task['id'], event)
    if seq is None:
        # Task not in runtime (registered via legacy direct dict insert in
        # tests, etc.) — fall back to direct append for backward compat.
        # We MUST mint a seq ourselves before falling through to event_log
        # persistence below; otherwise append_persistent_event would receive
        # ``event_id=None`` and refuse the row, leaving cold replay with a
        # hole that looks (to the user) like the message disappeared.
        with task['events_lock']:
            seq = len(task['events'])
            event['seq'] = seq
            task['events'].append(event)
        try:
            from lib.push import push_event
            push_event('chat', task['id'], event)
        except Exception as e:
            logger.warning('[Task] push_event fallback failed task=%s: %s',
                           task['id'][:8], e)

    # ★ Track phase in task for polling fallback
    if event.get('type') == 'phase':
        p = {'phase': event['phase'], 'detail': event.get('detail', '')}
        if event.get('toolContext'): p['toolContext'] = event['toolContext']
        if event.get('tools'): p['tools'] = event['tools']
        if event.get('round'): p['round'] = event['round']
        task['phase'] = p
    elif event.get('type') == 'delta':
        task['phase'] = None  # Clear phase when LLM starts producing tokens

    # ★ Persist to task_events table for durable Last-Event-ID replay.
    #   This survives cleanup_old_tasks AND server restart, eliminating the
    #   "tool list disappeared after I came back" class of bugs.
    try:
        from lib.tasks_pkg.event_log import append_persistent_event, flush_pending
        append_persistent_event(task['id'], seq, event)
        if event.get('type') == 'done':
            flush_pending(task['id'])
    except Exception as e:
        logger.debug('[Manager] append_persistent_event failed (non-fatal): %s', e)

    # ★ Wake any async API handler awaiting this task (event-driven wait,
    #   replaces the old busy-poll loops). Every event nudges the waiter so
    #   SSE generators flush incrementally; terminal events additionally
    #   release the admission slot + fire BYO/tool-env disposal callbacks.
    try:
        from lib.agent_core.admission import notify_task
        _is_terminal = (event.get('type') in ('done', 'error', 'aborted')
                        or task.get('status') in ('done', 'error', 'aborted'))
        notify_task(task['id'], terminal=_is_terminal)
    except Exception as e:
        logger.debug('[Manager] admission notify failed task=%s: %s',
                     task['id'][:8], e)

def _tool_rounds_have_dedicated_home(task):
    """True when the task's toolRounds are durably stored in conversations.messages.

    Regular DB-backed chats persist toolRounds onto the last assistant message
    via _sync_result_to_conversation / _sync_partial_to_conversation; endpoint
    tasks persist them per-turn via _sync_endpoint_turns_to_conversation. For all
    of these, task_results.tool_rounds is a redundant duplicate of a potentially
    multi-MB blob — re-written on every ~10s checkpoint AND the final persist,
    every byte fsync-bound on the (often FUSE-mounted) PG data dir.

    Inline-message tasks (eval harness, /v1 + compat APIs, autopilot sub-tasks)
    have NO conversation row, so task_results is their sole store and the blob
    MUST be kept.
    """
    return bool(task.get('convId')) and not task.get('_inline_messages')


def load_tool_rounds_from_conversation(conv_id):
    """Return toolRounds from a conversation's last assistant message, or [].

    Recovery-path fallback for readers of a task_results row whose tool_rounds
    column was intentionally left NULL (see _tool_rounds_have_dedicated_home).
    Returns [] when the conversation is missing/unparseable or carries no
    assistant toolRounds.
    """
    if not conv_id:
        return []
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row or not row[0]:
            return []
        messages = json.loads(row[0])
        for m in reversed(messages):
            if m.get('role') == 'assistant' and m.get('toolRounds'):
                return m['toolRounds']
    except Exception as e:
        logger.warning('[Recovery] load_tool_rounds_from_conversation failed conv=%s: %s',
                       conv_id, e)
    return []


def load_endpoint_turns_from_conversation(conv_id):
    """Return the trailing endpoint turns from a conversation's messages, or [].

    Endpoint-mode results are persisted into the conversation's ``messages``
    array (by ``_sync_endpoint_turns_to_conversation`` in endpoint.py), NOT
    into the single ``task_results`` content blob.  When a poll outlives the
    in-memory task (evicted past TTL, or server restarted), the DB-path of
    ``/api/chat/poll`` no longer has ``task['_endpoint_turns']`` to echo, so
    the frontend can't rebuild the multi-turn structure and renders a single
    stale bubble until a manual refresh.

    This recovery reader reconstructs the same list from the durable
    conversation messages: it finds where the original (non-endpoint)
    conversation ends and returns everything after it — the planner, every
    worker iteration, and every critic review.  Mirrors the ``baseEnd`` slice
    the frontend (``_pollFallback`` / SSE state handler) computes, so the
    poll DB branch can hand back a byte-equivalent ``endpointTurns`` payload.

    Returns [] when the conversation is missing/unparseable or carries no
    endpoint turns.
    """
    if not conv_id:
        return []
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row or not row[0]:
            return []
        messages = json.loads(row[0])
        original_end = 0
        for i, m in enumerate(messages):
            if (not m.get('_epIteration')
                    and not m.get('_isEndpointReview')
                    and not m.get('_isEndpointPlanner')):
                original_end = i + 1
        return messages[original_end:]
    except Exception as e:
        logger.warning('[Recovery] load_endpoint_turns_from_conversation failed conv=%s: %s',
                       conv_id, e)
        return []


def build_result_meta(task):
    """Build the persisted-result metadata dict from a finished task.

    Extracted so the autopilot hook can sync the parent's final assistant
    message to the conversation DB BEFORE it appends the virtual-user turn
    and spawns the follow-up — otherwise the follow-up registers as the
    conversation's latest task and the later persist_task_result sync is
    dropped by the freshness guard, freezing the parent reply at its last
    streaming checkpoint (truncated, finishReason=None).
    """
    meta = {}
    if task.get('finishReason'): meta['finishReason'] = task['finishReason']
    if task.get('usage'): meta['usage'] = task['usage']
    if task.get('preset'): meta['preset'] = task['preset']
    if task.get('toolSummary'): meta['toolSummary'] = task['toolSummary']
    if task.get('_fallback_model'):
        meta['fallbackModel'] = task['_fallback_model']
        meta['fallbackFrom'] = task.get('_fallback_from', '')
        if task.get('_fallback_reason'):
            meta['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            meta['fallbackKind'] = task['_fallback_kind']
    if task.get('id'): meta['taskId'] = task['id']
    if task.get('model'): meta['model'] = task['model']
    if task.get('provider_id'): meta['provider_id'] = task['provider_id']
    if task.get('thinkingDepth'): meta['thinkingDepth'] = task['thinkingDepth']
    if task.get('apiRounds'): meta['apiRounds'] = task['apiRounds']
    if task.get('modifiedFiles'): meta['modifiedFiles'] = task['modifiedFiles']
    if task.get('modifiedFileList'): meta['modifiedFileList'] = task['modifiedFileList']
    # Orchestration flow per-node run trace (resolved brief + bounded I/O per
    # node) — persisted so the canvas/inspector overlay survives reload /
    # server restart, served via /api/v1/chat/flow-trace/<task>.
    if task.get('_flow_trace'): meta['flowTrace'] = task['_flow_trace']
    if task.get('_flow_label'): meta['flowLabel'] = task['_flow_label']
    # ★ Endpoint-mode terminal signal — persisted so the /api/chat/poll DB
    #   branch (task evicted past TTL / server restarted) can still tell the
    #   frontend "this is a FINISHED endpoint task". Without it, a poll-fallback
    #   that outlives the in-memory task hits the DB branch, returns no
    #   endpointMode, and the frontend overwrites the multi-turn endpoint
    #   structure with the last in-progress turn's single content blob — a
    #   state-sync gap only a manual refresh repaired. The authoritative turns
    #   live in the conversation messages (synced by endpoint.py), so the flag
    #   tells the frontend to reconcile from there rather than from this row.
    if task.get('endpoint_mode'):
        meta['endpointMode'] = True
        if task.get('_endpoint_stop_reason'):
            meta['endpointStopReason'] = task['_endpoint_stop_reason']
    return meta


def _merge_tool_rounds(task):
    """Merge checkpoint + current toolRounds, in order (the continue-flow merge).

    Single source of truth for the ``_checkpointToolRounds + toolRounds``
    concatenation that the final-persist, partial-checkpoint, and both
    conversation-sync paths all need.

    Returns a list of SHALLOW-COPIED round dicts. The copy is load-bearing for
    thread-safety: the swarm driver thread stamps ``_swarmSnapshot`` onto a
    live round dict (master._persist_agent_snapshot) while THIS path may be
    running ``json_dumps_pg(messages)`` on the same rounds from the
    orchestrator thread. Serializing a by-reference dict that another thread
    mutates raises ``RuntimeError: dictionary changed size during iteration``
    (silently swallowed by the sync's except → checkpoint dropped) or persists
    a half-stamped round. A shallow ``dict(r)`` copy is cheap — it duplicates
    only the key→value references (the multi-KB ``toolContent`` string is
    shared, not copied) — and gives json a stable dict to walk. The
    ``_swarmSnapshot`` value (a dict) is copied by-reference, which is correct:
    the stamp REPLACES that key with a fresh object rather than mutating it
    in place, so the snapshot a given serialize sees is always internally
    consistent.
    """
    cp = task.get('_checkpointToolRounds') or []
    cur = task.get('toolRounds') or []
    merged = (list(cp) + cur) if cp else cur
    return [dict(r) if isinstance(r, dict) else r for r in merged]


# Static column order for the task_results upsert — shared by the final-result
# and the running-checkpoint writers so the two can never drift.
_TASK_RESULTS_COLS = (
    'task_id', 'conv_id', 'content', 'thinking', 'error',
    'status', 'tool_rounds', 'metadata', 'created_at', 'completed_at',
)


def _upsert_task_row(task, conv_id, *, content, thinking, status,
                     error_json, tr_json, meta_json):
    """Single source of truth for the ``task_results`` upsert.

    Owns the DB acquire + the ``upsert(..., insert_cols=[10], retry=True)``
    shape (``retry=True`` commits — see lib/database._core_schema.upsert).
    Callers supply only the fields that vary between the final-result write
    (``status='done'|'error'``, full metadata) and the running checkpoint
    (``status='running'``, partial metadata).  ``created_at`` /
    ``completed_at`` are derived here identically for both.
    """
    from lib.database._core_schema import TASK_RESULTS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    upsert(db, TASK_RESULTS, {
        'task_id': task['id'], 'conv_id': conv_id,
        'content': content, 'thinking': thinking,
        'error': error_json, 'status': status, 'tool_rounds': tr_json,
        'metadata': meta_json,
        'created_at': int(task.get('created_at', time.time()) * 1000),
        'completed_at': int(time.time() * 1000),
    }, insert_cols=list(_TASK_RESULTS_COLS), retry=True)


def persist_task_result(task):
    content_len = len(task.get('content') or '')
    thinking_len = len(task.get('thinking') or '')
    error = task.get('error')
    status = task.get('status')
    task_id_short = task['id'][:8]
    conv_id_short = task.get('convId', '')

    finish_reason = task.get('finishReason') or 'unknown'
    model = task.get('model') or '?'
    provider = task.get('provider_id') or '?'

    # ★ Diagnostic: warn about suspiciously empty results
    if status == 'done' and content_len == 0 and thinking_len == 0 and not error and not task.get('aborted'):
        logger.warning('[Task %s] conv=%s ⚠️ PERSISTING EMPTY RESULT — task completed with no content, no thinking, no error. '
                       'finishReason=%s model=%s provider=%s. '
                       'This likely indicates a stream that never received LLM tokens.',
                       task_id_short, conv_id_short, finish_reason, model, provider)
    elif status == 'done' and content_len == 0 and thinking_len > 0:
        logger.warning('[Task %s] conv=%s ⚠️ PERSISTING THINKING-ONLY result — content is empty but thinking has %d chars. '
                       'finishReason=%s model=%s provider=%s. '
                       'The LLM may have been interrupted after thinking but before generating content.',
                       task_id_short, conv_id_short, thinking_len, finish_reason, model, provider)
    else:
        logger.info('[Task %s] conv=%s Persisting result: status=%s content=%dchars thinking=%dchars '
                    'finishReason=%s model=%s provider=%s error=%s',
                     task_id_short, conv_id_short, status, content_len, thinking_len,
                     finish_reason, model, provider, error or 'none')

    # Build meta BEFORE the try so it's always available for _sync_result_to_conversation
    meta = build_result_meta(task)

    # ★ Merge checkpoint toolRounds for DB persistence (continue flow)
    _merged_tr = _merge_tool_rounds(task)

    try:
        # Only store the (potentially multi-MB) toolRounds blob when this task
        # has no conversation row to hold it — see _tool_rounds_have_dedicated_home.
        # For DB-backed/endpoint tasks the conversation is the durable store and
        # recovery readers fall back to load_tool_rounds_from_conversation().
        tr_json = None if _tool_rounds_have_dedicated_home(task) else json.dumps(_merged_tr, ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        # Error envelope is JSON-serialised at the wire — task_results.error
        # is TEXT, but every consumer (SSE done, /api/chat/poll, conversation
        # message persistence) round-trips through lib.error_envelope so the
        # frontend only ever sees the typed dict.
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, task['convId'], content=task['content'],
                         thinking=task['thinking'], status=task['status'],
                         error_json=error_json, tr_json=tr_json, meta_json=meta_json)
        logger.debug('[Task %s] conv=%s Persisted to DB successfully', task_id_short, conv_id_short)
    except Exception:
        logger.error('[Task %s] conv=%s ❌ Persist FAILED — content (%d chars) and thinking (%d chars) may be lost!',
                     task_id_short, conv_id_short, content_len, thinking_len, exc_info=True)

    # ★ Write result back to conversation — ensures data survives even if
    #   no frontend client is connected (SSE closed, user closed tab, etc.)
    # For endpoint mode tasks, the multi-turn sync happens in endpoint.py
    # via _sync_endpoint_turns_to_conversation(). We still call the regular
    # sync as a fallback for the single-turn content + metadata.
    if not task.get('endpoint_mode') or not task.get('_endpoint_turns'):
        _sync_result_to_conversation(task, meta)
    else:
        logger.info('[Task %s] conv=%s Skipping single-turn sync — endpoint mode with %d turns '
                     '(already synced by endpoint loop)',
                     task['id'][:8], task.get('convId', ''), len(task.get('_endpoint_turns', [])))

    # ★ Update proactive scheduler task execution status
    _update_proactive_execution_status(task)

    # ★ Auto-dispatch next queued message (server-side queue)
    _dispatch_queued_message(task)

    # ★ Cross-conversation awareness (Layer 2): lazily (re)generate this
    #   conversation's project summary after a successful reply, but ONLY when
    #   it's a real project conversation. Non-blocking — runs in a daemon
    #   thread so it never delays task completion or the next queued message.
    _maybe_refresh_project_summary(task)


def _maybe_refresh_project_summary(task):
    """Post-reply trigger for the lazy project-summary generator (Layer 2).

    Fires only for completed, non-aborted project conversations. The summary
    engine itself is the gate for *whether* to regenerate (msg_count growth),
    so this just decides whether the conversation is even a project candidate
    and kicks off a background, fire-and-forget refresh.
    """
    try:
        if task.get('status') != 'done' or task.get('aborted'):
            return
        conv_id = task.get('convId')
        if not conv_id:
            return
        cfg = task.get('config') or {}
        if not cfg.get('projectEnabled') or not cfg.get('projectPath'):
            return
        from lib.conversations.project_summary import ensure_summary
        ensure_summary(conv_id, blocking=False)
    except Exception as e:
        logger.debug('[ProjSummary] post-reply trigger skipped conv=%s: %s',
                     task.get('convId', '?'), e)


def _update_proactive_execution_status(task):
    """Update the proactive scheduler task's execution status when its agentic task completes."""
    task_id = task.get('id', '')
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        # Find any proactive task whose last_execution_task_id matches this task
        row = db.execute(
            'SELECT id FROM scheduled_tasks WHERE last_execution_task_id=? AND task_type=?',
            [task_id, 'agent']
        ).fetchone()
        if not row:
            return  # Not a proactive execution

        sched_id = row['id']
        status = task.get('status', 'done')
        exec_status = 'ok' if status == 'done' and not task.get('error') else 'error'
        now = datetime.now().isoformat()

        db.execute(
            'UPDATE scheduled_tasks SET last_execution_status=?, updated_at=? WHERE id=?',
            [exec_status, now, sched_id]
        )
        db.commit()
        logger.info('[Proactive:%s] Execution %s completed with status=%s',
                    sched_id[:8], task_id[:8], exec_status)
    except Exception as e:
        logger.warning('[Proactive] Failed to update execution status for task %s: %s',
                       task_id[:8], e, exc_info=True)


def _dispatch_queued_message(task):
    """Check for queued messages and dispatch the next one after task completion.

    Runs in a fire-and-forget manner — failures are logged but don't affect
    the calling task's persistence.

    When a task is aborted by the user, queued messages are still dispatched —
    the user explicitly stopped the current generation, so the next queued
    message should proceed.  Only on errors do we skip dispatch (user may
    want to fix something before the queued message runs).
    """
    conv_id = task.get('convId', '')
    if not conv_id:
        return

    # Autopilot's hook (``maybe_run_autopilot``) runs immediately before us
    # and may have already spawned a synthetic-user follow-up.  Spawning a
    # second successor here would race-abort it.  The queued real message
    # is left in the queue and will be picked up when the autopilot
    # follow-up itself completes.
    if task.get('_autopilot_spawned_followup'):
        logger.info('[Queue] Skipping dispatch — autopilot already spawned '
                    'follow-up task %s for conv=%s',
                    task['_autopilot_spawned_followup'][:8], conv_id[:8])
        return

    try:
        from lib.message_queue import dispatch_next_queued, get_queue_depth
        # Check if there are queued messages before dispatching
        depth = get_queue_depth(conv_id)
        if depth == 0:
            return

        if task.get('aborted'):
            logger.info('[Queue] Task was aborted for conv=%s — dispatching next queued message (depth=%d)',
                        conv_id[:8], depth)
        new_task_id = dispatch_next_queued(conv_id)
        if new_task_id:
            logger.info('[Queue] Auto-dispatched queued message → task %s for conv=%s',
                        new_task_id[:8], conv_id[:8])
    except Exception as e:
        logger.warning('[Queue] Auto-dispatch failed for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)


def _sync_result_to_conversation(task, meta):
    """Write the completed task result into the conversation's messages in the DB.

    Finds or creates the last assistant message and fills in content, thinking,
    toolRounds, finishReason, etc.  This makes the backend self-sufficient —
    even if no frontend client receives the 'done' SSE event, the conversation
    is updated.

    Runs in a separate try/except so failures don't affect task_results persistence.
    """
    conv_id = task.get('convId', '')
    task_id_short = task['id'][:8]
    pfx = f'[SyncConv {task_id_short}]'

    content = task.get('content') or ''
    thinking = task.get('thinking') or ''
    error = task.get('error')

    # Skip if there's truly nothing to write (e.g. aborted before any tokens)
    if not content and not thinking and not error:
        logger.debug('%s conv=%s Skipping conv sync — no content/thinking/error to write', pfx, conv_id)
        return

    # ── FRESHNESS GUARD: reject writes from stale/superseded tasks ──
    # When a user stops a task and regenerates, a new task becomes the
    # "latest" for this conversation. The old task may still be winding
    # down (abort is cooperative), and its _sync_result_to_conversation
    # would overwrite the new task's data. This guard prevents that.
    if conv_id:
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        if latest and latest != task['id']:
            _abort_reason = task.get('_abort_reason', '')
            _autopilot_child = task.get('_autopilot_spawned_followup')
            if task.get('aborted') or _abort_reason:
                # Expected path: the user started a newer task (e.g. Stop →
                # Edit → Regenerate) for this conv, so this task was aborted
                # and is now finishing its in-flight work. Cooperative abort
                # means it only stops at the next checkpoint, so it can reach
                # this point with stale content. Skipping the write is correct
                # and routine — not an error.
                logger.debug(
                    '%s conv=%s skipping conv sync: superseded by newer task %s '
                    '(this task aborted, reason=%s, %dchars stale content discarded)',
                    pfx, conv_id[:8], latest[:8], _abort_reason or 'superseded', len(content),
                )
            elif _autopilot_child:
                # Expected path: this task's OWN autopilot hook spawned a
                # follow-up task (the virtual-user turn) before persist ran,
                # so the follow-up is now 'latest' for the conv. This task was
                # not aborted — it finished normally and was superseded by its
                # own child. The follow-up rebuilt the conversation from the DB
                # (including this task's answer), so skipping the write here is
                # correct — the autopilot append path owns the DB write.
                logger.debug(
                    '%s conv=%s skipping conv sync: superseded by own autopilot '
                    'follow-up %s (%dchars; autopilot owns the DB write)',
                    pfx, conv_id[:8], _autopilot_child[:8], len(content),
                )
            else:
                # Unexpected: a task that was never aborted is no longer the
                # latest for its conv. This shouldn't normally happen and may
                # point to a missing abort path — worth a look.
                logger.warning(
                    '%s conv=%s skipping conv sync: superseded by newer task %s, '
                    'but this task was never aborted (%dchars discarded). '
                    'Unexpected — a new task replaced this one without aborting it.',
                    pfx, conv_id[:8], latest[:8], len(content),
                )
            return

    # ── External-caller short-circuit ──
    # Tasks started via /api/chat/start with inline `messages` in the POST
    # body (SWE-bench harness, eval tools, external backends) have no
    # corresponding row in the `conversations` table — results are read by
    # the caller from `task_results` directly. Skip the write-back path so
    # we don't flood error.log with "Conversation not found" warnings.
    if task.get('_inline_messages'):
        logger.debug('%s conv=%s Inline-message task — skipping conv sync by design', pfx, conv_id)
        return

    db = None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()

        if not row:
            logger.warning('%s conv=%s Conversation not found in DB — cannot sync result back', pfx, conv_id)
            return

        # Capture updated_at for optimistic locking (CAS guard).
        # ``row`` is a sqlite3.Row / psycopg DictRow — named access works
        # for both backends, no need for the hasattr() shim.
        _row_updated_at = row['updated_at']
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError):
            logger.error('%s conv=%s Failed to parse existing messages JSON', pfx, conv_id, exc_info=True)
            return

        if not messages:
            logger.warning('%s conv=%s Conversation has 0 messages — cannot sync result back', pfx, conv_id)
            return

        # ── CROSS-TALK DETECTION: verify message count consistency ──
        # Normal cause: the frontend saves completed previous turns to the DB
        # between task creation and task completion, so the DB naturally has more
        # messages than the snapshot sent to create_task().  We only flag as a
        # true anomaly when the extra messages contain consecutive same-role
        # entries (e.g. assistant-assistant or user-user), which cannot arise
        # from normal turn-taking and may indicate cross-talk or data corruption.
        expected_msg_count = task.get('_initial_msg_count')
        if expected_msg_count is not None and len(messages) > expected_msg_count + 2:
            extra_msgs = messages[expected_msg_count:]
            extra_summary = [(m.get('role'), len(m.get('content') or ''), m.get('model', 'N/A'))
                             for m in extra_msgs]
            # ── Skip dedup when endpoint-mode history is present ──
            # Endpoint mode legitimately produces consecutive-same-role messages
            # by design: planner+worker are both role=assistant, and critic+next
            # turn user are both role=user. These are NOT cross-talk anomalies.
            # Dedup here would destroy workers (shorter than planners) and, worse,
            # drop a short new user follow-up (e.g. "why did it stop?") in favor
            # of the preceding long critic review.  The message builder already
            # collapses historical endpoint sessions before sending to the LLM,
            # so we don't need to touch the persisted conversation.
            has_endpoint_history = any(
                (m.get('_isEndpointPlanner')
                 or m.get('_isEndpointReview')
                 or m.get('_epIteration') is not None
                 or m.get('_epIter') is not None
                 or m.get('_epPlannerIteration') is not None
                 or m.get('_epNextPhase'))
                for m in messages
            )
            # Check for consecutive same-role messages in the extras
            has_consecutive_same_role = any(
                extra_msgs[i].get('role') == extra_msgs[i + 1].get('role')
                for i in range(len(extra_msgs) - 1)
            )
            if has_consecutive_same_role and has_endpoint_history:
                logger.info(
                    '%s conv=%s Message count drift (DB=%d, task_start=%d, delta=%d) '
                    'with consecutive same-role — but ENDPOINT history detected. '
                    'Skipping dedup (planner+worker and critic+next-user are '
                    'expected same-role pairs). Extra msgs: %s',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )
            elif has_consecutive_same_role:
                logger.error(
                    '%s conv=%s ⛔ MESSAGE COUNT ANOMALY with consecutive same-role: '
                    'DB has %d messages but task started with %d — %d extra. '
                    'Extra msgs: %s — auto-deduplicating',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )
                # Auto-fix: remove consecutive duplicate-role messages
                # Keep the message with more content when two same-role msgs are adjacent.
                # ★ Guard: NEVER drop the last two messages (trailing user + assistant
                #   slot) — a short new user follow-up (e.g. "why?") must always win
                #   over any earlier same-role message it might be adjacent to.
                _tail_protect_idx = max(0, len(messages) - 2)
                deduped = [messages[0]]
                for idx, m in enumerate(messages[1:], start=1):
                    if (m.get('role') == deduped[-1].get('role')
                            and idx < _tail_protect_idx):
                        # Keep the one with more content
                        existing_len = len(deduped[-1].get('content') or '')
                        new_len = len(m.get('content') or '')
                        if new_len > existing_len:
                            deduped[-1] = m
                        logger.info('%s conv=%s Removed duplicate %s message (kept %d chars, dropped %d chars)',
                                   pfx, conv_id, m.get('role'), max(existing_len, new_len), min(existing_len, new_len))
                    else:
                        deduped.append(m)
                messages = deduped
                logger.info('%s conv=%s After dedup: %d messages (was %d)',
                           pfx, conv_id, len(messages), expected_msg_count + len(extra_msgs))
            else:
                logger.debug(
                    '%s conv=%s Message count drift (DB=%d, task_start=%d, delta=%d) — '
                    'normal frontend save of previous turns. Extra msgs: %s',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )

        # Find the last assistant message to fill in
        last_msg = messages[-1]

        if last_msg.get('role') != 'assistant':
            # ── Guard: an aborted/superseded task must NOT append a new
            #   assistant slot ──
            # When the user clicks Stop → Regenerate, the regen handler
            # truncates the conversation down to the user turn (tail is now
            # role='user') and starts a fresh task. The old aborted task can
            # reach this point AFTER that truncation but BEFORE the new task
            # registers as `_conv_latest_task` (so the freshness guard above
            # didn't catch it). Blindly appending the stale assistant content
            # here resurrects the just-truncated turn → the "U1 A1 U1 A2"
            # doubled-context bug. Aborted tasks may only FILL an existing
            # trailing assistant slot, never create one.
            if task.get('aborted') or task.get('_abort_reason'):
                logger.info('%s conv=%s Last message is role=%s and this task is '
                            'aborted (reason=%s) — dropping stale write instead of '
                            'appending a new assistant (prevents truncated-turn '
                            'resurrection)',
                            pfx, conv_id, last_msg.get('role'),
                            task.get('_abort_reason') or 'aborted')
                return
            # No trailing assistant message — append one
            logger.info('%s conv=%s Last message is role=%s, appending new assistant message',
                       pfx, conv_id, last_msg.get('role'))
            last_msg = {'role': 'assistant', 'content': '', 'thinking': ''}
            messages.append(last_msg)

        # ── Guard: don't overwrite with LESS content ──
        # The frontend may have already synced a fuller version via PUT
        existing_content_len = len(last_msg.get('content') or '')
        existing_thinking_len = len(last_msg.get('thinking') or '')
        new_content_len = len(content)
        new_thinking_len = len(thinking)

        # ★ Merge checkpoint toolRounds for continue flow
        tool_rounds = _merge_tool_rounds(task)

        if existing_content_len > new_content_len and existing_thinking_len > new_thinking_len:
            # ★ FIX: Even when frontend has more content (synced before us),
            #   still update toolRounds + metadata — the backend has richer
            #   tool data (toolContent, assistantContent) that the frontend
            #   may have missed if the SSE stream broke mid-delivery.
            #   Without this, page refresh → Continue loses toolContent
            #   because the frontend's stale sync overwrote our checkpoint.
            _tr_updated = False
            if tool_rounds:
                _existing_tr = last_msg.get('toolRounds') or []
                # Only replace if we have more rounds or the existing rounds
                # are missing toolContent (frontend sync race condition)
                _existing_has_tc = all(r.get('toolContent') for r in _existing_tr if r.get('status') == 'done')
                _new_has_tc = any(r.get('toolContent') for r in tool_rounds if r.get('status') == 'done')
                if len(tool_rounds) > len(_existing_tr) or (not _existing_has_tc and _new_has_tc):
                    last_msg['toolRounds'] = tool_rounds
                    _tr_updated = True
            # Always update finishReason/metadata (frontend may not have received done event)
            if meta.get('finishReason') and not last_msg.get('finishReason'):
                last_msg['finishReason'] = meta['finishReason']
            if meta.get('usage') and not last_msg.get('usage'):
                last_msg['usage'] = meta['usage']
            if meta.get('model') and not last_msg.get('model'):
                last_msg['model'] = meta['model']
            if meta.get('provider_id') and not last_msg.get('provider_id'):
                last_msg['provider_id'] = meta['provider_id']
            if _tr_updated or meta.get('finishReason'):
                logger.info('%s conv=%s Content guard: existing=%d+%d > new=%d+%d, '
                           'but still updating toolRounds=%s metadata=%s',
                           pfx, conv_id, existing_content_len, existing_thinking_len,
                           new_content_len, new_thinking_len,
                           _tr_updated, bool(meta.get('finishReason')))
            else:
                logger.info('%s conv=%s Server already has MORE content (existing=%d+%d > new=%d+%d) — '
                           'frontend likely already synced. Skipping.',
                           pfx, conv_id, existing_content_len, existing_thinking_len,
                           new_content_len, new_thinking_len)
                return

        else:
            # Normal path: backend has equal or more content — update everything
            if content:
                last_msg['content'] = content
            if thinking:
                last_msg['thinking'] = thinking
            if error:
                last_msg['error'] = error

        # Copy metadata fields that the frontend would normally set.
        # Terminal metadata is backend-authoritative — once the task reaches
        # this code path the backend has the truth, and any earlier value
        # the frontend sync may have written (e.g. 'interrupted' before the
        # final 'stop' arrived) is superseded.
        if tool_rounds:
            last_msg['toolRounds'] = tool_rounds
        if meta.get('finishReason'):
            last_msg['finishReason'] = meta['finishReason']
        if meta.get('usage'):
            last_msg['usage'] = meta['usage']
        if meta.get('preset'):
            last_msg['preset'] = meta['preset']
        if meta.get('toolSummary'):
            last_msg['toolSummary'] = meta['toolSummary']
        if meta.get('model'):
            last_msg['model'] = meta['model']
        if meta.get('provider_id'):
            last_msg['provider_id'] = meta['provider_id']
        if meta.get('taskId'):
            last_msg['_taskId'] = meta['taskId']
        if meta.get('fallbackModel'):
            last_msg['fallbackModel'] = meta['fallbackModel']
            last_msg['fallbackFrom'] = meta.get('fallbackFrom', '')
            if meta.get('fallbackReason'):
                last_msg['fallbackReason'] = meta['fallbackReason']
            if meta.get('fallbackKind'):
                last_msg['fallbackKind'] = meta['fallbackKind']
        if meta.get('apiRounds'):
            last_msg['apiRounds'] = meta['apiRounds']
        if meta.get('modifiedFiles'):
            last_msg['modifiedFiles'] = meta['modifiedFiles']
        if meta.get('modifiedFileList'):
            last_msg['modifiedFileList'] = meta['modifiedFileList']

        # ── Cost snapshot (persisted at sync time, not lazily fetched) ──
        # Cost depends only on usage + model + provider + the pricing table
        # at the time of the call. By stamping the result onto the message
        # AND each apiRounds entry now, every render path reads the value
        # directly from msg.cost / rd.cost — zero per-render network calls.
        # See `compute_cost` in lib/cost.py.
        # The cost is "as of message time" — pricing changes do NOT
        # retroactively rewrite history. This matches what was actually
        # charged and is what billing/auditing wants anyway.
        try:
            from lib.cost import compute_cost as _compute_cost
            _msg_model = (last_msg.get('model') or meta.get('model') or '')
            _msg_provider = (last_msg.get('provider_id')
                              or meta.get('provider_id') or None)
            if last_msg.get('usage') and not last_msg.get('cost'):
                _c = _compute_cost(last_msg['usage'],
                                    model_id=_msg_model,
                                    provider_id=_msg_provider)
                if _c:
                    last_msg['cost'] = _c
            _rounds = last_msg.get('apiRounds') or []
            for _rd in _rounds:
                if not isinstance(_rd, dict):
                    continue
                if _rd.get('cost'):
                    continue
                _ru = _rd.get('usage') or {}
                if not _ru:
                    continue
                _rc = _compute_cost(
                    _ru,
                    model_id=_rd.get('model') or _msg_model,
                    provider_id=(_rd.get('provider_id')
                                  or _rd.get('providerId')
                                  or _msg_provider))
                if _rc:
                    _rd['cost'] = _rc
        except Exception as _ce:
            logger.warning('%s conv=%s Cost stamp failed (non-fatal): %s',
                           pfx, conv_id[:8] if conv_id else '?', _ce)

        # Backfill stable per-message IDs.  Newly created messages get a
        # UUID; existing messages keep theirs.  Index-free addressing is
        # what makes routes/translate.py and PATCH /messages/by-id/<mid>
        # robust against concurrent inserts.
        _assign_message_ids(messages)

        # memory prefetch: persist indicator payload for reload visibility
        if task.get('_memoryPrefetch'):
            last_msg['_memoryPrefetch'] = task['_memoryPrefetch']

        # preferences-applied chip: persist so the chip survives reload
        if task.get('_preferencesApplied'):
            last_msg['_preferencesApplied'] = task['_preferencesApplied']

        # related-conversations chip: persist so the chip survives reload
        if task.get('_relatedConversations'):
            last_msg['_relatedConversations'] = task['_relatedConversations']

        # preferences-learned: persist the "Noted: you prefer X" moment(s)
        if task.get('_preferencesLearned'):
            last_msg['_preferencesLearned'] = task['_preferencesLearned']

        # git-shim: persist the round commit sha for redo/diff references.
        if task.get('gitSha'):
            last_msg['_gitSha'] = task['gitSha']

        # Serialize and write back — json_dumps_pg strips null bytes from
        # raw data AND removes \u0000 escapes from the JSON text.
        messages_json = json_dumps_pg(messages)
        now_ms = int(time.time() * 1000)

        # ── Also clear activeTaskId from settings so subsequent reloads
        #    don't re-trigger Case B recovery for an already-synced task ──
        settings_row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        settings_json = None
        if settings_row:
            try:
                s = json.loads(settings_row[0] or '{}')
                changed = False
                if s.get('activeTaskId'):
                    s['activeTaskId'] = None
                    changed = True
                # ★ Update lastMsgRole/lastMsgTimestamp so metadata shells
                # reflect the new last message (assistant, not user) for Case E
                if messages:
                    lm = messages[-1]
                    if s.get('lastMsgRole') != lm.get('role') or s.get('lastMsgTimestamp') != lm.get('timestamp'):
                        s['lastMsgRole'] = lm.get('role')
                        s['lastMsgTimestamp'] = lm.get('timestamp')
                        changed = True
                if changed:
                    settings_json = json.dumps(s, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Task] Failed to parse/clear activeTaskId from settings for conv=%s: %s', conv_id, e, exc_info=True)

        # ── Optimistic lock: only update if no concurrent write occurred ──
        # Use updated_at as CAS guard to prevent overwriting a fresher
        # frontend sync.  If the row was updated since our SELECT, our
        # read-modify-write would clobber the frontend's data.
        # ── Also update search_text for fast conversation search ──
        from lib.conversations import build_search_text
        search_text = build_search_text(messages)
        # ── Why use raw db.execute()+commit instead of db_execute_with_retry?
        #     The retry helper masks rowcount (its docstring says "returns
        #     None"), and we need the rowcount to detect CAS-miss reliably.
        #     A re-SELECT of updated_at has a TOCTOU window where a third
        #     writer landing between our UPDATE and the verify SELECT would
        #     falsely report CAS-miss — suppressing side-effects (cost
        #     stamp, auto-translate) that we just durably committed.
        if settings_json:
            cur = db.execute(
                '''UPDATE conversations
                   SET messages=?, updated_at=?, msg_count=?, settings=?, search_text=?
                   WHERE id=? AND user_id=1 AND updated_at=?''',
                (messages_json, now_ms, len(messages), settings_json, search_text, conv_id, _row_updated_at)
            )
        else:
            cur = db.execute(
                '''UPDATE conversations
                   SET messages=?, updated_at=?, msg_count=?, search_text=?
                   WHERE id=? AND user_id=1 AND updated_at=?''',
                (messages_json, now_ms, len(messages), search_text, conv_id, _row_updated_at)
            )
        db.commit()
        _cas_succeeded = (getattr(cur, 'rowcount', 0) or 0) > 0
        # FTS index is only updated when CAS succeeds.  Updating FTS for a
        # write we lost would leave search hits pointing at content we
        # never persisted — search results would surface dead data.
        if _cas_succeeded:
            from lib.conversations import update_conversation_fts
            update_conversation_fts(db, conv_id, search_text)
        if not _cas_succeeded:
            logger.info('%s conv=%s Optimistic lock missed — row was updated concurrently '
                       '(expected_updated_at=%s). '
                       'Frontend likely synced first; backend sync skipped (safe).',
                       pfx, conv_id, _row_updated_at)
        else:
            logger.info('%s conv=%s ✅ Synced result to conversation — content=%dchars thinking=%dchars '
                        'msgs=%d (was: content=%d thinking=%d)',
                        pfx, conv_id, new_content_len, new_thinking_len, len(messages),
                        existing_content_len, existing_thinking_len)

        # ── Invalidate meta cache so subsequent GET /api/conversations
        #    returns the cleared activeTaskId immediately ──
        try:
            from lib.conversations import invalidate_meta_cache
            invalidate_meta_cache()
        except Exception as e:
            logger.debug('[Manager] meta cache invalidation skipped: %s', e)

        # ── Auto-translate: server-side safety net for translation ──
        # Ensures translation happens even if the frontend is offline / switched away.
        # Independent of the row CAS above: _maybe_auto_translate_assistant
        # re-reads fresh DB state, dedups against existing translatedContent
        # and running translate tasks, and commits via a targeted by-id write
        # — it never does the full-row write the CAS guards.  Gating it on
        # _cas_succeeded meant that when a live frontend won the conversation
        # row-write race (the active-view case), nobody translated until the
        # user switched conversations or clicked translate.  Fire regardless.
        if content and not error:
            try:
                _maybe_auto_translate_assistant(conv_id, content, len(messages) - 1, db, task=task)
            except Exception as te:
                logger.warning('%s conv=%s Auto-translate trigger failed (non-fatal): %s',
                               pfx, conv_id, te)
        else:
            # No content / errored task: the safety net is skipped, so if the
            # tool loop spun up an incremental accumulator (per-round segments
            # translated in the background) it would otherwise dangle until its
            # 300s idle-timeout and log a misleading "finalize never called"
            # warning. Tear it down explicitly. No-op when no accumulator
            # exists (autoTranslate off — the common case).
            try:
                from lib.translate import cancel_incremental
                if cancel_incremental(task):
                    logger.info('%s conv=%s cancelled incremental accumulator '
                                '(task ended with no content / error=%s)',
                                pfx, conv_id, bool(error))
            except Exception as ce:
                logger.debug('%s conv=%s cancel_incremental failed: %s', pfx, conv_id, ce)

    except Exception as e:
        logger.error('%s conv=%s ❌ Failed to sync result to conversation: %s',
                     pfx, conv_id, e, exc_info=True)


def checkpoint_task_partial(task):
    """Persist the current in-flight task state to DB so it survives a server crash.

    Called after each tool-execution round in the orchestrator loop.
    Writes to both task_results (for poll recovery) and the conversation
    (for direct page-reload recovery).

    Uses status='running' so the frontend can distinguish a partial checkpoint
    from a final result (status='done'|'error').
    """
    content_len = len(task.get('content') or '')
    thinking_len = len(task.get('thinking') or '')
    task_id_short = task['id'][:8]
    conv_id = task.get('convId', '')

    # Don't bother checkpointing if there's nothing meaningful yet
    if content_len == 0 and thinking_len == 0:
        return

    # ★ Merge checkpoint toolRounds for continue flow
    _merged_tr = _merge_tool_rounds(task)

    try:
        # See _tool_rounds_have_dedicated_home: skip the duplicate blob for
        # tasks whose toolRounds are checkpointed into conversations.messages
        # by _sync_partial_to_conversation on the same cadence.
        tr_json = None if _tool_rounds_have_dedicated_home(task) else json.dumps(_merged_tr, ensure_ascii=False)
        meta = {}
        if task.get('model'): meta['model'] = task['model']
        if task.get('preset'): meta['preset'] = task['preset']
        if task.get('thinkingDepth'): meta['thinkingDepth'] = task['thinkingDepth']
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        # Error envelope is JSON-serialised at the wire — see persist_task_result.
        _cp_error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status='running',
                         error_json=_cp_error_json, tr_json=tr_json, meta_json=meta_json)
        logger.debug('[Checkpoint %s] conv=%s Saved partial: content=%dchars thinking=%dchars',
                     task_id_short, conv_id, content_len, thinking_len)
    except Exception as e:
        logger.warning('[Checkpoint %s] conv=%s Failed to checkpoint: %s',
                       task_id_short, conv_id, e, exc_info=True)

    # Also sync partial content into the conversation's messages in DB
    # For endpoint mode, skip — endpoint.py handles multi-turn sync
    if not task.get('endpoint_mode'):
        _sync_partial_to_conversation(task)

    # ── CROSS-TALK DETECTION: log when multiple tasks are being checkpointed concurrently ──
    with tasks_lock:
        running_tasks = [(tid[:8], t.get('convId', '')[:8])
                         for tid, t in tasks.items()
                         if t.get('status') == 'running' and tid != task['id']]
    if running_tasks:
        logger.debug(
            '[Checkpoint %s] conv=%s ⚠️ %d other running task(s): %s — '
            'concurrent streams increase cross-talk risk on frontend',
            task_id_short, conv_id, len(running_tasks),
            running_tasks
        )


def _sync_partial_to_conversation(task):
    """Write partial streaming state into the conversation's last assistant message.

    Comprehensive checkpoint: writes content, thinking, toolRounds, and
    structural metadata (model, modifiedFileList, _memoryPrefetch,
    gitSha) so a page reload mid-stream reconstructs the same UI the user
    saw before the disconnect — without depending on the in-memory task
    object, the activeTaskId stash, or poll fallback.

    Skips terminal-only fields (finishReason, usage, toolSummary) since they
    aren't final until the task completes.
    """
    conv_id = task.get('convId', '')
    content = task.get('content') or ''
    thinking = task.get('thinking') or ''
    if not content and not thinking:
        return

    # ── FRESHNESS GUARD: reject checkpoint writes from stale tasks ──
    if conv_id:
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        if latest and latest != task['id']:
            logger.debug('[Checkpoint] conv=%s Stale task %s — skipping partial sync (latest=%s)',
                         conv_id[:8], task['id'][:8], latest[:8])
            return

    # ★ Merge checkpoint toolRounds for continue flow
    tool_rounds = _merge_tool_rounds(task)

    # Bounded CAS retry — under contention with the frontend or other writers
    # we re-read and try again rather than silently dropping the checkpoint.
    MAX_CAS = 3
    last_err = None
    for attempt in range(MAX_CAS):
        try:
            db = get_thread_db(DOMAIN_CHAT)
            row = db.execute(
                'SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            if not row:
                return

            try:
                messages = json.loads(row[0] or '[]')
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning('[Manager] Unparseable conversation messages for conv=%s: %s', conv_id, exc)
                return

            if not messages:
                return

            cur_updated_at = row[1]

            last_msg = messages[-1]
            if last_msg.get('role') != 'assistant':
                # Do NOT materialize a brand-new trailing assistant row from a
                # thinking-only first checkpoint.  A turn that streams a stray
                # reasoning fragment and then dies (server crash) would otherwise
                # leave an empty husk ({content:'', thinking:'I'}) with no
                # finishReason — which renders as a blank bubble with no finish
                # tag and slips past the frontend ghost-cleanup (the stray
                # `thinking` defeats its `!thinking` guard).  Wait until there's
                # real CONTENT before appending the row; thinking accumulated by
                # then is written alongside it, so nothing is dropped.  Updating
                # an EXISTING assistant row (frontend placeholder) is unaffected.
                if not content:
                    logger.debug('[Checkpoint] conv=%s deferring trailing assistant '
                                 'row — thinking-only first checkpoint (no content yet)',
                                 conv_id[:8])
                    return
                last_msg = {'role': 'assistant', 'content': '', 'thinking': ''}
                messages.append(last_msg)

            existing_content_len = len(last_msg.get('content') or '')
            existing_thinking_len = len(last_msg.get('thinking') or '')

            # Track whether anything actually changed; we skip the UPDATE if
            # content didn't grow AND no new structural data is available.
            mutated = False

            if content and len(content) > existing_content_len:
                last_msg['content'] = content
                mutated = True
            if thinking and len(thinking) > existing_thinking_len:
                last_msg['thinking'] = thinking
                mutated = True

            if tool_rounds:
                _existing_tr = last_msg.get('toolRounds') or []
                # Replace if we have more rounds OR existing rounds lack toolContent
                # (frontend race may have synced an earlier tool-result without it).
                _existing_done_have_tc = all(
                    r.get('toolContent') for r in _existing_tr if r.get('status') == 'done'
                )
                _new_done_have_tc = any(
                    r.get('toolContent') for r in tool_rounds if r.get('status') == 'done'
                )
                if (len(tool_rounds) > len(_existing_tr)
                        or (not _existing_done_have_tc and _new_done_have_tc)):
                    last_msg['toolRounds'] = tool_rounds
                    mutated = True

            # Structural metadata that is meaningful BEFORE final completion.
            # Backend is authoritative for these; only fill if frontend hasn't.
            for src_key, dst_key in (
                ('model', 'model'),
                ('provider_id', 'provider_id'),
                ('preset', 'preset'),
                ('modifiedFiles', 'modifiedFiles'),
                ('modifiedFileList', 'modifiedFileList'),
                ('apiRounds', 'apiRounds'),
                ('_memoryPrefetch', '_memoryPrefetch'),
                ('_preferencesApplied', '_preferencesApplied'),
                ('_relatedConversations', '_relatedConversations'),
                ('_preferencesLearned', '_preferencesLearned'),
            ):
                v = task.get(src_key)
                if v and not last_msg.get(dst_key):
                    last_msg[dst_key] = v
                    mutated = True
            git_sha = task.get('gitSha')
            if git_sha and not last_msg.get('_gitSha'):
                last_msg['_gitSha'] = git_sha
                mutated = True

            # Backfill stable IDs onto every message — pure write-side hook.
            if _assign_message_ids(messages):
                mutated = True

            if not mutated:
                return

            from lib.conversations import build_search_text
            messages_json = json_dumps_pg(messages)
            search_text = build_search_text(messages)
            now_ms = int(time.time() * 1000)
            cur = db.execute(
                'UPDATE conversations SET messages=?, updated_at=?, msg_count=?, search_text=? '
                'WHERE id=? AND user_id=1 AND updated_at=?',
                (messages_json, now_ms, len(messages), search_text, conv_id, cur_updated_at)
            )
            db.commit()
            rowcount = getattr(cur, 'rowcount', None)
            if rowcount == 0:
                # CAS miss — retry with a fresh read.
                logger.debug('[Checkpoint] conv=%s CAS miss attempt %d/%d — re-reading',
                             conv_id[:8], attempt + 1, MAX_CAS)
                time.sleep(0.02 * (attempt + 1))
                continue
            # FTS update is gated on CAS success.  Without this guard, a
            # losing partial checkpoint would still rewrite the FTS row,
            # making search hits point at content the messages column
            # never accepted.  See _sync_result_to_conversation for the
            # equivalent guard on the terminal sync path.
            from lib.conversations import update_conversation_fts
            update_conversation_fts(db, conv_id, search_text)
            logger.debug('[Checkpoint] conv=%s Synced partial: content=%d→%d thinking=%d→%d tools=%d',
                         conv_id, existing_content_len, len(content),
                         existing_thinking_len, len(thinking), len(tool_rounds or []))
            return
        except Exception as e:
            last_err = e
            logger.debug('[Checkpoint] conv=%s partial sync attempt %d/%d failed: %s',
                         conv_id, attempt + 1, MAX_CAS, e)
            time.sleep(0.05 * (attempt + 1))
    if last_err is not None:
        logger.debug('[Checkpoint] conv=%s gave up after %d attempts: %s',
                     conv_id, MAX_CAS, last_err)



def recover_stale_tasks_on_startup():
    """Clean up stale tasks from a previous server crash at startup time.

    When the server crashes mid-generation:
    - task_results has entries with status='running' (from checkpoints)
    - conversations have activeTaskId set in settings (never cleared)

    This function:
    1. Marks all stale task_results as 'interrupted'
    2. Clears activeTaskId from all conversation settings
    3. Syncs interrupted task content into conversation messages

    This ensures the frontend doesn't need to do Case B recovery for every
    stale conversation on every page load, which dramatically speeds up boot.
    """
    try:
        db = get_thread_db(DOMAIN_CHAT)

        # ── Step 1: Mark stale running tasks as interrupted ──
        stale_rows = db.execute(
            "SELECT task_id, conv_id, content, thinking FROM task_results WHERE status='running'"
        ).fetchall()

        # conv_id → task_id of the interrupted task carrying the MOST recovered
        # text.  task_results.conv_id is BACKEND-AUTHORITATIVE (create_task stamps
        # it), unlike the frontend-synced settings.activeTaskId which is null/stale
        # after a mid-stream crash (the PUT that persists it may never have landed).
        # Keying the merge off THIS map is what lets a crash-interrupted turn be
        # recovered into conversations.messages even when activeTaskId was lost —
        # the root fix for "Continue starts a brand-new agent from scratch".
        interrupted_task_by_conv = {}
        if stale_rows:
            _best_recovered_len = {}
            for row in stale_rows:
                tid = row['task_id']
                cid = row['conv_id'] or ''
                clen = len(row['content'] or '')
                tlen = len(row['thinking'] or '')
                logger.info('[Startup] Marking stale task %s (conv=%s) as interrupted: '
                            'content=%dchars thinking=%dchars',
                            tid[:8], cid[:8], clen, tlen)
                if cid:
                    _tot = clen + tlen
                    if _tot >= _best_recovered_len.get(cid, -1):
                        _best_recovered_len[cid] = _tot
                        interrupted_task_by_conv[cid] = tid
            db.execute("UPDATE task_results SET status='interrupted' WHERE status='running'")
            db.commit()
            logger.info('[Startup] Marked %d stale running task(s) as interrupted '
                        '(%d owning conv(s) identified via task_results.conv_id)',
                        len(stale_rows), len(interrupted_task_by_conv))

        # ── Step 2+3: Merge recovered content into conversations + clear stale
        #    activeTaskId.  Drive off TWO sources, UNIONed by conv_id:
        #      (a) conversations still carrying settings.activeTaskId — clear the
        #          now-dead pointer (json_extract is index-backed on PG via
        #          idx_conv_active_task and native on SQLite).
        #      (b) conversations that OWN an interrupted task via
        #          task_results.conv_id — AUTHORITATIVE; recovers the turn even
        #          when activeTaskId was never persisted (the mid-stream-crash
        #          case that used to orphan the interrupted content entirely).
        conv_rows = db.execute(
            "SELECT id, settings, messages FROM conversations WHERE user_id=1 "
            "AND json_extract(settings, '$.activeTaskId') IS NOT NULL"
        ).fetchall()
        conv_by_id = {r['id']: r for r in conv_rows}

        _missing_ids = [c for c in interrupted_task_by_conv if c not in conv_by_id]
        if _missing_ids:
            _ph = ','.join('?' for _ in _missing_ids)
            for r in db.execute(
                "SELECT id, settings, messages FROM conversations WHERE user_id=1 "
                f"AND id IN ({_ph})", tuple(_missing_ids)
            ).fetchall():
                conv_by_id[r['id']] = r
            logger.info('[Startup] %d interrupted-owning conv(s) had NO activeTaskId '
                        '(recovered via task_results.conv_id): %s',
                        len(_missing_ids), [c[:8] for c in _missing_ids])

        cleared = 0
        for cid, crow in conv_by_id.items():
            try:
                settings = json.loads(crow['settings'] or '{}')
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                continue
            atid = settings.get('activeTaskId')
            # Authoritative merge source: the interrupted task OWNED by this conv
            # (task_results.conv_id) wins over the frontend-synced activeTaskId
            # pointer — the interrupted task is the one that actually holds the
            # recovered content/thinking/toolRounds.
            merge_task_id = interrupted_task_by_conv.get(cid) or atid
            if not merge_task_id and not atid:
                continue
            # Clear the dead pointer if present.
            if atid:
                settings['activeTaskId'] = None
            settings_json = json.dumps(settings, ensure_ascii=False)

            # ── Merge interrupted task data into the conversation messages
            #    (the checkpoint may carry partial content the UI never saw) ──
            task_row = None
            if merge_task_id:
                task_row = db.execute(
                    "SELECT content, thinking, tool_rounds, metadata FROM task_results WHERE task_id=?",
                    (merge_task_id,)
                ).fetchone()

            messages_json = None
            if task_row:
                task_content = task_row['content'] or ''
                task_thinking = task_row['thinking'] or ''
                if task_content or task_thinking:
                    try:
                        messages = json.loads(crow['messages'] or '[]')
                        if messages:
                            last_msg = messages[-1]
                            if last_msg.get('role') == 'assistant':
                                # Only update if task has more content
                                existing_content = len(last_msg.get('content') or '')
                                existing_thinking = len(last_msg.get('thinking') or '')
                                if len(task_content) > existing_content:
                                    last_msg['content'] = task_content
                                if len(task_thinking) > existing_thinking:
                                    last_msg['thinking'] = task_thinking
                                if not last_msg.get('finishReason'):
                                    last_msg['finishReason'] = 'interrupted'
                                # Merge toolRounds from task
                                if task_row['tool_rounds']:
                                    try:
                                        tr = json.loads(task_row['tool_rounds'])
                                        if tr and len(tr) > len(last_msg.get('toolRounds') or []):
                                            last_msg['toolRounds'] = tr
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                # Merge metadata
                                if task_row['metadata']:
                                    try:
                                        meta = json.loads(task_row['metadata'])
                                        if meta.get('model') and not last_msg.get('model'):
                                            last_msg['model'] = meta['model']
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                messages_json = json_dumps_pg(messages)
                            elif last_msg.get('role') == 'user':
                                # Task started but no assistant msg was appended yet
                                new_msg = {
                                    'role': 'assistant',
                                    'content': task_content,
                                    'thinking': task_thinking,
                                    'finishReason': 'interrupted',
                                    'timestamp': int(time.time() * 1000),
                                }
                                if task_row['tool_rounds']:
                                    try:
                                        new_msg['toolRounds'] = json.loads(task_row['tool_rounds'])
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                if task_row['metadata']:
                                    try:
                                        meta = json.loads(task_row['metadata'])
                                        if meta.get('model'):
                                            new_msg['model'] = meta['model']
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                messages.append(new_msg)
                                messages_json = json_dumps_pg(messages)
                    except (json.JSONDecodeError, TypeError) as exc:
                        logger.warning('[Startup] Failed to parse messages for conv=%s: %s',
                                       cid[:8], exc)

            now_ms = int(time.time() * 1000)
            if messages_json:
                from lib.conversations import build_search_text
                messages_parsed = json.loads(messages_json)
                search_text = build_search_text(messages_parsed)
                db.execute(
                    "UPDATE conversations SET settings=?, messages=?, updated_at=?, "
                    "msg_count=?, search_text=? WHERE id=? AND user_id=1",
                    (settings_json, messages_json, now_ms,
                     len(messages_parsed), search_text, cid)
                )
            else:
                db.execute(
                    "UPDATE conversations SET settings=?, updated_at=? WHERE id=? AND user_id=1",
                    (settings_json, now_ms, cid)
                )
            cleared += 1
            logger.info('[Startup] Recovered conv=%s from task=%s '
                        '(activeTaskId_cleared=%s messages_updated=%s)',
                        cid[:8],
                        merge_task_id[:8] if merge_task_id else 'none',
                        bool(atid), bool(messages_json))

        if cleared:
            db.commit()
            logger.info('[Startup] Recovered %d conversation(s) (merged interrupted '
                        'content + cleared any dead activeTaskId)', cleared)

        total = len(stale_rows) + cleared
        if total:
            logger.info('[Startup] ✅ Stale task recovery complete: %d task(s) interrupted, '
                        '%d conv(s) recovered', len(stale_rows), cleared)
            # Invalidate meta cache so first frontend request gets clean data
            try:
                from lib.conversations import invalidate_meta_cache
                invalidate_meta_cache()
            except Exception as e:
                logger.debug('[Startup] meta cache invalidation skipped: %s', e)
        else:
            logger.debug('[Startup] No stale tasks or activeTaskIds found — clean shutdown')

    except Exception as e:
        logger.error('[Startup] Stale task recovery failed (non-fatal): %s', e, exc_info=True)


def cleanup_old_tasks():
    """Drop finished tasks past TTL and prune the conv-latest-task index.

    Notes:
      - Legacy semantics removed tasks based on ``created_at`` regardless of
        finish time. TaskRuntime uses ``finished_at`` instead, which is more
        accurate (a task that ran for 30 minutes shouldn't be wiped 30 minutes
        after starting if it's still streaming).
      - We snapshot the task ids ABOUT to be cleaned BEFORE calling
        cleanup_stale() so we can prune the conv-latest-task index too.
    """
    now = time.time()
    finished_ids: set = set()
    with _chat_runtime._lock:
        for tid, t in _chat_runtime._tasks.items():
            if t['status'] in ('done', 'error', 'aborted'):
                finished_at = t.get('finished_at')
                # Fall back to created_at for tasks that don't have finished_at
                # (e.g. tests that mark status='done' directly without finish()).
                ref_t = finished_at if finished_at else t.get('created_at', 0)
                if now - ref_t > _chat_runtime.ttl:
                    finished_ids.add(tid)
    n = _chat_runtime.cleanup_stale()
    # Clean up _conv_latest_task entries whose tasks were just removed
    if finished_ids:
        with _conv_latest_task_lock:
            stale_convs = [cid for cid, tid in _conv_latest_task.items()
                           if tid in finished_ids]
            for cid in stale_convs:
                del _conv_latest_task[cid]
    if n:
        logger.debug('[Manager] cleanup_old_tasks removed %d tasks', n)
    # ★ Stuck-task backstop (rides the same tick). cleanup_stale only evicts
    #   FINISHED tasks, so a purely-wedged running task (never superseded) would
    #   otherwise live forever with no terminal state. See reap_stuck_running_tasks.
    try:
        reap_stuck_running_tasks()
    except Exception as e:
        logger.warning('[Manager] reap_stuck_running_tasks failed: %s', e, exc_info=True)


# Age (seconds) after which a running task that has produced ZERO output
# (no events, no content, no thinking) is considered wedged and force-failed.
# Env-tunable; 0 disables the backstop. Default 30 min — comfortably longer
# than any legitimate pre-first-token wait (queueing, long tool prep).
def _stuck_task_max_silent_secs() -> int:
    import os
    try:
        return int(os.environ.get('TOFU_STUCK_TASK_MAX_SILENT_SECS', '') or '1800')
    except (ValueError, TypeError):
        return 1800


def reap_stuck_running_tasks() -> int:
    """Force-terminate running tasks that are wedged with zero output.

    Targets the "pure stuck" case the supersede/abort path does NOT cover: a
    task that was never superseded but whose thread is wedged before producing
    anything (the exact shape of the incident that motivated this — a stream
    that received 0 tokens, emitted 0 events, for hours). Left alone, such a
    task stays ``status='running'`` in memory forever and, having never
    finalized, has NO terminal ``task_results`` row → after a restart a poll
    404s and the turn is lost.

    Discriminator (deliberately conservative to avoid killing a task that is
    legitimately BLOCKED ON HUMAN INPUT — ask_user / write-approval / stdin):
    such a task has already emitted at least one event (the phase / tool_call
    for the prompt), so we require **zero events AND zero content AND zero
    thinking** plus an age past the silence threshold. A human-waiting task
    fails the zero-events test and is never reaped.

    Marks the task aborted (reason ``stuck_no_output``) and writes an error
    terminal floor so a poll resolves to a terminal state instead of a 404.
    Returns the number of tasks reaped.
    """
    max_silent = _stuck_task_max_silent_secs()
    if max_silent <= 0:
        return 0
    now = time.time()
    stuck = []
    with tasks_lock:
        for tid, t in tasks.items():
            if t.get('status') != 'running' or t.get('aborted'):
                continue
            # Zero output so far?
            if (t.get('content') or '') or (t.get('thinking') or ''):
                continue
            try:
                with t['events_lock']:
                    n_events = len(t['events'])
            except Exception:
                # No events structure (legacy/malformed) — treat as no output.
                n_events = 0
            if n_events > 0:
                continue
            age = now - t.get('created_at', now)
            if age < max_silent:
                continue
            t['aborted'] = True
            t['_abort_timestamp'] = now
            t['_abort_reason'] = 'stuck_no_output'
            t['status'] = 'error'
            from lib.error_envelope import make_envelope as _make_env
            t['error'] = _make_env(
                'internal',
                detail=('Task produced no output for %d seconds and was '
                        'terminated as stuck.' % int(age)),
                model=(t.get('config') or {}).get('model', '') or '',
                context='stuck-task-reaper',
                source='lib.tasks_pkg.manager',
            )
            t['finishReason'] = 'error'
            t['finished_at'] = now
            stuck.append(t)
    for t in stuck:
        logger.warning('[Task %s] conv=%s ⚠️ STUCK — 0 events/0 content for %.0fs, '
                       'force-failed and writing terminal floor',
                       t['id'][:8], (t.get('convId') or '')[:8],
                       now - t.get('created_at', now))
        _write_stuck_terminal_floor(t)
    if stuck:
        logger.warning('[Manager] reap_stuck_running_tasks force-failed %d wedged task(s)',
                       len(stuck))
    return len(stuck)


def _write_stuck_terminal_floor(task) -> None:
    """Persist a terminal ``status='error'`` row for a reaped stuck task so a
    later poll (even post-restart) resolves terminally instead of 404.

    Best-effort; reuses the shared ``_upsert_task_row`` (keyed on task_id).
    """
    try:
        conv_id = task.get('convId', '') or ''
        meta = build_result_meta(task)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status='error',
                         error_json=error_json, tr_json=None, meta_json=meta_json)
        logger.debug('[Task %s] conv=%s Wrote stuck terminal floor to task_results',
                     task['id'][:8], conv_id[:8])
    except Exception as e:
        logger.warning('[Task %s] Failed to write stuck terminal floor: %s',
                       task.get('id', '?')[:8], e, exc_info=True)

# ── Streaming checkpoint interval (seconds) ──
# During LLM token streaming, we periodically persist partial content to
# the DB so data survives server crashes even when there are no tool rounds.
_STREAM_CHECKPOINT_INTERVAL = 5

def stream_llm_response(task, body, tag='', on_tool_call_ready=None):
    """Stream an LLM response, wiring deltas into the task's event system.

    Delegates all key selection, retry, 429/401/403 failover to the
    central ``dispatch_stream`` — no duplicate logic needed here.

    Args:
        on_tool_call_ready: callback(tool_call_dict) — fired as each tool
            call's arguments finish streaming.  The orchestrator uses this
            to start executing read-only tools while the model is still
            generating the next tool call (streaming tool execution).

    ★ Crash-recovery: periodically checkpoints to DB every ~5s during
    streaming so that even pure-LLM responses (no tool calls) survive
    a server crash with minimal data loss.
    """
    pfx = f'[Task {task["id"][:8]}][{tag}]'
    model = body.get('model', '?')
    # ★ Init to 0.0 (epoch) so the FIRST content/thinking delta checkpoints
    #   immediately, then settle into the _STREAM_CHECKPOINT_INTERVAL cadence.
    #   Starting at time.time() left a pre-first-checkpoint window where a
    #   server crash after the first tokens but before the 5s tick lost the
    #   whole turn. checkpoint_task_partial() no-ops while content+thinking are
    #   still empty, so an early call before any token is harmless. Mirrors the
    #   orchestrator tool-loop's `_last_checkpoint = 0.0` (orchestrator.py).
    _last_stream_ckpt = 0.0

    # ★ Timing: measure time-to-first-token (TTFT) for the FIRST LLM round
    #   of this task only (the "waiting" window the user sees). Anchored to
    #   '_t_prep_done' (set in run_task once context is assembled) and fired
    #   once, on the first content/thinking delta. Guarded so tool-round
    #   re-calls and tasks without the anchor don't re-log.
    _t_request_start = time.time()

    def _log_ttft_once():
        if task.get('_ttft_done'):
            return
        task['_ttft_done'] = True
        _prep_done = task.get('_t_prep_done')
        _now = time.time()
        if _prep_done:
            logger.info('%s [Timing] TTFT=%.3fs (context-ready→first-token), '
                        'request=%.3fs (build_body→first-token) model=%s',
                        pfx, _now - _prep_done, _now - _t_request_start, model)
        else:
            logger.info('%s [Timing] first-token after %.3fs (request) model=%s',
                        pfx, _now - _t_request_start, model)

    def _maybe_checkpoint_during_stream():
        """Called on every content/thinking delta — checkpoint if interval elapsed."""
        nonlocal _last_stream_ckpt
        now = time.time()
        if now - _last_stream_ckpt >= _STREAM_CHECKPOINT_INTERVAL:
            _last_stream_ckpt = now
            try:
                checkpoint_task_partial(task)
            except Exception as e:
                logger.debug('%s streaming checkpoint failed (non-fatal): %s', pfx, e)
            # ── Presence heartbeat (throttled, rides the checkpoint cadence).
            #    Token flow IS work — a long single-LLM turn with no tool rounds
            #    must keep the peer ACTIVE, not flap to idle. One bump per
            #    checkpoint interval (~5s), inside the ACTIVE_TTL window, so no
            #    per-token writes. Best-effort.
            _cfg = task.get('config') or {}
            _pp = _cfg.get('projectPath') or ''
            _cid = task.get('convId') or ''
            if _pp and _cid:
                try:
                    from lib.presence import heartbeat as _presence_heartbeat
                    _presence_heartbeat(_pp, _cid, phase='generating')
                except Exception as e:
                    logger.debug('%s presence heartbeat failed (non-fatal): %s', pfx, e)

    def _on_thinking(td):
        _log_ttft_once()
        with task['content_lock']:
            task['thinking'] += td
        append_event(task, build_event(EventType.DELTA, thinking=td))
        _maybe_checkpoint_during_stream()

    def _on_content(cd):
        _log_ttft_once()
        with task['content_lock']:
            task['content'] += cd
        append_event(task, build_event(EventType.DELTA, content=cd))
        _maybe_checkpoint_during_stream()

    def _on_retry(attempt, reason='', status_code=0):
        """Emit SSE phase event so user sees retry status instead of 'Waiting…'.

        We attach the MODEL name and current cycle count so a long wait
        reveals exactly which key/model is being throttled instead of a
        generic spinner.  Previously users just saw "Waiting…" for 60-120s
        during 429 cycling with no indication that the server was alive
        and actively retrying.
        """
        if status_code == 429:
            # Rate-limit: surface the model clearly and phrase it as a
            # queue wait rather than an error.
            detail = (f'⏳ 模型 {model} 限流中，正在排队重试 '
                      f'(第 {attempt} 次)…')
        elif reason:
            detail = f'Retrying… {reason} ({model}, attempt {attempt})'
        else:
            detail = f'Retrying {model}… (attempt {attempt})'
        append_event(task, build_event(
            EventType.PHASE,
            phase='retrying',
            detail=detail,
            attempt=attempt,
            statusCode=status_code,
            model=model,
        ))

    # ── Consume zero-byte force-rotate signal ──
    # If the previous round zero-byte'd, ``analyse_stream_result`` set
    # ``task['_force_rotate_pair']`` to ``(key_name, model)``.  We pass
    # it as ``avoid_pairs`` to dispatch so the picker steers away from
    # the poisoned slot for THIS attempt only — clear immediately after
    # so a third zero-byte on a different slot doesn't keep the avoid
    # list stuck on the original.
    _avoid_pairs = None
    _rotate_signal = task.pop('_force_rotate_pair', None)
    if _rotate_signal:
        _avoid_pairs = {_rotate_signal}
        logger.info('%s zero-byte force-rotate: avoiding %s:%s for this dispatch',
                    pfx, _rotate_signal[0], _rotate_signal[1])

    # ★ Surface the in-flight request as a live phase BEFORE the first token.
    #   Between a finished tool and the model's next token there is a silent
    #   gap (prompt prefill / TTFT) during which no content/thinking delta
    #   fires — and if the next turn is a tool call with no preamble, nothing
    #   renders until tool_start.  Without this the spinner stays frozen on
    #   the previous "Analyzing results…" label and the task looks hung.
    #   Cleared automatically by the first content/thinking delta, or by
    #   tool_start (hasActiveSearch) on the frontend.
    _model_label = _display_model_name(model)
    append_event(task, build_event(
        EventType.PHASE, phase='waiting_model',
        detail=f'Sent to {_model_label}, waiting for it to start replying…',
        model=model))

    msg, finish_reason, usage = dispatch_stream(
        body,
        on_thinking=_on_thinking,
        on_content=_on_content,
        on_tool_call_ready=on_tool_call_ready,
        abort_check=lambda: task.get('aborted', False),
        prefer_model=model,
        log_prefix=pfx,
        # ★ User-facing request: the user explicitly chose this model in
        #   the frontend preset selector.  429 retries must stay within
        #   this model's slots (different keys / alias group) — never
        #   silently fall back to a cheaper/different model.
        strict_model=True,
        on_retry=_on_retry,
        avoid_pairs=_avoid_pairs,
    )

    # ★ Timing fallback: if the first round was tool-call-only (no content/
    #   thinking deltas fired the TTFT hook), log it now using stream return.
    _log_ttft_once()

    # ★ Propagate provider_id from dispatch metadata into task
    _dispatch = (usage or {}).get('_dispatch', {})
    if _dispatch.get('provider_id'):
        task['provider_id'] = _dispatch['provider_id']

    # ★ Notify user if a model token limit was auto-learned during this request
    _limit_info = (usage or {}).get('_model_limit_learned')
    if _limit_info:
        # Notify via phase event (transient UI status, does NOT pollute
        # assistantMsg.content).  The limit is persisted automatically.
        append_event(task, build_event(
            EventType.PHASE,
            phase='retrying',
            detail=(f'⚙️ Auto-detected model limit: {_limit_info["model"]} '
                    f'max_tokens={_limit_info["new_limit"]:,} '
                    f'(was {_limit_info["old_limit"]:,})'),
        ))
        logger.info('%s ⚙️ Model limit auto-learned and user notified: %s max_tokens=%d',
                    pfx, _limit_info['model'], _limit_info['new_limit'])

    _content_len = len(msg.get('content', '') or '')
    _thinking_len = len(msg.get('reasoning_content', '') or '')
    _tool_calls = len(msg.get('tool_calls', []))
    _provider = task.get('provider_id', '?')
    logger.info('%s conv=%s stream_llm_response complete: finish_reason=%s model=%s '
                'provider=%s content=%dchars thinking=%dchars tool_calls=%d',
                pfx, task.get('convId', ''), finish_reason, model,
                _provider, _content_len, _thinking_len, _tool_calls)

    # ★ Feed authoritative prompt_tokens into the usage cache so the NEXT
    #   round's compaction check returns a bit-exact number instead of
    #   falling back to the CJK-aware heuristic. Inspired by OpenCode's
    #   MessageV2.Assistant.tokens — the provider already told us the
    #   truth, so trust it instead of re-estimating.
    _total_prompt_tokens = 0
    try:
        conv_id = task.get('convId', '') or ''
        # prompt_tokens is OpenAI-shape; Anthropic returns input_tokens.
        _prompt_tokens = 0
        if isinstance(usage, dict):
            _prompt_tokens = int(
                usage.get('prompt_tokens')
                or usage.get('input_tokens')
                or 0
            )
            # Anthropic excludes cache from input_tokens; add it back so
            # _total_prompt_tokens reflects the FULL prompt the provider
            # accepted (which is what we use for context-limit expansion).
            _cw = int(usage.get('cache_creation_input_tokens') or 0)
            _cr = int(usage.get('cache_read_input_tokens') or 0)
            if (_cw or _cr) and _prompt_tokens <= (_cw + _cr):
                _total_prompt_tokens = _prompt_tokens + _cw + _cr
            else:
                _total_prompt_tokens = _prompt_tokens
        if conv_id and _prompt_tokens > 0:
            from lib.token_counter import record_usage
            # ``body['messages']`` is the exact list we sent. Recording it
            # lets the cache detect edit/regenerate (prefix changed →
            # invalidate) vs append-only (reuse + delta).
            record_usage(
                conv_id,
                prompt_tokens=_prompt_tokens,
                model=model,
                message_count=len(body.get('messages') or []),
                messages=body.get('messages'),
            )
    except Exception as e:
        # Usage-cache is a best-effort optimisation — never let a bug
        # here break the LLM return path.
        logger.debug('%s record_usage failed (non-fatal): %s', pfx, e)

    # ★ Auto-learn an EXPANDED context limit when this provider just
    #   accepted a prompt larger than our presumed ceiling. Mirrors the
    #   shrink-on-overflow path in llm_fallback.py.
    if _total_prompt_tokens > 0:
        try:
            from lib.context_limits import learn_expand_from_success
            from lib.tasks_pkg.compaction import _get_context_limit
            _prior_limit = _get_context_limit(task)
            _expand_info = learn_expand_from_success(
                task.get('provider_id') or '',
                model,
                _total_prompt_tokens,
                preset_limit=_prior_limit,
            )
            if _expand_info:
                append_event(task, build_event(
                    EventType.PHASE,
                    phase='retrying',
                    detail=(
                        f'⚙️ Auto-detected larger context window for '
                        f'{model}: '
                        f'{_expand_info["new_limit"]:,} tokens '
                        f'(was {_expand_info["old_limit"]:,})'
                    ),
                ))
                logger.info('%s ⚙️ Context limit expanded: %s %d → %d '
                            '(observed prompt=%d)',
                            pfx, model, _expand_info['old_limit'],
                            _expand_info['new_limit'], _total_prompt_tokens)
        except Exception as e:
            logger.debug('%s context_limits expand-learn failed: %s', pfx, e)

    return msg, finish_reason, usage
