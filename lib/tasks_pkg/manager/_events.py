"""Event log append + stable per-message id helpers.

Chat-specific extensions on top of :class:`~lib.agent_core.task_runtime.TaskRuntime`'s
plain event append: phase tracking, durable persistent event-log rows,
liveness clock, and terminal-notify wiring.

``append_event`` is monkeypatched by MANY tests, so it must remain reachable
and steerable through the package facade.
"""

from contextlib import nullcontext
import time
import uuid

from lib.conversation_sync.attempt_identity import is_conversation_attempt
from lib.log import get_logger

from lib.tasks_pkg.manager.runtime import chat_task_runtime

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
        # Duplicate-id heal (): a conv can end up with TWO array
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


def reset_task_text(task, *, content='', thinking=''):
    """Replace live text and advance the snapshot generation atomically."""
    # Runtime-owned tasks always carry the lock. Legacy/recovered task-shaped
    # mappings (and extension/test seams) may predate that field; they are not
    # live stream writers, so a no-op guard preserves read/reset compatibility
    # without mutating the mapping just to inspect it.
    with task.get('content_lock') or nullcontext():
        task['content'] = content
        task['thinking'] = thinking
        epoch = int(task.get('_contentEpoch') or 0) + 1
        task['_contentEpoch'] = epoch
    return epoch


def snapshot_task_text(task):
    """Return content, thinking, and generation from one locked snapshot."""
    with task.get('content_lock') or nullcontext():
        return (
            task.get('content') or '',
            task.get('thinking') or '',
            int(task.get('_contentEpoch') or 0),
        )


def _probe_durable_next_seq(task_id):
    """One past the task's highest durable event sequence, or None on failure.

    Adoption seeds ``_eventNextSeq`` from this so a re-registered task never
    re-mints a sequence the durable log already owns (the 2026-08-21
    withholding-push flood: live-but-unregistered tasks took the legacy
    fallback, re-minted ``seq = len(events)`` from a TRIMMED in-memory list,
    and every frame collided with the original run's storage_events rows —
    'Event sequence has a conflicting payload' — so every authoritative push
    was withheld and the client froze until refresh). ``None`` means "cannot
    seed safely right now" (the Sidecar authority is unreadable), so the
    caller withholds this frame and retries adoption on the next one.
    """
    try:
        from lib.storage import get_storage_client
        row = get_storage_client().query(
            'event.latest', {'task_id': task_id})
        return int(row['sequence']) + 1 if row else 0
    except Exception as e:
        logger.debug('[Manager] durable seq probe failed task=%s: %s',
                     (task_id or '?')[:8], e)
        return None


def _try_readopt_task(task) -> bool:
    """Re-register a live chat task that fell out of the chat runtime.

    Instead of letting a detached task dictionary become a second sequence
    authority, seed
    the task's next seq from the durable log and re-adopt it into the
    runtime, so every subsequent frame flows through the normal monotonic
    path — no payload conflicts, pushes resume.

    Refuses: terminal tasks (finished work must not resurrect as a phantom
    'running' row), ``discard_task``-tombstoned dicts (the autopilot VU
    carrier's unregister is BY DESIGN), and tasks whose durable seq cannot
    be probed right now (an unseeded adopt would mint straight into the
    conflict range).
    """
    task_id = str((task or {}).get('id') or '')
    if not task_id:
        return False
    if task.get('status') in ('done', 'error', 'aborted'):
        return False
    if task.get('_discarded_at'):
        return False
    seeded = _probe_durable_next_seq(task_id)
    if seeded is None:
        return False
    with task.get('events_lock') or nullcontext():
        retained = task.get('events')
        if retained:
            try:
                retained_next = int(retained[-1].get('seq', -1)) + 1
            except (AttributeError, TypeError, ValueError, OverflowError):
                retained_next = -1
            if retained_next < seeded:
                # The retained tail was fallback-minted from a trimmed list
                # and would re-enter the durable-owned range — the exact
                # conflict flood being closed. The runtime reconcile prefers
                # a retained event's seq over the private hint, so reset the
                # replay window: durable rows are the truth (these frames
                # were withheld from clients anyway) and the next poll
                # reports a cursor reset.
                logger.warning('[Manager] task %s retained %d fallback-minted '
                               'event(s) diverged from the durable log '
                               '(next=%d < seed=%d) — resetting the replay '
                               'window', task_id[:8], len(retained),
                               retained_next, seeded)
                retained.clear()
        if not task.get('events'):
            task['_eventBaseSeq'] = seeded
            task['_eventNextSeq'] = seeded
        else:
            try:
                task['_eventNextSeq'] = max(
                    seeded, int(task['events'][-1].get('seq', seeded - 1)) + 1)
            except (AttributeError, TypeError, ValueError, OverflowError):
                task['_eventNextSeq'] = seeded
    if not chat_task_runtime.adopt(task):
        return False
    logger.warning('[Manager] re-registered live task %s into the chat '
                   'runtime (seeded _eventNextSeq=%d) — it was unregistered '
                   'while still emitting events', task_id[:8], seeded)
    return True


