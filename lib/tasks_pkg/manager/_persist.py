"""Task-result persistence, metadata building, and heavy-state release.

``persist_task_result`` and ``_upsert_task_row`` are monkeypatched by tests and
MUST stay facade-reachable + steerable.
"""

import json

from lib.conversation_sync.attempt_identity import is_conversation_attempt
from lib.error_envelope import to_json as _err_to_json
from lib.log import get_logger
from lib.tasks_pkg.manager._events import snapshot_task_text
from lib.storage_projection import (
    _USAGE_TRANSIENT_KEYS,  # noqa: F401 — manager facade re-export
    _sanitize_api_rounds_for_persist,
    _sanitize_usage_for_persist,
    _trim_round_for_persist,
)

logger = get_logger(__name__)


def _tool_rounds_have_dedicated_home(task):
    """True when the turn projection durably owns the task's tool rounds.

    Conversation attempts fold tool rounds into their authoritative turn on
    every structural/terminal event.  Duplicating a potentially multi-MiB blob
    in ``task_results`` would create a second recovery authority.

    Inline and headless tasks have no turn projection, so ``task_results`` is
    their sole durable home and must retain the blob.
    """
    return is_conversation_attempt(task)


def terminal_state_log_summary(task, *, persisted: bool):
    """Return a compact one-line summary of a task's IN-MEMORY terminal state.

    The finish-bar fields (finishReason / usage / apiRounds / cost) are computed
    in memory during finalization but only reach the DB if the checkpoint /
    persist write succeeds. When that write throws — the classic case is
    ``task_results`` never being written because the connection pool is
    exhausted (400/400) — the row is absent and every recovery path renders an
    empty finish-bar with no way to tell WHY from the logs alone. This summary
    is emitted UNCONDITIONALLY on the failure branches (see ``persist_task_result``
    and ``checkpoint_task_partial``) so the terminal metadata that failed to
    persist is still recoverable from ``error.log``, and ``persisted=False``
    records the fact that it did not reach the DB.

    Best-effort and allocation-cheap: numbers/sizes only, never the multi-KB
    content/thinking blobs.
    """
    try:
        usage = task.get('usage') or {}
        cost = task.get('cost') or {}
        api_rounds = task.get('apiRounds') or []
        return (
            'finishReason=%s model=%s provider=%s content=%dchars thinking=%dchars '
            'usage=%s(in=%s,out=%s) apiRounds=%d cost=%s persisted=%s' % (
                task.get('finishReason') or 'none',
                task.get('model') or '?',
                task.get('provider_id') or '?',
                len(task.get('content') or ''),
                len(task.get('thinking') or ''),
                bool(usage),
                usage.get('inputTokens', usage.get('input_tokens', '?')),
                usage.get('outputTokens', usage.get('output_tokens', '?')),
                len(api_rounds) if isinstance(api_rounds, list) else 0,
                cost.get('costCny', 'none') if isinstance(cost, dict) else 'none',
                persisted,
            )
        )
    except Exception as _e:
        return 'terminal-summary-unavailable(%s)' % (_e,)


