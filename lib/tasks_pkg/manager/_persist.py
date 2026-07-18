"""Result persistence — task_results upsert, result-meta build, tool-round
merge/trim, heavy-state release, and the terminal ``persist_task_result``.

Also the conversation-recovery readers (``load_tool_rounds_from_conversation`` /
``load_endpoint_turns_from_conversation``).

Cross-module note: ``persist_task_result`` fans out into conversation-sync +
queue-drain + summary helpers that live in ``_sync.py``. Those are imported
FUNCTION-LOCALLY inside ``persist_task_result`` to keep the module dependency
graph acyclic (``_sync`` imports the low-level helpers from THIS module at the
top level).

``persist_task_result`` and ``_upsert_task_row`` are monkeypatched by tests and
MUST stay facade-reachable + steerable.
"""

import json

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.error_envelope import to_json as _err_to_json
from lib.log import get_logger

logger = get_logger(__name__)


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
    meta = {}
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
    if task.get('apiRounds'): meta['apiRounds'] = _sanitize_api_rounds_for_persist(task['apiRounds'])
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

# usage sub-keys that are backend-only stream diagnostics (never read by any
# render path). _wire_fp is the giant (~226 KB/round); the rest are tiny but
# equally value-free once persisted, so drop the whole diagnostic set.
_USAGE_TRANSIENT_KEYS = ('_wire_fp', '_wire_static')


def _sanitize_usage_for_persist(usage):
    """Return a copy of *usage* with transient wire-diagnostic keys dropped.

    ``usage._wire_fp`` is a per-round ~226 KB canonical-message list captured
    for live cache-miss tracing (lib/llm/_sse_core.py → cache_tracking.py); it
    is consumed WITHIN the run and never read by any render path, so persisting
    it just bloats the conversation. Returns *usage* unchanged (same object)
    when there is nothing to strip, so the common small-usage case is free.
    """
    if not isinstance(usage, dict):
        return usage
    if not any(k in usage for k in _USAGE_TRANSIENT_KEYS):
        return usage
    return {k: v for k, v in usage.items() if k not in _USAGE_TRANSIENT_KEYS}


def _sanitize_api_rounds_for_persist(api_rounds):
    """Return a copy of *api_rounds* with each round's usage diagnostics stripped."""
    if not isinstance(api_rounds, list):
        return api_rounds
    out = []
    for r in api_rounds:
        if isinstance(r, dict) and isinstance(r.get('usage'), dict):
            r = {**r, 'usage': _sanitize_usage_for_persist(r['usage'])}
        out.append(r)
    return out


def _trim_round_for_persist(r):
    """Drop the transient run_command streaming buffer from a DONE tool round.

    ``_partialOutput`` is the live terminal buffer accumulated during streaming
    (lib/tasks_pkg/handlers/code_exec.py). On a completed round the authoritative
    output is already in ``results[0].output`` / ``toolContent``; the buffer is
    pure bloat (18 MB observed while toolContent was 2 KB). We only drop it once
    the round is ``done`` — a still-running round keeps it so a mid-stream
    state-snapshot reconnect can still replay the partial output. Returns *r*
    unchanged when there is nothing to trim.
    """
    if not isinstance(r, dict):
        return r
    if r.get('status') == 'done' and r.get('_partialOutput'):
        r = dict(r)
        r.pop('_partialOutput', None)
    return r


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


# Static column order for the task_results upsert — shared by the final-result
# and the running-checkpoint writers so the two can never drift.
_TASK_RESULTS_COLS = (
    'task_id', 'conv_id', 'content', 'thinking', 'error',
    'status', 'tool_rounds', 'metadata', 'segments', 'created_at', 'completed_at',
)


