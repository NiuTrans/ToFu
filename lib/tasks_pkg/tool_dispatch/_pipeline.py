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
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid
# concurrent.futures.TimeoutError only became an alias of builtin
# TimeoutError in 3.11 — on 3.10 it is a DISTINCT class, so
# ``except TimeoutError`` does NOT catch an as_completed/fut.result timeout
# and the pool-timeout lane never fires (3.10 CI leg, 2026-08-06).
from concurrent.futures import TimeoutError as _FuturesTimeoutError
from typing import Any

from lib.agent_core.events import EventType, build_event, now_ms
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
from lib.tasks_pkg.manager._events import _strip_base64_for_snapshot
from lib.tasks_pkg.tool_hooks import run_post_hooks, run_pre_hooks
from lib.tool_rejection import (
    stamp_tool_rejection,
    tool_rejection_descriptor,
)
from lib.tools.gateway import EXECUTE_TOOLS_NAME, GATEWAY_TOOL_NAMES
from lib.tools.result_projection import (
    TOOL_RESULT_PRODUCER_METADATA_KEY,
    TOOL_RESULT_PROJECTION_ITEMS_KEY,
)

from lib.tasks_pkg.tool_dispatch._approval import _handle_approval
from lib.tasks_pkg.tool_dispatch._flags import (
    _cache_entry_producer_metadata,
    _cache_entry_projection_items,
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
    return bool(task.get('_attended'))


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


def _parallel_worker_limit(item_count: int, *, programmatic: bool = False
                           ) -> int:
    """Personal-server-safe pool size with a hard PTC concurrency ceiling."""
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
    from lib.tool_result_artifacts import register_artifact_provenance
    register_artifact_provenance(
        task, artifact_ref, tool_name=tool_name,
        display=round_entry.get('query', ''),
        llm_round=round_entry.get('llmRound'),
        tool_call_id=round_entry.get('toolCallId', ''))


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

    projection_items = (
        round_entry.pop(TOOL_RESULT_PROJECTION_ITEMS_KEY, None)
        if isinstance(round_entry, dict) else None)
    producer_metadata = (
        round_entry.pop(TOOL_RESULT_PRODUCER_METADATA_KEY, None)
        if isinstance(round_entry, dict) else None)

    # Post-tool hooks: modify/enrich result after execution.
    _uses_v2_result_contract = False
    if isinstance(tool_content, str):
        _projection_source = tool_content
        tool_content = run_post_hooks(fn_name, fn_args, tool_content, task)
        if tool_content != _projection_source:
            # Producer boundaries describe the producer's exact raw result.
            # A hook that rewrites it invalidates those offsets/previews; fall
            # back to argument-derived identities rather than show stale text.
            projection_items = None

    # Empty result marker: prevent models from misinterpreting
    # empty tool results as conversation end.
    if isinstance(tool_content, str):
        tool_content = mark_empty_result(fn_name, tool_content)

    # Local ToolScript gets a separate transient result lane. Capture only
    # after post-tool hooks (the semantic/redaction boundary) and before model
    # compaction. The sink owns its hard byte budget and never enters ``task``
    # or the durable transcript.
    if internal_result_sink is not None:
        try:
            internal_result_sink.capture(tc_id, tool_content)
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
        tool_content = unchanged_projection.model_content
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
                    task, _sync_key, (tool_content, *_cache_tail))

    # The canonical V2 envelope is server-owned evidence. Keep it only through
    # the current round's aggregate-budget pass, and place the sparse projection
    # in messages/events. This prevents byte counters, empty freshness fields,
    # evidence ids, and contract plumbing from consuming model context.
    tool_result_evidence = None
    if _uses_v2_result_contract and isinstance(tool_content, str):
        from lib.tools.result_envelope import split_tool_result_delivery

        internal_envelope = tool_content
        delivery = split_tool_result_delivery(internal_envelope)
        tool_content = delivery.model_text
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

    # Emit tool_complete AFTER budgeting so that toolContent
    #   reflects the ACTUAL content given to the model (budgeted/
    #   persisted form).  Preview must show what the model sees.
    try:
        if isinstance(tool_content, str):
            tc_content_str = tool_content
        else:
            tc_content_str = json.dumps(tool_content, ensure_ascii=False)
        if len(tc_content_str) > 50000:
            tc_content_str = tc_content_str[:50000] + '\n... [truncated for continue context]'

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
        #   ``or now_ms()`` is load-bearing in the other direction: a round that
        #   already went through ``_finalize_tool_round`` carries the REAL
        #   completion instant, and overwriting it with a later "now" would
        #   silently shrink a slow tool's measured duration toward zero.
        _t_start = (round_entry or {}).get('tStart')
        _t_end = (round_entry or {}).get('tEnd') or now_ms()
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
        if tool_result_evidence is not None:
            _evt['toolResultEvidence'] = dict(tool_result_evidence)
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

    _settled[tc_id] = tool_content
    _store_tool_call_id_receipt(task, tc_id, {
        'signature': _call_id_signature(fn_name, fn_args),
        'name': fn_name,
        'status': terminal_status or (round_entry or {}).get('status') or 'done',
    })
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
            new_content = delivery.model_text
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
        if new_content == message.get('content'):
            continue
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
                if len(content_text) > 50000:
                    content_text = (
                        content_text[:50000]
                        + '\n... [truncated for continue context]'
                    )
                round_entry['toolContent'] = content_text
                original_chars = original_chars_by_call_id.get(tool_call_id, 0)
                compacted_chars = len(content_text)
                round_entry['compactionLayer'] = 'L0'
                round_entry['compactedFromChars'] = original_chars
                round_entry['compactedToChars'] = compacted_chars

                # Parse the internal envelope, not content_text: the latter
                # may carry the 50k continue-context truncation suffix.
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
) -> bool:
    """Apply confirmation/manual-write policy; return true when rejected.

    Always-confirm tools fail closed when no human is attending.  Ordinary
    writes only enter this boundary in Manual mode, with the existing
    read-only-command and durable browser-grant exceptions preserved.
    """
    requires_confirmation = (
        fn_name in confirmation_tools and not task['aborted'])
    if requires_confirmation and not attended:
        reject_content = (
            f'{fn_name} requires attended human confirmation and was not '
            'executed in this unattended task.')
        stamp_tool_rejection(
            round_entry,
            {'kind': 'confirmation_required', 'tool': fn_name},
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
        return False
    approved, reject_content = _handle_approval(
        task, fn_name, fn_args, rn, round_entry, project_path,
        round_num, model, cfg=cfg, mint_receipt=requires_confirmation)
    if approved:
        return False

    stamp_tool_rejection(
        round_entry,
        {'kind': 'approval_denied', 'tool': fn_name},
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
        Used only by the local ToolScript backend; never retained on ``task``.

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
    _historical_used_call_ids = _collect_used_tool_call_ids(
        task, messages, current_round_entries=_current_round_entries,
        exclude_trailing_assistant=True)
    # Minting mutates this working set, so every conflict in this invocation is
    # O(1) after one O(history) index build instead of re-scanning the complete
    # long-task transcript for each recycled positional id.
    _used_call_ids = set(_historical_used_call_ids)

    parallel_items = []
    claimed_model_call_ids: set[str] = set()
    # A provider response is an ordered occurrence list. Equal tool names and
    # arguments under different positions are still separate model actions;
    # only a recycled correlation id is repaired. Content equality is not
    # evidence that transport delivered one event twice.
    for _item_idx, item in enumerate(parsed_tcs):
        item = _repair_current_tool_call_identity(
            task, messages, parsed_tcs, _item_idx, item,
            round_num=round_num, tid=tid, receipts=_call_id_receipts,
            historical_used_ids=_historical_used_call_ids,
            used_ids=_used_call_ids, claimed_ids=claimed_model_call_ids,
            settled_results=_settled_results)
        tc, fn_name, tc_id, fn_args, rn, round_entry, _parse_err = item

        # JSON parse failure / hallucinated-tool rejection → return error to
        # LLM, skip execution.
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
                    if _rejected:
                        stamp_tool_rejection(_event, _rejected)
                    if _contract_error:
                        _event['_contractError'] = _contract_error
                    append_event(task, _event)
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
            tool_results[tc_id] = (_blocked, False)
            tool_verdicts[tc_id] = 'rejected'
            _settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry, _blocked,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num, terminal_status='rejected')
            logger.warning('[Task %s] Rejected Multi-agent subagent write: '
                           'agent=%s tool=%s', tid, _agent_name, fn_name)
            continue

        # ── Plan Mode read-only guard ──
        # The assembly-time wire filter keeps mutating schemas away from the
        # model, but a call can still reach dispatch (Tool Search resurfacing,
        # late-discovered MCP tools, caller-supplied tools=[...], a stale
        # replayed round). Reject it with a model-visible error — the same
        # settle shape as the multi-agent lane above — so Plan Mode's
        # read-only contract is an authority rule, not a prompt suggestion.
        if (_plan_mode
                and not plan_mode_call_allowed(fn_name, fn_args, _write_tools)):
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
                    if fn_name == 'web_search' or (cached_display and fn_name == 'fetch_url'):
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
        # Inspired by Claude Code's PreToolUse hooks.
        _hook_result = run_pre_hooks(fn_name, fn_args, task)
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
    #  Inspired by Claude Code's isConcurrencySafe partitioning:
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
        max_workers = _parallel_worker_limit(
            len(parallel_items), programmatic=_has_bounded_gateway_children)
        _pool = ThreadPoolExecutor(max_workers=max_workers)
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
        _futures = {
            _pool.submit(
                _execute_tool_one_in_pool, task,
                tc, fn_name, tc_id, fn_args, rn, round_entry,
                cfg, project_path, project_enabled,
                all_tools=tool_list,
            ): (tc_id, fn_name)
            for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items
        }
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
        #   tests/test_no_backend_timeouts.py) and every non-readOnly MCP tool
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
        if (isinstance(tool_content, dict)
                and tool_content.get('__screenshot__')):
            _shot_txt, _ = _screenshot_display_content(
                model, tool_content)
            _settle_current(
                task, fn_name, tc_id_ret, fn_args, rn, round_entry,
                _shot_txt, idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num)
        else:
            _settle_current(
                task, fn_name, tc_id_ret, fn_args, rn, round_entry,
                tool_content, idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num)

    # ── A serial write may flip task['aborted'] mid-lane (fix 1 ordering).
    #    Reads were submitted BEFORE the write ran, so reproduce the OLD
    #    pre-pool abort verdict here instead of letting the drain loop's
    #    in-pool path drop already-completed reads as "Unknown tool".
    if _futures and task.get('aborted'):
        for _pf, (_pid, _pfn) in _futures.items():
            if not _pf.done():
                _pf.cancel()
            if _pid not in tool_results:
                tool_results[_pid] = ('Task aborted by user.', False)
                tool_verdicts[_pid] = 'aborted'

    # ══════════════════════════════════════════
    #  Main phase: Drain the read pool (reads were submitted BEFORE the writes)
    # ══════════════════════════════════════════
    if _futures:
        try:
            try:
                _remaining = (_tool_deadline - time.monotonic()
                              if _tool_deadline is not None else 0.0)
                for fut in as_completed(_futures, timeout=max(0.0, _remaining)):
                    # ── Abort check during parallel execution: cancel remaining futures ──
                    if task.get('aborted'):
                        logger.info('[Task %s] Abort detected during parallel tool execution — cancelling remaining', tid)
                        for pending_fut, (pending_id, pending_fn) in _futures.items():
                            if not pending_fut.done():
                                pending_fut.cancel()
                                if pending_id not in tool_results:
                                    tool_results[pending_id] = ('Task aborted by user.', False)
                                    tool_verdicts[pending_id] = 'aborted'
                        break
                    fut_tc_id, fut_fn_name = _futures[fut]
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
                                    task, _pi_cache_key, _pi_cache_entry)
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
                        _is_shot = (isinstance(tool_content, dict)
                                    and tool_content.get('__screenshot__'))
                        _pi = _parallel_by_id.get(ret_tc_id)
                        if _pi is not None:
                            _settle_arg = tool_content
                            if _is_shot:
                                _settle_arg, _ = _screenshot_display_content(
                                    model, tool_content)
                            _settle_current(
                                task, _pi[1], ret_tc_id, _pi[3],
                                _pi[4], _pi[5], _settle_arg,
                                idempotent_tools=_idempotent_tools,
                                cache=_cache, tid=tid,
                                round_num=round_num)
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
                # yielded by as_completed before the TimeoutError was raised.
                # Without this, completed-but-unyielded results are silently
                # lost and fall through to 'Unknown tool' in the post-phase.
                for fut, (fut_tc_id, fut_fn_name) in _futures.items():
                    if fut.done():
                        if fut_tc_id not in tool_results:
                            try:
                                ret_tc_id, tool_content, is_search = fut.result()
                                tool_results[ret_tc_id] = (tool_content, is_search)
                                logger.info(
                                    '[Task %s] conv=%s Recovered completed tool %s (tc_id=%s) after timeout',
                                    tid, task.get('convId', ''), fut_fn_name, fut_tc_id)
                            except Exception as e:
                                logger.warning(
                                    '[Task %s] conv=%s Tool %s (tc_id=%s) completed with error after timeout: %s',
                                    tid, task.get('convId', ''), fut_fn_name, fut_tc_id, e)
                                tool_results[fut_tc_id] = (f'Tool execution error: {e}', False)
                                tool_verdicts[fut_tc_id] = 'error'
                    else:
                        fut.cancel()
                        tool_results[fut_tc_id] = (f'Tool execution timed out: {fut_fn_name}', False)
                        tool_verdicts[fut_tc_id] = 'error'
                        # 'error', not 'aborted': the user did not stop this
                        # tool — the pool ceiling did. The content sentinel
                        # carries the 'timed out' wording onto the rendered
                        # row's reason span.
        finally:
            # Stop the heartbeat ticker first so it can't emit after the round
            # settles (it checks round status, but stop the loop deterministically).
            _pool_hb_stop.set()
            # On timeout use wait=False + cancel_futures=True to avoid
            # blocking indefinitely on still-running tool threads.
            # On normal completion wait=True is fine (all futures done).
            _pool.shutdown(wait=not _timed_out, cancel_futures=_timed_out)

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
    for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parsed_tcs:
        if tc_id in tool_results:
            tool_content, is_search = tool_results[tc_id]
        else:
            # SHOULD-NOT-HAPPEN: every dispatch branch above is expected to
            # populate tool_results[tc_id]. If a tc_id reaches here unfilled,
            # a dispatch branch silently skipped writing its result — the model
            # would get a misleading "Unknown tool" with no trace of the real
            # cause. Log it so the silent-skip is diagnosable (§2 zero-silent-failure).
            logger.warning(
                '[Task %s] conv=%s tool result missing for tool=%s tc_id=%s '
                'round=%d — no dispatch branch populated it; returning '
                'Unknown-tool fallback to LLM (should not happen)',
                tid, task.get('convId', '') if task else '',
                fn_name, tc_id, round_num)
            tool_content, is_search = (f'Unknown tool: {fn_name}', False)
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
                round_num=round_num)
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
    try:
        from lib.tasks_pkg.wire_messages import apply_wire_sanitize
        from lib.tasks_pkg.manager import task_user_id
        _wire = apply_wire_sanitize(
            messages, conv_id=task.get('convId', ''),
            provider_id=task.get('provider_id') or '',
            user_id=task_user_id(task))
        snapshot = _strip_base64_for_snapshot(_wire)
        snap_evt = build_event(
            EventType.MESSAGES_SNAPSHOT,
            # Request Inspector contract: post-tool mirror, NOT an LLM request.
            kind='state',
            model=model,
            roundNum=round_num + 1,
            label=f'Round {round_num + 1} 工具结果后 · {len(snapshot)}条',
            messages=snapshot,
            contextManifest=list(task.get('_contextManifest') or []),
        )
        if tool_list:
            snap_evt['tools'] = tool_list
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