def build_result_meta(task):
    """Build the persisted-result metadata dict from a finished task.

    Extracted so the autopilot hook can sync the parent's final assistant
    message to the conversation DB BEFORE it appends the virtual-user turn
    and spawns the follow-up — otherwise the follow-up registers as the
    conversation's latest task and the later persist_task_result sync is
    dropped by the freshness guard, freezing the parent reply at its last
    streaming checkpoint (truncated, finishReason=None).
    """
    if task.get('_costExperiment') and not task.get('costExperiment'):
        try:
            from lib.cost_experiments import build_task_cost_experiment_outcome
            _outcome = build_task_cost_experiment_outcome(task)
            if _outcome:
                task['costExperiment'] = _outcome
        except Exception as _xe:
            logger.warning('[CostExperiment] pre-persist outcome failed '
                           '(non-fatal): %s', _xe, exc_info=True)

    meta = {'contentEpoch': int(task.get('_contentEpoch') or 0)}
    if is_conversation_attempt(task):
        # Startup's legacy task-result recovery must settle this executor row
        # but must not merge it back into the archived messages JSON. The
        # attempt/turn tables already own that durable projection.
        meta['turnId'] = task.get('_turnId') or ''
        meta['attemptId'] = task.get('_attemptId') or ''
    if task.get('finishReason'): meta['finishReason'] = task['finishReason']
    if task.get('usage'): meta['usage'] = _sanitize_usage_for_persist(task['usage'])
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
    if task.get('_affinityKey'): meta['affinityKey'] = task['_affinityKey']
    if task.get('_reconnectable'): meta['reconnectable'] = True
    if task.get('_userId') not in (None, ''):
        meta['userId'] = str(task['_userId'])
    if task.get('costExperiment'):
        meta['costExperiment'] = task['costExperiment']
    elif task.get('_costExperiment'):
        # Assignment is persisted even if outcome construction later fails;
        # reports can then distinguish an unobserved turn from non-enrollment.
        meta['costExperiment'] = task['_costExperiment']
    if task.get('_responsesItems'):
        meta['_responsesItems'] = task['_responsesItems']
    if task.get('_anthropicContentBlocks'):
        meta['_anthropicContentBlocks'] = task['_anthropicContentBlocks']
    if task.get('programRuns'):
        # Canonical PTC state for headless/task-results consumers. Regular chat
        # messages also persist this as a top-level assistant field.
        meta['programRuns'] = task['programRuns']
    if task.get('_toolOrchestrationDecisions'):
        # Persist the bounded provider-neutral decision projection.  It keeps
        # wire availability distinct from real program/agent trajectories and
        # derives adoptionStatus from canonical runtime state on every write.
        from lib.orchestration_adoption import (
            public_orchestration_decisions)
        meta['toolOrchestrationDecisions'] = (
            public_orchestration_decisions(task))
    if task.get('_todoState'):
        # Versioned checklist stack. Raw todo_write rounds remain the audit log;
        # this compact sidecar is the recovery/current-state authority.
        from lib.tools.todo import public_todo_state
        meta['todoState'] = public_todo_state(task['_todoState'])
    if task.get('_todo_blocked'):
        meta['todoBlocked'] = task['_todo_blocked']
    if task.get('apiRounds'): meta['apiRounds'] = _sanitize_api_rounds_for_persist(task['apiRounds'])
    if task.get('modifiedFiles'): meta['modifiedFiles'] = task['modifiedFiles']
    if task.get('modifiedFileList'): meta['modifiedFileList'] = task['modifiedFileList']
    # Orchestration flow per-node run trace (resolved brief + bounded I/O per
    # node) — persisted so the canvas/inspector overlay survives reload /
    # server restart, served via /api/v1/chat/flow-trace/<task>.
    if task.get('_flow_trace'): meta['flowTrace'] = task['_flow_trace']
    if task.get('_flow_label'): meta['flowLabel'] = task['_flow_label']
    # Flow-run metadata remains useful to headless task-result consumers. The
    # conversation transcript itself is projected independently by Sync v3.
    if task.get('flow_mode'):
        meta['flowMode'] = True
        meta['flowPhase'] = task.get('_flow_phase', 'working')
        meta['flowIteration'] = task.get('_flow_iteration', 0)
        meta['flowProjection'] = task.get('_flow_projection', 'flow')
        for key, value in (task.get('_flow_current_turn') or {}).items():
            if key in ('turnRole', 'emits', 'vuMsgId', 'autopilotRunId'):
                meta[key] = value
        if task.get('_flow_stop_reason'):
            meta['flowStopReason'] = task['_flow_stop_reason']
    return meta


