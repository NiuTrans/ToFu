# HOT_PATH
"""Tool-execution pipeline — approval → parallel dispatch → result-append.

The public entry-point is :func:`execute_tool_pipeline`, the big orchestrator
extracted from the inner loop of ``orchestrator.run_task``.  Also houses the
``_append_screenshot_message`` multimodal-result helper.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from lib.observability import InstrumentedThreadPoolExecutor
import uuid
# concurrent.futures.TimeoutError only became an alias of builtin
# TimeoutError in 3.11 — on 3.10 it is a DISTINCT class, so
# ``except TimeoutError`` does NOT catch an as_completed/fut.result timeout
# and the pool-timeout lane never fires (3.10 CI leg, 2026-08-06).
from concurrent.futures import TimeoutError as _FuturesTimeoutError
from typing import Any

from lib.agent_core.execution_clock import task_execution_time_ms
from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.model_info import model_supports_vision
from lib.tasks_pkg.compaction.api import (
    budget_tool_result,
    budget_tool_result_v2,
    clamp_tool_result_text,
    enforce_round_aggregate_budget,
    enforce_round_aggregate_budget_v2,
    mark_empty_result,
)
from lib.tasks_pkg.executor import _execute_tool_one, _finalize_tool_round
from lib.tasks_pkg.manager import append_event
from lib.tasks_pkg.tool_hooks import run_post_hooks, run_pre_hooks
from lib.tasks_pkg.tool_search_projection import cached_tool_search_projection
from lib.tool_results.tool_rejection import (
    stamp_tool_rejection,
    tool_rejection_descriptor,
)
from lib.tools.gateway import EXECUTE_TOOLS_NAME, GATEWAY_TOOL_NAMES, SEARCH_TOOLS_NAME
from lib.tools.result_projection import (
    TOOL_RESULT_PRODUCER_METADATA_KEY,
    TOOL_RESULT_PROJECTION_ITEMS_KEY,
)

from lib.tasks_pkg.tool_dispatch._approval import _handle_approval
from lib.tasks_pkg.tool_dispatch._flags import (
    _cache_entry_producer_metadata,
    _cache_entry_projection_items,
    _project_cache_entry_is_current,
    _build_cache_hit_meta,
    _call_id_signature,
    _ensure_tool_call_id_receipts,
    _ensure_tool_result_cache,
    _publish_execute_gateway_direct_siblings,
    _invalidate_project_cache,
    _make_cache_key,
    _safe_count_tokens,
    _store_tool_result_cache_entry,
    _store_tool_call_id_receipt,
    _task_confirmation_tools,
    _task_partitions,
    _unpack_cache_entry,
)
from lib.tasks_pkg.tool_dispatch._heartbeat import (
    _SERIAL_BLOCKING_TOOLS,
    _execute_tool_one_in_pool,
    _start_tool_heartbeat,
)
from lib.tasks_pkg.tool_dispatch._unchanged import (
    UnchangedProjection,
    compact_unchanged_tool_names,
    maybe_project_unchanged_result,
    remember_full_result,
)

logger = get_logger(__name__)


# In-memory protocol tools whose calls are order-sensitive inside one model
# response.  They are deliberately separate from ``_write_tools``: updating a
# task-local checklist must be serial, but it is not an external mutation and
# therefore must not trigger Manual-mode write approval or project-cache
# invalidation.  Keep this list narrow; the serial lane preserves
# ``parsed_tcs`` order exactly.
_ORDERED_STATE_TOOLS = frozenset({'todo_write'})


# Read tools whose dedup-cache entry a same-round WRITE can make stale. When a
# read executes CONCURRENTLY with the serial write lane (see the read-pool
# dispatch in execute_tool_pipeline), its result may capture pre-write bytes.
# Caching that result after the write's _invalidate_project_cache already ran
# would leave a stale entry that a later FreshGate-checked hit serves verbatim
# (the freshness token now matches the post-write disk, so FreshGate cannot see
# it). These are exactly the filesystem-state reads a write can move.
_WRITE_SENSITIVE_READ_TOOLS = frozenset({
    'read_files', 'list_dir', 'grep_search', 'find_files', 'inspect_image',
})


def _tool_result_recovery_policy(task: dict[str, Any], tool_name: str) -> str:
    """Resolve overflow recovery only from the request-owned tool contract."""
    documents = (task or {}).get('_toolContractDocumentsByName') or {}
    document = documents.get(tool_name) if isinstance(documents, dict) else None
    policy = (document.get('resultRecovery')
              if isinstance(document, dict) else 'artifact')
    policy = str(policy or 'artifact').strip().lower()
    return policy if policy in {'artifact', 'source', 'none'} else 'artifact'


def _post_tool_snapshot_enabled(task: dict[str, Any]) -> bool:
    """Whether to emit the per-round post-tool ``kind='state'`` wire snapshot.

    The snapshot is a DEBUG-only mirror that deep-copies + re-sanitizes the
    WHOLE message history every round (O(history)); for unattended / autonomous
    tasks there is no interactive debug UI to consume it, so it is pure
    overhead. Attended (interactive) tasks keep it. Env override:
    ``TOFU_POST_TOOL_SNAPSHOT=1`` forces on, ``=0`` forces off.
    """
    raw = os.environ.get('TOFU_POST_TOOL_SNAPSHOT', '').strip().lower()
    if raw in ('1', 'true', 'on', 'yes'):
        return True
    if raw in ('0', 'false', 'off', 'no'):
        return False
    return bool(task.get('_attended') or task.get('_invocationEventSink'))


def _blocked_multi_agent_write(
        tc: dict[str, Any], fn_name: str, banned_tools: frozenset) -> str:
    """Return the non-root agent name when its mutation must be refused."""
    caller = tc.get('caller') if isinstance(tc, dict) else None
    if not isinstance(caller, dict) or caller.get('type') != 'multi_agent':
        return ''
    agent_name = str(caller.get('agent_name') or '')
    return (agent_name if agent_name and agent_name != '/root'
            and fn_name in banned_tools else '')


def _collect_used_tool_call_ids(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    current_round_entries: tuple[dict[str, Any], ...] = (),
    exclude_trailing_assistant: bool = False,
) -> set[str]:
    """Index call ids that belong to earlier or enclosing tool occurrences.

    Top-level and nested dispatch both receive their current assistant carrier
    as the trailing message, while parsing has already appended the current
    round entries to ``task['toolRounds']``.  Callers performing historical
    collision detection exclude those exact current objects.  The minting
    fallback scans everything because a generated id must be globally unique.
    """
    seen: set[str] = set()
    message_limit = len(messages or ())
    if (exclude_trailing_assistant and message_limit
            and isinstance(messages[-1], dict)
            and messages[-1].get('role') == 'assistant'
            and messages[-1].get('tool_calls')):
        message_limit -= 1
    for msg in (messages or ())[:message_limit]:
        if not isinstance(msg, dict):
            continue
        tcid = msg.get('tool_call_id')
        if tcid:
            seen.add(str(tcid))
        for tc in msg.get('tool_calls') or ():
            if isinstance(tc, dict) and tc.get('id'):
                seen.add(str(tc.get('id')))

    current_entry_ids = {id(entry) for entry in current_round_entries}
    for row in (task or {}).get('toolRounds') or ():
        if (isinstance(row, dict) and id(row) not in current_entry_ids
                and row.get('toolCallId')):
            seen.add(str(row.get('toolCallId')))
    receipts = (task or {}).get('_tool_call_id_receipts')
    if isinstance(receipts, dict):
        seen.update(str(call_id) for call_id in receipts)
    return seen


def _mint_fresh_call_id(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    preferred: str,
    round_num: int,
    *,
    used_ids: set[str] | None = None,
) -> str:
    """Mint a call id that is globally unique across the conversation.

    Positional-id models (kimi-k3 ``{tool}_{index-in-message}``) re-emit the
    SAME id every round with different arguments. Executing those under the
    model's id makes ``messages`` carry two tool_result/tool_call entries with
    one ``tool_call_id``; id-keyed writers (the round-aggregate budget
    apply-back below, L1's ``_round_index``, the rebuild path) then re-bind the
    OLD cached result to the NEW content every turn — the exact
    ``PREFIX MUTATION DETECTED`` re-bill loop. A fresh id that can never
    collide with any id already present in the conversation makes that class
    of rewrite unrepresentable.
    """
    seen = (used_ids if used_ids is not None
            else _collect_used_tool_call_ids(task, messages))

    base = preferred or 'call'
    while True:
        candidate = f'{base}#r{round_num}#{uuid.uuid4().hex[:8]}'
        if candidate not in seen:
            seen.add(candidate)
            return candidate


def _repair_current_tool_call_identity(
    task: dict[str, Any],
    messages: list[dict[str, Any]],
    parsed_tcs: list[tuple],
    item_index: int,
    item: tuple,
    *,
    round_num: int,
    tid: str,
    receipts: dict[str, dict],
    historical_used_ids: set[str],
    used_ids: set[str],
    claimed_ids: set[str],
    settled_results: dict[str, Any],
) -> tuple:
    """Remint recycled/history and same-response duplicate correlation IDs.

    A positional-ID provider may reuse ``{tool}_{position}`` in every response.
    Executing under that old ID makes later ID-keyed history reconciliation
    capable of rewriting an already-cached result.  Every provider occurrence
    remains a fresh action; receipts describe it but never replay it.
    """
    tc, fn_name, tc_id, fn_args, rn, round_entry, parse_error = item
    call_signature = _call_id_signature(fn_name, fn_args)
    receipt = receipts.get(tc_id)
    if tc_id in historical_used_ids:
        exact_reemit = isinstance(receipt, dict) and (
            receipt.get('signature') == call_signature
            and receipt.get('name') == fn_name)
        settled_results.pop(tc_id, None)
        pending_envelopes = task.get('_pending_tool_result_envelopes')
        if isinstance(pending_envelopes, dict):
            pending_envelopes.pop(tc_id, None)

        original_id = tc_id
        tc_id = _mint_fresh_call_id(
            task, messages, original_id, round_num, used_ids=used_ids)
        tc['id'] = tc_id
        if isinstance(round_entry, dict):
            round_entry['toolCallId'] = tc_id
        item = (
            tc, fn_name, tc_id, fn_args, rn, round_entry, parse_error)
        _store_tool_call_id_receipt(task, original_id, {
            'signature': call_signature,
            'name': fn_name,
            'status': 'reissued',
        })
        if exact_reemit:
            logger.info(
                '[Task %s] call_id re-emit: %s id=%s re-issued with '
                'identical args — reminted unique id=%s and executing LIVE '
                '(no receipt replay)', tid, fn_name, original_id[:12],
                tc_id[-12:])
        elif isinstance(receipt, dict):
            logger.warning(
                '[Task %s] call_id CONFLICT: %s id=%s recycled with new '
                'args — reminted unique id=%s and executing as a fresh call',
                tid, fn_name, original_id[:12], tc_id[-12:])
        else:
            logger.warning(
                '[Task %s] historical call_id reuse: %s id=%s had no '
                'retained signature receipt — reminted unique id=%s and '
                'executing LIVE', tid, fn_name, original_id[:12], tc_id[-12:])

    if tc_id in claimed_ids:
        conflicting_id = tc_id
        tc_id = _mint_fresh_call_id(
            task, messages, conflicting_id, round_num, used_ids=used_ids)
        tc['id'] = tc_id
        if isinstance(round_entry, dict):
            round_entry['toolCallId'] = tc_id
        item = (
            tc, fn_name, tc_id, fn_args, rn, round_entry, parse_error)
        logger.warning(
            '[Task %s] duplicate in-batch call_id: %s id=%s reminted as %s '
            'for an independent response occurrence',
            tid, fn_name, conflicting_id[:12], tc_id[-12:])

    claimed_ids.add(tc_id)
    used_ids.add(tc_id)
    parsed_tcs[item_index] = item
    return item


def _bounded_tool_env_int(name: str, default: int, minimum: int,
                          maximum: int) -> int:
    raw = os.environ.get(name, '')
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        logger.warning('[ToolPipeline] invalid %s=%r; using %d',
                       name, raw, default)
        value = default
    return max(minimum, min(value, maximum))


def _completed_tools_until_cancel(futures, task, deadline):
    """Bound each wait so Stop is observed even when no worker produces output."""
    pending = set(futures)
    while pending:
        if task.get('aborted'):
            # Preserve real receipts completed before Stop was observed. Only
            # the remaining occurrences receive the cancellation verdict.
            completed = {f for f in pending if f.done() and not f.cancelled()}
            yield from completed
            yield None
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _FuturesTimeoutError()
        done, pending = wait(
            pending, timeout=min(0.1, remaining), return_when=FIRST_COMPLETED)
        for future in done:
            yield future


def _harvest_completed_tool_results(futures, task, tool_results, tool_verdicts,
                                   *, round_num, reason):
    """Recover omitted executor receipts without waiting, retrying or resettling."""
    for future, (call_id, name, _generation) in futures.items():
        if call_id in tool_results or not future.done():
            continue
        try:
            # The common executor has validated the handler's occurrence.
            # Keep ownership anchored to the submitted Future's identity.
            _returned_id, content, is_search = future.result()
            tool_results[call_id] = (content, is_search)
            logger.warning(
                '[Task %s] conv=%s Recovered completed tool=%s tc_id=%s '
                'round=%d after %s; handler was not repeated',
                task['id'][:8], task.get('convId', ''), name, call_id,
                round_num, reason)
        except Exception as error:
            logger.warning(
                '[Task %s] conv=%s Tool=%s tc_id=%s completed with error '
                'at round=%d after %s: %s',
                task['id'][:8], task.get('convId', ''), name, call_id,
                round_num, reason, error)
            tool_results[call_id] = (f'Tool execution error: {error}', False)
            tool_verdicts[call_id] = 'error'


def _drain_cancelled_tool_workers(task, futures):
    """Release scoped resources before allowing the task to become terminal.

    Python threads cannot be killed safely. A worker still owns the live task,
    round and possibly external write handles: abandoning it with wait=False
    would allow mutations AFTER settlement. Cancellation callbacks are the
    existing runtime's resource boundary; keep ownership until workers exit.
    Non-cooperative handlers therefore remain cancelling (not falsely stopped).
    No reaper thread or replacement executor is spawned here.
    """
    from lib.tasks_pkg.tool_runtime import cancel_task_contexts

    pending = set(futures)
    for future in pending:
        future.cancel()
    while pending:
        # Repeat fanout to close the submit -> context registration race.
        # Callback removal is exactly-once in the runtime context registry.
        cancel_task_contexts(task)
        pending = {future for future in pending if not future.done()}
        if pending:
            wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)


def _parallel_idle_reuse_hint(item_count: int, *, programmatic: bool = False
                           ) -> int:
    """Legacy launch hints size idle reuse only; every accepted tool starts."""
    from runtime_guards import deployment_resource_default
    configured = _bounded_tool_env_int(
        'TOOL_MAX_PARALLEL_WORKERS',
        deployment_resource_default(
            'TOOL_MAX_PARALLEL_WORKERS', os.environ),
        1,
        32)
    if programmatic:
        from lib.tools.programmatic import PROGRAMMATIC_MAX_CONCURRENT_CALLS
        configured = min(configured, PROGRAMMATIC_MAX_CONCURRENT_CALLS)
    return max(1, min(configured, max(1, int(item_count))))


# ── Per-turn search/fetch accumulator budget ──
# ``all_search_results_text`` accumulates web_search/fetch_url text across
# EVERY round of a turn. The no-prose fallback (orchestrator._finalize) joins
# it into one assistant message when the model returned tools but no prose, so
# an unbounded list re-sends the whole corpus and blows up memory + context.
# Bound it in UTF-8 bytes; keep the NEWEST results (closest to the final
# answer) and head-truncate any single oversized result with a marker. Sits
# below the per-round aggregate budget (300k chars) because this buffer spans
# the whole turn, not one round.
SEARCH_RESULTS_ACCUM_BYTES = 200_000

_SEARCH_RESULTS_FALLBACK_SEPARATOR = '\n\n---\n\n'

_SEARCH_RESULTS_TRUNCATION_MARKER = (
    '\n… [search-result text truncated to fit the turn accumulator budget]')


def _utf8_len(text: str) -> int:
    return len(text.encode('utf-8', errors='replace'))


def _truncate_to_utf8_bytes(text: str, max_bytes: int) -> str:
    """Longest prefix of *text* whose UTF-8 encoding is <= *max_bytes*."""
    if max_bytes <= 0:
        return ''
    encoded = text.encode('utf-8', errors='replace')
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode('utf-8', errors='ignore')


def _trim_search_results_accumulator(results: list[str]) -> None:
    """Bound *results* to :data:`SEARCH_RESULTS_ACCUM_BYTES`, newest kept."""
    if not results:
        return
    newest = results[-1]
    if _utf8_len(newest) > SEARCH_RESULTS_ACCUM_BYTES:
        marker = _SEARCH_RESULTS_TRUNCATION_MARKER
        head_budget = max(0, SEARCH_RESULTS_ACCUM_BYTES - _utf8_len(marker))
        results[:] = [_truncate_to_utf8_bytes(newest, head_budget) + marker]
        return
    total = sum(_utf8_len(item) for item in results)
    while total > SEARCH_RESULTS_ACCUM_BYTES and len(results) > 1:
        total -= _utf8_len(results.pop(0))


def _append_search_result_text(results: list[str], text: str) -> None:
    """Append one search/fetch result and re-enforce the accumulator budget."""
    if not isinstance(text, str):
        text = str(text)
    results.append(text)
    _trim_search_results_accumulator(results)


def bounded_search_results_text(
    results: list[str],
    separator: str = _SEARCH_RESULTS_FALLBACK_SEPARATOR,
) -> str:
    """Join search results, keeping the JOINED output within the byte budget.

    Newest results are kept first; the oldest are dropped when the budget is
    exhausted, and a single result larger than the budget is head-truncated
    with a marker. Used by the orchestrator's no-prose fallback and sources
    footer so a list that bypassed the accumulator still cannot re-send an
    unbounded corpus.
    """
    sep_len = _utf8_len(separator)
    kept: list[str] = []
    total = 0
    for item in reversed(results):
        if not isinstance(item, str):
            item = str(item)
        sep_cost = sep_len if kept else 0
        room = SEARCH_RESULTS_ACCUM_BYTES - total - sep_cost
        if room <= 0:
            break
        if _utf8_len(item) > room:
            marker = _SEARCH_RESULTS_TRUNCATION_MARKER
            head_budget = room - _utf8_len(marker)
            if head_budget > 0:
                item = _truncate_to_utf8_bytes(item, head_budget) + marker
            else:
                item = _truncate_to_utf8_bytes(item, room)
        kept.append(item)
        total += sep_cost + _utf8_len(item)
    kept.reverse()
    return separator.join(kept)


def _register_spilled_artifact_origin(task, round_entry, tool_name, content):
    """Record which tool call a freshly-spilled artifact pointer came from.

    Both L0 budget lanes (per-result and round-aggregate) replace an
    oversized result with a v2 envelope whose ``artifactRef`` is a bare
    content hash. The origin is registered HERE — the only place source
    identity and pointer coexist — so a later continuation row
    (read_tool_artifact / search_tool_artifact) can label itself with the
    source round + tool instead of the digest. No-op unless ``content`` is a
    v2 envelope carrying a pointer.
    """
    if not (round_entry and isinstance(content, str)
            and '"artifactRef":"tool-result:' in content):
        return
    try:
        artifact_ref = str(json.loads(content).get('artifactRef') or '')
    except (json.JSONDecodeError, AttributeError, TypeError):
        return
    if not artifact_ref:
        return
    from lib.tool_results.tool_result_artifacts import register_artifact_provenance
    register_artifact_provenance(
        task, artifact_ref, tool_name=tool_name,
        display=round_entry.get('query', ''),
        llm_round=round_entry.get('llmRound'),
        tool_call_id=round_entry.get('toolCallId', ''))


def _project_spilled_artifact_alias(task, round_entry, tool_name,
                                    internal_envelope, model_text):
    """Swap the model-visible ``artifactRef`` digest for a semantic alias.

    The internal envelope and the evidence sidecar keep the canonical
    ``tool-result:<sha256>`` pointer — CAS dedup, compaction anchors, signed
    cursor binding, and the storage validator all depend on it. Only the
    sparse model projection carries ``tr:<slug>:<prefix>`` (shorter,
    BPE-friendly, memorable); a later read/search resolves it back to the
    digest before any storage call. Any mint/store failure keeps the digest
    projection — always valid, just longer.
    """
    if not (isinstance(internal_envelope, str) and isinstance(model_text, str)
            and '"artifactRef":"tool-result:' in internal_envelope
            and '"artifactRef":"tool-result:' in model_text):
        return model_text
    try:
        artifact_ref = str(json.loads(internal_envelope).get('artifactRef') or '')
    except (json.JSONDecodeError, AttributeError, TypeError):
        return model_text
    try:
        from lib.tasks_pkg.manager import task_user_id
        owner = task_user_id(task)
    except Exception as diagnostic_error:
        logger.debug('_project_spilled_artifact_alias failure handled: %s', type(diagnostic_error).__name__)
        owner = None
    from lib.tool_results.tool_result_artifacts import mint_artifact_alias
    alias = mint_artifact_alias(
        task, artifact_ref, tool_name=tool_name,
        display=(round_entry.get('query', '')
                 if isinstance(round_entry, dict) else ''),
        user_id=owner)
    if not alias:
        return model_text
    # Byte-exact swap: both strings are escape-free JSON string literals in a
    # compact-json projection. An ambiguous shape keeps the digest instead.
    needle = '"artifactRef":"%s"' % artifact_ref
    if model_text.count(needle) != 1:
        return model_text
    return model_text.replace(needle, '"artifactRef":"%s"' % alias)


def _settle_tool_result(
    task: dict[str, Any],
    fn_name: str,
    tc_id: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any] | None,
    tool_content: Any,
    *,
    idempotent_tools: frozenset,
    cache: dict,
    tid: str,
    round_num: int,
    terminal_status: str | None = None,
    internal_result_sink: Any = None,
    context_messages: list[dict[str, Any]] | None = None,
    unchanged_receipt_tools: frozenset[str] = frozenset(),
    settled_results: dict[str, Any] | None = None,
    canonical_content: Any = None,
) -> Any:
    """Settle ONE tool call: budget its result, stamp the round, emit
    ``tool_complete``. Returns the final (budgeted) content for the message.

    WHY THIS IS A FUNCTION (). This work used to live inline in the
    post-phase loop, which runs AFTER ``pool.shutdown(wait=True)``. That made a
    tool's completion unobservable until every SIBLING in the same round had
    also finished: in a round with a 0.05s ``read_files`` and a 40s
    ``web_search``, the fast tool's content/token chips landed 40 seconds after
    it actually returned, and the user had no way to tell which of the two was
    slow. Hoisting it into a function lets the ``as_completed`` loop settle each
    tool AT ITS OWN completion instant, while the post-phase keeps ownership of
    the one thing that genuinely must stay ordered — appending the
    ``role:'tool'`` messages in the model's ORIGINAL tool-call order.

    Everything here is PER-TOOL by construction. The round-AGGREGATE budget is
    deliberately NOT here: it needs every result to size the round, so it stays
    after the barrier and corrects an already-announced result with a
    ``tool_compacted`` event instead of delaying the first announcement.

    ``terminal_status`` () carries a NON-SUCCESS verdict for the
    lanes where the tool never actually ran — ``rejected`` (hallucinated call,
    pre-hook block, user pressed Reject) or ``aborted`` (user pressed Stop) —
    and, since 2026-08-06, ``error`` for the lanes where it ran but FAILED
    (raised, or cancelled by the parallel-pool timeout ceiling).
    Settling those lanes promptly is the whole point of this extension, but a
    settle that reported success would be far worse than the latency it
    removes: a write the user REFUSED would render as applied. So the verdict is
    stamped on the round AND shipped on the wire, and the client is required
    never to overwrite it (see ``stream_reducer.js``' tool_complete case).
    Without this parameter such a round would sit at ``pending_approval`` /
    ``searching`` until the end-of-task dangling sweep — i.e. spin for the rest
    of the turn.

    Idempotent: a second call for the same ``tc_id`` is a no-op (returns the
    already-settled content), so the post-phase can call it unconditionally
    without double-emitting for a tool an earlier lane already settled.
    """
    # Production dispatch supplies an invocation-local map.  The task fallback
    # remains for focused direct callers, but no long task needs to retain every
    # settled body across later LLM rounds or nested gateway pipelines.
    _settled = (settled_results if settled_results is not None
                else task.setdefault('_settled_tool_results', {}))
    if tc_id in _settled:
        return _settled[tc_id]

    # The result owner now controls presentation. A timed-out worker can still
    # own resources, but cannot replace this occurrence's published evidence.
    from lib.tasks_pkg.tool_runtime import close_tool_result_publication
    close_tool_result_publication(
        task, round_num=rn, tool_call_id=tc_id, round_entry=round_entry,
        terminal_status=terminal_status)

    projection_items = (
        round_entry.pop(TOOL_RESULT_PROJECTION_ITEMS_KEY, None)
        if isinstance(round_entry, dict) else None)
    producer_metadata = (
        round_entry.pop(TOOL_RESULT_PRODUCER_METADATA_KEY, None)
        if isinstance(round_entry, dict) else None)

    # Post-tool hooks: modify/enrich result after execution.
    _uses_v2_result_contract = False
    from lib.project_dispatch.admission import is_dispatch_task
    if isinstance(tool_content, str) and not is_dispatch_task(task):
        _projection_source = tool_content
        from lib.tools.result_value import posthook_result
        tool_content = posthook_result(tool_content,
            lambda text: run_post_hooks(fn_name, fn_args, text, task))
        if tool_content != _projection_source:
            # Producer boundaries describe the producer's exact raw result.
            # A hook that rewrites it invalidates those offsets/previews; fall
            # back to argument-derived identities rather than show stale text.
            projection_items = None

    # Empty result marker: prevent models from misinterpreting
    # empty tool results as conversation end.
    if isinstance(tool_content, str):
        tool_content = mark_empty_result(fn_name, tool_content)

    from lib.tools.result_value import capture_result, TrustedResultText
    # The handler has already settled executed failures. Carry its verdict
    # into the canonical receipt as well as the terminal event; otherwise a
    # refused write has a failed card but an executionStatus=ok receipt.
    if terminal_status is None and (round_entry or {}).get('status') in {
            'error', 'rejected', 'aborted', 'unanswerable'}:
        terminal_status = round_entry['status']
    # ``canonical_content`` carries the evidence-rich source (an image-
    # protocol dict with retained ``_modelReplaySources``) when the model-
    # visible content had to be flattened to display text. Capturing the
    # text alone would silently drop the media from the artifact tree while
    # still reporting retentionCoverage=complete — the "media empty"
    # read-back lie. The capture seam always owns the honest evidence.
    captured_result = capture_result(
        canonical_content if canonical_content is not None else tool_content,
        execution_status=terminal_status,
        source_coverage='partial' if (producer_metadata or {}).get('truncated') else (producer_metadata or {}).get('sourceCoverage'))
    if terminal_status is None:
        from lib.tools.result_value import tool_result_failure_verdict
        terminal_status = tool_result_failure_verdict(captured_result)
    if captured_result.execution_status != 'ok':
        # A failed observation is not a reusable result. In particular, a
        # declared timeout can replace an otherwise successful worker return
        # after the parallel lane has populated its cache. Evict at the shared
        # settlement boundary so a later authorized call can observe recovery.
        if isinstance(round_entry, dict):
            round_entry['_cacheableResult'] = False
        if fn_name in idempotent_tools:
            cache.pop(_make_cache_key(fn_name, fn_args), None)
    if isinstance(tool_content, str):
        tool_content = TrustedResultText(tool_content, canonical=captured_result,
                                         envelope=isinstance(tool_content, TrustedResultText) and tool_content.envelope)

    # Retained media descriptors ride the round row before any budget lane
    # runs: the v2 envelope path collapses the runtime canonical to a single
    # artifact pointer (children=()), after which these refs exist only
    # inside the persisted manifest. The post-phase multimodal projection
    # re-reads this stamp so a vision model still receives the actual images.
    if isinstance(round_entry, dict):
        _retained_media_refs = captured_result.media_references()
        if _retained_media_refs:
            round_entry['_retainedMediaRefs'] = _retained_media_refs

    scientific_capture=task.get('_evolutionScientificCommandCapture') if task else None
    if callable(scientific_capture):
        try:scientific_capture(task,fn_name,tc_id,fn_args,captured_result)
        except Exception:
            logger.warning('[ToolPipeline] scientific original capture failed',exc_info=True)

    # Local JavaScript gets a separate transient result lane. Capture only
    # after post-tool hooks (the semantic/redaction boundary) and before model
    # compaction. The sink owns its hard byte budget and never enters ``task``
    # or the durable transcript.
    if internal_result_sink is not None:
        try:
            internal_result_sink.capture(tc_id, captured_result)
        except Exception as exc:
            logger.warning(
                '[ToolPipeline] internal program result capture failed for '
                '%s: %s', tc_id, exc)

    # Mutable control-plane observers execute every time. When a fresh result
    # is byte-identical and its prior full projection is still literally in
    # the active context, replace only this new model projection with a short
    # receipt. The transient program sink above intentionally keeps the full
    # value. Error settlements are never folded into a success-like receipt.
    unchanged_projection = UnchangedProjection('', tool_content)
    fresh_result_content = tool_content
    fresh_result_chars = (
        len(tool_content) if isinstance(tool_content, str) else 0)
    fresh_result_tokens = 0
    if round_entry and isinstance(tool_content, str):
        # Count the fresh producer observation before unchanged projection.
        # ``toolTokens`` below remains the model-paid compact count.
        fresh_result_tokens = _safe_count_tokens(
            tool_content, model=task.get('model', '') if task else '')
        if fresh_result_tokens > 0:
            round_entry['rawToolTokens'] = fresh_result_tokens
    unchanged_enabled = fn_name in unchanged_receipt_tools
    if (isinstance(tool_content, str)
            and terminal_status is None
            and unchanged_enabled):
        unchanged_projection = maybe_project_unchanged_result(
            task,
            tool_name=fn_name,
            arguments=fn_args,
            tool_content=tool_content,
            messages=context_messages,
            enabled=True,
        )
        if unchanged_projection.compacted and fresh_result_tokens > 0:
            receipt_tokens = _safe_count_tokens(
                unchanged_projection.model_content,
                model=task.get('model', '') if task else '')
            paid_baseline_tokens = (
                unchanged_projection.previous_model_tokens
                or fresh_result_tokens)
            if (receipt_tokens <= 0
                    or receipt_tokens >= paid_baseline_tokens):
                unchanged_projection = UnchangedProjection(
                    unchanged_projection.raw_digest, fresh_result_content)
        tool_content = TrustedResultText(unchanged_projection.model_content,
                                         canonical=captured_result)
        if unchanged_projection.compacted and round_entry is not None:
            round_entry['compactionLayer'] = 'unchanged'
            round_entry['compactedFromChars'] = fresh_result_chars
            round_entry['compactedToChars'] = len(tool_content)

    # Layer 0: Budget tool results before they enter context. V2 applies one
    # bounded envelope contract to every tool; legacy mode retains its older
    # per-tool exemptions. Oversized batched read_files results use the
    # producer sidecar above so each requested file remains represented.
    # Layer 1 (micro_compact) will further compress these once
    # they fall outside the hot tail.
    _l0_pre_chars = len(tool_content) if isinstance(tool_content, str) else 0
    if isinstance(tool_content, str):
        _conv_id = task.get('convId', '') if task else ''
        from lib.context_experiment_flags import normalize_context_experiment_flags
        _result_contract = normalize_context_experiment_flags(
            (task or {}).get('config') or {})['tools']['resultEnvelope']
        if _result_contract == 'v2':
            _uses_v2_result_contract = True
            from lib.tasks_pkg.manager import task_user_id
            tool_content = budget_tool_result_v2(
                fn_name, tool_content,
                user_id=task_user_id(task),
                model=(task or {}).get('model', ''),
                observed_at_ms=int((task or {}).get('_observedAtMs') or 0),
                world_version=str((task or {}).get('_worldVersion') or ''),
                tool_arguments=fn_args,
                projection_items=projection_items,
                producer_metadata=producer_metadata,
                recovery_policy=_tool_result_recovery_policy(task, fn_name),
            )
        else:
            tool_content = budget_tool_result(fn_name, tool_content,
                                              tool_use_id=tc_id,
                                              conv_id=_conv_id)
    # Deliberately OUTSIDE the shrink check above: structural truncation
    # (>64 items) also mints a pointer, sometimes without shrinking chars.
    _register_spilled_artifact_origin(task, round_entry, fn_name, tool_content)
    # Layer 2: tool-agnostic hard ceiling — the LAST line of
    # defence. Unlike Layer 0 (budget_tool_result) this has NO
    # per-tool exemption, so it ALSO clamps read_files (which Layer 0
    # skips). Makes the "opaque blob floods context" bug class
    # unrepresentable: a relative-path PNG decoded as text, a str()'d
    # image dict, or any future leak gets clamped to a survivable
    # result instead of a fatal HTTP 400. See conv mqgfkmxy (2026-06).
    if isinstance(tool_content, str):
        _conv_id_hc = task.get('convId', '') if task else ''
        tool_content = clamp_tool_result_text(
            fn_name, tool_content, tc_id=tc_id, conv_id=_conv_id_hc)

    # Sync the budgeted/offloaded form back into the dedup cache.
    # The cache entry was populated with the PRE-budget content (the
    # parallel-phase writer / the streaming prefetch injector), while
    # budgeting above only rewrote the local message copy. Left unsynced, the
    # full result (e.g. a 680 KB web_search dump) stays pinned in the live task
    # and is replayed verbatim on a later dedup hit, re-flooding context with
    # content the offloader had already spilled to disk. Rewrite content[0]
    # to the budgeted string, preserving the rest of
    # the entry (is_search / source / display / engine_breakdown / vertical)
    # so the rich UI-replay path is unchanged. Producer sidecars in slots 8–9
    # are consumed here: the cache then owns the final internal envelope and
    # must not retain a second preview/omission representation.
    if isinstance(tool_content, str) and fn_name in idempotent_tools:
        _sync_key = _make_cache_key(fn_name, fn_args)
        _cached_entry = cache.get(_sync_key)
        if (_cached_entry is not None
                and isinstance(_cached_entry, (tuple, list))
                and len(_cached_entry) >= 1
                and isinstance(_cached_entry[0], str)):
            _cached_projection = _cache_entry_projection_items(_cached_entry)
            _cached_producer_metadata = _cache_entry_producer_metadata(
                _cached_entry)
            if (len(_cached_entry[0]) > len(tool_content)
                    or _cached_projection is not None
                    or _cached_producer_metadata is not None):
                _cache_tail = tuple(_cached_entry)[1:]
                if (_cached_projection is not None
                        or _cached_producer_metadata is not None):
                    _cache_tail = _cache_tail[:6]
                _store_tool_result_cache_entry(
                    task, _sync_key, (tool_content, *_cache_tail),
                    generation=getattr(_cached_entry, 'generation', None))

    _program_input = None
    _program_latch = task.get('_ptc_local')
    if isinstance(_program_latch, dict) and _program_latch.get('tier') == 'program':
        from lib.tasks_pkg.manager import task_user_id
        from lib.tools.program_results import retain_program_result, describe_program_input
        _owner_id = task_user_id(task)
        _retained_input = retain_program_result(task, fn_name, fn_args, tc_id,
                                               captured_result, user_id=_owner_id)
        _catalog = task.get('_executable_tool_catalog') or []
        _names = {(tool.get('function') or {}).get('name') for tool in _catalog
                  if isinstance(tool, dict)} - {None}
        _program_input = describe_program_input(task, _retained_input,
                                                user_id=_owner_id, catalog_names=_names)

    # The canonical V2 envelope is server-owned evidence. Keep it only through
    # the current round's aggregate-budget pass, and place the sparse projection
    # in messages/events. This prevents byte counters, empty freshness fields,
    # evidence ids, and contract plumbing from consuming model context.
    tool_result_evidence = None
    if _uses_v2_result_contract and isinstance(tool_content, str):
        from lib.tools.result_envelope import split_tool_result_delivery, with_program_input

        internal_envelope = (with_program_input(tool_content, _program_input)
                             if _program_input is not None else tool_content)
        delivery = split_tool_result_delivery(internal_envelope)
        tool_content = _project_spilled_artifact_alias(
            task, round_entry, fn_name, internal_envelope, delivery.model_text)
        tool_result_evidence = delivery.evidence
        task.setdefault('_pending_tool_result_envelopes', {})[tc_id] = (
            internal_envelope)

    # Stamp semantic compaction against the model-visible value, not against
    # the larger server envelope that never enters provider context. The L0
    # stamp claims the model never saw the full result, so it requires real
    # withholding: a truncated V2 envelope (bounded preview / artifactRef
    # recovery), or any shrink in the legacy/clamp lanes, which only shrink
    # by replacing content with a placeholder. A complete V2 "ok" envelope
    # merely re-serializes a structured result (sorted keys, tight
    # separators); that can shrink chars while losing nothing, and stamping
    # it renders a false COMPACTED pill. An unparseable envelope
    # (evidence is None) means the hard clamp mangled it — real loss.
    _l0_withheld_content = (not _uses_v2_result_contract
                            or tool_result_evidence is None
                            or bool(tool_result_evidence.get('truncated')))
    if (round_entry
            and isinstance(tool_content, str)
            and _l0_pre_chars > len(tool_content)
            and _l0_withheld_content):
        round_entry['compactionLayer'] = 'L0'
        round_entry['compactedFromChars'] = _l0_pre_chars
        round_entry['compactedToChars'] = len(tool_content)

    # Successful settlement is terminal even when the display adapter emits
    # no result cards. Otherwise the task-end sweep mislabels a returned tool
    # as aborted. Explicit refusal/failure/interruption verdicts stay intact.
    if round_entry is not None:
        if terminal_status:
            round_entry['status'] = terminal_status
        elif round_entry.get('status') in ('searching', 'executing'):
            round_entry['status'] = 'done'

    # Emit tool_complete AFTER budgeting so that toolContent
    #   reflects the ACTUAL content given to the model (budgeted/
    #   persisted form).  Preview must show what the model sees.
    try:
        if isinstance(tool_content, str):
            tc_content_str = tool_content
        else:
            tc_content_str = json.dumps(tool_content, ensure_ascii=False)
        # L0/V2 and the hard result ceiling already bound this value. A second
        # display-only truncation here would make Continue replay different
        # bytes from the content just returned to the model.

        # Persist toolContent on round_entry so checkpoint writes
        #   it to DB.  Without this, crash-recovery loses tool
        #   context and Continue rolls back ALL tool rounds
        #   (toolContent == null → incomplete).
        if round_entry:
            round_entry['toolContent'] = tc_content_str
            if tool_result_evidence is not None:
                round_entry['toolResultEvidence'] = dict(
                    tool_result_evidence)

        # ``spawn_agents`` can finish an entire fast wave before its returned
        # handle reaches this round. Completion callbacks then had nowhere to
        # attach their snapshot (no live handle match and no durable round
        # yet), leaving history with the "details were not persisted" shell.
        # Settlement is the first guaranteed handle-readable boundary: repair
        # the exact round from the still-authoritative active session. The
        # snapshot helper is monotonic/idempotent, so a callback that already
        # won remains untouched.
        if fn_name == 'spawn_agents' and round_entry is not None:
            from lib.swarm.snapshot import (
                reconcile_spawn_round_from_active_session,
            )
            reconcile_spawn_round_from_active_session(task, round_entry)
        # Per-tool token count: gives the frontend an accurate
        # measure of the cost the model actually pays for this
        # result. Falls back to 0 on backend failure; the
        # frontend then renders chars instead.
        _tc_tokens = _safe_count_tokens(tc_content_str,
                                        model=task.get('model', '') if task else '')
        if round_entry and _tc_tokens > 0:
            round_entry['toolTokens'] = _tc_tokens
        if isinstance(tool_content, str):
            remember_full_result(
                task,
                tool_name=fn_name,
                arguments=fn_args,
                tool_call_id=tc_id,
                projection=unchanged_projection,
                final_model_content=tool_content,
                result_evidence=tool_result_evidence,
                enabled=unchanged_enabled,
                final_model_tokens=_tc_tokens,
            )

        # Timing: carry the round's own clocks onto the terminal frame so the
        # row stays self-describing on a cold replay that never saw the
        # tool_start (see _finalize_tool_round for the same contract).
        #
        # Write them back onto the round. Stamping only the event is not enough:
        #   cold projections ship the whole ``toolRounds`` objects, so a lane
        #   that settled here without writing back
        #   left its round with a tStart and NO tEnd — the execution segment
        #   unresolvable exactly on the recovery paths a user takes when
        #   investigating a slow turn (and for any client with no SSE at all).
        #   ``or task_execution_time_ms(task)`` is load-bearing in the other direction: a round that
        #   already went through ``_finalize_tool_round`` carries the REAL
        #   completion instant, and overwriting it with a later "now" would
        #   silently shrink a slow tool's measured duration toward zero.
        _t_start = (round_entry or {}).get('tStart')
        _t_end = (round_entry or {}).get('tEnd') or task_execution_time_ms(task)
        if _t_start is None:
            _t_start = _t_end
        if round_entry is not None:
            round_entry['tStart'] = _t_start
            round_entry['tEnd'] = _t_end

        _evt = build_event(
            EventType.TOOL_COMPLETE,
            roundNum=rn,
            toolCallId=tc_id,
            toolName=fn_name,
            toolContent=tc_content_str,
            tStart=_t_start,
            tEnd=_t_end,
        )
        if isinstance((round_entry or {}).get('inspectorRef'), dict):
            _evt['inspectorRef'] = dict(round_entry['inspectorRef'])
        if tool_result_evidence is not None:
            _evt['toolResultEvidence'] = dict(tool_result_evidence)
        if ((round_entry or {}).get('toolModelContent') is not None
                and not task.get('_transientRuntime')):
            _evt['toolModelContent'] = round_entry['toolModelContent']
        # Terminal verdict (). A lane that never executed the tool
        #   settles with a NON-SUCCESS verdict. Stamp it on the round so the
        #   spinner stops NOW instead of at the end-of-task dangling sweep, and
        #   carry it on the wire so the client does not promote it to 'done' —
        #   rendering a refused write as applied is strictly worse than the
        #   latency this settle removes.
        _status = terminal_status or (round_entry or {}).get('status')
        if terminal_status and round_entry is not None:
            round_entry['status'] = terminal_status
        if _status and _status not in ('done', 'searching', 'executing'):
            _evt['status'] = _status
        _rejection = tool_rejection_descriptor(round_entry)
        if _rejection is not None:
            stamp_tool_rejection(_evt, _rejection)
        if _tc_tokens > 0:
            _evt['toolTokens'] = _tc_tokens
        if round_entry and round_entry.get('compactionLayer'):
            _evt['compactionLayer'] = round_entry['compactionLayer']
            _evt['compactedFromChars'] = round_entry.get('compactedFromChars')
            _evt['compactedToChars'] = round_entry.get('compactedToChars')
            if round_entry.get('rawToolTokens'):
                _evt['rawToolTokens'] = round_entry['rawToolTokens']
        append_event(task, _evt)
    except Exception as e:
        logger.warning(
            '[Task %s] tool_complete event error for tool=%s at round %d (non-fatal): %s',
            tid, fn_name, round_num, e, exc_info=True)

    from lib.tasks_pkg.working_tool_sources import settle_tool_source
    try:
        settle_tool_source(task, fn_name, tc_id, round_entry, captured_result)
    except (ValueError, PermissionError):
        logger.debug('[WorkingSource] Unavailable scope or invalid canonical text; no source issued', exc_info=True)

    _settled[tc_id] = tool_content
    _store_tool_call_id_receipt(task, tc_id, {
        'signature': _call_id_signature(fn_name, fn_args),
        'name': fn_name,
        'status': terminal_status or (round_entry or {}).get('status') or 'done',
    })
    from lib.tasks_pkg.tool_call_outcome import audit_round_outcome
    audit_round_outcome(
        task, round_entry,
        status=terminal_status or (round_entry or {}).get('status') or 'done')
    return tool_content


def _screenshot_display_content(model: str, tool_content: dict) -> tuple[str, bool]:
    """Resolve what a screenshot result should DISPLAY, and whether the active
    model can actually see it.

    Returns ``(display_text, is_no_vision)``.

    WHY THIS IS A FUNCTION (). The screenshot branch used to live
    entirely in the post-phase, justified as "the verdict depends on the model's
    vision capability, which is resolved later". That reasoning was WRONG:
    ``model`` is a parameter of ``execute_tool_pipeline`` and
    ``model_supports_vision(model) -> bool`` is a pure function of it, so the
    verdict is knowable the instant the tool returns. Measured consequence of
    the old placement: a zero-cost screenshot beside a 1.2s ``web_search``
    emitted its ``tool_complete`` AFTER the slow sibling — and a browser
    screenshot is one of the calls a user is most likely to read as "stuck".

    Only the MESSAGE side genuinely has to stay in the post-phase: appending the
    multimodal / placeholder ``role:'tool'`` message must follow the model's
    original tool-call order.
    """
    if model and not model_supports_vision(model):
        # Text-only model: never build an image_url block (build_body would
        # strip it later and leave a misleading "analyze it visually" text).
        # Return a truthful result so the model knows the image is unreadable
        # and stops re-rendering / re-reading images.
        custom_fallback = tool_content.get('_no_vision_fallback')
        if custom_fallback:
            return (str(custom_fallback), True)
        return (
            '[Image not shown — the current model (%s) has no vision '
            'support, so this image cannot be analyzed. Do not retry '
            'reading images; rely on text, code, and test output '
            'instead.]' % model,
            True,
        )
    return (tool_content.get('_text_fallback', '') or 'Image captured.', False)


_INLINE_MODEL_IMAGE_MAX_BYTES = 256 * 1024
_INLINE_MODEL_IMAGES_PER_TURN_MAX_BYTES = 1024 * 1024


def _prepare_screenshot_replay(task, row, screenshot, model, tool_name, call_id):
    """Freeze the multimodal model receipt before the terminal event is sent."""
    if row is None or row.get('toolModelContent') is not None:
        return
    if _screenshot_display_content(model, screenshot)[1]:
        return
    carrier = []
    _append_screenshot_message(carrier, call_id, screenshot)
    blocks = carrier[0]['content']
    if task.get('_userId') and not task.get('_transientRuntime'):
        from types import SimpleNamespace
        from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
        payloads = []
        for block in blocks:
            if block.get('type') == 'image_url':
                header, encoded = block['image_url']['url'].split(',', 1)
                payloads.append({'mimeType': header[5:].split(';')[0], 'data': encoded})
        capture_mcp_result_images(
            task, SimpleNamespace(image_contents=payloads),
            source_tool=tool_name, tool_call_id=call_id, reference_field='_toolModelImages')
        refs = {ref.get('sourceOrdinal'): ref for ref in task.get('_toolModelImages', [])
                if ref.get('toolCallId') == call_id and ref.get('sourceTool') == tool_name}
        projected = []
        live_projected = []
        ordinal = 0
        for block in blocks:
            if block.get('type') != 'image_url':
                projected.append(block)
                live_projected.append(block)
                continue
            if ordinal in refs:
                projected.append({'type': 'image_url', 'image_url': {
                    'url': 'tofu-media:' + refs[ordinal]['attachmentId']}})
                live_projected.append(block)
            else:
                # Retention failure must not blind the active model. A small
                # inline receipt is the bounded storage-failure fallback;
                # larger pixels remain available to this call and the next
                # model-view checkpoint, with an honest cold-replay marker.
                url = block['image_url']['url']
                encoded_bytes = (len(url.encode('utf-8')) if len(url) <= _INLINE_MODEL_IMAGE_MAX_BYTES
                                 else _INLINE_MODEL_IMAGE_MAX_BYTES + 1)
                inline_used = int(task.get('_inlineModelImageBytes') or 0)
                if (encoded_bytes <= _INLINE_MODEL_IMAGE_MAX_BYTES
                        and inline_used + encoded_bytes <= _INLINE_MODEL_IMAGES_PER_TURN_MAX_BYTES):
                    projected.append(block)
                    task['_inlineModelImageBytes'] = inline_used + encoded_bytes
                else:
                    projected.append({'type': 'text', 'text':
                        '[Image unavailable on cold replay: the source could not be retained.]'})
                live_projected.append(block)
            ordinal += 1
        blocks = projected
        screenshot['_modelReplayBlocks'] = live_projected
        screenshot['_modelReplaySources'] = [
            block['image_url']['url'] for block in projected
            if block.get('type') == 'image_url' and block['image_url']['url'].startswith('tofu-media:')]
    row['toolModelContent'] = blocks
    screenshot.setdefault('_modelReplayBlocks', blocks)


def _apply_round_aggregate_budget(
    task: dict[str, Any],
    parsed_tcs: list[tuple],
    round_results: list[tuple[str, str, str]],
    appended_tool_messages: list[dict[str, Any]],
    *,
    settled_results: dict[str, Any] | None = None,
) -> None:
    """Compact oversized model-visible results and reconcile their UI rows.

    Only messages appended by the current tool round may be rewritten.  A
    positional call id can recur in history, so scanning the full message list
    would mutate an already-cached result and break the next request prefix.
    """
    if not round_results:
        pending_envelopes = task.get('_pending_tool_result_envelopes')
        if isinstance(pending_envelopes, dict):
            for parsed_call in parsed_tcs:
                pending_envelopes.pop(parsed_call[2], None)
            if not pending_envelopes:
                task.pop('_pending_tool_result_envelopes', None)
        return

    from lib.context_experiment_flags import normalize_context_experiment_flags
    result_contract = normalize_context_experiment_flags(
        (task or {}).get('config') or {})['tools']['resultEnvelope']
    pending_envelopes = task.get('_pending_tool_result_envelopes')
    if not isinstance(pending_envelopes, dict):
        pending_envelopes = {}
    aggregate_results = {
        tool_call_id: (
            pending_envelopes.get(tool_call_id, content)
            if result_contract == 'v2' else content,
            tool_name,
            tool_call_id,
        )
        for tool_call_id, content, tool_name in round_results
        if isinstance(content, str)
    }
    conversation_id = task.get('convId', '') if task else ''
    original_chars_by_call_id = {
        tool_call_id: len(content)
        for tool_call_id, content, _ in round_results
        if isinstance(content, str)
    }
    if result_contract == 'v2':
        from lib.tasks_pkg.manager import task_user_id
        recovery_by_call_id = {
            parsed_call[2]: {
                'policy': _tool_result_recovery_policy(task, parsed_call[1]),
                'arguments': parsed_call[3],
            }
            for parsed_call in parsed_tcs
            if parsed_call[2] in aggregate_results
        }
        updated_results = enforce_round_aggregate_budget_v2(
            aggregate_results,
            user_id=task_user_id(task),
            model=(task or {}).get('model', ''),
            observed_at_ms=int((task or {}).get('_observedAtMs') or 0),
            recovery_by_call_id=recovery_by_call_id,
        )
    else:
        updated_results = enforce_round_aggregate_budget(
            aggregate_results, conv_id=conversation_id)

    parsed_by_call_id = {
        parsed_call[2]: parsed_call for parsed_call in parsed_tcs
    }
    for message in appended_tool_messages:
        if message.get('role') != 'tool':
            continue
        tool_call_id = message.get('tool_call_id', '')
        if tool_call_id not in updated_results:
            continue
        new_internal_content, _, _ = updated_results[tool_call_id]
        tool_result_evidence = None
        if result_contract == 'v2' and isinstance(new_internal_content, str):
            from lib.tools.result_envelope import split_tool_result_delivery

            delivery = split_tool_result_delivery(new_internal_content)
            _parsed_call = parsed_by_call_id.get(tool_call_id)
            new_content = _project_spilled_artifact_alias(
                task,
                _parsed_call[5] if _parsed_call is not None else None,
                _parsed_call[1] if _parsed_call is not None else '',
                new_internal_content, delivery.model_text)
            tool_result_evidence = delivery.evidence
            pending_envelopes[tool_call_id] = new_internal_content
        else:
            new_content = new_internal_content

        parsed_call = parsed_by_call_id.get(tool_call_id)
        if parsed_call is not None:
            round_entry = parsed_call[5]
            if round_entry and tool_result_evidence is not None:
                round_entry['toolResultEvidence'] = dict(
                    tool_result_evidence)
        _existing_content = message.get('content')
        _existing_text = _existing_content
        if isinstance(_existing_content, list):
            # Multimodal projection: the budget rewrite owns the text part;
            # compare against it so an unchanged result is a true no-op
            # (no spurious L0 compaction stamp/event for an image message).
            _existing_text = next(
                (block.get('text') for block in reversed(_existing_content)
                 if isinstance(block, dict) and block.get('type') == 'text'),
                None)
        if (new_content == _existing_content
                or (_existing_text is not None
                    and new_content == _existing_text)):
            continue
        if isinstance(_existing_content, list) and isinstance(new_content, str):
            # Multimodal projection (retained tofu-media image blocks plus a
            # text receipt): compact only the text part. The image blocks
            # remain valid evidence beside the compacted text; dropping them
            # here would re-blind a vision model the projection just served.
            _kept_blocks = [block for block in _existing_content
                            if not (isinstance(block, dict)
                                    and block.get('type') == 'text')]
            _kept_blocks.append({'type': 'text', 'text': new_content})
            message['content'] = _kept_blocks
            if parsed_call is not None:
                _mv_row = parsed_call[5]
                if isinstance(_mv_row, dict) and isinstance(
                        _mv_row.get('toolModelContent'), list):
                    import copy as _copy_mv
                    _mv_row['toolModelContent'] = _copy_mv.deepcopy(
                        _kept_blocks)
        else:
            message['content'] = new_content

        if parsed_call is not None:
            round_entry = parsed_call[5]
            entry_round_num = parsed_call[4]
            tool_name = parsed_call[1]
            if round_entry:
                content_text = (
                    new_content
                    if isinstance(new_content, str)
                    else str(new_content)
                )
                round_entry['toolContent'] = content_text
                original_chars = original_chars_by_call_id.get(tool_call_id, 0)
                compacted_chars = len(content_text)
                round_entry['compactionLayer'] = 'L0'
                round_entry['compactedFromChars'] = original_chars
                round_entry['compactedToChars'] = compacted_chars

                # Parse the internal envelope, not the sparse model text.
                _register_spilled_artifact_origin(
                    task, round_entry, tool_name, new_internal_content)
                round_entry['toolTokens'] = _safe_count_tokens(
                    content_text,
                    model=task.get('model', '') if task else '',
                )
                try:
                    compacted_event = build_event(
                        EventType.TOOL_COMPACTED,
                        roundNum=entry_round_num,
                        toolCallId=tool_call_id,
                        toolName=tool_name,
                        compactionLayer='L0',
                        compactedFromChars=original_chars,
                        compactedToChars=compacted_chars,
                        toolTokens=round_entry.get('toolTokens', 0),
                        compactedContent=content_text,
                    )
                    if tool_result_evidence is not None:
                        compacted_event['toolResultEvidence'] = dict(
                            tool_result_evidence)
                    append_event(task, compacted_event)
                    logger.info(
                        '[L0] tool_compacted emitted: tc_id=%s tool=%s '
                        'round=%s %dch→%dch (%+.0f%% chars)',
                        tool_call_id[:12] if tool_call_id else '?',
                        tool_name or '?', entry_round_num,
                        original_chars, compacted_chars,
                        (compacted_chars / original_chars - 1) * 100
                        if original_chars else 0,
                    )
                except Exception as event_error:
                    logger.warning(
                        '[L0] tool_compacted SSE emit failed: tc_id=%s '
                        'tool=%s round=%s err=%s',
                        tool_call_id[:12] if tool_call_id else '?',
                        tool_name or '?', entry_round_num, event_error,
                    )
        settled = (settled_results if settled_results is not None
                   else task.get('_settled_tool_results'))
        if isinstance(settled, dict):
            settled[tool_call_id] = new_content

    # Internal envelopes exist only until the current round's shared-budget
    # reconciliation. Dedup caches retain their own bounded copy where useful;
    # this transient map must not grow with a long-running task.
    for tool_call_id, _, _ in round_results:
        pending_envelopes.pop(tool_call_id, None)
    if not pending_envelopes:
        task.pop('_pending_tool_result_envelopes', None)


def _git_guard_presence_roots(command, project_path, fn_args, conv_id):
    """Attribute presence to each destructive Git checkout, not dispatch cwd.

    Resolve the same explicit/sticky cwd as the project runner, then literal
    shell cd and Git -C hops. Git global options stop at the subcommand: a
    commit message or pathspec containing -C must not select a different root.
    Unresolvable paths fall back to the existing approval gate via the caller.
    """
    from lib.project_mod.config import get_conv_cwd
    from lib.project_mod.path_resolution import _resolve_base
    from lib.project_mod.tools import _sticky_cwd_enabled
    from lib.project_mod.git_command_scope import git_command_scopes

    cwd = project_path or ''
    working_dir = fn_args.get('working_dir')
    if working_dir:
        base, relative = _resolve_base(cwd, working_dir, conv_id=conv_id)
        cwd = os.path.join(base, relative) if relative and relative != '.' else base
    elif conv_id and _sticky_cwd_enabled():
        cwd = get_conv_cwd(conv_id) or cwd
    if not cwd:
        return ['']  # An unattached task cannot borrow the server's cwd/presence.
    return list(dict.fromkeys(
        root for scope in git_command_scopes(command, cwd, environment=os.environ)
        if scope.classification == 'tree_destroying'
        for root in scope.presence_roots()))


def _approval_stops_tool_call(
    *,
    task: dict[str, Any],
    fn_name: str,
    fn_args: dict[str, Any],
    tc_id: str,
    rn: int,
    round_entry: dict[str, Any] | None,
    confirmation_tools: set[str] | frozenset[str],
    write_tools: set[str] | frozenset[str],
    attended: bool,
    auto_apply: bool,
    cfg: dict[str, Any],
    project_path: str | None,
    round_num: int,
    model: str,
    tool_results: dict[str, tuple[Any, bool]],
    idempotent_tools: set[str] | frozenset[str],
    cache: dict[str, Any],
    tid: str,
    internal_result_sink: Any = None,
    settled_results: dict[str, Any] | None = None,
    receipt_required: bool = False,
) -> bool:
    """Apply confirmation/manual-write policy; return true when rejected.

    Always-confirm tools fail closed when no human is attending.  Ordinary
    writes only enter this boundary in Manual mode, with the existing
    read-only-command and durable browser-grant exceptions preserved.
    """
    # Approval receipts and Git-presence decisions must bind the checkout
    # that will actually execute, including retained canonical path literals.
    from lib.tasks_pkg.workspace_delivery import rebind_project_tool_arguments
    if fn_name in {'run_command', 'write_file', 'edit_file', 'read_files',
                   'grep_search', 'find_files', 'repo_map', 'inspect_image'}:
        effective_arguments = rebind_project_tool_arguments(task, fn_args)
        if effective_arguments is not fn_args:
            fn_args.clear()
            fn_args.update(effective_arguments)
    from lib.tasks_pkg.executor._file_recovery import check_file_recovery, recovery_receipt
    file_recovery = check_file_recovery(task, fn_name, fn_args, round_entry or {}, project_path)
    recovery_refusal = file_recovery.refusal
    if file_recovery.unknown and not recovery_refusal:
        if recovery_receipt(task, fn_name, tc_id, fn_args, file_recovery):
            return False
        if attended:
            approved, recovery_refusal = _handle_approval(
                task, fn_name, fn_args, rn, round_entry, project_path, round_num, model,
                cfg=cfg, mint_receipt=True, file_recovery=file_recovery)
            if task.get('aborted') or approved:
                return False
        else:
            from lib.tools.result_envelope import typed_tool_error
            recovery_refusal = typed_tool_error('tool_effect_takeover_required', retryable=False,
                message='An earlier write to this file remains unknown. A new write requires an attended human decision.',
                next_action='Report this unresolved operation to the owner; independent work may continue.').to_model_text()
    if recovery_refusal:
        stamp_tool_rejection(round_entry, {'kind': 'effect_recovery_required', 'tool': fn_name},
                             reason=recovery_refusal, retryable=False)
        tool_results[tc_id] = (recovery_refusal, False)
        _settle_tool_result(task, fn_name, tc_id, fn_args, rn, round_entry, recovery_refusal,
            idempotent_tools=idempotent_tools, cache=cache, tid=tid, round_num=round_num,
            terminal_status='rejected', internal_result_sink=internal_result_sink,
            settled_results=settled_results)
        return True

    # ── Git workspace guard (docs/runbooks/GIT_WORKSPACE_GUARD.md) ──

    git_approval_diagnostics = None
    # A git-carrying run_command is decided by the EXECUTING agent, not the
    # human: safe writes and solo tree-destroyers run with an automatic
    # receipt; a tree-destroyer with active sibling conversations is refused
    # with conservative project-presence evidence, not a lock/conflict claim.
    # Unknown shapes fall through to
    # the existing attended gate below.
    if fn_name == 'run_command' and not task.get('aborted'):
        from lib.project_mod import git_guard as _git_guard
        _gg_authority = ''
        _gg_refusal = None
        try:
            _gg_class = _git_guard.classify(fn_args.get('command', ''))

            if _gg_class is None and _git_guard.enabled():
                diagnostic = _git_guard.command_diagnostics(fn_args.get('command', ''))
                if diagnostic['segments']:
                    git_approval_diagnostics = {
                        **diagnostic, 'execution': 'not_started',
                        'evidenceLevel': 'syntax_unresolved',
                    }
            if _gg_class == 'safe_write':
                _gg_authority = 'git_safe_write'
            elif _gg_class == 'tree_destroying':
                _gg_command = fn_args.get('command', '')
                _gg_conv = str(task.get('convId') or '')
                _gg_roots = _git_guard_presence_roots(
                    _gg_command, project_path, fn_args, _gg_conv)
                if not _gg_roots:
                    raise ValueError('Destructive Git target could not be resolved')
                _gg_root = _gg_roots[0]
                _ack = _git_guard.extract_ack(_gg_command)
                if _git_guard.force_delete_targets_merged(
                        _gg_command, _gg_roots):
                    # `git branch -D` whose every target is provably merged
                    # can neither orphan commits nor break a worktree (Git
                    # refuses to delete a checked-out branch), so active
                    # peers are irrelevant — take the safe-write receipt.
                    _gg_authority = 'git_safe_write'
                else:
                    if _ack:
                        _git_guard.consume_ack(_gg_root, _gg_conv, _ack)
                    _gg_peers = [
                        {**peer, 'checkoutRoot': root} for root in _gg_roots
                        for peer in _git_guard.active_peers(
                            root, task.get('_userId'), exclude_conv=_gg_conv)
                    ]
                    if not _gg_peers:
                        _gg_authority = 'agent_git_guard_solo'
                    else:
                        _gg_refusal = _git_guard.format_refusal(
                            _gg_command, _gg_peers, '', roots=_gg_roots)
        except Exception as _gg_exc:
            # Fail open to the existing attended gate; never break dispatch.
            logger.debug('[ToolPipeline] git guard failed open: %s', _gg_exc)
            _gg_authority, _gg_refusal = '', None
        # A known high-risk Git segment still triggers presence policy, but
        # solo presence cannot authorize opaque code elsewhere in the chain.
        if _gg_authority and not _git_guard.automatic_receipt_eligible(
                fn_args.get('command', '')):
            _gg_authority = ''
        if _gg_authority:
            from lib.tasks_pkg.tool_dispatch._approval import (
                mint_approval_receipt)
            if mint_approval_receipt(
                    task, fn_name, tc_id, fn_args,
                    authority=_gg_authority):
                return False
            # Receipt failure: fall through to the existing gate.
        elif _gg_refusal is not None:
            stamp_tool_rejection(
                round_entry,
                {
                    'kind': 'git_guard', 'tool': fn_name,
                    'condition': 'active_project_presence_settled',

                    'gitDiagnostics': _git_guard.refusal_diagnostics(
                        fn_args.get('command', '')),
                    'retryPolicy': {
                        'mode': 'after_condition_change',
                        'automatic': False,
                    },
                },
                reason=_gg_refusal, retryable=True,
            )
            tool_results[tc_id] = (_gg_refusal, False)
            _settle_tool_result(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                _gg_refusal, idempotent_tools=idempotent_tools,
                cache=cache, tid=tid, round_num=round_num,
                terminal_status='rejected',
                internal_result_sink=internal_result_sink,
                settled_results=settled_results)
            return True

    # Only registry-declared writes accept Auto as receipt authority. Custom
    # receipt-requiring tools retain attended confirmation by default.
    # Strictest-wins: the registry-declared Auto exception
    # (``ToolSpec.auto_approval_tools``, a validated subset of
    # ``confirmation_tools``) must not bypass the per-call demotions below —
    # a destructive ``run_command`` still requires confirmation even in Auto,
    # and a browser tool without a durable grant still asks. Only the narrow
    # documented exceptions (navigate/close_tab/research_page, or a live
    # domain grant) may demote a confirmation tool.
    _demoted_by_exception = False
    if fn_name in confirmation_tools:
        from lib.tools.registry import all_specs
        if fn_name == 'run_command':
            from lib.project_mod.command_analysis import _is_destructive_command
            _demoted_by_exception = not _is_destructive_command(
                fn_args.get('command', ''))
        elif fn_name.startswith('browser_'):
            if fn_name in (
                    'browser_navigate', 'browser_close_tab',
                    'browser_research_page'):
                _demoted_by_exception = True
            else:
                try:
                    from lib.browser.access import browser_tool_access
                    browser_tool_access(
                        fn_name, fn_args,
                        owner_user_id=str(task.get('_userId') or ''),
                        client_id=str(cfg.get('browserClientId') or ''))
                    _demoted_by_exception = True
                except Exception as exc:
                    logger.debug(
                        '[ToolPipeline] browser access check keeps approval '
                        'for %s: %s', fn_name, exc)
                    _demoted_by_exception = False
        else:
            # Registry-declared Auto authority (skills.md trust boundary); undeclared keeps strictest.
            _demoted_by_exception = any(fn_name in spec.auto_approval_tools for spec in all_specs())
    if (fn_name in confirmation_tools and auto_apply and not task['aborted']
            and _demoted_by_exception):
        from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt
        allowed = any(fn_name in spec.auto_approval_tools for spec in all_specs())
        if allowed and mint_approval_receipt(
                task, fn_name, tc_id, fn_args, authority='auto_write_mode'):
            return False
    def git_approval_hint(content):
        if not git_approval_diagnostics:
            return content
        return (str(content or '') + '\n[git-guard] Syntax or external execution could not '
                'be proven ordinary; this is not evidence of a file conflict or '
                'active writer. Inspect exact Git options and target/index state. '
                'Unknown/signing forms require the existing approval; do not '
                'disable protections, force an overwrite or retry unchanged.')

    requires_confirmation = (
        fn_name in confirmation_tools and not task['aborted'])
    if requires_confirmation and not attended:
        reject_content = (
            f'{fn_name} requires attended human confirmation and was not '
            'executed in this unattended task.')

        reject_content = git_approval_hint(reject_content)
        stamp_tool_rejection(
            round_entry,
            {'kind': 'confirmation_required', 'tool': fn_name,
             **({'gitDiagnostics': git_approval_diagnostics} if git_approval_diagnostics else {})},
            reason=reject_content, retryable=False,
        )
        tool_results[tc_id] = (reject_content, False)
        _settle_tool_result(
            task, fn_name, tc_id, fn_args, rn, round_entry,
            reject_content, idempotent_tools=idempotent_tools,
            cache=cache, tid=tid, round_num=round_num,
            terminal_status='rejected',
            internal_result_sink=internal_result_sink,
            settled_results=settled_results)
        return True

    needs_approval = requires_confirmation or (
        fn_name in write_tools
        and attended and not auto_apply and not task['aborted']
        and not (round_entry and round_entry.get('toolName') == 'code_exec')
    )
    if needs_approval and fn_name == 'run_command':
        from lib.project_mod.command_analysis import _is_destructive_command
        needs_approval = _is_destructive_command(fn_args.get('command', ''))

    if needs_approval and fn_name.startswith('browser_'):
        if fn_name in (
                'browser_navigate', 'browser_close_tab',
                'browser_research_page'):
            needs_approval = False
        else:
            try:
                from lib.browser.access import browser_tool_access
                browser_tool_access(
                    fn_name, fn_args,
                    owner_user_id=str(task.get('_userId') or ''),
                    client_id=str(cfg.get('browserClientId') or ''))
                needs_approval = False
            except Exception as exc:
                logger.debug(
                    '[ToolPipeline] browser access check requires approval '
                    'for %s: %s', fn_name, exc)
                needs_approval = True

    if not needs_approval:
        if receipt_required:
            from lib.tasks_pkg.tool_dispatch._approval import mint_approval_receipt
            if not mint_approval_receipt(task, fn_name, tc_id, fn_args, authority='auto_write_mode'):
                raise ValueError('Automatic tool policy could not bind its receipt')
        return False

    # Approval delegation: an agent-registered policy (set_approval_delegate)
    # adjudicates before the human gate. Fail-closed by construction:
    #   * never applies to child invocations (no human-interaction port, and
    #     nested adjudication must not multiply cost),
    #   * an approve binds a single-use 'agent_adjudicator' receipt to the
    #     exact call — the same discipline as a human decision,
    #   * an undecided/unavailable adjudicator falls THROUGH to the human
    #     gate; a delegate can never make the gate weaker.
    from lib.tasks_pkg.invocation import is_child_invocation
    if not is_child_invocation(task):
        from lib.tasks_pkg.approval_delegate import (
            adjudicate_tool_call, approval_delegate_for)
        delegate_policy = approval_delegate_for(task, fn_name)
        if delegate_policy is not None:
            verdict = adjudicate_tool_call(
                task, fn_name=fn_name, fn_args=fn_args,
                policy=delegate_policy, model=model)
            if verdict is not None and verdict.get('approve') is True:
                from lib.tasks_pkg.tool_dispatch._approval import (
                    mint_approval_receipt)
                if mint_approval_receipt(
                        task, fn_name, tc_id, fn_args,
                        authority='agent_adjudicator'):
                    if round_entry is not None:
                        round_entry['approvalDelegate'] = {
                            'decision': 'approved',
                            'reason': str(verdict.get('reason') or '')[:300],
                            'authority': 'agent_adjudicator',
                        }
                    logger.info(
                        '[Task %s] %s approved by adjudicator: %s',
                        tid, fn_name, str(verdict.get('reason'))[:160])
                    return False
                # Receipt binding failed — do not honor the verdict blindly;
                # fall through to the human gate.
                logger.warning('[Task %s] adjudicator approve for %s could '
                               'not bind a receipt — human gate decides',
                               tid, fn_name)
            elif verdict is not None and verdict.get('approve') is False:
                reject_content = (
                    f'⚠️ Approval delegate denied this {fn_name} call: '
                    f'{verdict.get("reason") or "denied by policy"}. You may '
                    'adjust the call to fit the policy, update the policy '
                    'with set_approval_delegate, or let it reach the human '
                    'gate by clearing the delegate first.')
                stamp_tool_rejection(
                    round_entry,
                    {'kind': 'approval_denied', 'tool': fn_name,
                     'authority': 'agent_adjudicator'},
                    reason=reject_content,
                    retryable=True,
                )
                tool_results[tc_id] = (reject_content, False)
                _settle_tool_result(
                    task, fn_name, tc_id, fn_args, rn, round_entry,
                    reject_content, idempotent_tools=idempotent_tools,
                    cache=cache, tid=tid, round_num=round_num,
                    terminal_status='rejected',
                    internal_result_sink=internal_result_sink,
                    settled_results=settled_results)
                return True

    approved, reject_content = _handle_approval(
        task, fn_name, fn_args, rn, round_entry, project_path,
        round_num, model, cfg=cfg, mint_receipt=requires_confirmation or receipt_required)
    if task.get('aborted') or approved:
        # Fall through to the existing post-approval abort settlement.
        return False

    reject_content = git_approval_hint(reject_content)
    stamp_tool_rejection(
        round_entry,
        {'kind': 'approval_denied', 'tool': fn_name,
         **({'gitDiagnostics': git_approval_diagnostics} if git_approval_diagnostics else {})},
        reason=reject_content or 'User rejected the tool call.',
        retryable=True,
    )
    tool_results[tc_id] = (reject_content, False)
    _settle_tool_result(
        task, fn_name, tc_id, fn_args, rn, round_entry,
        reject_content, idempotent_tools=idempotent_tools,
        cache=cache, tid=tid, round_num=round_num,
        terminal_status='rejected',
        internal_result_sink=internal_result_sink,
        settled_results=settled_results)
    return True


def _mcp_result_image_payloads(content) -> list:
    """Valid MCP ImageContent payloads carried out-of-band by *content*.

    The MCP bridge and reader-routed fetch_url results attach images to a str
    subclass; plain strings and screenshot carriers return [].
    """
    payloads = getattr(content, 'image_contents', None)
    if not isinstance(payloads, (list, tuple)) or not payloads:
        return []
    return [p for p in payloads
            if isinstance(p, dict)
            and isinstance(p.get('data'), str) and p['data'].strip()]


def _mcp_image_refs_for_call(task, call_id) -> list:
    """Existing media refs for one tool call, across both reference lanes.

    Direct MCP tools are captured by the handler adapter into ``_mcpImages``;
    the pipeline captures reader-routed fetch results into
    ``_toolModelImages``. Reusing either avoids a duplicate ingest of the same
    call's image.
    """
    if not isinstance(task, dict):
        return []
    refs = []
    for field in ('_mcpImages', '_toolModelImages'):
        for ref in task.get(field) or []:
            if isinstance(ref, dict) and ref.get('toolCallId') == call_id:
                refs.append(ref)
    return refs


def _mcp_image_blocks_for_model(task, text, payloads, call_id) -> list:
    """Multimodal content blocks: text + one image block per payload.

    Persisted images become ``tofu-media:<attachmentId>`` refs, which
    build_body's owner-scoped validation resolves to inline base64 for BOTH
    the live message and the replayed toolModelContent. A payload whose
    capture failed falls back to its inline data URL within the per-image
    budget, else to a truthful text marker — never silently dropped.
    """
    from lib.mcp.result_content import MAX_MCP_IMAGE_BYTES

    ref_by_ordinal = {
        ref.get('sourceOrdinal'): ref
        for ref in _mcp_image_refs_for_call(task, call_id)
    }
    blocks = [{'type': 'text', 'text': str(text)}]
    for ordinal, payload in enumerate(payloads):
        ref = ref_by_ordinal.get(ordinal)
        attachment_id = str((ref or {}).get('attachmentId') or '')
        if attachment_id:
            blocks.append({'type': 'image_url',
                           'image_url': {'url': f'tofu-media:{attachment_id}'}})
            continue
        compact = ''.join(payload['data'].split())
        mime = str(payload.get('mimeType') or 'image/png')
        if (len(compact) * 3) // 4 <= MAX_MCP_IMAGE_BYTES:
            blocks.append({'type': 'image_url',
                           'image_url': {'url': f'data:{mime};base64,{compact}'}})
        else:
            blocks.append({'type': 'text', 'text': (
                f'[tool image {ordinal} unavailable: media capture failed '
                f'for call {call_id}]')})
    return blocks


def _prepare_mcp_image_replay(task, row, content, model, tool_name, call_id):
    """Freeze MCP ImageContent into row['toolModelContent'] before settle.

    Mirrors _prepare_screenshot_replay's contract; text-only models keep
    plain text so no unusable image refs are projected.
    """
    if not isinstance(row, dict) or row.get('toolModelContent') is not None:
        return
    if not (model and model_supports_vision(model)):
        return
    payloads = _mcp_result_image_payloads(content)
    if not payloads:
        return
    if (isinstance(task, dict) and task.get('_userId')
            and not task.get('_transientRuntime')
            and not _mcp_image_refs_for_call(task, call_id)):
        from types import SimpleNamespace
        from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
        capture_mcp_result_images(
            task, SimpleNamespace(image_contents=payloads),
            source_tool=tool_name, tool_call_id=call_id,
            reference_field='_toolModelImages')
    row['toolModelContent'] = _mcp_image_blocks_for_model(
        task, str(content), payloads, call_id)


def _dispatch_settle_content(task, row, content, model, tool_name, call_id):
    """Retain image receipts before either dispatch lane emits completion."""
    if isinstance(content, dict) and content.get('__screenshot__'):
        _prepare_screenshot_replay(task, row, content, model, tool_name, call_id)
        return _screenshot_display_content(model, content)[0]
    if _mcp_result_image_payloads(content):
        _prepare_mcp_image_replay(task, row, content, model, tool_name, call_id)
    return content


def _canonical_settle_source(content):
    """Return the evidence-rich canonical source for an image-protocol result.

    Screenshot lanes flatten the result dict to display text for the model,
    but the canonical capture (artifact tree, gateway sink) must still see
    the dict: by the time any settle lane runs, ``_prepare_screenshot_replay``
    has stamped ``_modelReplaySources`` with the retained ``tofu-media:``
    refs, so ``capture_result`` emits text + media parts instead of a
    media-less text part that misreports retentionCoverage=complete.
    """
    if isinstance(content, dict) and (
            content.get('__screenshot__') or content.get('__batch_images__')):
        return content
    return None


def _append_settled_mcp_image_message(
    task, round_entry, tool_content, model, fn_name, tc_id, tc,
    messages, appended_tool_msgs, settle_current, fn_args, rn,
    settlement_kwargs,
) -> None:
    """Settle MCP image text, then append matching live and replay blocks."""
    _prepare_mcp_image_replay(
        task, round_entry, tool_content, model, fn_name, tc_id)
    image_payloads = _mcp_result_image_payloads(tool_content)
    if not (round_entry or {}).get('_idempotentReplay'):
        tool_content = settle_current(
            task, fn_name, tc_id, fn_args, rn, round_entry, tool_content,
            **settlement_kwargs)
    image_blocks = _mcp_image_blocks_for_model(
        task, str(tool_content), image_payloads, tc_id)
    if isinstance(round_entry, dict) and isinstance(
            round_entry.get('toolModelContent'), list):
        # The settle-time budget may have shrunk the text; keep the
        # frozen replay blocks consistent with toolContent.
        round_entry['toolModelContent'] = list(image_blocks)
    tool_msg = {'role': 'tool', 'tool_call_id': tc_id,
                'content': image_blocks}
    if isinstance(tc.get('caller'), dict):
        tool_msg['caller'] = dict(tc['caller'])
    messages.append(tool_msg)
    appended_tool_msgs.append(tool_msg)


def _missing_tool_result(task, name, call_id, round_entry):
    """Describe a collection failure without guessing absence or effect outcome."""
    from lib.tools.effect_recovery import effect_recovery_policy
    from lib.tools.result_envelope import typed_tool_error

    policy = effect_recovery_policy(task.get('_toolContractDocumentsByName'), name)
    effect = (round_entry or {}).get('executionEffect')
    retryable = policy in {'read_only', 'idempotent'} and (
        effect is None or isinstance(effect, dict) and effect.get('recovery') == policy)
    if retryable:
        next_action = (
            'The declared recovery policy permits retry through the normal tool '
            'gateway and its authorization checks. Continue independent authorized '
            'work if result collection fails again.')
    else:
        next_action = (
            'Inspect the existing operation status or durable receipt before '
            'repeating this call; its effects are unconfirmed. Continue independent '
            'authorized work and report any unresolved outcome.')
    return typed_tool_error(
        'tool_result_missing', retryable=retryable, next_action=next_action,
        message=f'The harness did not collect a result for tool "{name}" '
                f'(call "{call_id}"). Tool availability and execution outcome '
                'cannot be inferred from the missing receipt.',
    ).to_envelope_text()


def _append_ordered_tool_replay(
    _appended_tool_msgs, _cache,
    _idempotent_tools, _program_messages_to_settle, _round_results_for_budget, _settle_current,
    all_search_results_text, messages, model, parsed_tcs,
    round_num, task, tid, tool_results,
    tool_verdicts,
):
    """Publish settled results in original call order, retaining image and program receipts."""
    for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parsed_tcs:
        if tc_id in tool_results:
            tool_content, is_search = tool_results[tc_id]
        else:
            # Completed Futures have already been harvested. No receipt remains
            # from which to infer tool availability or an external effect.
            logger.warning(
                '[Task %s] conv=%s tool result missing for tool=%s tc_id=%s '
                'round=%d — returning a typed collection failure',
                tid, task.get('convId', '') if task else '',
                fn_name, tc_id, round_num)
            tool_content, is_search = (
                _missing_tool_result(task, fn_name, tc_id, round_entry), False)
            tool_verdicts[tc_id] = 'error'
        if is_search:
            _append_search_result_text(all_search_results_text, tool_content)

        # Convert screenshot dict → image_url content block for vision models.
        # The MESSAGE append is what genuinely belongs here ():
        #   the role:'tool' message must enter the list in the model's ORIGINAL
        #   tool-call order. The tool_complete EVENT was already emitted at the
        #   tool's own completion instant by the dispatch lane; the settle below
        #   is idempotent, so it returns the cached content without re-emitting
        #   (and still does the work for a screenshot that never reached a
        #   dispatch lane at all).
        if isinstance(tool_content, dict) and tool_content.get('__screenshot__'):
            _prepare_screenshot_replay(task, round_entry, tool_content, model, fn_name, tc_id)
            # ``model`` (the PARAMETER) is authoritative: the orchestrator passes
            # the round's resolved model and only MIRRORS it onto task['model']
            # afterwards, so reading the mirror risks a stale value on a
            # mid-turn model fallback — and the dispatch-time settle must reach
            # the same verdict as this post-phase message append, or the UI text
            # and the model's own tool message would disagree.
            _shot_txt, _no_vision = _screenshot_display_content(
                model, tool_content)
            if _no_vision:
                _tool_msg = {'role': 'tool', 'tool_call_id': tc_id,
                             'content': _shot_txt}
                if isinstance(tc.get('caller'), dict):
                    _tool_msg['caller'] = dict(tc['caller'])
                messages.append(_tool_msg)
                _appended_tool_msgs.append(_tool_msg)
                logger.info(
                    '[Task %s] conv=%s text-only model %s — image tool result '
                    'for tc=%s replaced with no-vision placeholder',
                    tid, task.get('convId', '') if task else '', model, tc_id)
            else:
                _append_screenshot_message(messages, tc_id, tool_content)
                if isinstance(tc.get('caller'), dict):
                    messages[-1]['caller'] = dict(tc['caller'])
                _appended_tool_msgs.append(messages[-1])
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry, _shot_txt,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num,
                canonical_content=_canonical_settle_source(tool_content))
        elif _mcp_result_image_payloads(tool_content) and model_supports_vision(model):
            # MCP ImageContent (reader-routed fetch_url, direct MCP tools):
            # same multimodal protocol as screenshots — persisted media refs
            # in toolModelContent, blocks appended in tool-call order. The
            # no-vision case falls through to the plain-text else branch.
            _append_settled_mcp_image_message(
                task, round_entry, tool_content, model, fn_name, tc_id, tc,
                messages, _appended_tool_msgs, _settle_current, fn_args, rn,
                dict(idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                     round_num=round_num, terminal_status=tool_verdicts.get(tc_id)))
        else:
            # Settle this tool (idempotent). Tools dispatched through the
            #   parallel pool / serial lanes already settled at their OWN
            #   completion instant — this call returns their cached content
            #   without re-emitting. Only tools that never went through a
            #   dispatch lane (dedup cache hits, approval rejections,
            #   pre-hook blocks, abort short-circuits, the missing-result
            #   fallback) actually do work here.
            #
            #   The LOOP itself must stay: it walks ``parsed_tcs``, so the
            #   ``role:'tool'`` messages enter the message list in the model's
            #   ORIGINAL tool-call order regardless of completion order. An
            #   out-of-order tool_call/tool_result pairing is a hard API error
            #   on Anthropic — that ordering is the reason this phase exists,
            #   and it is NOT what was making the UI wait.
            if not (round_entry or {}).get('_idempotentReplay'):
                tool_content = _settle_current(
                    task, fn_name, tc_id, fn_args, rn, round_entry, tool_content,
                    idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                    round_num=round_num,
                    terminal_status=tool_verdicts.get(tc_id))

            # Collect for aggregate budget check
            _round_results_for_budget.append((tc_id, tool_content, fn_name))

            # Multimodal gateway/composed results: a composed canonical tree
            # (execute_tools children, JavaScript childResults) can retain
            # ``tofu-media:`` refs even though its model text is a JSON
            # receipt. A vision model must receive those images as real
            # content blocks — the bare JSON string alone blinds it exactly
            # like the old inline-base64 spill did. build_body's owner-scoped
            # validation resolves the refs to inline pixels for the live
            # request, and ``toolModelContent`` freezes the same blocks for
            # replay (the MCP image path's established contract).
            _gw_media_refs = []
            if model and model_supports_vision(model):
                if isinstance(round_entry, dict):
                    _gw_media_refs = list(
                        round_entry.get('_retainedMediaRefs') or ())
                if not _gw_media_refs:
                    from lib.tools.result_value import TrustedResultText as _TRT
                    _gw_canonical = (getattr(tool_content, 'canonical', None)
                                     if isinstance(tool_content, _TRT) else None)
                    if _gw_canonical is not None:
                        _gw_media_refs = _gw_canonical.media_references()
            if _gw_media_refs:
                import copy as _copy
                _gw_blocks = [
                    {'type': 'image_url', 'image_url': {'url': _ref}}
                    for _ref in _gw_media_refs]
                _gw_blocks.append(
                    {'type': 'text', 'text': str(tool_content)})
                _tool_msg = {'role': 'tool', 'tool_call_id': tc_id,
                             'content': _gw_blocks,
                             '_modelImageSources': list(_gw_media_refs)}
                if (isinstance(round_entry, dict)
                        and round_entry.get('toolModelContent') is None):
                    round_entry['toolModelContent'] = _copy.deepcopy(_gw_blocks)
            else:
                _tool_msg = {'role': 'tool', 'tool_call_id': tc_id,
                             'content': tool_content}
            if isinstance(tc.get('caller'), dict):
                _tool_msg['caller'] = dict(tc['caller'])
            messages.append(_tool_msg)
            _appended_tool_msgs.append(_tool_msg)

        caller = tc.get('caller')
        if isinstance(caller, dict) and caller.get('type') == 'program':
            _program_messages_to_settle.append((
                tc,
                tool_verdicts.get(tc_id)
                or (round_entry or {}).get('status')
                or 'done',
                messages[-1],
            ))


def execute_tool_pipeline(
    task: dict[str, Any],
    parsed_tcs: list[tuple],
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    tool_list: list[dict] | None,
    messages: list[dict[str, Any]],
    all_search_results_text: list[str],
    round_num: int,
    model: str,
    *,
    internal_result_sink: Any = None,
    publish_direct_gateway_siblings: bool = True,
) -> bool:
    """Run the full tool-execution pipeline: approval → parallel dispatch → result append.

    Returns
    -------
    bool
        True if a tool-execution timeout occurred during this round.

    Handles three phases:

    1. **Error short-circuit** — tool calls with JSON parse errors get an
       error result returned to the LLM without execution.
    2. **Serial approval** — write operations (``write_file``, ``apply_diff``)
       and server-kill commands that require user approval are executed one
       at a time, blocking until the user approves or rejects.
    3. **Parallel execution** — all remaining tool calls run concurrently
       in a :class:`~concurrent.futures.ThreadPoolExecutor`.

    After execution, tool result messages are appended to *messages* in the
    original tool-call order, and ``tool_complete`` events are emitted.

    Parameters
    ----------
    task : dict
        Live task dict — mutated (events appended, toolRounds updated).
    parsed_tcs : list[tuple]
        7-tuples from :func:`parse_tool_calls`.
    cfg : dict
        Task configuration dict (``autoApply``, etc.).
    project_path : str
        Filesystem path to the project root.
    project_enabled : bool
        Whether project-mode tools are active.
    tool_list : list | None
        Full tool definitions (passed through to ``_execute_tool_one``).
    messages : list[dict]
        Conversation message list — tool result messages appended in-place.
    all_search_results_text : list[str]
        Accumulator for search result text — appended in-place.
    round_num : int
        Current zero-based loop round (for snapshot labels and logging).
    model : str
        Current model identifier (for logging).
    internal_result_sink : optional
        Transient, byte-bounded receiver for post-hook/pre-compaction results.
        Used only by the local JavaScript backend; never retained on ``task``.

    publish_direct_gateway_siblings : bool
        Publish the direct-call multiset for this provider response. Nested
        gateway-child pipelines pass ``False`` so they cannot replace their
        top-level carrier's response-scoped delegation state.
    """
    tid = task['id'][:8]
    # One dispatch invocation owns one settlement barrier.  In particular, a
    # nested execute_tools pipeline must not share or retain the outer call's
    # complete results through the task dict.
    _settled_results: dict[str, Any] = {}
    task.pop('_settled_tool_results', None)  # repair pre-C234 live task state

    def _settle_current(*args: Any, **kwargs: Any) -> Any:
        kwargs['internal_result_sink'] = internal_result_sink
        kwargs['context_messages'] = messages
        kwargs['unchanged_receipt_tools'] = unchanged_receipt_tools
        kwargs['settled_results'] = _settled_results
        return _settle_tool_result(*args, **kwargs)
    # Auto-apply default (2026-08-21 policy): writes do NOT require approval
    # unless the user explicitly switched this conversation to Manual
    # (autoApply=False then arrives via the request / conv settings). A caller
    # that omits autoApply therefore defaults to AUTO — attended or not. This
    # also closes the autonomous-dispatch deadlock: brain/queued turns ride the
    # interactive chat lane (so _attended=True) with a config that omits
    # autoApply; the old attended→Manual default made them block 120s on an
    # approval nobody was watching and then record a silent "rejected".
    # ``_attended`` stays in the gate predicate below as the headless safety:
    # an explicit Manual on an unattended task must never block either.
    _attended = bool(task.get('_attended'))
    auto_apply = cfg.get('autoApply')
    if auto_apply is None:
        auto_apply = True
    tool_results = {}  # tc_id → (tool_content, is_search)

    # Carry the read-start fence in existing future metadata, never a task
    # registry or a reference to the parent's cache for detached writers.
    from lib.write_freshness import project_read_cache_generation
    # Failure VERDICTS (2026-08-06 'silent timeout' incident, conv
    #   msh9fzvo6gmuvh). A lane that failed the tool records ONLY an error
    #   string in tool_results — the tuple's second slot is is_search, NOT a
    #   success flag — so the post-phase settle shipped tool_complete with NO
    #   status, and the client promoted the round to 'done': a get_conversation
    #   that TIMED OUT rendered as a perfectly successful card, the failure
    #   visible only in the raw debug panel. Every failure lane MUST record a
    #   terminal verdict here ('error' / 'aborted'); the post-phase settle
    #   passes it as terminal_status so it is stamped on the round AND shipped
    #   on the wire, exactly like the rejected/aborted lanes ().
    tool_verdicts: dict[str, str] = {}  # tc_id → 'error' | 'aborted'
    _pipeline_timed_out = False
    # Per-task write/idempotent partitions (base UNION custom env flags).
    _write_tools, _idempotent_tools = _task_partitions(task)
    _confirmation_tools = _task_confirmation_tools(task)
    unchanged_receipt_tools = compact_unchanged_tool_names()
    # ── Plan Mode read-only authority ──
    # The ban set rides the SAME per-task write partition (incl. the
    # conservative MCP classification + custom-env flags) so Plan Mode can
    # never drift from the concurrency/approval authority.
    from lib.tasks_pkg.plan_mode import (
        plan_mode_call_allowed, plan_mode_enabled, plan_mode_rejection)
    _plan_mode = plan_mode_enabled(cfg)
    # Native subagents use the same strict leaf-worker partition as local
    # Swarm even outside Plan Mode. ToolSpec.write_tools alone intentionally
    # excludes advisory project-brain mutations and non-mutating interaction /
    # nested-control tools, so it is not a sufficient authority.
    from lib.swarm.routing import read_only_agent_banned_names
    _multi_agent_banned = read_only_agent_banned_names(_write_tools)

    # ══════════════════════════════════════════
    #  Pre-phase: Serial write-approval tools
    # ══════════════════════════════════════════
    # ── Per-task dedup cache for idempotent tools ──
    # Stored on the task dict so it's scoped to one task execution.
    _cache = _ensure_tool_result_cache(task)
    _call_id_receipts = _ensure_tool_call_id_receipts(task)
    _current_round_entries = tuple(
        item[5] for item in parsed_tcs if isinstance(item[5], dict))
    for round_entry in _current_round_entries:
        # Direct executor callers audit on return. Here the pipeline must
        # first resolve its deadline/cancellation verdict and canonical result.
        round_entry['_outcomeAuditOwner'] = 'pipeline'
    _historical_used_call_ids = _collect_used_tool_call_ids(
        task, messages, current_round_entries=_current_round_entries,
        exclude_trailing_assistant=True)
    # Minting mutates this working set, so every conflict in this invocation is
    # O(1) after one O(history) index build instead of re-scanning the complete
    # long-task transcript for each recycled positional id.
    _used_call_ids = set(_historical_used_call_ids)

    from lib.project_dispatch.admission import is_dispatch_task
    if is_dispatch_task(task) and parsed_tcs:
        from lib.project_dispatch.scope import admit_tool
        for item in parsed_tcs:
            admit_tool(task, item[1], item[3], project_path=project_path)

    parallel_items = []
    claimed_model_call_ids: set[str] = set()
    # Each response position retains its own execution and paired result.
    # Provider flood admission occurs before parsing, never by replacing a
    # legitimate repeated action with another occurrence's result.
    for _item_idx, item in enumerate(parsed_tcs):
        item = _repair_current_tool_call_identity(
            task, messages, parsed_tcs, _item_idx, item,
            round_num=round_num, tid=tid, receipts=_call_id_receipts,
            historical_used_ids=_historical_used_call_ids,
            used_ids=_used_call_ids, claimed_ids=claimed_model_call_ids,
            settled_results=_settled_results)
        tc, fn_name, tc_id, fn_args, rn, round_entry, _parse_err = item

        # JSON parse failure / hallucinated-tool rejection: never execute.
        if _parse_err:
            if round_entry:
                _rejected = tool_rejection_descriptor(round_entry)
                _contract_error = round_entry.get('_contractError')
                _err_meta = {'type': 'error', 'content': _parse_err,
                             'toolName': fn_name}
                if _rejected or _contract_error:
                    # Keep the distinct 'rejected' status (don't let
                    # _finalize_tool_round flip it to 'done') and carry the
                    # typed descriptor onto result meta + event so the
                    # frontend cannot project a refused call as completed.
                    if _rejected:
                        stamp_tool_rejection(
                            _err_meta, _rejected,
                            legacy_result_alias=True,
                        )
                    if _contract_error:
                        _err_meta['contractError'] = _contract_error
                    round_entry['results'] = [_err_meta]
                    round_entry['status'] = 'rejected'
                    _event = build_event(
                        EventType.TOOL_RESULT,
                        roundNum=rn,
                        toolCallId=round_entry.get('toolCallId', ''),
                        toolName=fn_name,
                        query=round_entry.get('query', fn_name),
                        results=[_err_meta],
                        status='rejected',
                    )
                    for key in ('taskId', 'attemptId', 'llmRound'):
                        if round_entry.get(key) is not None:
                            _event[key] = round_entry[key]
                    if _rejected:
                        stamp_tool_rejection(_event, _rejected)
                    if _contract_error:
                        _event['_contractError'] = _contract_error
                    append_event(task, _event)
                    from lib.tasks_pkg.tool_call_outcome import (
                        audit_round_outcome)
                    audit_round_outcome(task, round_entry, status='rejected')
                else:
                    _finalize_tool_round(
                        task, rn, round_entry,
                        [_err_meta],
                        query_override=round_entry.get('query', fn_name),
                    )
            tool_results[tc_id] = (_parse_err, False)
            # Settle NOW (). This tool never ran, so the round is
            #   knowably finished the instant it is inspected — deferring to the
            #   post-phase made a zero-cost refusal wait for the round's slowest
            #   REAL tool. The verdict rides along so the client cannot promote
            #   a rejected hallucination to 'done'.
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry, _parse_err,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num,
                terminal_status=(round_entry or {}).get('status') or 'rejected')
            continue

        # Native Multi-agent subagents are analysis-only in Tofu. OpenAI
        # attributes every function call to an agent; the Responses translator
        # preserves that attribution in ``caller``. Reject state-changing calls
        # from any non-root agent at the execution boundary, so the read-only
        # policy remains an authority rule even if prompt guidance is ignored.
        _agent_name = _blocked_multi_agent_write(
            tc, fn_name, _multi_agent_banned)
        if _agent_name:
            _blocked = (
                'Tool call rejected: native Multi-agent subagents are '
                'read-only. Return the finding to /root; only the root agent '
                'may request a state-changing action.')
            stamp_tool_rejection(
                round_entry,
                {'kind': 'multi_agent_read_only', 'agent': _agent_name,
                 'tool': fn_name},
                reason=_blocked, retryable=False,
            )
            from lib.tasks_pkg.tool_call_outcome import audit_round_outcome
            audit_round_outcome(task, round_entry, status='rejected')
            tool_results[tc_id] = (_blocked, False)
            tool_verdicts[tc_id] = 'rejected'
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry, _blocked,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num, terminal_status='rejected')
            logger.warning('[Task %s] Rejected Multi-agent subagent write: '
                           'agent=%s tool=%s', tid, _agent_name, fn_name)
            continue

        from lib.tasks_pkg.coordinator_policy import coordinator_call_rejection
        coordinator_denial = coordinator_call_rejection(task, fn_name, fn_args, project_path)
        if coordinator_denial:
            stamp_tool_rejection(round_entry,
                {'kind': 'coordinator_dispatch_only', 'tool': fn_name},
                reason=coordinator_denial, retryable=False)
            tool_results[tc_id] = (coordinator_denial, False)
            tool_verdicts[tc_id] = 'rejected'
            _settle_current(task, fn_name, tc_id, fn_args, rn, round_entry, coordinator_denial,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num, terminal_status='rejected')
            continue

        # ── Plan Mode read-only guard ──
        # The assembly-time wire filter keeps mutating schemas away from the
        # model, but a call can still reach dispatch (Tool Search resurfacing,
        # late-discovered MCP tools, caller-supplied tools=[...], a stale
        # replayed round). Reject it with a model-visible error — Plan Mode's
        # read-only contract is an authority rule, not a prompt suggestion.
        from lib.tasks_pkg.manager import task_user_id
        if (_plan_mode
                and not plan_mode_call_allowed(fn_name, fn_args, _write_tools,
                                               owner_user_id=task_user_id(task))):
            _blocked = plan_mode_rejection(fn_name)
            stamp_tool_rejection(
                round_entry,
                {'kind': 'plan_mode_read_only', 'tool': fn_name},
                reason=_blocked, retryable=False,
            )
            tool_results[tc_id] = (_blocked, False)
            tool_verdicts[tc_id] = 'rejected'
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry, _blocked,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num, terminal_status='rejected')
            logger.info('[Task %s] Plan Mode rejected mutating tool call: %s',
                        tid, fn_name)
            continue

        # ── Dedup check for idempotent tools ──
        if fn_name in _idempotent_tools:
            cache_key = _make_cache_key(fn_name, fn_args)
            cached = _cache.get(cache_key)

            if (cached is not None
                    and not _project_cache_entry_is_current(fn_name, cached)):
                _cache.pop(cache_key, None)
                cached = None
            # ── FreshGate: never serve a STALE cached read ──
            # The streaming pre-exec/dedup cache bypasses the project-tool
            # handler, so a cached read_files/inspect_image result can be
            # arbitrarily older than the disk (sibling edit, git checkout).
            # Serving it hands the model stale bytes AND never re-stamps
            # the write-freshness token — the 're-reads never clear the
            # refusal' loop (). When a covered file moved since
            # this conversation's token, drop the entry and fall through to
            # a REAL read (which re-stamps via the handler seam).
            if cached is not None:
                try:
                    from lib.tasks_pkg.handlers._write_freshness_gate import (
                        FILE_READ_TOOLS, cached_read_is_stale,
                    )
                    if (fn_name in FILE_READ_TOOLS
                            and cached_read_is_stale(task, fn_args,
                                                     project_path)):
                        _cache.pop(cache_key, None)
                        cached = None
                        logger.info(
                            '[Task %s] conv=%s FreshGate: %s cache hit '
                            'BYPASSED — covered file changed since cached '
                            'read; re-executing',
                            tid, task.get('convId', ''), fn_name)
                except Exception as _fe:
                    logger.debug('[FreshGate] cached-read staleness check '
                                 'failed (non-fatal): %s', _fe)
            search_projection = None
            if cached is not None and fn_name == SEARCH_TOOLS_NAME:
                search_projection = cached_tool_search_projection(
                    _unpack_cache_entry(cached)[0])
                if search_projection is None:
                    # Invalid cache data is not evidence of zero matches.
                    # Let the normal handler produce a fresh typed result.
                    _cache.pop(cache_key, None)
                    cached = None
            if cached is not None:
                cached_content, cached_is_search, cached_source, cached_display, cached_engine_bkdn, cached_vertical, cached_search_diag = \
                    _unpack_cache_entry(cached)
                cached_projection_items = _cache_entry_projection_items(cached)
                cached_producer_metadata = (
                    _cache_entry_producer_metadata(cached))
                is_prefetch = cached_source == 'prefetch'
                # Compute content length for logging without materializing
                # a massive str() for screenshot dicts (which contain base64)
                if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                    _log_len = cached_content.get('compressedSize', 0)
                    _log_suffix = ' (image)'
                else:
                    _log_len = len(str(cached_content))
                    _log_suffix = ''
                logger.info(
                    '[Task %s] conv=%s %s HIT: %s with same args at round %d — '
                    'returning %s result (%d chars%s) instead of re-executing',
                    tid, task.get('convId', ''),
                    'PREFETCH' if is_prefetch else 'DEDUP',
                    fn_name, round_num,
                    'prefetched' if is_prefetch else 'cached',
                    _log_len, _log_suffix,
                )
                # Preserve __screenshot__ dicts as-is so the post-phase
                # can detect them and convert to image_url blocks.
                # Converting to str() would dump 800K+ of base64 text
                # directly into the context, blowing up the token count.
                if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                    dedup_content = cached_content  # keep as dict
                else:
                    dedup_content = cached_content if isinstance(cached_content, str) else str(cached_content)
                # Update round_entry to show cached/prefetched status
                if round_entry:
                    # Use stored display_results for web_search / fetch_url if available
                    # — this preserves per-result rows (titles, URLs, snippets) in the UI
                    # instead of collapsing to a single generic meta row.
                    if search_projection is not None:
                        search_results, search_meta = search_projection
                        extra = {
                            **search_meta,
                            'cacheSource': 'prefetch' if is_prefetch else 'cache',
                        }
                        round_entry.update(extra)
                        _finalize_tool_round(
                            task, rn, round_entry, search_results,
                            query_override=str(fn_args.get('query') or fn_name),
                            extra_event_fields=extra,
                        )
                    elif fn_name == 'web_search' or (cached_display and fn_name == 'fetch_url'):
                        extra = {}
                        extra['cacheSource'] = 'prefetch' if is_prefetch else 'cache'
                        round_entry['cacheSource'] = extra['cacheSource']
                        if cached_engine_bkdn:
                            round_entry['engineBreakdown'] = cached_engine_bkdn
                            extra['engineBreakdown'] = cached_engine_bkdn
                        if cached_vertical:
                            # Batch web_search carries multiple verticals. The
                            # streaming prefetch path wraps them as
                            # {'batch': [...]}, and the dedup path may hand us a
                            # bare list. Both must land in the plural `verticals`
                            # field — the frontend renders that as an array;
                            # `vertical` (singular) expects one {domain, items}
                            # dict and would silently drop a list/wrapper
                            # (showing the bare "vertical: auto" badge with no card).
                            if isinstance(cached_vertical, dict) and 'batch' in cached_vertical:
                                _verts = cached_vertical.get('batch') or []
                            elif isinstance(cached_vertical, list):
                                _verts = cached_vertical
                            else:
                                _verts = None
                            if _verts is not None:
                                round_entry['verticals'] = _verts
                                extra['verticals'] = _verts
                            else:
                                round_entry['vertical'] = cached_vertical
                                extra['vertical'] = cached_vertical
                        # Zero-result web_search: forward the diagnostic the
                        # orchestrator attached at store time so the frontend
                        # renders the honest network-error / no-matches row
                        # (parity with the serial handler's searchDiag path)
                        # instead of a fabricated single "result".
                        if fn_name == 'web_search' and not cached_display and cached_search_diag:
                            round_entry['searchDiag'] = cached_search_diag
                            extra['searchDiag'] = cached_search_diag
                        _finalize_tool_round(
                            task, rn, round_entry, cached_display or [],
                            query_override=round_entry.get('query', fn_name),
                            extra_event_fields=extra or None,
                        )
                    else:
                        _meta = _build_cache_hit_meta(
                            fn_name, fn_args, cached_content, is_prefetch,
                            cached_display=cached_display,
                        )
                        _finalize_tool_round(
                            task, rn, round_entry, [_meta],
                            query_override=round_entry.get('query', fn_name),
                        )
                    if cached_projection_items:
                        round_entry[TOOL_RESULT_PROJECTION_ITEMS_KEY] = (
                            cached_projection_items)
                    if cached_producer_metadata:
                        round_entry[TOOL_RESULT_PRODUCER_METADATA_KEY] = (
                            cached_producer_metadata)
                tool_results[tc_id] = (dedup_content, cached_is_search)
                # Settle NOW (). A cache/prefetch hit costs ZERO
                #   time: for a streaming-prefetch hit the tool already ran while
                #   the model was still emitting tokens (StreamingToolExecutor
                #   → inject_into_cache). Leaving it to the post-phase made the
                #   FASTEST class of tool in the product the one that waited
                #   longest — measured: tool_complete(cached) landed after
                #   tool_complete(slow-sibling).
                _settle_current(
                    task, fn_name, tc_id, fn_args, rn, round_entry,
                    dedup_content, idempotent_tools=_idempotent_tools,
                    cache=_cache, tid=tid, round_num=round_num)
                continue

        if _approval_stops_tool_call(
            task=task, fn_name=fn_name, fn_args=fn_args, tc_id=tc_id,
            rn=rn, round_entry=round_entry,
            confirmation_tools=_confirmation_tools,
            write_tools=_write_tools, attended=_attended,
            auto_apply=auto_apply, cfg=cfg, project_path=project_path,
            round_num=round_num, model=model, tool_results=tool_results,
            idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
            internal_result_sink=internal_result_sink,
            settled_results=_settled_results,
        ):
            continue

        # ── Abort check: skip remaining tools if user clicked Stop ──
        # Re-checked AFTER the approval gate, not only before it: the gate can
        # block up to the approval timeout in ``request_write_approval``, so a
        # Stop pressed while the dialog is open must still win over the
        # approved-but-not-yet-dispatched execution below.
        if task.get('aborted'):
            logger.info('[Task %s] Skipping tool %s (tc_id=%s) — task aborted', tid, fn_name, tc_id[:8])
            tool_results[tc_id] = ('Task aborted by user.', False)
            # Settle NOW with an ABORTED verdict (). Previously the
            #   round kept status='searching' until
            #   orchestrator._finalize_dangling_tool_rounds swept it at task end
            #   — so a Stop left a spinner turning on every not-yet-run tool for
            #   the remainder of the turn. 'aborted' (never 'done') is what makes
            #   the row render the static "interrupted" affordance instead.
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                'Task aborted by user.', idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num,
                terminal_status='aborted')
            continue

        # ── Serial-dispatch for long-blocking tools ──
        _serial_cfg = _SERIAL_BLOCKING_TOOLS.get(fn_name)
        if _serial_cfg and _serial_cfg['match'](fn_args) and not task['aborted']:
            _reason = _serial_cfg['reason']
            logger.info('[Task %s] %s dispatched serially (%s) at round %d',
                        tid, fn_name, _reason, round_num)
            # Inject extra args (e.g. _parent_task) if configured
            _inject_fn = _serial_cfg.get('inject')
            if _inject_fn:
                fn_args.update(_inject_fn(task, rn))
            # Heartbeat this lane too (). These tools block for
            #   MINUTES by design and emit no delta, so without a ticker both
            #   reaper liveness clocks go stale and the task is force-failed
            #   at TOFU_STUCK_TASK_MAX_SILENT_SECS. ``ask_human`` self-bumps,
            #   while ``await_task`` does not and its own wait caps at 3600s —
            #   double the reap threshold. The module comment in _heartbeat.py
            #   claimed this lane was already covered; it was not.
            _hb_stop, _hb_thread = _start_tool_heartbeat(task, [item], tid)
            try:
                tc_id_ret, tool_content, is_search = _execute_tool_one(
                    task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                    cfg, project_path, project_enabled,
                    all_tools=tool_list,
                )
            finally:
                _hb_stop.set()
            tool_results[tc_id_ret] = (tool_content, is_search)
            logger.info('[Task %s] %s serial dispatch completed at round %d '
                        '(result_len=%d)', tid, fn_name, round_num, len(str(tool_content)))
            # Settle immediately (). These tools block for MINUTES.
            #   Holding their
            #   settle until the post-phase meant the round they belong to could
            #   not report ANY tool as finished until the human answered.
            if not (isinstance(tool_content, dict)
                    and tool_content.get('__screenshot__')):
                _settle_current(
                    task, fn_name, tc_id_ret, fn_args, rn, round_entry,
                    tool_content, idempotent_tools=_idempotent_tools,
                    cache=_cache, tid=tid, round_num=round_num)
            continue

        # ── Pre-tool hooks: validate/block/modify before execution ──
        _hook_result = None if is_dispatch_task(task) else run_pre_hooks(fn_name, fn_args, task)
        if _hook_result and _hook_result.action == 'block':
            logger.info('[Task %s] Pre-hook BLOCKED tool %s: %s',
                        tid, fn_name, _hook_result.message)
            _blocked_content = f'Tool blocked by pre-execution hook: {_hook_result.message}'
            # Surface the hook's recovery guidance to the model so a block is
            # an ACTIONABLE redirect (what was refused + how to proceed safely)
            # rather than a dead-end the loop can't recover from.
            _recovery = getattr(_hook_result, 'additional_context', '') or ''
            if _recovery:
                _blocked_content = f'{_blocked_content}\n\n{_recovery}'
            _hook_rejection = stamp_tool_rejection(
                round_entry,
                {'kind': 'pre_execution_hook', 'tool': fn_name},
                reason=_blocked_content, retryable=bool(_recovery),
            )
            tool_results[tc_id] = (_blocked_content, False)
            # Settle the round NOW. Without this the round stays in its
            #   'searching' start-state forever (no result, no terminal
            #   status): the live UI shows a permanent "Running…" spinner and
            #   the persisted round only gets swept to 'aborted' by the
            #   task-end dangling sweep — so an EARLY blocked tool renders as
            #   still-running even after the loop has advanced dozens of rounds
            #   past it. Emit a terminal 'rejected' result exactly like the
            #   parse-error / hallucinated-tool branch above.
            if round_entry is not None:
                _block_meta = {
                    'type': 'error',
                    'content': _blocked_content,
                    'toolName': fn_name,
                    'source': 'Blocked',
                    'snippet': _hook_result.message,
                    'badge': 'blocked',
                }
                stamp_tool_rejection(
                    _block_meta, _hook_rejection,
                    legacy_result_alias=True,
                )
                # For run_command / code_exec, shape the meta so the frontend's
                # purpose-built "not run" terminal card renders it (⊘ blocked +
                # inline reason) — that renderer keys on meta.command / notRun.
                # A generic error meta would fall through to a plain error line.
                if fn_name in ('run_command', 'code_exec'):
                    _block_meta['command'] = fn_args.get('command') or round_entry.get('query') or ''
                    _block_meta['notRun'] = True
                    _block_meta['exitCode'] = 'not-run'
                    _block_meta['reason'] = _blocked_content
                round_entry['results'] = [_block_meta]
                round_entry['status'] = 'rejected'
                round_entry['toolContent'] = _blocked_content
                try:
                    _block_event = build_event(
                        EventType.TOOL_RESULT,
                        roundNum=rn,
                        toolCallId=round_entry.get('toolCallId', ''),
                        toolName=fn_name,
                        query=round_entry.get('query', fn_name),
                        results=[_block_meta],
                        status='rejected',
                    )
                    for key in ('taskId', 'attemptId', 'llmRound'):
                        if round_entry.get(key) is not None:
                            _block_event[key] = round_entry[key]
                    stamp_tool_rejection(_block_event, _hook_rejection)
                    append_event(task, _block_event)
                except Exception as _blk_ev:
                    logger.warning(
                        '[Task %s] tool_result (pre-hook block) emit failed for '
                        'tool=%s round=%s (non-fatal): %s',
                        tid, fn_name, rn, _blk_ev, exc_info=True)
            # Settle NOW (). The hook refused the tool before it
            #   ran, so this round costs zero time. It already carries
            #   status='rejected' above; passing it through keeps the verdict on
            #   the wire so the client cannot promote a BLOCKED tool to 'done'.
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                _blocked_content, idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num,
                terminal_status='rejected')
            continue

        parallel_items.append(item)

    # ══════════════════════════════════════════
    #  Ordered-state / write-tool serial phase (concurrency safety)
    #  Write tools run serially to prevent filesystem race conditions.
    #  Order-sensitive task-local protocols (currently todo_write) share the
    #  ordered lane without inheriting write approval or cache invalidation.
    # ══════════════════════════════════════════
    _serial_items = [
        item for item in parallel_items
        if item[1] in _write_tools or item[1] in _ORDERED_STATE_TOOLS
    ]
    parallel_items = [
        item for item in parallel_items
        if item[1] not in _write_tools and item[1] not in _ORDERED_STATE_TOOLS
    ]
    # A read that runs CONCURRENTLY with a sibling write may capture pre-write
    # bytes. The dedup-cache population in the drain loop below therefore
    # refuses to cache write-sensitive reads in a round that also wrote.
    _has_serial_write = any(it[1] in _write_tools for it in _serial_items)

    # A provider can emit BOTH ``execute_tools`` and one of its calls[] children
    # as direct siblings. That is one routing error, not two user actions. Build
    # a response-scoped FIFO multiset after authority/approval/hook filtering,
    # so a refused direct occurrence cannot absorb a runnable child. Nested
    # child pipelines have no ``execute_tools`` carrier and cannot overwrite it.
    if (publish_direct_gateway_siblings
            and any(item[1] == EXECUTE_TOOLS_NAME for item in parsed_tcs)):
        _publish_execute_gateway_direct_siblings(
            task,
            round_num,
            [
                (direct_name, direct_id, direct_args)
                for (_direct_tc, direct_name, direct_id, direct_args,
                     _direct_rn, _direct_entry, _direct_parse_error)
                in (_serial_items + parallel_items)
                if direct_name not in GATEWAY_TOOL_NAMES
            ],
        )
    # ══════════════════════════════════════════
    #  Read pool starts IMMEDIATELY (fix 1)
    # ══════════════════════════════════════════
    # Reads used to be submitted only AFTER every serial (write) tool had
    # finished, so a slow run_command delayed the whole round's reads (the
    # spawn_wait measured on the flame graph). Submit them FIRST; the serial
    # lane below runs on this thread while the pool executes.
    _pool = None
    _futures: dict = {}
    _timed_out = False
    _pool_hb_stop = None
    _parallel_by_id: dict[str, tuple] = {}
    _tool_deadline = None
    if parallel_items:
        # ── Abort check before spawning parallel pool ──
        if task.get('aborted'):
            logger.info('[Task %s] Skipping %d parallel tools — task aborted', tid, len(parallel_items))
            for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items:
                tool_results[tc_id] = ('Task aborted by user.', False)
                tool_verdicts[tc_id] = 'aborted'
            parallel_items = []  # skip the pool entirely

    if parallel_items:
        _has_bounded_gateway_children = any(
            (
                isinstance(tc.get('caller'), dict)
                and tc['caller'].get('type') == 'program'
            )
            or str(tc.get('source') or '') in {
                'execute_program', 'execute_calls'
            }
            for tc, _fn, _id, _args, _rn, _entry, _pe in parallel_items
        )
        max_workers = _parallel_idle_reuse_hint(
            len(parallel_items), programmatic=_has_bounded_gateway_children)
        _pool = InstrumentedThreadPoolExecutor(max_workers=max_workers,
                                               metric_pool='tool-parallel',
                                               thread_name_prefix='tool-parallel')
        # ── Item 3: long-tool heartbeat ──────────────────────────────────
        # A single blocking tool (a slow web_search on dead hosts, a hung MCP
        # call, a stalled browser action) emits NO delta while it runs, so the
        # SSE stream goes silent — a buffering proxy idle-times-out. This
        # daemon ticker fires every TOOL_HEARTBEAT_INTERVAL seconds while the
        # pool wait blocks, emitting a ``tool_progress`` per still-active
        # round so the UI shows "Searching… (Ns)". Fast tools finish before
        # the first tick, so they never emit a heartbeat.
        # EVIDENCE GRADING (): for ordinary tools these ticks are
        #   marked ``_selfTick`` and do NOT feed the reaper liveness clocks —
        #   liveness must come from REAL output (stdout chunks / results).
        #   Only ratified human-wait tools (ask_human / await_task(wait)) keep
        #   the reaper exemption. See _heartbeat.py.
        _pool_hb_stop, _pool_hb_thread = _start_tool_heartbeat(task, parallel_items, tid)
        # O(1) tc_id → item lookup for the drain loop (fix 2).
        _parallel_by_id = {_it[2]: _it for _it in parallel_items}
        _futures = {}
        for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items:
            try:
                _start_generation = project_read_cache_generation()
                future = _pool.submit(_execute_tool_one_in_pool, task,
                    tc, fn_name, tc_id, fn_args, rn, round_entry,
                    cfg, project_path, project_enabled, all_tools=tool_list)
            except Exception as error:
                # Failed submit has never dispatched the callable. Keep its
                # original error in the ordinary exactly-once settlement lane.
                future = Future()
                future.set_exception(error)
            _futures[future] = (tc_id, fn_name, _start_generation)
        # Measure the parallel timeout from SUBMISSION so the serial lane's
        # wall time cannot extend a hung read past TOOL_PARALLEL_TIMEOUT.
        _tool_deadline = time.monotonic() + _bounded_tool_env_int(
            'TOOL_PARALLEL_TIMEOUT', 300, 1, 3600)

    for item in _serial_items:
        tc, fn_name, tc_id, fn_args, rn, round_entry, _pe = item
        if task.get('aborted'):
            logger.info('[Task %s] Skipping serial write tool %s — task aborted', tid, fn_name)
            tool_results[tc_id] = ('Task aborted by user.', False)
            # Same abort contract as the pre-phase lane ().
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                'Task aborted by user.', idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num,
                terminal_status='aborted')
            continue
        logger.debug('[Task %s] Ordered serial dispatch: %s at round %d',
                     tid, fn_name, round_num)
        # Heartbeat this lane (). ``run_command`` resolves
        #   timeout=None BY DESIGN (no ceiling, pinned by
        #   tests/project_mod/test_no_backend_timeouts.py) and every non-readOnly MCP tool
        #   lands in this same write partition. Running them bare left BOTH
        #   reaper clocks silent, so the reaper's 1800s became an invisible,
        #   unconfigurable ceiling that killed the WHOLE task — measured
        #   2026-07-31: tasks 38562f78 and 31d08c82, each 1846s in
        #   run_command, process group killed, ¥22.95 + ¥12.01 of completed
        #   rounds discarded. Aliveness is proven by BEATING, never by
        #   not-timing-out.
        # EVIDENCE GRADING (, same day, same incident family):
        #   the beat must be EVIDENCE, not self-rescue. For ordinary tools
        #   this tick is marked ``_selfTick`` and keeps ONLY the transport
        #   alive; the reaper clocks are fed by real stdout chunks instead
        #   (a producing command never goes stale). A command silent >30min
        #   IS reaped now — wedged by definition; the ratified human-wait
        #   exemption covers only ask_human / await_task(wait).
        # External writes may block and therefore need a transport heartbeat.
        # Task-local todo transitions are bounded in-memory work; avoiding a
        # heartbeat thread on every revision keeps the progress protocol cheap.
        _hb_stop = None
        if fn_name in _write_tools:
            _hb_stop, _hb_thread = _start_tool_heartbeat(task, [item], tid)
        try:
            tc_id_ret, tool_content, is_search = _execute_tool_one(
                task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                cfg, project_path, project_enabled,
                all_tools=tool_list,
            )
        finally:
            if _hb_stop is not None:
                _hb_stop.set()
        tool_results[tc_id_ret] = (tool_content, is_search)
        if fn_name in _write_tools:
            _invalidate_project_cache(_cache, trigger=fn_name)
        # Settle at THIS tool's own completion () — a serial write
        #   used to run before the parallel pool started, so deferring its
        #   settle to the post-phase made it wait for every read tool in the
        #   round. Same barrier, different lane. Screenshots included
        #   () — see the parallel lane for why the vision verdict
        #   needs no barrier.
        _settle_arg = _dispatch_settle_content(
            task, round_entry, tool_content, model, fn_name, tc_id_ret)
        _settle_current(
            task, fn_name, tc_id_ret, fn_args, rn, round_entry,
            _settle_arg, idempotent_tools=_idempotent_tools,
            cache=_cache, tid=tid, round_num=round_num,
            canonical_content=_canonical_settle_source(tool_content))

    # ══════════════════════════════════════════
    #  Main phase: Drain the read pool (reads were submitted BEFORE the writes)
    # ══════════════════════════════════════════
    if _futures:
        try:
            try:
                for fut in _completed_tools_until_cancel(
                        _futures, task, _tool_deadline):
                    # ── Abort check during parallel execution: cancel remaining futures ──
                    if fut is None:
                        logger.info('[Task %s] Abort detected during parallel tool execution — cancelling remaining', tid)
                        for pending_fut, (pending_id, pending_fn, _generation) in _futures.items():
                            pending_fut.cancel()
                            if pending_id not in tool_results:
                                tool_results[pending_id] = ('Task aborted by user.', False)
                                tool_verdicts[pending_id] = 'aborted'
                        break
                    fut_tc_id, fut_fn_name, _start_generation = _futures[fut]
                    try:
                        ret_tc_id, tool_content, is_search = fut.result()
                        tool_results[ret_tc_id] = (tool_content, is_search)
                        # ── Populate dedup cache for idempotent tools ──
                        if fut_fn_name in _idempotent_tools:
                            _pi = _parallel_by_id.get(ret_tc_id)
                            # Concurrency safety (fix 1): a read that ran
                            #   beside a sibling write may hold PRE-write bytes,
                            #   and this population happens after the write's
                            #   _invalidate_project_cache already ran — caching
                            #   now would leave a stale entry a later
                            #   FreshGate-checked hit serves verbatim.
                            if (_pi is not None
                                    and (_pi[5] or {}).get(
                                        '_cacheableResult', True)
                                    and not (
                                    _has_serial_write
                                    and fut_fn_name in _WRITE_SENSITIVE_READ_TOOLS)):
                                _pi_cache_key = _make_cache_key(fut_fn_name, _pi[3])
                                # For web_search / fetch_url, also cache display_results
                                # + engineBreakdown from the round_entry for later cache hits —
                                # this keeps the rich per-result UI even on dedup replay
                                # (e.g. batch "3 URLs" stays as 3 rows, not 1 generic row).
                                _pi_display = None
                                _pi_eng_bkdn = None
                                _pi_vert = None
                                _pi_search_diag = None
                                _pi_re = _pi[5]  # round_entry
                                if fut_fn_name in ('web_search', 'fetch_url'):
                                    if _pi_re and _pi_re.get('results'):
                                        _pi_display = _pi_re['results']
                                    if _pi_re:
                                        _pi_eng_bkdn = _pi_re.get('engineBreakdown')
                                        _pi_vert = _pi_re.get('vertical') or _pi_re.get('verticals')
                                        # Zero-result searches: the handler
                                        # stamps searchDiag on the round —
                                        # cache it so a later hit renders
                                        # the honest diagnostic row.
                                        _pi_search_diag = _pi_re.get('searchDiag')
                                elif fut_fn_name in ('read_files', 'inspect_image'):
                                    # Preserve the FULLY-MERGED inline render
                                    # descriptors (images + SVG source URIs) so a
                                    # dedup replay renders identically to the fresh
                                    # read. SVG content caches as a plain str, so
                                    # its out-of-band imageDataUris would otherwise
                                    # be lost on the second identical read.
                                    _pi_res = (_pi_re or {}).get('results') or []
                                    if (_pi_res and isinstance(_pi_res[0], dict)
                                            and _pi_res[0].get('imageDataUris')):
                                        _pi_display = _pi_res[0]['imageDataUris']
                                _pi_projection = None
                                if _pi_re:
                                    _pi_projection = _pi_re.get(
                                        TOOL_RESULT_PROJECTION_ITEMS_KEY)
                                _pi_cache_entry = (
                                    tool_content, is_search, 'dedup',
                                    _pi_display, _pi_eng_bkdn, _pi_vert,
                                    _pi_search_diag)
                                if _pi_projection is not None:
                                    _pi_cache_entry += (_pi_projection,)
                                _store_tool_result_cache_entry(
                                    task, _pi_cache_key, _pi_cache_entry,
                                    generation=_start_generation)
                        # ── Invalidate project cache after write/exec ops ──
                        elif fut_fn_name in ('write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                                             'insert_content', 'insert_contents',
                                             'code_exec', 'bash_exec', 'run_command'):
                            _invalidate_project_cache(_cache, trigger=fut_fn_name)

                        # SETTLE NOW, not after the barrier ().
                        #   This tool is done; budget its result, stamp the
                        #   round and emit tool_complete at THIS instant. The
                        #   old code deferred all of that past
                        #   pool.shutdown(wait=True), so a fast tool's content
                        #   and token chips waited for the slowest sibling in
                        #   the round — a 2s search kept spinning for as long
                        #   as a 40s one beside it, with no way for the user to
                        #   tell them apart.
                        #
                        #   Screenshots settle here TOO (): the
                        #   display text depends only on
                        #   model_supports_vision(model), a pure function of a
                        #   parameter we already hold, so there is nothing to
                        #   wait for. Only the multimodal MESSAGE append stays
                        #   in the post-phase, where tool-call order lives.
                        _pi = _parallel_by_id.get(ret_tc_id)
                        if _pi is not None:
                            _settle_arg = _dispatch_settle_content(
                                task, _pi[5], tool_content, model, _pi[1], ret_tc_id)
                            _settle_current(
                                task, _pi[1], ret_tc_id, _pi[3],
                                _pi[4], _pi[5], _settle_arg,
                                idempotent_tools=_idempotent_tools,
                                cache=_cache, tid=tid,
                                round_num=round_num,
                                canonical_content=_canonical_settle_source(
                                    tool_content))
                    except Exception as e:
                        # UnknownWorkspaceRootError is the LLM's fault
                        # (bad root prefix); it's already logged at WARNING
                        # at the raise site + INFO by executor.  Do not
                        # re-log as ERROR with traceback here — just record
                        # the error for the LLM and move on.
                        _is_unknown_root = False
                        try:
                            from lib.project_mod.config import UnknownWorkspaceRootError
                            _is_unknown_root = isinstance(e, UnknownWorkspaceRootError)
                        except ImportError as _imp:
                            logger.debug('[Task %s] UnknownWorkspaceRootError '
                                         'import failed: %s', tid, _imp)
                        if _is_unknown_root:
                            logger.info(
                                '[Task %s] conv=%s Tool %s (tc_id=%s) '
                                'recoverable workspace-root error '
                                'returned to LLM at round %d: %s',
                                tid, task.get('convId', ''),
                                fut_fn_name, fut_tc_id, round_num, e)
                        else:
                            logger.error(
                                '[Task %s] conv=%s Tool %s (tc_id=%s) execution failed at round %d model=%s',
                                tid, task.get('convId', ''), fut_fn_name, fut_tc_id, round_num, model, exc_info=True)

                        tool_results[fut_tc_id] = (f'Tool execution error: {e}', False)
                        tool_verdicts[fut_tc_id] = 'error'
            except (TimeoutError, _FuturesTimeoutError):
                _timed_out = True
                _pipeline_timed_out = True
                _n_pending = sum(1 for f in _futures if not f.done())
                logger.error(
                    '[Task %s] conv=%s Tool parallel execution timeout at round %d (%d tools pending) model=%s',
                    tid, task.get('convId', ''), round_num, _n_pending, model,
                    exc_info=True)

                # Harvest results from futures that completed but weren't
                # yielded before the TimeoutError was raised. A published
                # verdict always wins over later worker completion.
                _harvest_completed_tool_results(
                    _futures, task, tool_results, tool_verdicts,
                    round_num=round_num, reason='collector timeout')
                for fut, (fut_tc_id, fut_fn_name, _generation) in _futures.items():
                    if fut_tc_id not in tool_results:
                        fut.cancel()
                        from lib.tasks_pkg.executor._effects import tool_timeout_result
                        pending_row = _parallel_by_id[fut_tc_id][5]
                        tool_results[fut_tc_id] = (
                            tool_timeout_result(task, fut_fn_name, pending_row), False)
                        tool_verdicts[fut_tc_id] = 'error'
                        # 'error', not 'aborted': the user did not stop this
                        # tool — the pool ceiling did. Effect evidence remains
                        # independent: an entered worker may still be running.
        finally:
            # Stop the heartbeat ticker first so it can't emit after the round
            # settles (it checks round status, but stop the loop deterministically).
            _pool_hb_stop.set()
            # Preserve the finite pool-timeout contract. Runtime cancellation
            # callbacks are synchronous and arbitrary handlers may never exit;
            # neither can be drained on this timeout path. Running threads may
            # still mutate shared state: wait=False is NOT a resource fence.
            # Stop retains cooperative cleanup/ownership until workers exit.
            if task.get('aborted') and not _timed_out:
                _drain_cancelled_tool_workers(task, _futures)
            _pool.shutdown(wait=not _timed_out, cancel_futures=True)

    if len(tool_results) < len(parsed_tcs):
        # A collector may omit a terminal Future. Reuse its executor receipt
        # before classifying the gap; do not enter handlers or add a new wait.
        _harvest_completed_tool_results(
            _futures, task, tool_results, tool_verdicts,
            round_num=round_num, reason='collector gap')

    # ══════════════════════════════════════════
    #  Post-phase: Add tool messages in original order
    # ══════════════════════════════════════════
    _round_results_for_budget: list[tuple[str, str, str]] = []  # (tc_id, content, tool_name)
    _program_messages_to_settle: list[tuple[dict, str, dict]] = []
    # Only these messages were appended THIS round; the aggregate-budget
    # apply-back below must edit them and ONLY them. Iterating the whole
    # ``messages`` list and re-binding by ``tool_call_id`` is the cache-killing
    # bug: a duplicate id in history (positional-id recycle) makes the OLD
    # already-cached tool_result get overwritten with the NEW round's content.
    _appended_tool_msgs: list[dict[str, Any]] = []
    _append_ordered_tool_replay(
        _appended_tool_msgs, _cache,
        _idempotent_tools, _program_messages_to_settle, _round_results_for_budget, _settle_current,
        all_search_results_text, messages, model, parsed_tcs,
        round_num, task, tid, tool_results,
        tool_verdicts,
    )

    _apply_round_aggregate_budget(
        task,
        parsed_tcs,
        _round_results_for_budget,
        _appended_tool_msgs,
        settled_results=_settled_results,
    )

    # Measure the exact content that the next Responses request will replay.
    # Aggregate L0 compaction above can replace large results, so measuring in
    # the per-call loop would overstate bytes and report false truncations.
    if _program_messages_to_settle:
        from lib.tasks_pkg.orchestrator._programmatic import (
            settle_programmatic_call,
        )
        for _ptc, _status, _tool_message in _program_messages_to_settle:
            settle_programmatic_call(
                task, _ptc, _status, content=_tool_message.get('content'))

    # Emit snapshot AFTER tool results appended — WIRE-FORM view (same single
    # source of truth as the orchestrator's pre-LLM and final snapshots), so
    # the panel reflects exactly what the model will receive next round. Runs
    # on an independent copy via apply_wire_sanitize (does not mutate messages).
    # Gated (fix 3): the O(history) deep-copy + sanitize is DEBUG-only, so it
    # is skipped for unattended / headless tasks unless TOFU_POST_TOOL_SNAPSHOT
    # opts back in.
    if not _post_tool_snapshot_enabled(task):
        return _pipeline_timed_out
    # Reduced-scope FINDING C: the post-tool snapshot re-copies the WHOLE
    # message history each round (O(history) → quadratic storage). The
    # pre-request snapshot for the same round already ran the same sanitize +
    # strip. When no message was appended or edited since that snapshot (same
    # length and same tail identity) the post-tool frame would be byte-equal —
    # a pure duplicate. Skip the re-snapshot and reuse the round's existing
    # evidence; the Inspector keys by (taskId, roundNum) and already holds the
    # identical request payload for this round.
    try:
        from lib.tasks_pkg.round_evidence import snapshot_unchanged_since_request
        if snapshot_unchanged_since_request(task, messages):
            logger.debug(
                '[Task %s] messages_snapshot post-tool skipped at round %d — '
                'unchanged since the request snapshot', tid, round_num + 1)
            return _pipeline_timed_out
    except Exception:
        logger.debug('[Task %s] post-tool unchanged check failed (non-fatal)',
                     tid, exc_info=True)
    try:
        from lib.tasks_pkg.round_evidence import build_round_evidence
        from lib.tasks_pkg.round_evidence import begin_round_evidence
        ref = task.get('_inspectorRef') or begin_round_evidence(
            task, task.get('_inspectorTaskId') or task['id'], round_num + 1,
            task.get('_flow_phase') or '')
        snap_evt = build_round_evidence(
            task, messages, ref=ref, kind='state', model=model, tools=tool_list)
        append_event(task, snap_evt)
    except Exception:
        logger.warning(
            '[Task %s] messages_snapshot post-tool failed at round %d model=%s',
            tid, round_num + 1, model, exc_info=True)

    return _pipeline_timed_out


def _append_screenshot_message(messages, tc_id, tool_content):
    """Convert a screenshot dict into a multimodal tool message and append it.

    Parameters
    ----------
    messages : list[dict]
        Conversation messages — appended in-place.
    tc_id : str
        The tool_call_id to associate with the result message.
    tool_content : dict
        Screenshot dict with keys ``dataUrl``, ``format``, ``originalSize``,
        ``compressedSize``, ``compressionApplied``.
    """
    # A multi-image batch (read_files of several images) carries every image
    # in ``images``; otherwise treat the dict itself as the single image.
    if isinstance(tool_content.get('_modelReplayBlocks'), list):
        import copy
        messages.append({'role': 'tool', 'tool_call_id': tc_id,
                         'content': copy.deepcopy(tool_content['_modelReplayBlocks']),
                         '_modelImageSources': list(tool_content.get('_modelReplaySources') or [])})
        return
    img_dicts = tool_content.get('images') or [tool_content]

    def _data_url_parts(img):
        du = img.get('dataUrl', '')
        if du.startswith('data:'):
            header, b64 = du.split(',', 1)
            return header.split(':')[1].split(';')[0], b64
        return f'image/{img.get("format", "png")}', du

    content_blocks = []
    for img in img_dicts:
        media_type, b64_data = _data_url_parts(img)
        content_blocks.append({
            'type': 'image_url',
            'image_url': {'url': f'data:{media_type};base64,{b64_data}'},
        })

    # Use custom text description if provided (e.g. image gen results),
    # otherwise fall back to the generic screenshot description.
    text_desc = tool_content.get('_text_fallback')
    if not text_desc:
        fmt = tool_content.get('format', 'png')
        orig_size = tool_content.get('originalSize', 0)
        comp_size = tool_content.get('compressedSize', 0)
        size_info = f'{comp_size:,} bytes'
        if tool_content.get('compressionApplied') and orig_size:
            size_info = f'{orig_size:,} → {comp_size:,} bytes (compressed)'
        text_desc = (
            f'📸 Screenshot captured ({fmt}, {size_info}). '
            f'The image above shows the current visible area of the page. '
            f'Analyze it visually.'
        )
    if len(img_dicts) > 1:
        text_desc = f'{len(img_dicts)} images loaded above.\n{text_desc}'

    content_blocks.append({'type': 'text', 'text': text_desc})

    messages.append({
        'role': 'tool',
        'tool_call_id': tc_id,
        'content': content_blocks,
    })