def append_event(task, event):
    """Append an event to the task's event log (chat-specific behaviour).

    Chat extends the runtime's plain append with:
      1. Phase tracking on task['phase'] (polling fallback consumer).
      2. Persistent event_log row for durable Last-Event-ID replay across
         cleanup_old_tasks + server restart.

    The runtime takes care of ``events`` append, the ``events_lock``, and
    pushing to the 'chat' WebSocket channel.

    Sub-agent proxy tasks (lib/swarm/agent.py::_dispatch_tool) set
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

    # Keep the correlation envelope identical on the TaskRuntime and legacy
    # fallback paths.  ``taskId``/``requestId`` are data fields, never metric
    # labels, so they remain safe for end-to-end diagnostics.
    event.setdefault('taskId', task.get('id', ''))
    if task.get('_requestId'):
        event.setdefault('requestId', task['_requestId'])
    if event.get('type') in ('delta_reset', 'retry_reset'):
        event.setdefault('contentEpoch', int(task.get('_contentEpoch') or 0))

    # Per-task wire transform (2026-07-26, VU-carrier stream contract).
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

    # Track phase in task for polling fallback — AND for the v2 attempt-event
    #   payload.  This block MUST run before the durable-persist section below:
    #   record_task_event snapshots task['phase'] into the v2 event payload, so
    #   a 'phase' frame must land its own phase (not the previous one), and a
    #   'delta'/terminal frame must persist the CLEARED phase (None) so the
    #   frontend folds the stage text exactly when v1 would (pt: v2 lane had
    #   no stream phase text at all — the projection never carried it).
    if event.get('type') == 'phase':
        p = {'phase': event['phase'], 'detail': event.get('detail', '')}
        # i18n plumb: forward the stable detailKey (+ optional detailArgs) so
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
        # Terminal events retire the phase snapshot. Previously ONLY deltas
        #   cleared it, so a task that ended while its last phase was still up
        #   (killed mid-compaction-summary, error right after a retrying beat)
        #   kept serving that live-looking phase to cold replay
        #   FOREVER — the multi-hour stale "compressing context…" HUD
        #   (measured 2026-08-01: 20:10's compacting phase still on a bubble
        #   at 22:22, ). A finished task has no current phase.
        task['phase'] = None
    elif (event.get('type') == 'compaction_done'
          and isinstance(task.get('phase'), dict)
          and task['phase'].get('phase') == 'compacting'):
        # The compacting phase's OWN terminal: fold it the moment the
        # compaction lands rather than leaving it up until the next round's
        # phase event (or forever, if the task dies in between). Only fold a
        # phase that IS compacting — never clobber an unrelated live phase.
        task['phase'] = None

    if _wire is not None:
        _wire.setdefault('taskId', task.get('id', ''))
        if task.get('_requestId'):
            _wire.setdefault('requestId', task['_requestId'])
        if is_conversation_attempt(task):
            _wire.setdefault('conversationId', task.get('convId', ''))
            _wire.setdefault('turnId', task.get('_turnId', ''))
            _wire.setdefault('attemptId', task.get('_attemptId', ''))

    if _wire is not None:
        # Durable-before-visible ordering: the persistent task_events row MUST
        #   commit before the frame is pushed to the client, so a cold reconnect
        #   folding the log (event_fold.fold_cold_state_text) can never be behind
        #   the bytes the client already holds. We hand the persist to the
        #   runtime's before_push hook (fired after seq assignment, before push).
        #   Best-effort: a DB blip is logged, never blocks the stream.
        def _persist_before_push(_seq):
            if (event.get('type') == 'phase'
                    and isinstance(task.get('phase'), dict)):
                # Phase heartbeats repaint by the authoritative event sequence;
                # ``attempt`` remains reserved for actual retries.
                task['phase']['seq'] = _seq
            if task.get('_transientRuntime'):
                return
            if is_conversation_attempt(task):
                from lib.turn_lifecycle import record_task_event
                # One frame = one authority transaction (2026-08-20
                # double-write root fix): the storage_events row rides INSIDE
                # turn.event.record, so the turn projection and the
                # cold-replay log can never diverge (the old two-command
                # dance let one commit while the other timed out — the
                # "conflicting payload" family).  Only a stale/coalesced
                # outcome persists the row standalone, exactly as before.
                outcome = record_task_event(task, _wire, task_event={
                    'task_id': task['id'], 'sequence': _seq, 'event': _wire,
                })
                if (outcome and event.get('type') in
                        ('done', 'error', 'aborted')):
                    # The turn projection is now durably terminal. Translation
                    # may start only after this authority boundary, otherwise
                    # its projection CAS races the final model projection.
                    from lib.translate.terminal import (
                        schedule_terminal_turn_translations,
                    )
                    schedule_terminal_turn_translations(task)
                if outcome != 'carried':
                    from lib.tasks_pkg.event_log import append_persistent_event
                    append_persistent_event(task['id'], _seq, _wire)
                if not outcome:
                    # The conversation authority permanently rejected this attempt
                    # (settled or superseded): this worker is a zombie with no
                    # durable sink. Flag the cooperative abort so the
                    # round-start gate / abort_check stop it at the next poll
                    # instead of letting it flood error.log for hours
                    # (2026-08-17: 5 tasks ran 2h+ past their v2 abort,
                    # ~30k withheld pushes). Idempotent — the same stamp the
                    # abort endpoints plant.
                    if not task.get('aborted'):
                        task['aborted'] = True
                        import time as _time
                        task['_abort_timestamp'] = _time.time()
                        task['_abort_reason'] = 'conversation_attempt_stale_fence'
                        abort_event = task.get('abort_event')
                        if abort_event is not None:
                            abort_event.set()
                        logger.warning('[Task %s] conversation attempt rejected as stale — '
                                       'cooperative abort flagged so the worker '
                                       'unwinds instead of looping',
                                       (task.get('id') or '?')[:8])
                    raise RuntimeError(
                        'conversation event rejected: attempt is stale or no longer current')
            else:
                # Inline/headless tasks have no conversation-attempt row; the
                # standalone event log is their durable replay authority.
                from lib.tasks_pkg.event_log import append_persistent_event
                append_persistent_event(task['id'], _seq, _wire)

        seq = chat_task_runtime.append_event(task['id'], _wire,
                                         before_push=_persist_before_push)
        if seq is None and _try_readopt_task(task):
            # Live task that had fallen out of the registry — re-registered
            # with a durable-aligned seq seed; retry through the runtime's
            # monotonic path instead of the legacy fallback's len() mint.
            seq = chat_task_runtime.append_event(task['id'], _wire,
                                             before_push=_persist_before_push)
        if seq is None:
            # The TaskRuntime owns event sequence allocation. Minting from a
            # detached dict created a second sequence authority and caused
            # durable conflicts after retained windows were trimmed. A live
            # task may recover through _try_readopt_task on a later event; a
            # terminal or deliberately discarded task is simply retired.
            if task.get('status') in ('done', 'error', 'aborted') \
                    or task.get('_discarded_at'):
                logger.debug(
                    '[Manager] ignored event for retired task=%s type=%s',
                    task['id'][:8], event.get('type'))
            else:
                withheld_count = int(task.get('_registryWithheldCount') or 0) + 1
                task['_registryWithheldCount'] = withheld_count
                task.setdefault('_registryWithheldAt', time.time())
                if withheld_count == 1 or withheld_count & (withheld_count - 1) == 0:
                    logger.warning(
                        '[Manager] withheld event for unregistered task=%s '
                        'type=%s count=%d; no alternate sequence authority exists',
                        task['id'][:8], event.get('type'), withheld_count)
            return None

    # Liveness clock #1 (see reap_stuck_running_tasks): REAL progress events
    #   — deltas / tool results / tool stdout chunks / retry & waiting phases —
    #   bump _t_last_event. A rate-limited-but-alive turn keeps emitting retry
    #   phases, so this stays fresh and the reaper never mistakes it for wedged.
    #   (Clock #2, _dispatch_heartbeat, is refreshed around live dispatch /
    #   model waits / ratified human-wait tools.)
    #
    # EVIDENCE GRADING (owner ruling 2026-07-31, ): an event
    #   carrying ``_selfTick`` is the tool-heartbeat pinging ITSELF — it keeps
    #   the SSE transport non-silent but proves NOTHING about the tool being
    #   alive, so it must NOT bump this clock. Before the grading, a hung
    #   run_command (2.5h of zero output, task 96c56840) was kept
    #   reap-immune by its own heartbeat ticks. Human-wait serial tools
    #   (ask_human / await_task(wait) / timer_create) emit UNMARKED ticks —
    #   their ratified exemption is preserved byte-for-byte.
    if not event.get('_selfTick'):
        task['_t_last_event'] = time.time()

    # Persistence now happens in _persist_before_push (durable-before-visible
    #   ordering, above) — the row is committed BEFORE the client push, not
    #   after. Only the terminal flush_pending remains here (no-op for API
    #   compat; harmless if the persist raced).
    if event.get('type') == 'done' and not task.get('_transientRuntime'):
        try:
            from lib.tasks_pkg.event_log import flush_pending
            flush_pending(task['id'])
        except Exception as e:
            logger.debug('[Manager] flush_pending failed (non-fatal): %s', e)

    # The storage-free embed/headless runtime owns a task-local wakeup hook.
    # It is deliberately task data (not a process-global subscriber table),
    # so independent embedded runtimes cannot observe each other's events.
    _transient_notifier = task.get('_transientEventNotifier')
    if callable(_transient_notifier):
        try:
            _transient_notifier()
        except Exception as e:
            logger.debug('[Manager] transient event notify failed task=%s: %s',
                         task['id'][:8], e)

    # Wake any async API handler awaiting this task (event-driven wait,
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