# ── Persisted-payload trimming: drop transient/diagnostic bloat ──────────
#
# Three fields balloon the persisted conversation JSON without any value once
# a turn is done — they are transient streaming buffers or backend-only
# diagnostics that no render path reads. Left in place they inflate a single
# conversation to 100+ MB, so the browser exhausts memory the moment it loads
# and renders it (proven: mr80gsd8rywph9 = 121 MB, dominated by usage._wire_fp).
# We strip them at the DB persist boundary (and mirror the strip on the
# frontend PUT + IndexedDB cache) so the authoritative store never carries them.
#
#   1. usage._wire_fp / _wire_static — the post-translation wire fingerprint
#      (a ~226 KB canonicalized-message LIST per round). Captured in
#      lib/llm/_sse_core.py purely for same-run cache-miss diagnosis by
#      lib/tasks_pkg/cache_tracking.py, which keeps its OWN in-memory copy
#      (prev.wire_fp). NO frontend code reads usage._wire_fp — grep-verified.
#   2. toolRounds[]._partialOutput — the live run_command terminal buffer that
#      grows during streaming. Once the round is done the authoritative output
#      lives in results[0].output / toolContent; _partialOutput is dead weight
#      (18 MB in mqxbemdr7asicp while toolContent was 2 KB). The render path
#      uses toolContent, never _partialOutput, on a completed round.
#
# These two are dropped unconditionally on persist. Inline base64 image URIs
# (toolRounds[].results[].imageDataUris[].uri) are ALSO multi-MB but ARE the
# render source, so they are handled on the frontend cache side (strip from the
# IndexedDB copy, keep in the live/DB copy) — not here.

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
    # The shallow-copy is thread-safety (see docstring); layer the persist
    # trim on top so a DONE round's transient _partialOutput buffer never
    # reaches the DB. _trim_round_for_persist returns dict(r) when it strips,
    # so it subsumes the shallow copy for those rounds.
    return [_trim_round_for_persist(dict(r)) if isinstance(r, dict) else r
            for r in merged]


def _upsert_task_row(task, conv_id, *, content, thinking, status,
                     error_json, tr_json, meta_json, segments_json=None):
    """CAS-write one durable task checkpoint through the storage authority.

    Recovery-owned ``interrupted`` rows and every terminal row fence stale
    running writers. A conversation-backed task is also owner-scoped and is
    discarded when its parent was deleted; inline tasks need no parent.
    """
    if task.get('_transientRuntime'):
        return True

    import time
    from lib.identity import require_user_id
    from lib.storage import StorageError, get_storage_client

    owner_user_id = require_user_id(
        task.get('_userId'), context='task result checkpoint')
    client = get_storage_client(write=True)
    if (conv_id and not task.get('_inline_messages')
            and not client.query('conversation.get', {
                'conv_id': conv_id, 'user_id': owner_user_id})):
        logger.info('[Task %s] conv=%s skipping task result: parent absent',
                    task['id'][:8], conv_id[:8])
        return False

    written_at = int(time.time() * 1000)
    transient_error = None
    for attempt in range(5):
        current = client.query(
            'record.get', {'namespace': 'task_results', 'key': task['id']})
        current_value = (current or {}).get('value') or {}
        if current_value.get('status') == 'interrupted':
            logger.warning('[Task %s] task result fenced after recovery',
                           task['id'][:8])
            return False
        if status == 'running' and current_value.get('status') not in (None, 'running'):
            logger.warning('[Task %s] running checkpoint cannot regress %s',
                           task['id'][:8], current_value.get('status'))
            return False
        value = {
            'task_id': task['id'],
            'conv_id': conv_id,
            'user_id': owner_user_id,
            'content': content,
            'thinking': thinking,
            'error': error_json,
            'status': status,
            'tool_rounds': tr_json,
            'metadata': meta_json,
            'segments': segments_json,
            'created_at': int(task.get('created_at', time.time()) * 1000),
            'completed_at': written_at,
        }
        for tombstone_key in ('abort_requested_at', 'abort_source'):
            if tombstone_key in current_value:
                value[tombstone_key] = current_value[tombstone_key]
        try:
            client.command(
                'task_results.checkpoint', {
                    'key': task['id'],
                    'value': value,
                    'expected_version': int((current or {}).get('version') or 0),
                },
                None,
            )
            return True
        except StorageError as exc:
            if exc.code == 'database_conflict':
                continue
            if exc.code not in {
                'database_busy', 'database_timeout', 'database_unavailable',
            }:
                raise
            transient_error = exc
            logger.warning('[Task %s] task result write attempt %d/5 failed: %s',
                           task['id'][:8], attempt + 1, exc)
            if attempt < 4:
                time.sleep(0.05 * (attempt + 1))
    if transient_error is not None:
        raise transient_error
    logger.warning('[Task %s] task result CAS contention', task['id'][:8])
    return False


