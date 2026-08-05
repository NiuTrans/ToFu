"""Event log append + stable per-message id helpers.

Chat-specific extensions on top of :class:`~lib.task_runtime.TaskRuntime`'s
plain event append: phase tracking, durable persistent event-log rows,
liveness clock, and terminal-notify wiring.

``append_event`` is monkeypatched by MANY tests, so it must remain reachable
and steerable through the package facade.
"""

import uuid

from lib.log import get_logger

from lib.tasks_pkg.manager._state import _chat_runtime

logger = get_logger(__name__)


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
    seen: dict = {}
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        if not m.get('_msgId'):
            m['_msgId'] = str(uuid.uuid4())
            changed = True
        mid = m['_msgId']
        # ★ Duplicate-id heal (pt_e0ea29f2): a conv can end up with TWO array
        #   entries sharing one _msgId (an aborted streaming residue persisted
        #   with the client-minted id, then its retry committing with the SAME
        #   id — measured on conv ms8bx7089s3268: idx1 fr=aborted + idx2
        #   fr=stop, both tmp_196fedef). Every id-keyed consumer (frontend
        #   surgical reconcile / order assertion, PATCH /messages/by-id)
        #   collapses onto the FIRST match. On a duplicate, the EARLIER
        #   (stale, no-longer-live) occurrence is re-minted; the LATEST keeps
        #   the id — the newest occurrence is the live/committed turn the
        #   client reconciles by id (rescue-PUT rebase, translation frames).
        if mid in seen:
            prev = seen[mid]
            logger.warning(
                '[MsgIds] duplicate _msgId %.16s at idx %d and %d — re-minting '
                'the earlier (stale) occurrence; the latest keeps the id',
                mid, prev, i)
            messages[prev]['_msgId'] = str(uuid.uuid4())
            seen[messages[prev]['_msgId']] = prev
            changed = True
        seen[mid] = i
    return changed