def _conv_row_exists(db, conv_id: str) -> bool:
    """True iff a ``conversations`` row exists for ``conv_id`` (user 1).

    A monkeypatchable seam (own module function) used by ``_upsert_task_row``
    to reject a ``task_results`` write for a conversation that has been DELETED
    out from under a still-running task. Best-effort: on any probe failure
    return ``True`` (fail-open) so a transient DB hiccup never silently drops a
    legitimate result write — the orphan-row case is a narrow race, losing a
    real result would be worse.
    """
    try:
        row = db.execute(
            'SELECT 1 FROM conversations WHERE id=? AND user_id=1 LIMIT 1',
            (conv_id,)
        ).fetchone()
        return row is not None
    except Exception as e:
        logger.debug('[Manager] _conv_row_exists probe failed conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e)
        return True


def _upsert_task_row(task, conv_id, *, content, thinking, status,
                     error_json, tr_json, meta_json, segments_json=None):
    """Single source of truth for the ``task_results`` upsert.

    Owns the DB acquire + the ``upsert(..., insert_cols=[10], retry=True)``
    shape (``retry=True`` commits — see lib/database._core_schema.upsert).
    Callers supply only the fields that vary between the final-result write
    (``status='done'|'error'``, full metadata) and the running checkpoint
    (``status='running'``, partial metadata).  ``created_at`` /
    ``completed_at`` are derived here identically for both.

    Orphan guard: for a conv-backed task (non-inline, non-empty ``conv_id``)
    whose parent ``conversations`` row is GONE — the delete-vs-persist race
    where the conv was deleted while this task was still winding down — SKIP
    the write. ``_sync_result_to_conversation`` already guards the messages
    write with ``if not row: return``; without the same guard here a late
    terminal / checkpoint write re-inserts an ORPHAN ``task_results`` row after
    ``DELETE FROM task_results`` ran in ``_delete_conv_blocking``. Inline-message
    tasks (external callers, ``conv_id=''`` VU/reporter carriers) legitimately
    have no conversations row and read results straight from ``task_results`` —
    they are NOT guarded.
    """
    import time
    from lib.database._core_schema import TASK_RESULTS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    if (conv_id and not task.get('_inline_messages')
            and not _conv_row_exists(db, conv_id)):
        logger.info('[Task %s] conv=%s Skipping task_results upsert — parent '
                    'conversation row is gone (deleted); not resurrecting an '
                    'orphan row (status=%s)',
                    task['id'][:8], conv_id[:8], status)
        return
    upsert(db, TASK_RESULTS, {
        'task_id': task['id'], 'conv_id': conv_id,
        'content': content, 'thinking': thinking,
        'error': error_json, 'status': status, 'tool_rounds': tr_json,
        'metadata': meta_json, 'segments': segments_json,
        'created_at': int(task.get('created_at', time.time()) * 1000),
        'completed_at': int(time.time() * 1000),
    }, insert_cols=list(_TASK_RESULTS_COLS), retry=True)


# Heavy INPUT fields pinned on the task dict that have NO reader after the
# turn reaches a terminal state. They are the dominant grow-with-conversation
# retainers (measured 2026-07-11: essentially all of the ~3.3 GB private-dirty
# heap is per-task state, not import baseline). ``messages`` is the full API
# input context (whole conversation), ``_endpoint_turns`` the per-turn endpoint
# snapshots. Both are consumed DURING the turn; every POST-terminal reader
# (chat_poll DB path, killed-recovery, reconcile) rebuilds from the DB
# (``task_results`` / ``conversations``), never from these in-memory copies.
# Released at the terminal persist chokepoint so a finished task no longer pins
# a whole conversation's worth of bytes for the ttl=3600s retention window
# (and forever, for the never-evicted carriers). ``events`` is deliberately
# KEPT — a reconnecting SSE client replays ``task['events'][cursor:]`` within
# the TTL window. The async profile-consolidation daemon captures ``messages``
# by its own reference arg (spawned by the orchestrator BEFORE this runs), so
# nulling the dict key here frees the bytes exactly when that daemon finishes,
# not at task-TTL — strictly better.
_HEAVY_TERMINAL_FIELDS = ('messages', '_endpoint_turns')


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
        return released
    except Exception as e:
        logger.debug('[Task %s] heavy-state release skipped: %s',
                     (task.get('id') or '')[:8], e)
        return 0


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

    # ★ Segment-timeline SoT (epic pt_cb8f98b0cb9b47fb, step 1 — SHIPS DARK).
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

    try:
        # Only store the (potentially multi-MB) toolRounds blob when this task
        # has no conversation row to hold it — see _tool_rounds_have_dedicated_home.
        # For DB-backed/endpoint tasks the conversation is the durable store and
        # recovery readers fall back to load_tool_rounds_from_conversation().
        tr_json = None if _tool_rounds_have_dedicated_home(task) else json.dumps(_merged_tr, ensure_ascii=False)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        # ★ segments (epic pt_cb8f98b0cb9b47fb, step 2): persist the THIN form
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
        # is TEXT, but every consumer (SSE done, /api/chat/poll, conversation
        # message persistence) round-trips through lib.error_envelope so the
        # frontend only ever sees the typed dict.
        error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, task['convId'], content=task['content'],
                         thinking=task['thinking'], status=task['status'],
                         error_json=error_json, tr_json=tr_json, meta_json=meta_json,
                         segments_json=segments_json)
        logger.debug('[Task %s] conv=%s Persisted to DB successfully', task_id_short, conv_id_short)
    except Exception as _pf_err:
        from lib.database import is_expected_shutdown_error
        if is_expected_shutdown_error(_pf_err):
            logger.info('[Task %s] conv=%s persist aborted during shutdown (expected: %s)',
                        task_id_short, conv_id_short, type(_pf_err).__name__)
        else:
            logger.error('[Task %s] conv=%s ❌ Persist FAILED — content (%d chars) and thinking (%d chars) may be lost!',
                         task_id_short, conv_id_short, content_len, thinking_len, exc_info=True)
            # ★ P0 observability: the task_results row did NOT reach the DB
            #   (classic cause: connection pool exhausted). Emit the in-memory
            #   terminal metadata unconditionally so the finish-bar fields
            #   (finishReason/usage/apiRounds/cost) are recoverable from
            #   error.log even though the row is absent — and record that they
            #   were NOT persisted. Without this, an empty finish-bar can only
            #   be explained by querying the DB after the fact.
            logger.error('[Task %s] conv=%s ⚠️ TERMINAL METADATA NOT PERSISTED — %s',
                         task_id_short, conv_id_short,
                         terminal_state_log_summary(task, persisted=False))

    # ★ Write result back to conversation — ensures data survives even if
    #   no frontend client is connected (SSE closed, user closed tab, etc.)
    # For endpoint mode tasks, the multi-turn sync happens in endpoint.py
    # via _sync_endpoint_turns_to_conversation(). We still call the regular
    # sync as a fallback for the single-turn content + metadata.
    from lib.tasks_pkg.manager._sync import (
        _sync_result_to_conversation,
        _update_proactive_execution_status,
        _dispatch_queued_message,
        _maybe_refresh_project_summary,
    )
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

    # ★ Release the heavy per-task input state now that everything durable is
    #   in the DB and all in-turn consumers (sync, queue drain, summary) have
    #   run. This is the RSS-at-source fix for the shared-cgroup OOM: a
    #   finished task no longer pins a whole conversation's message context for
    #   the retention window. Last statement in the function on purpose.
    _released = _release_heavy_task_state(task)
    if _released:
        logger.debug('[Task %s] released %d heavy terminal field(s) to bound RSS',
                     task['id'][:8], _released)