# Heavy INPUT fields pinned on the task dict that have NO reader after the
# turn reaches a terminal state. They are the dominant grow-with-conversation
# retainers (measured 2026-07-11: essentially all of the ~3.3 GB private-dirty
# heap is per-task state, not import baseline). ``messages`` is the full API
# input context (whole conversation), ``_flow_turns`` the per-turn Flow
# snapshots. Both are consumed DURING the turn; every POST-terminal reader
# (chat_poll DB path, killed-recovery, reconcile) rebuilds from the DB
# (``task_results`` / ``conversations``), never from these in-memory copies.
# Released at the terminal persist chokepoint so a finished task no longer pins
# a whole conversation's worth of bytes for the ttl=3600s retention window
# (and forever, for the never-evicted carriers). ``events`` is deliberately
# KEPT — a reconnecting SSE client replays from the absolute cursor within the
# retained TTL window. The async profile-consolidation daemon captures ``messages``
# by its own reference arg (spawned by the orchestrator BEFORE this runs), so
# nulling the dict key here frees the bytes exactly when that daemon finishes,
# not at task-TTL — strictly better.
_HEAVY_TERMINAL_FIELDS = ('messages', '_flow_turns')


def _release_heavy_task_state(task) -> int:
    """Null the heavy input fields on a TERMINAL task. Returns count released.

    No-op unless the task is terminal (defensive: never strip a task that
    could still stream). Best-effort — never raises into the persist path.
    """
    try:
        if task.get('status') not in ('done', 'error', 'aborted'):
            return 0
        released = 0
        for f in _HEAVY_TERMINAL_FIELDS:
            if task.get(f):
                task[f] = None
                released += 1
        if released:
            # The 60-second maintenance owner coalesces completions and calls
            # malloc_trim outside the latency-sensitive terminal persist path.
            try:
                from lib.tasks_pkg.manager._maintenance import (
                    request_released_task_heap_trim,
                )
                request_released_task_heap_trim()
            except Exception as trim_error:
                logger.debug(
                    '[Task %s] terminal heap-trim request skipped: %s',
                    (task.get('id') or '')[:8], trim_error)
        return released
    except Exception as e:
        logger.debug('[Task %s] heavy-state release skipped: %s',
                     (task.get('id') or '')[:8], e)
        return 0


