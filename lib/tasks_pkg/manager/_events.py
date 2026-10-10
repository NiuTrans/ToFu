"""Event log append + stable per-message id helpers.

Chat-specific extensions on top of :class:`~lib.agent_core.task_runtime.TaskRuntime`'s
plain event append: phase tracking, durable persistent event-log rows,
liveness clock, and terminal-notify wiring.

``append_event`` is monkeypatched by MANY tests, so it must remain reachable
and steerable through the package facade.
"""

from contextlib import nullcontext

from lib.agent_core.publication import check_publication
import time
import uuid

from lib.agent_core.events import EventType
from lib.conversation_sync.attempt_identity import is_conversation_attempt
from lib.error_envelope import make_envelope
from lib.log import get_logger
from lib.task_replay import (
    TASK_REPLAY_TERMINAL_EVENT_TYPES,
    TASK_REPLAY_TERMINAL_STATUSES,
)

from lib.tasks_pkg.manager.runtime import chat_task_runtime
from lib.tasks_pkg.manager._provider_ingress_guard import (
    active_provider_ingress_token,
    enqueue_ingress_delivery,
)

logger = get_logger(__name__)


_TERMINAL_TASK_STATUSES = frozenset(TASK_REPLAY_TERMINAL_STATUSES)
_POST_SETTLEMENT_OBSERVER_EVENT_TYPES = frozenset({
    EventType.ROUND_COMMITTED,
    EventType.PREFERENCE_LEARNED,
})

# Detached swarm frames. A spawned sub-agent keeps emitting panel/progress
# events after its spawning turn settles (the turn ends the moment the main
# agent returns; the swarm outlives it by design). Those frames are routed to
# the SPAWNING task dict, whose attempt is now terminal — so without this
# allowlist every swarm frame re-enters the settled attempt authority, is
# rejected by the stale-attempt fence, and plants a cooperative-abort stamp on
# the shared task. The swarm's own abort_check reads that stamp and force-
# cancels every live sub-agent (the mtvj1lls7knjg5 false-positive abort).
# A swarm frame is a pure panel/replay observer — never a turn-authority write
# — so it belongs on the standalone replay log, exactly like the commit-round
# and preference observers above. Gated on terminal task status below.
_SWARM_OBSERVER_EVENT_TYPES = frozenset({
    'swarm_phase',
    'swarm_agent_phase',
    'swarm_agent_progress',
    'swarm_agent_complete',
    'swarm_agent_tool_call',
})


def _is_post_settlement_observer_event(task, event) -> bool:
    """Return whether ``event`` is a sanctioned settled-Turn observer.

    These producers start only after terminal persistence (or, for a detached
    swarm, outlive their settled spawning turn) and never write turn
    authority.  Their event frame remains valuable for live/cold task replay,
    but can never be carried by the already-settled attempt transaction.  Keep
    the allowlist exact so every late model/tool/lifecycle frame still hits
    the stale-attempt fence.
    """
    return (
        task.get('status') in _TERMINAL_TASK_STATUSES
        and event.get('type') in (
            _POST_SETTLEMENT_OBSERVER_EVENT_TYPES | _SWARM_OBSERVER_EVENT_TYPES)
    )


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


def find_message_by_id(messages, msg_id):
    """Locate a message by ``_msgId``. Returns (idx, msg) or (None, None)."""
    if not msg_id or not isinstance(messages, list):
        return None, None
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get('_msgId') == msg_id:
            return i, m
    return None, None


def _strip_base64_for_snapshot(messages):
    """Project bounded public messages for a diagnostic snapshot."""
    stripped = []
    for msg in messages:
        m = dict(msg)
        # A canonical in-process body can carry protocol replay sidecars that
        # provider adapters consume later. They are neither OpenAI message
        # fields nor Request Inspector data and may contain duplicated payloads.
        m.pop('_responses_items', None)
        m.pop('_anthropic_content_blocks', None)
        m.pop('_tofuSameRoleSeam', None)
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
    if task.get('status') in TASK_REPLAY_TERMINAL_STATUSES:
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
    from lib.tasks_pkg.event_delivery import delivery_lane_for
    lane = delivery_lane_for(task)
    if task.get('_eventDeliveryAdmissionError') and event.get('type') not in TASK_REPLAY_TERMINAL_EVENT_TYPES:
        from lib.tasks_pkg.event_delivery import EventDeliveryError
        raise EventDeliveryError('Task event delivery admission previously failed') from (
            lane.error if lane is not None else None)
    with lane.producer_lock if lane is not None else nullcontext():
        current_lane = lane if task.get('_eventDeliveryLane') is lane else None
        return _append_event_ordered(task, event, lane=current_lane)