def _new_assistant_slot(task):
    """Build a fresh trailing assistant message slot for a task's DB commit.

    Adopts the CLIENT-shipped stable id (``task['_assistantMsgId']``, minted in
    the browser before the send POST and shipped as ``config.assistantMsgId``)
    as the slot's ``_msgId`` — instead of letting ``_assign_message_ids`` mint a
    DIFFERENT server UUID. This is the assistant-side analogue of the user-side
    fix in ``build_user_msg_from_payload`` (turn_builder.py): if the ids diverge,
    the live frontend bubble (which carries the ``tmp_`` client id) is never
    recognised as the SAME message as the committed row on a reconnect / rescue
    PUT, so the frontend appends it a SECOND time → duplicate assistant bubbles.
    Preserving the id makes server and client agree on one identity for the turn.

    Empty ``_assistantMsgId`` (headless / external / legacy callers that never
    shipped one) falls through with NO ``_msgId``; ``_assign_message_ids`` then
    mints a UUID as before — no regression for those paths.
    """
    slot = {'role': 'assistant', 'content': '', 'thinking': ''}
    _amid = (task or {}).get('_assistantMsgId')
    if _amid:
        slot['_msgId'] = _amid
    return slot


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

    # ★ Per-task wire transform (2026-07-26, VU-carrier stream contract).
    #   A VU carrier sub-task installs ``_vu_event_transform`` so its OWN
    #   stream / push channel / persisted event log all carry the VU
    #   envelope (wrapped ``autopilot_vu_event`` frames + verbatim
    #   lifecycle frames), never the raw inner agent turn — the client that
    #   hops onto the carrier stream after the parent's done must see the
    #   SAME contract the parent stream carried. The transform returns the
    #   frame to emit, or ``None`` to drop it from the stream entirely.
    #   Facade bookkeeping below (phase tracking / liveness / done-flush)
    #   deliberately keeps reading the RAW event, so a wrapped ``phase``
    #   still updates task['phase'] for the poll fallback.
    _wire = event
    _xform = task.get('_vu_event_transform')
    if _xform is not None:
        try:
            _wire = _xform(task, event)
        except Exception as e:
            logger.warning('[Task] _vu_event_transform failed task=%s: %s — '
                           'emitting raw frame', task['id'][:8], e)
            _wire = event

    if _wire is not None:
        # ★ Durable-before-visible ordering: the persistent task_events row MUST
        #   commit before the frame is pushed to the client, so a cold reconnect
        #   folding the log (event_fold.fold_cold_state_text) can never be behind
        #   the bytes the client already holds. We hand the persist to the
        #   runtime's before_push hook (fired after seq assignment, before push).
        #   Best-effort: a DB blip is logged, never blocks the stream.
        def _persist_before_push(_seq):
            from lib.tasks_pkg.event_log import append_persistent_event
            append_persistent_event(task['id'], _seq, _wire)

        seq = _chat_runtime.append_event(task['id'], _wire,
                                         before_push=_persist_before_push)
        if seq is None:
            # Task not in runtime (registered via legacy direct dict insert in
            # tests, etc.) — fall back to direct append for backward compat.
            # We MUST mint a seq ourselves before falling through to event_log
            # persistence below; otherwise append_persistent_event would receive
            # ``event_id=None`` and refuse the row, leaving cold replay with a
            # hole that looks (to the user) like the message disappeared.
            with task['events_lock']:
                seq = len(task['events'])
                _wire['seq'] = seq
                task['events'].append(_wire)
            # Persist BEFORE the fallback push too (same durable-before-visible
            # ordering as the runtime path above).
            try:
                _persist_before_push(seq)
            except Exception as e:
                logger.debug('[Manager] legacy-path persist failed (non-fatal): %s', e)
            try:
                from lib.push import push_event
                push_event('chat', task['id'], _wire)
            except Exception as e:
                logger.warning('[Task] push_event fallback failed task=%s: %s',
                               task['id'][:8], e)

    # ★ Liveness clock #1 (see reap_stuck_running_tasks): REAL progress events
    #   — deltas / tool results / tool stdout chunks / retry & waiting phases —
    #   bump _t_last_event. A rate-limited-but-alive turn keeps emitting retry
    #   phases, so this stays fresh and the reaper never mistakes it for wedged.
    #   (Clock #2, _dispatch_heartbeat, is refreshed around live dispatch /
    #   model waits / ratified human-wait tools.)
    #
    # ★ EVIDENCE GRADING (owner ruling 2026-07-31, pt_8524e0ec): an event
    #   carrying ``_selfTick`` is the tool-heartbeat pinging ITSELF — it keeps
    #   the SSE transport non-silent but proves NOTHING about the tool being
    #   alive, so it must NOT bump this clock. Before the grading, a hung
    #   run_command (2.5h of zero output, task 96c56840) was kept
    #   reap-immune by its own heartbeat ticks. Human-wait serial tools
    #   (ask_human / await_task(wait) / timer_create) emit UNMARKED ticks —
    #   their ratified exemption is preserved byte-for-byte.
    import time
    if not event.get('_selfTick'):
        task['_t_last_event'] = time.time()

    # ★ Track phase in task for polling fallback
    if event.get('type') == 'phase':
        p = {'phase': event['phase'], 'detail': event.get('detail', '')}
        # ★ i18n plumb: forward the stable detailKey (+ optional detailArgs) so
        #   the poll-fallback consumer localizes the label the same way the
        #   live SSE consumer does. Empty/absent keys fall back to `detail`.
        if event.get('detailKey'):
            p['detailKey'] = event['detailKey']
        if event.get('detailArgs'):
            p['detailArgs'] = event['detailArgs']
        if event.get('toolContext'): p['toolContext'] = event['toolContext']
        if event.get('toolContextTools'):
            p['toolContextTools'] = event['toolContextTools']
        if event.get('tools'): p['tools'] = event['tools']
        # The PHASE wire event now carries the unified canonical `roundNum`
        # (Phase 3 §5); the poll-fallback phase dict keeps its local `round`
        # key (what the frontend phase render reads as buf.phase.round).
        if event.get('roundNum'): p['round'] = event['roundNum']
        task['phase'] = p
    elif event.get('type') == 'delta':
        task['phase'] = None  # Clear phase when LLM starts producing tokens
    elif event.get('type') in ('done', 'error', 'aborted'):
        # ★ Terminal events retire the phase snapshot. Previously ONLY deltas
        #   cleared it, so a task that ended while its last phase was still up
        #   (killed mid-compaction-summary, error right after a retrying beat)
        #   kept serving that live-looking phase to the poll lane / cold replay
        #   FOREVER — the multi-hour stale "compressing context…" HUD
        #   (measured 2026-08-01: 20:10's compacting phase still on a bubble
        #   at 22:22, epic pt_f222e9ed). A finished task has no current phase.
        task['phase'] = None
    elif (event.get('type') == 'compaction_done'
          and isinstance(task.get('phase'), dict)
          and task['phase'].get('phase') == 'compacting'):
        # The compacting phase's OWN terminal: fold it the moment the
        # compaction lands rather than leaving it up until the next round's
        # phase event (or forever, if the task dies in between). Only fold a
        # phase that IS compacting — never clobber an unrelated live phase.
        task['phase'] = None

    # ★ Persistence now happens in _persist_before_push (durable-before-visible
    #   ordering, above) — the row is committed BEFORE the client push, not
    #   after. Only the terminal flush_pending remains here (no-op for API
    #   compat; harmless if the persist raced).
    if event.get('type') == 'done':
        try:
            from lib.tasks_pkg.event_log import flush_pending
            flush_pending(task['id'])
        except Exception as e:
            logger.debug('[Manager] flush_pending failed (non-fatal): %s', e)

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