def _stamp_conv_provider_id(task):
    """Persist the provider that ACTUALLY served this task into conv settings.

    The context gauge's limit lookup keys on ``provider::model``
    (``runtimeScope._contextPolicy.per_model``), and the frontend resolves
    the provider from ``conv.provider_id`` — which legacy conversations never
    recorded (settings.provider_id was NULL fleet-wide), so the gauge showed
    "—" forever whenever the global ``config.provider_id`` fallback missed.
    The authoritative provider is only known AFTER dispatch (fallback chains
    can land on a different slot than requested), so the terminal persist
    chokepoint is the earliest honest writer.

    Value-only write: an unchanged value returns False from the mutate and
    skips the UPDATE + cache invalidation entirely. Best-effort — a stamp
    failure must never break result persistence.
    """
    provider_id = task.get('provider_id')
    conv_id = task.get('convId') or ''
    if not provider_id or not conv_id or task.get('_inline_messages'):
        return
    try:
        # update_conversation_settings — the MUTATOR api. (The sibling
        # set_conversation_settings takes a plain dict of updates; handing it
        # this callback made dict.update(function) raise
        # "'function' object is not iterable" on every terminal persist.)
        from lib.conversations import update_conversation_settings

        def _stamp(settings):
            if settings.get('provider_id') == provider_id:
                return False
            settings['provider_id'] = provider_id
            return True

        # notify=False: provider_id is invisible metadata — the sidebar/push
        # must not churn for it. The local meta-cache invalidation still runs
        # (structural, inside the gate), so the next settings read is fresh.
        from lib.tasks_pkg.manager._registry import task_user_id
        update_conversation_settings(
            conv_id,
            _stamp,
            user_id=int(task_user_id(task)),
            notify=False,
        )
    except Exception as e:
        logger.warning('[Task %s] conv=%s provider_id settings stamp failed '
                       '(non-fatal): %s',
                       (task.get('id') or '')[:8], conv_id[:8], e)