def _append_event_ordered(task, event, *, lane=None):
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

    check_publication()
    if task.get('_invocationPublicationClosed'):
        raise RuntimeError('Child invocation publication revoked')
    if task.get('_suppressEvents'):
        return

    # Field-level wire contract at the DELIVERY seam: build_event validates
    # kwargs at construction, but emitters legitimately stamp conditional
    # fields by mutation afterwards (status / rejection / compaction fields
    # on tool_complete). This re-checks the final post-mutation frame — the
    # shape that actually reaches the wire. No-op for events without a
    # declared schema (one dict lookup); warn-only in production.
    from lib.agent_core.events import check_event
    from lib.agent_core.execution_clock import stamp_execution_clock
    check_event(event)

    if event.get('type') in TASK_REPLAY_TERMINAL_EVENT_TYPES:
        # Resource settlement precedes every terminal persistence/push. This is
        # the one operational lifecycle seam shared by normal, Flow, reaper,
        # queued-abort, and compatibility entry points. A hard cleanup failure
        # cannot be published as success; durable/TTL-backed recovery is
        # represented by the session receipt as a satisfied ``deferred`` debt.
        try:
            from lib.agent_core.execution_session import settle_task_execution
            receipt = settle_task_execution(task, event=event)
        except ValueError:
            # Explicit legacy/test carriers that bypass TaskRuntime own no
            # request resources and retain their existing terminal projection.
            receipt = None
        if receipt is not None and not receipt.invariants_satisfied:
            envelope = make_envelope(
                'internal', context='execution-settlement',
                source='lib.tasks_pkg.manager._events',
            )
            task['status'] = 'error'
            task['finishReason'] = 'error'
            task['error'] = envelope
            event['finishReason'] = 'error'
            event['error'] = envelope
            logger.error(
                '[Task %s] terminal success refused: execution resource '
                'invariant failed', str(task.get('id') or '?')[:8],
            )

    # Terminal execution includes required resource settlement, but excludes
    # subsequent observer persistence and browser delivery.
    stamp_execution_clock(task, event)

    # Fold the exact canonical event into the bounded phase ledger before
    # provider-ingress isolation can make this frame memory-local. The ledger
    # is presentation evidence only; a failure here must never affect task
    # execution or event delivery.
    _trace_observed_at = int(time.time() * 1000)
    try:
        from lib.tasks_pkg.turn_trace import observe_task_trace_event
        observe_task_trace_event(
            task, event, observed_at_ms=event.get('executionAtMs', _trace_observed_at))
    except Exception as exc:
        logger.debug('[TaskTrace] phase observation skipped task=%s: %s',
                     str(task.get('id') or '?')[:8], exc)

    # Keep the correlation envelope identical on the TaskRuntime and legacy
    # fallback paths.  ``taskId``/``requestId`` are data fields, never metric
    # labels, so they remain safe for end-to-end diagnostics.
    # Fill only a compatible local envelope. A relayed child with just one
    # producer stamp must not acquire the display carrier's other stamp.
    from lib.tool_round_identity import execution_identity
    _producer_attempt, _producer_task = execution_identity(event)
    if ((_producer_task in ('', task.get('id')))
            and (_producer_attempt in ('', task.get('_attemptId')))):
        event.setdefault('taskId', task.get('id', ''))
        if task.get('_attemptId'):
            event.setdefault('attemptId', task['_attemptId'])
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
        p = {
            'phase': event['phase'],
            'detail': event.get('detail', ''),
            # Server wall clock for the browser's transport/render receipt.
            # It lives on the v3 phase snapshot, not on every raw delta.
            'emittedAt': _trace_observed_at,
        }
        # i18n plumb: forward the stable detailKey (+ optional detailArgs) so
        #   the poll-fallback consumer localizes the label the same way the
        #   live SSE consumer does. Empty/absent keys fall back to `detail`.
        if event.get('detailKey'):
            p['detailKey'] = event['detailKey']
        if event.get('detailArgs'):
            p['detailArgs'] = event['detailArgs']
        if event.get('model'):
            p['model'] = event['model']
        if isinstance(event.get('modelRoute'), dict):
            # Flow role routing is user-visible execution policy. Preserve it
            # on the v3 live-phase snapshot so reconnect/poll consumers see
            # the same selected→resolved disclosure as the raw phase frame.
            p['modelRoute'] = dict(event['modelRoute'])
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
    elif event.get('type') in TASK_REPLAY_TERMINAL_EVENT_TYPES:
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
        for _clock_key in ('executionAtMs', 'executionOriginAtMs', 'executionClockId'):
            if _clock_key in event:
                _wire.setdefault(_clock_key, event[_clock_key])
        for _identity_key in ('taskId', 'attemptId'):
            if event.get(_identity_key):
                _wire.setdefault(_identity_key, event[_identity_key])
        if task.get('_requestId'):
            _wire.setdefault('requestId', task['_requestId'])
        if is_conversation_attempt(task):
            _wire.setdefault('conversationId', task.get('convId', ''))
            _wire.setdefault('turnId', task.get('_turnId', ''))

    if _wire is not None:
        # Managed root execution has one task-wide persist→push FIFO.
        # Ordinary frames are admitted as detached snapshots and can lag the
        # executor; critical interaction/checkpoint/terminal fences converge
        # earlier accepted work. Legacy provider ports keep their own guard.
        _ingress_token = active_provider_ingress_token(task)
        from lib.agent_core.events import interaction_types
        barrier = event.get('type') in (
            frozenset(TASK_REPLAY_TERMINAL_EVENT_TYPES) | interaction_types()
            | {'background_command_inject'})
        publication_status = task.get('status')
        projection_source = None
        projection_bytes = 0
        projection_identity = None
        if lane is not None:
            from copy import deepcopy
            _wire = deepcopy(_wire)
            event = deepcopy(event)
            if not barrier and is_conversation_attempt(task):
                from lib.turn_lifecycle import capture_task_projection_source, task_event_projection_is_progress
                from lib.tasks_pkg.event_delivery import coordinator, retained_snapshot_bytes
                with coordinator().condition:
                    previous_source, previous_bytes = lane.projection_source, lane.projection_bytes
                    previous_identity = lane.projection_identity
                projection_source = capture_task_projection_source(
                    task, event, previous_source=previous_source)
                if task_event_projection_is_progress(event) and previous_source is not None:
                    projection_bytes = previous_bytes
                    projection_identity = previous_identity
                else:
                    projection_bytes = retained_snapshot_bytes({
                        key: value for key, value in projection_source.items()
                        if key not in {'content', 'thinking', 'phase'}})

        prepared_durable_wire = None
        def _persist_before_push(_seq, *, preceding_task_events=None):
            nonlocal prepared_durable_wire
            check_publication()
            if task.get('_invocationPublicationClosed'):
                raise RuntimeError('Child invocation publication revoked')
            if (lane is None and event.get('type') == 'phase'
                    and isinstance(task.get('phase'), dict)):
                # Phase heartbeats repaint by the authoritative event sequence;
                # ``attempt`` remains reserved for actual retries.
                task['phase']['seq'] = _seq
            if task.get('_transientRuntime'):
                return
            if is_conversation_attempt(task):
                from lib.tasks_pkg.event_log import (
                    EventAttemptStaleError,
                    append_persistent_event,
                    project_persistent_event,
                    reset_persistent_event_projection,
                )

                def _flag_stale_attempt_zombie():
                    # The conversation authority permanently rejected this attempt
                    # (settled or superseded): this worker is a zombie with no
                    # durable sink. Flag the cooperative abort so the
                    # round-start gate / abort_check stop it at the next poll
                    # instead of letting it flood error.log for hours
                    # (2026-08-17: 5 tasks ran 2h+ past their v2 abort,
                    # ~30k withheld pushes). Idempotent — the same stamp the
                    # abort endpoints plant.
                    if task.get('aborted'):
                        return
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
                # The live frame remains byte-identical. Only its durable
                # storage_events carrier receives the shared storage
                # projection (usage diagnostics stripped; messages snapshots
                # prefix-delta encoded). The August 20 atomic carrier
                # originally bypassed this boundary and reintroduced
                # multi-GiB O(rounds²) snapshot growth.
                if lane is not None:
                    if prepared_durable_wire is None:
                        prepared_durable_wire = project_persistent_event(task['id'], _wire)
                    # A lost ACK can follow a committed prefix-delta frame.
                    # Retrying must retain its exact bytes, even when failure
                    # reset the codec baseline for subsequent events.
                    durable_wire = prepared_durable_wire
                else:
                    durable_wire = project_persistent_event(task['id'], _wire)
                if _is_post_settlement_observer_event(
                        {'status': publication_status} if lane is not None else task, _wire):
                    # Commit-round/profile daemons own dedicated settled-Turn
                    # CAS paths.  The attempt is terminal by construction, so
                    # attempting turn.event.record here can only fail, plant a
                    # false zombie-abort on successful work, and then fall
                    # back to this exact standalone replay append.  Preserve
                    # durable-before-visible ordering without re-entering the
                    # closed attempt authority.
                    append_persistent_event(task['id'], _seq, durable_wire)
                    return
                from lib.turn_lifecycle import record_task_event
                # One frame = one authority transaction (2026-08-20
                # double-write root fix): the storage_events row rides INSIDE
                # turn.event.record, so the turn projection and the
                # cold-replay log can never diverge (the old two-command
                # dance let one commit while the other timed out — the
                # "conflicting payload" family).  Only a coalesced outcome
                # persists the row standalone. Since 2026-09-18 that
                # standalone append carries ``require_attempts_live``: the
                # authority enforces the attempt fence inside the append
                # transaction (replacing a per-frame pre-read that cost one
                # round trip per visible delta), so a genuinely stale
                # (settled/superseded) attempt's late frames are now
                # rejected outright instead of persisted-and-then-aborted.
                try:
                    fence = {}
                    carrier = {'task_id': task['id'], 'sequence': _seq, 'event': durable_wire}
                    if preceding_task_events:
                        carrier['preceding_events'] = preceding_task_events
                    outcome = record_task_event(task, _wire, task_event=carrier, fence_out=fence, **(
                        {'projection_source': projection_source}
                        if projection_source is not None else {}))
                except Exception:
                    # The transaction may not have committed. Force the next
                    # snapshot to be a self-contained baseline rather than a
                    # delta that depends on this uncertain row.
                    reset_persistent_event_projection(
                        task['id'], durable_wire)
                    raise
                if (not outcome and event.get('type') in TASK_REPLAY_TERMINAL_EVENT_TYPES
                        and task.get('_terminalTurnPending')):
                    # A live terminal CAS rejection is still owned by this
                    # executor, not a stale worker or a standalone replay fact.
                    # Keep the atomic carrier for maintenance; never publish a
                    # diagnostic done row ahead of the authoritative Turn.
                    reset_persistent_event_projection(task['id'], durable_wire)
                    raise RuntimeError('terminal Turn settlement remains pending')
                if not outcome:
                    reset_persistent_event_projection(task['id'], durable_wire)
                    from lib.turn_lifecycle import get_attempt, LIVE_ATTEMPT_STATUSES
                    from lib.tasks_pkg.manager._registry import task_user_id
                    from lib.storage.errors import StorageError
                    current_attempt = get_attempt(task['_attemptId'], user_id=task_user_id(task))
                    if (current_attempt['status'] in LIVE_ATTEMPT_STATUSES
                            and current_attempt.get('taskId') in (None, '', task['id'])):
                        # A rejected live CAS is not a revocation witness.
                        # Retain its exact head for repair rather than dropping
                        # the accepted tail or planting a false zombie abort.
                        raise StorageError('database_conflict',
                                           'Live task event authority write was rejected')
                    _flag_stale_attempt_zombie()
                    from lib.tasks_pkg.event_delivery import EventDeliveryRevoked
                    raise EventDeliveryRevoked(
                        'conversation event rejected: attempt is stale or no longer current')
                if (outcome and event.get('type') in
                        TASK_REPLAY_TERMINAL_EVENT_TYPES):
                    # The turn projection is now durably terminal. Translation
                    # may start only after this authority boundary, otherwise
                    # its projection CAS races the final model projection.
                    from lib.translate.terminal import (
                        schedule_terminal_turn_translations,
                    )
                    schedule_terminal_turn_translations(task)
                if outcome != 'carried':
                    try:
                        append_persistent_event(
                            task['id'], _seq, durable_wire,
                            require_attempts_live=(
                                fence.get('require_attempts_live')))
                    except EventAttemptStaleError:
                        # The append transaction proved the attempt stale —
                        # the exact verdict the old per-frame pre-read
                        # produced on this lane. Same fail-closed outcome:
                        # nothing durable, nothing pushed, worker aborted.
                        _flag_stale_attempt_zombie()
                        from lib.tasks_pkg.event_delivery import EventDeliveryRevoked
                        raise EventDeliveryRevoked(
                            'conversation event rejected: attempt is stale or no longer current')
                if not outcome:
                    _flag_stale_attempt_zombie()
                    from lib.tasks_pkg.event_delivery import EventDeliveryRevoked
                    raise EventDeliveryRevoked(
                        'conversation event rejected: attempt is stale or no longer current')
            else:
                # Inline/headless tasks have no conversation-attempt row; the
                # standalone event log is their durable replay authority.
                from lib.tasks_pkg.event_log import append_persistent_event, project_persistent_event
                if lane is not None:
                    if prepared_durable_wire is None:
                        prepared_durable_wire = project_persistent_event(task['id'], _wire)
                    append_persistent_event(task['id'], _seq, prepared_durable_wire)
                else:
                    append_persistent_event(task['id'], _seq, _wire)

        admission_errors = []
        def _publish_ordered_event():
            if chat_task_runtime.push_channel and task.get('_userId'):
                from lib.agent_core.push import push_event
                push_event(chat_task_runtime.push_channel, task['id'], _wire,
                           user_id=int(task['_userId']))

        def _enqueue_before_push(sequence):
            from lib.tasks_pkg.event_delivery import coordinator, retained_snapshot_bytes
            try:
                if event.get('type') == 'phase' and isinstance(task.get('phase'), dict):
                    task['phase']['seq'] = sequence
                    if projection_source is not None and isinstance(projection_source.get('phase'), dict):
                        projection_source['phase']['seq'] = sequence
                own_values = ((_wire,) if projection_source is None else
                    (_wire, projection_source['content'], projection_source['thinking'],
                     projection_source.get('phase')))
                progress_frame = None
                persist_progress = None
                progress_wire_bytes = 0
                progress_payload_bytes = 0
                if (projection_source is not None
                        and task_event_projection_is_progress(event)):
                    from lib.tasks_pkg.event_delivery import ProgressFrame
                    from lib.tasks_pkg.event_log import project_persistent_event
                    progress_frame = ProgressFrame(sequence, _wire, _publish_ordered_event)
                    progress_wire_bytes = retained_snapshot_bytes(_wire) * 2
                    from lib.storage.protocol import canonical_json
                    progress_payload_bytes = len(canonical_json(_wire)) + 256
                    def persist_progress(frames):
                        preceding = [{'task_id': task['id'], 'sequence': frame.sequence,
                            'event': project_persistent_event(task['id'], frame.event)}
                            for frame in frames[:-1]]
                        _persist_before_push(sequence, preceding_task_events=preceding)
                # submit checks the producer fence atomically with FIFO
                # acceptance and transfers deferred contexts to this root.
                # record_task_event still owns the parent's attempt/CAS fence.
                coordinator().submit(lane, sequence,
                    retained_snapshot_bytes(*own_values) + retained_snapshot_bytes(_wire),
                    lambda: _persist_before_push(sequence), _publish_ordered_event,
                    projection_source=projection_source, projection_bytes=projection_bytes,
                    projection_identity=projection_identity, progress_frame=progress_frame,
                    persist_progress=persist_progress, progress_wire_bytes=progress_wire_bytes,
                    progress_payload_bytes=progress_payload_bytes)
                if _ingress_token:
                    from lib.tasks_pkg.manager._provider_ingress_guard import record_deferred_observer_event
                    record_deferred_observer_event(task, token=_ingress_token,
                        sequence=sequence, event_type=event.get('type') or '')
            except Exception as exc:
                task['_eventDeliveryAdmissionError'] = type(exc).__name__
                task['_eventVisibilityOmittedSequence'] = sequence
                task['aborted'] = True
                abort_event = task.get('abort_event')
                if abort_event is not None:
                    abort_event.set()
                admission_errors.append(exc)
                raise

        def _persist_at_barrier(sequence):
            from lib.tasks_pkg.event_delivery import flush_task_events
            # Own terminal debt before waiting; timeout cannot let maintenance
            # publish diagnostics while earlier accepted events are pending.
            terminal = event.get('type') in TASK_REPLAY_TERMINAL_EVENT_TYPES
            if terminal and is_conversation_attempt(task):
                from copy import deepcopy
                task['_terminalTurnPending'] = (deepcopy(_wire), {
                    'task_id': task['id'], 'sequence': sequence,
                    'event': deepcopy(_wire)})
                task['_terminalPersistencePending'] = True
                task['_terminalPersistenceRetryReady'] = True
            flush_task_events(task)
            _persist_before_push(sequence)
            from lib.tasks_pkg.event_delivery import advance_event_visibility
            advance_event_visibility(task, sequence)
            if terminal:
                lane.projection_source = None

        _before_push = (None if _ingress_token else _persist_before_push)
        deliver_push = not bool(_ingress_token) and not callable(task.get('_invocationEventSink'))
        if lane is not None:
            _before_push = _persist_at_barrier if barrier else _enqueue_before_push
            deliver_push = barrier
        seq = chat_task_runtime.append_event(
            task['id'],
            _wire,
            before_push=_before_push,
            deliver_push=deliver_push,
        )
        if (seq is None and not _ingress_token
                and _try_readopt_task(task)):
            # Live task that had fallen out of the registry — re-registered
            # with a durable-aligned seq seed; retry through the runtime's
            # monotonic path instead of the legacy fallback's len() mint.
            # Provider ingress deliberately does not attempt this DB-backed
            # repair: cumulative task content remains intact and the first
            # post-provider event performs the re-adoption/convergence.
            seq = chat_task_runtime.append_event(
                task['id'],
                _wire,
                before_push=_before_push,
                deliver_push=deliver_push,
            )
        if admission_errors:
            raise admission_errors[0]
        if seq is None:
            # The TaskRuntime owns event sequence allocation. Minting from a
            # detached dict created a second sequence authority and caused
            # durable conflicts after retained windows were trimmed. A live
            # task may recover through _try_readopt_task on a later event; a
            # terminal or deliberately discarded task is simply retired.
            if task.get('status') in TASK_REPLAY_TERMINAL_STATUSES \
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
        if _ingress_token and lane is None:
            if (event.get('type') == 'phase'
                    and isinstance(task.get('phase'), dict)):
                task['phase']['seq'] = seq

            def _deliver_ingress_event(_seq=seq, _event_wire=_wire):

                check_publication()
                if task.get('_invocationPublicationClosed'):
                    raise RuntimeError('Child invocation publication revoked')
                try:
                    _persist_before_push(_seq)
                except Exception:
                    # Mirror the runtime's authoritative-frame wedge marker so
                    # chat_poll can still escalate a delivery wedge that now
                    # happens on the delivery worker instead of this thread.
                    task['_pushWithheldAt'] = time.time()
                    task['_pushWithheldCount'] = int(
                        task.get('_pushWithheldCount') or 0) + 1
                    raise
                if task.get('_invocationPublicationClosed'):
                    raise RuntimeError('Child invocation publication revoked')
                withheld_at = task.pop('_pushWithheldAt', None)
                withheld_count = int(task.pop('_pushWithheldCount', None) or 0)
                if withheld_at is not None:
                    # Mirror the runtime's recovery receipt so a wedge that
                    # opened on this delivery worker also closes in the log.
                    logger.warning(
                        '[Manager] ingress persistence recovered task=%s '
                        'seq=%s after %.1fs (%d frames withheld)',
                        task['id'][:8], _seq,
                        time.time() - float(withheld_at), withheld_count)
                if callable(task.get('_invocationEventSink')):
                    from lib.tasks_pkg.invocation import deliver_invocation_event
                    deliver_invocation_event(task, _event_wire)
                if (chat_task_runtime.push_channel and task.get('_userId')
                        and not task.get('_invocationPublicationFence')
                        and not callable(task.get('_invocationEventSink'))):
                    try:
                        from lib.agent_core.push import push_event
                        push_event(
                            chat_task_runtime.push_channel,
                            task['id'],
                            _event_wire,
                            user_id=int(task['_userId']),
                        )
                    except Exception as push_error:
                        logger.debug(
                            '[Manager] ingress delivery push failed task=%s: %s',
                            task['id'][:8], push_error)

            enqueue_ingress_delivery(
                task,
                token=_ingress_token,
                sequence=seq,
                event_type=event.get('type') or '',
                deliver=_deliver_ingress_event,
            )

    if (_wire is not None and not _ingress_token
            and callable(task.get('_invocationEventSink'))):
        # Semantic child handoff is not a transient wakeup or push observer.
        # Run it outside TaskRuntime.before_push (which may withhold errors).
        from lib.tasks_pkg.invocation import deliver_invocation_event
        deliver_invocation_event(task, _wire)

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
        _is_terminal = (
            event.get('type') in TASK_REPLAY_TERMINAL_EVENT_TYPES
            or task.get('status') in TASK_REPLAY_TERMINAL_STATUSES
        )
        notify_task(task['id'], terminal=_is_terminal)
    except Exception as e:
        logger.debug('[Manager] admission notify failed task=%s: %s',
                     task['id'][:8], e)