def persist_task_result(task, *, _defer_heavy_release: bool = False):
    """Terminal result persist. Keyword-only ``_defer_heavy_release``:

    When True, SKIP the trailing ``_release_heavy_task_state`` (the caller
    releases later). Used by ``_finalize_and_emit_done``, which must persist
    the terminal row BEFORE the autopilot hook — a VU sub-task runs INLINE
    inside that hook and can hang indefinitely (measured 2026-07-31: task
    752273db's row stayed 'running' 2h57m while its VU sub-task sat in a
    wedged run_command), so anything the parent owes the world must land
    first. But the VU also reads ``task['messages']`` — so the heavy-state
    release moves to AFTER the hook instead of riding this call."""
    if task.get('_transientRuntime'):
        if not _defer_heavy_release:
            _release_heavy_task_state(task)
        return True

    content, thinking, content_epoch = snapshot_task_text(task)
    content_len = len(content)
    thinking_len = len(thinking)
    error = task.get('error')
    status = task.get('status')
    task_id_short = task['id'][:8]
    conv_id_short = task.get('convId', '')

    finish_reason = task.get('finishReason') or 'unknown'
    model = task.get('model') or '?'
    provider = task.get('provider_id') or '?'

    # Diagnostic: warn about suspiciously empty results
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

    # Build metadata before the write so serialization failures are reported as
    # persistence failures rather than leaving a partially described row.
    meta = build_result_meta(task)
    meta['contentEpoch'] = content_epoch

    # Merge checkpoint toolRounds for DB persistence (continue flow)
    _merged_tr = _merge_tool_rounds(task)

    # Segment-timeline SoT (, step 1 — SHIPS DARK).
    #   Assemble the ordered typed-segment list from the SAME merged rounds +
    #   terminal content/thinking. Nothing reads task['segments'] yet; it is
    #   populated here (the single terminal chokepoint) so later steps can flip
    #   the compat surfaces / persistence / frontend onto it. Best-effort: a
    #   segment-assembly failure must NEVER break result persistence.
    try:
        from lib.tasks_pkg.segments import assemble_segments
        task['segments'] = assemble_segments(task, merged=_merged_tr)
    except Exception as _seg_e:
        logger.warning('[Task %s] segment assembly failed (non-fatal, dark): %s',
                       task_id_short, _seg_e, exc_info=True)

    _task_row_owned = True
    try:
        # Only store the (potentially multi-MB) toolRounds blob when this task
        # has no conversation row to hold it — see _tool_rounds_have_dedicated_home.
        # Conversation attempts already persist tool rounds in their turn
        # projection; duplicating them in task_results creates two authorities.
        tr_json = None if _tool_rounds_have_dedicated_home(task) else json.dumps(_merged_tr, ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        # segments (, step 2): persist the THIN form
        #   (segments_to_json strips the _round mirror — it duplicates the
        #   tool_rounds column). Rehydrated on read via rehydrate_segments +
        #   the co-persisted toolRounds. Best-effort: never break persistence.
        segments_json = None
        try:
            _segs = task.get('segments')
            if _segs:
                from lib.tasks_pkg.segments import segments_to_json
                segments_json = json.dumps(segments_to_json(_segs), ensure_ascii=False)
        except Exception as _sj_e:
            logger.warning('[Task %s] segments serialize failed (non-fatal): %s',
                           task_id_short, _sj_e, exc_info=True)
        # Error envelope is JSON-serialised at the wire — task_results.error
        # is TEXT, but every consumer (SSE and conversation projection)
        # round-trips through lib.error_envelope so the
        # frontend only ever sees the typed dict.
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _task_row_owned = _upsert_task_row(
            task, task['convId'], content=content,
            thinking=thinking, status=task['status'],
            error_json=error_json, tr_json=tr_json, meta_json=meta_json,
            segments_json=segments_json)
        logger.debug('[Task %s] conv=%s Persisted to DB successfully', task_id_short, conv_id_short)
    except Exception as _pf_err:
        from lib.storage import storage_status
        if storage_status().get('state') in {'stopping', 'stopped'}:
            logger.info('[Task %s] conv=%s persist aborted during shutdown (expected: %s)',
                        task_id_short, conv_id_short, type(_pf_err).__name__)
        else:
            logger.error('[Task %s] conv=%s ❌ Persist FAILED — content (%d chars) and thinking (%d chars) may be lost!',
                         task_id_short, conv_id_short, content_len, thinking_len, exc_info=True)
            # P0 observability: the task_results row did NOT reach the DB
            #   (classic cause: connection pool exhausted). Emit the in-memory
            #   terminal metadata unconditionally so the finish-bar fields
            #   (finishReason/usage/apiRounds/cost) are recoverable from
            #   error.log even though the row is absent — and record that they
            #   were NOT persisted. Without this, an empty finish-bar can only
            #   be explained by querying the DB after the fact.
            logger.error('[Task %s] conv=%s ⚠️ TERMINAL METADATA NOT PERSISTED — %s',
                         task_id_short, conv_id_short,
                         terminal_state_log_summary(task, persisted=False))

    if _task_row_owned is False:
        # Recovery fenced this pre-boot executor. Every later action in this
        # function is a durable side effect (conversation sync, queue dispatch,
        # project-summary refresh), so continuing would let the stale owner
        # clobber the recovered turn even though its task_results CAS lost.
        logger.warning('[Task %s] conv=%s stale terminal finalizer stopped at '
                       'recovery fence; downstream writes suppressed',
                       task_id_short, conv_id_short)
        if not _defer_heavy_release:
            _release_heavy_task_state(task)
        return False

    # Stamp the actual serving provider into conv settings (see helper).
    _stamp_conv_provider_id(task)
    # Conversation projection and queue drain are committed by the terminal
    # turn event.  Task-result persistence owns only executor recovery data and
    # scheduler bookkeeping.
    from lib.tasks_pkg.manager._sync import _update_proactive_execution_status
    _update_proactive_execution_status(task)

    # Release the heavy per-task input state now that everything durable is
    #   in the task and turn stores. This is the RSS-at-source fix for the
    #   shared-cgroup OOM: a
    #   finished task no longer pins a whole conversation's message context for
    #   the retention window. Last statement in the function on purpose.
    #   SKIPPED when _defer_heavy_release — the autopilot hook still needs
    #   task['messages'] (the VU inherits the parent's context); the caller
    #   releases after the hook returns.
    if _defer_heavy_release:
        return True
    _released = _release_heavy_task_state(task)
    if _released:
        logger.debug('[Task %s] released %d heavy terminal field(s) to bound RSS',
                     task['id'][:8], _released)
    return True
