"""ORACLE: the Turn projection capture/projection pipeline before settled rows.

Vendored verbatim from base commit 1fa505314 (generated mechanically with
``ast.get_source_segment``) so the differential equivalence spec compares the
incremental pipeline against the exact pre-change computation. Only three
function-local imports were rewired to these vendored functions and the frame
budget is read from the live module so a test can lower it for both sides.
The activity-timeline eviction order (``_eviction_index``) and the bounded
window patch planner (``_evicted_run``) are policy, not the incremental
mechanism under comparison, so ``_trim_entries`` and ``_diff_value`` read them
from the live module too.
Helpers this change did not modify are imported from their live owners.
Never edit the vendored bodies to make a test pass.
"""
# ruff: noqa
from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping

import lib.storage.storage_projection as _storage_projection
from lib.agent_core.events import EventType, Phase
from lib.cost import (
    _ANTHROPIC_CACHE_WRITE_1H_MUL, _cache_write_1h_tokens, _legacy_preset_to_model,
    _round, normalize_usage, split_input_tokens,
)
from lib.pricing import DEFAULT_USD_CNY_RATE, get_pricing_data, lookup_pricing
from lib.storage.storage_projection import (
    _TOOL_ROUNDS_KEEP_TAIL_INTACT, _elide_results_payloads, _elide_string_field,
    _swarm_stub_snapshot_from_handle, sanitize_usage_for_persist,
    trim_tool_round_for_persist,
)
from lib.tasks_pkg.segments import (
    assemble_segments, is_synthetic_inbox_round, segments_to_json,
)
from lib.tasks_pkg.turn_trace import _TASK_STATUS_DROPPED_KEY, _TASK_STATUS_HISTORY_KEY
from lib.tool_results.tool_round_identity import _identity_text, execution_identity
from lib.turn_activity_timeline import (
    ACTIVITY_TIMELINE_MAX_ENTRIES, ACTIVITY_TIMELINE_MAX_JSON_BYTES,
    ACTIVITY_TIMELINE_VERSION, _COUNT_MAX, _INTEGER_MAX, _activity_anchors,
    _append_or_update, _close_open_entries, _diagnostic_text, _entry_id, _event_time,
    _eviction_index,
    _fold_compaction_event, _fold_error, _fold_model_complete, _fold_model_fallback,
    _fold_schema_rejection, _fold_tool_event, _llm_round_anchor, _nonnegative_int,
    _normalize_entry, _now_ms, _parent_request, _producer_identity, _round_number,
    _safe_args, _same_producer, _stable_span, _text, _track_llm_round,
    capture_compaction_boundary,
)
from lib.turn_lifecycle import (
    _PROJECTION_INJECTION_LANES, _PROJECTION_PROVENANCE_FIELDS, _PROJECTION_TASK_FIELDS,
    _file_changes_block, _merge_user_steer_records, _projection_injection_records,
    _projection_provenance, logger, task_event_projection_is_progress,
)
from lib.turn_projection_patch import (
    PROJECTION_PATCH_VERSION, PUBLIC_PROJECTION_FIELDS, _PROJECTION_IDENTITY_KEYS,
    _evicted_run, _is_json_sequence, _normalize_compaction, _normalize_context,
    _normalize_image_generation, _normalize_origin, _normalize_rolled_back,
    _normalize_translation, plan_execution_document, proposed_plan_document,
)
from lib.turn_projection_segments import (
    _RESUMABLE_TURN_STATUSES, _SEGMENT_TYPES, _append_missing_live_tool_segments,
    _drop_narrated_live_terminal_mirror, _fallback_block_id,
    _live_content_already_narrated, _normalize_sidecar_blocks,
    _round_has_explicit_payload_compaction, _segment_result_from_round,
    _tool_segment_id_counts, _tool_segment_matches_round, _unique_rounds_by_call_id,
)
from lib.turn_trace_contract import (
    TRACE_CONTRACT_VERSION, TRACE_MAX_CLIENT_OBSERVATIONS, TRACE_MAX_COUNTER,
    TRACE_MAX_GAPS, TRACE_MAX_PERSISTED_BYTES, TRACE_MAX_SPANS,
    TRACE_MAX_STATUS_ENTRIES, _TRACE_COMPACTION_MARKERS, _bounded_trace_text,
)
from lib.utils import fast_deepcopy_with_retained_bytes


def capture_task_projection_snapshot(task, event, *, previous_source=None, previous_bytes=0):
    return _capture_task_projection_snapshot(
        task, event, previous_source=previous_source, previous_bytes=previous_bytes,
        detach_structure=fast_deepcopy_with_retained_bytes)


# ── lib/turn_lifecycle.py @ 1fa505314 ────────────────────


def _capture_task_projection_snapshot(
        task, event, *, previous_source, previous_bytes, detach_structure):
    from lib.utils import fast_deepcopy as deepcopy
    progress = task_event_projection_is_progress(event)
    if progress and previous_source is not None:
        source = dict(previous_source)
        structure_bytes = previous_bytes
    else:
        keys = {
            'id', 'taskId', '_attemptId', 'attemptId', '_turnActor', '_turnStatus',
            'status', '_turnVisibleRunTurnIds', '_checkpointToolRounds', '_mcpImages',
            'toolRounds', 'apiRounds', 'error', '_activeModelRequestSpan',
            '_timingStatusHistory', '_timingStatusDroppedCount',
        }
        keys.update(key for key, _ in _PROJECTION_TASK_FIELDS)
        keys.update(key for key, _, _ in _PROJECTION_INJECTION_LANES)
        keys.update(key for key, _ in _PROJECTION_PROVENANCE_FIELDS)
        values = {key: task[key] for key in keys if key in task}
        if 'apiRounds' in values:
            values['apiRounds'] = sanitize_api_rounds_for_persist(values['apiRounds'])
        if 'toolRounds' in values:
            values['toolRounds'] = compact_tool_rounds_for_frame_budget([
                trim_tool_round_for_persist(dict(row)) if isinstance(row, dict) else row
                for row in values['toolRounds']])
        if task.get('_todoState'):
            # The owned checklist changes during tool execution, before any
            # terminal projection. Structural round receipts must preserve it
            # so a hard restart never needs to replay a completed tool.
            from lib.tools.todo import public_todo_state
            values['todoState'] = public_todo_state(task['_todoState'])
        values['config'] = {'checkpointToolRounds':
            (task.get('config') or {}).get('checkpointToolRounds', [])}
        # Build the complete immutable structural graph before detaching it.
        # The copying owner can now account its actual reachable objects as
        # it visits them, instead of traversing the same long history again.
        source, structure_bytes = detach_structure(values)
    from lib.tasks_pkg.manager._events import snapshot_task_text
    source['content'], source['thinking'], _ = snapshot_task_text(task)
    source['phase'] = deepcopy(task.get('phase'))
    return source, structure_bytes


def _task_projection(
    task: dict[str, Any],
    previous: dict[str, Any],
    raw_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    projection = dict(previous)
    cfg = task.get('config') or {}
    # Flow phases commit their visible rows independently through
    # ``sync_visible_run_turns``.  Later orchestration bookkeeping events must
    # not fold the aggregate task buffer back over the first phase's bubble.
    # The per-node flow_iteration reset clears task['thinking'] as well, so
    # the first turn's committed thinking needs the same guard as content.
    owns_visible_run_turns = bool(task.get('_turnVisibleRunTurnIds'))
    # cfg['contentPrefix'] is deliberately NOT a fallback here: it is a
    # one-time display seed applied to task['content'] at resume hydration
    # (apply_resume_state). Falling back to it re-projects the interrupted
    # attempt's stale prose whenever _discard_pretool_prose empties the
    # accumulator mid-run — the resumed turn then shows the old blob as its
    # terminal answer until finalize (conv mto8xq9we7xc6o, 2026-09-05).
    content = (projection.get('content', '') if owns_visible_run_turns else
               (task.get('content') or ''))
    checkpoint_rounds = (task.get('_checkpointToolRounds')
                         or cfg.get('checkpointToolRounds') or [])
    attempt_id = task.get('_attemptId') or task.get('attemptId') or ''
    task_id = task.get('id') or task.get('taskId') or ''
    # A Turn can outlive several executors. Preserve checkpoint ownership and
    # stamp unowned fresh rounds before the durable projection boundary.
    # Delegated producer stamps remain intact; display attempts are not their
    # execution identity. Logical counters continue from the retained frontier;
    # historical local-counter rows keep their exact producer scope.
    merged_rounds = tool_rounds_with_execution_identity(
        checkpoint_rounds, attempt_id='', task_id='',
    ) + tool_rounds_with_execution_identity(
        task.get('toolRounds') or [],
        attempt_id=attempt_id,
        task_id=task_id if attempt_id else '',
        overwrite=bool(attempt_id),
    )
    if owns_visible_run_turns:
        merged_rounds = previous.get('toolRounds') or []
    projected_rounds = [
        trim_tool_round_for_persist(dict(item))
        if isinstance(item, dict) else item
        for item in merged_rounds
    ]
    # Keep the durable document below one storage frame: without this cap a
    # long tool-heavy turn grows past the wire limit and every authoritative
    # write starts failing closed (task mtdx825fjmhmx5: 379 rejected frames).
    projected_rounds = compact_tool_rounds_for_frame_budget(projected_rounds)
    projection.update({
        'content': content,
        'thinking': (projection.get('thinking', '') if owns_visible_run_turns
                     else (task.get('thinking')
                           if task.get('thinking') is not None
                           else projection.get('thinking', ''))),
        'toolRounds': projected_rounds,
    })

    # New executors always own this field. Its presence intentionally clears
    # stale images on regenerate, while checkpoint/continue initialization
    # preserves prior refs before appending new MCP result images.
    if task.get('_mcpImages') is not None:
        projection['images'] = list(task.get('_mcpImages') or [])
    for source, target in _PROJECTION_TASK_FIELDS:
        if owns_visible_run_turns and source in {
            'segments', 'modifiedFiles', 'modifiedFileList', 'modifiedFilesTruncated', 'todoState',
        }:
            continue
        if task.get(source) is not None:
            projection[target] = task[source]
    if not owns_visible_run_turns and task.get('_todoState'):
        from lib.tools.todo import public_todo_state
        projection['todoState'] = public_todo_state(task['_todoState'])
    # Display-only injection records are copied and normalized independently:
    # unlike content/tool facts, their stable render identity is part of the
    # projection contract and must survive hydration.  Normalize previous
    # projections too so the next event upgrades rows written before blockId.
    for source, target, channel in _PROJECTION_INJECTION_LANES:
        records = (task[source] if task.get(source) is not None
                   else projection.get(target))
        if source == '_bgCommandInjects' and (
                records is not None or previous.get(target) is not None
                or (raw_event and raw_event.get('type') == 'background_command_inject')):
            # A lost commit ACK can leave the executor behind durable receipt
            # state. Never let its older sidecar erase a committed delivery.
            committed = _projection_injection_records(previous.get(target), channel)
            local = _projection_injection_records(records, channel)
            records = list(committed) if isinstance(committed, list) else []
            known = {item['blockId'] for item in records
                     if isinstance(item, dict) and item.get('blockId')}
            for item in local if isinstance(local, list) else []:
                if not isinstance(item, dict) or item.get('blockId') not in known:
                    records.append(item)
            if raw_event and raw_event.get('type') == 'background_command_inject':
                from lib.background_command_projection import background_command_injection_record
                receipt = background_command_injection_record(
                    raw_event, attempt_id=str(attempt_id))
                records = [item for item in records if not isinstance(item, dict)
                           or item.get('blockId') != receipt['blockId']]
                records.append(receipt)
        if source == '_userSteerInjects' and records is not None:
            records = _merge_user_steer_records(previous.get(target), records)
        if records is not None:
            projection[target] = _projection_injection_records(records, channel)
    provenance = _projection_provenance(task, projection.get('provenance'))
    if provenance is not None:
        projection['provenance'] = provenance
    # The legacy counter/list remain readable during migration, but the
    # renderer consumes one explicit, stable content block.  Derive it at the
    # projection authority so every client sees the same identity and list.
    file_changes = _file_changes_block(str(task.get('id') or ''), projection)
    if file_changes is not None:
        projection['fileChanges'] = file_changes
    # Per-round usage breakdown. The context-health gauge's "last round
    # prompt" reading is only honest when per-round usage survives a reload;
    # without it the frontend can only divide the turn's ACCUMULATED bill by
    # one and presents the whole turn's cost as a single prompt
    # (2026-08-20 fake "1.3M / 1.1M = 100%" reading on a ~170k prompt).
    # Sanitize like the legacy persist path so ``_wire_*`` diagnostics never
    # reach durable rows (measured GiB-class bloat in round_usage storage).
    if task.get('apiRounds'):
        projection['apiRounds'] = sanitize_api_rounds_for_persist(
            task['apiRounds'])
    # Authoritative settled-cost snapshot: ONE top-level total the finish
    # footer / cost popover read; apiRounds stays the per-round ledger.
    # Mirrors the done-event stamp in orchestrator/_finalize so live and
    # reload paths sum each API round under its own model/provider/tier.
    # Without this fold a reloaded projection carried usage but no cost, and
    # the footer fell back to a client-side batch lookup whose async landing
    # was diffed away by the surface footer compare (2026-08-29,
    # mtd9ci53zq3xfm: no hover cost breakdown after reload).
    if projection.get('usage'):
        try:
            _fallback_model = str(
                projection.get('model') or task.get('model') or '')
            _fallback_provider = task.get('provider_id') or None
            _settled_cost = compute_api_rounds_cost(
                projection.get('apiRounds'),
                fallback_model_id=_fallback_model,
                fallback_provider_id=_fallback_provider,
                use_frozen_costs=task.get('_nativeFrozenCostProjection') is True,
            )
            if _settled_cost is None and task.get('_nativeFrozenCostProjection') is not True:
                _settled_cost = compute_cost(
                    projection['usage'],
                    model_id=_fallback_model,
                    provider_id=_fallback_provider,
                )
            if _settled_cost:
                projection['cost'] = _settled_cost
        except Exception as _cost_exc:
            logger.debug('[TurnLifecycle] settled-cost fold failed: %s',
                         _cost_exc)
    if raw_event is not None:
        # Runtime events remain the canonical facts.  The public Turn owns one
        # bounded, replay-safe presentation projection so a refresh preserves
        # exactly when a tool was isolated, a model switched, or an error
        # occurred without putting any of those diagnostics into LLM context.
        activity_timeline = fold_activity_timeline(
            projection.get('activityTimeline'), raw_event, task,
        )
        if activity_timeline is not None:
            projection['activityTimeline'] = activity_timeline
    # The live status remains transient for rendering, but its bounded history
    # is durable diagnostic evidence. Provider-ingress isolation may keep an
    # individual phase frame memory-local; this cumulative projection crosses
    # the next authoritative boundary and prevents that user-visible prompt
    # from disappearing from postmortem analysis.
    projection = project_running_trace_status(projection, task)
    return projection_with_stable_segments(
        projection,
        actor=str(task.get('_turnActor') or 'assistant'),
        status=str(task.get('_turnStatus') or task.get('status') or 'running'),
    )


# ── lib/storage/storage_projection.py @ 1fa505314 ────────────────────


def sanitize_api_rounds_for_persist(api_rounds):
    """Remove wire diagnostics while retaining public round/cost/token data."""
    if not isinstance(api_rounds, list):
        return api_rounds
    out = []
    changed = False
    for round_item in api_rounds:
        if (isinstance(round_item, dict)
                and isinstance(round_item.get('usage'), dict)):
            clean_usage = sanitize_usage_for_persist(round_item['usage'])
            if clean_usage is not round_item['usage']:
                round_item = {**round_item, 'usage': clean_usage}
                changed = True
        out.append(round_item)
    return out if changed else api_rounds


def _string_payload_chars(value):
    """Cheap serialized-size proxy: JSON overhead stays proportional."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(_string_payload_chars(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_string_payload_chars(item) for item in value)
    return 0


def compact_tool_rounds_for_frame_budget(
    rounds,
    *,
    budget_bytes=None,
    keep_tail=_TOOL_ROUNDS_KEEP_TAIL_INTACT,
):
    """Elide the oldest settled rounds' heavy payloads when the toolRounds
    lane crosses the durable frame budget.

    Identity fields replay depends on (``toolCallId``, ``toolName``,
    ``toolArgs``, ``status``) are never touched, so the compacted lane stays a
    valid replay prefix; only free-text payload buffers (``toolContent``,
    ``results[]`` string values) become honest stubs. The newest ``keep_tail``
    rounds and any in-flight round stay intact. Copy-on-change: the input and
    every untouched round keep their identity, and the function is
    allocation-free while the lane already fits.
    """
    if not isinstance(rounds, list) or not rounds:
        return rounds
    budget = (_storage_projection.TOOL_ROUNDS_FRAME_BUDGET_BYTES
              if budget_bytes is None else budget_bytes)
    total = sum(_string_payload_chars(item) for item in rounds)
    if total <= budget:
        return rounds
    compacted = list(rounds)
    last_eligible = max(0, len(compacted) - keep_tail)
    for position in range(last_eligible):
        if total <= budget:
            break
        item = compacted[position]
        if not isinstance(item, dict):
            continue
        if item.get('status') in ('running', 'pending'):
            continue
        clone = copy.deepcopy(item)
        stub_snapshot = _swarm_stub_snapshot_from_handle(item)
        reclaimed = _elide_string_field(clone, 'toolContent')
        reclaimed += _elide_results_payloads(clone)
        if reclaimed <= 0:
            continue
        if stub_snapshot is not None:
            clone['_swarmSnapshot'] = stub_snapshot
            total += _string_payload_chars(stub_snapshot)
        clone['_persistCompacted'] = True
        compacted[position] = clone
        total -= reclaimed
    return compacted


# ── lib/tool_results/tool_round_identity.py @ 1fa505314 ────────────────────


def tool_round_with_execution_identity(
    value: Any,
    *,
    attempt_id: Any,
    task_id: Any = "",
    overwrite: bool = False,
) -> Any:
    """Backfill a copied occurrence only within its producing execution.

    A task/Turn may display another invocation's rounds. Its persistence scope
    must not replace that producer or attach the display attempt to a child.
    ``overwrite`` can canonicalize the same owner's fields, never reassign an
    explicitly owned occurrence (including during resume-history backfill).
    """
    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    normalized_attempt = _identity_text(attempt_id)
    normalized_task = _identity_text(task_id)
    existing_attempt, existing_task = execution_identity(value)
    if ((existing_task and existing_task != normalized_task)
            or (existing_attempt and existing_attempt != normalized_attempt)):
        return result
    if normalized_attempt and (overwrite or not result.get("attemptId")):
        result["attemptId"] = normalized_attempt
    if normalized_task and (overwrite or not result.get("taskId")):
        result["taskId"] = normalized_task
    return result


def tool_rounds_with_execution_identity(
    values: Any,
    *,
    attempt_id: Any,
    task_id: Any = "",
    overwrite: bool = False,
) -> list[Any]:
    """Copy a round list and stamp each mapping with one execution identity."""
    if not isinstance(values, (list, tuple)):
        return []
    return [
        tool_round_with_execution_identity(
            value,
            attempt_id=attempt_id,
            task_id=task_id,
            overwrite=overwrite,
        )
        for value in values
    ]


# ── lib/cost.py @ 1fa505314 ────────────────────


def compute_cost(
    usage: Optional[dict],
    model_id: str = '',
    provider_id: Optional[str] = None,
    *,
    at: Optional[float] = None,

    pricing_override: Optional[dict] = None,
) -> Optional[dict]:
    """Compute USD + CNY cost from a usage dict.

    Returns ``None`` when the usage is empty/null or all token counts
    are zero. Otherwise returns the same keys the JS ``calcCostCny``
    produces so callers can swap freely.
    """
    if not usage or not isinstance(usage, dict):
        return None

    model_id = _legacy_preset_to_model(model_id or '')

    _u = normalize_usage(usage)
    inp = _u['input']
    out = _u['output']
    cache_write = _u['cache_write']
    cache_read = _u['cache_read']
    think_tok = _u['thinking']

    # Some providers report thinking in `reasoning_tokens` but `out=0`
    # when the model emitted only thinking; treat thinking as output then.
    if think_tok > 0 and out == 0:
        out = think_tok

    if inp == 0 and out == 0 and cache_write == 0 and cache_read == 0:
        return None

    pricing_data = get_pricing_data()
    rate = pricing_data.get('usdToCny') or DEFAULT_USD_CNY_RATE
    uncached, total_input = split_input_tokens(usage)

    # One resolver call selects ONE context tier from the COMPLETE prompt.
    # Input/output/cache read/cache write all consume the resulting rates.
    mp = pricing_override if pricing_override is not None else (lookup_pricing(
        model_id,
        provider_id,
        at=at,
        prompt_tokens=total_input,
    ) if model_id else None)
    peak_mul = None
    if mp:
        base_in = float(mp.get('input') or 0)
        out_p = float(mp.get('output') or 0)
        cw_mul = float(mp.get('cacheWriteMul', 1.25))
        cr_mul = float(mp.get('cacheReadMul', 0.10))
        peak_mul = mp.get('peakMul')  # stamped by lookup_pricing at peak hours
        pricing_source = str(mp.get('_pricingSource') or 'resolved_price')
        currency = str(mp.get('currency') or 'USD').upper()
        selected_tier = mp.get('selectedTier')
    else:
        base_in = float(pricing_data.get('inputPrice') or 0)
        out_p = float(pricing_data.get('outputPrice') or 0)
        cw_mul = float(pricing_data.get('cacheWriteMul') or 1.25)
        cr_mul = float(pricing_data.get('cacheReadMul') or 0.10)
        # Keep the legacy amount for UI/backward compatibility, but label it
        # honestly: this global rate is not evidence that the requested model
        # has a matching price row.
        pricing_source = 'default_estimate'
        currency = 'USD'
        selected_tier = None

    # Only the reported 1-hour TTL share is repriced; without the breakdown
    # every write keeps the row's (5-minute) cacheWriteMul.
    cache_write_1h = _cache_write_1h_tokens(usage, cache_write)
    cw_1h_mul = None
    if cache_write_1h:
        cw_1h_mul = (mp or {}).get('cacheWrite1hMul')
        if cw_1h_mul is None:
            from lib.model_info import is_claude
            # The 2x default belongs to Anthropic's official schedule (5-minute
            # writes at 1.25x); a row declaring its own write multiplier keeps
            # it for both TTLs until it declares a 1-hour rate.
            official_schedule = math.isclose(cw_mul, 1.25)
            cw_1h_mul = (_ANTHROPIC_CACHE_WRITE_1H_MUL
                         if official_schedule and model_id and is_claude(model_id)
                         else cw_mul)
        cw_1h_mul = float(cw_1h_mul)

    unit_to_usd = (1.0 / rate) if currency == 'CNY' else 1.0
    base_in_usd = base_in * unit_to_usd
    out_p_usd = out_p * unit_to_usd
    output_cost_usd = (out * out_p_usd) / 1e6

    # ── Cache convention detection ──
    # Delegated to split_input_tokens (the SINGLE source of this decision, also
    # used by the cache-hit telemetry) so a reported hit-rate and the price
    # charged for the same round can never disagree. It keys on the payload's
    # KEY SPELLINGS, not on token magnitudes — see usage_cache_convention for
    # the 10x mis-pricing that the old ``inp <= cw + cr`` comparison caused
    # when a round read its entire prompt back from cache.
    if cache_write > 0 or cache_read > 0:
        uncached, total_input = split_input_tokens(usage)
        input_cost_usd = (uncached * base_in_usd) / 1e6
        cw_cost_usd = ((cache_write - cache_write_1h) * base_in_usd * cw_mul
                       + cache_write_1h * base_in_usd * (cw_1h_mul or 0.0)) / 1e6
        cr_cost_usd = (cache_read * base_in_usd * cr_mul) / 1e6
    else:
        uncached = inp
        total_input = inp
        input_cost_usd = (inp * base_in_usd) / 1e6
        cw_cost_usd = 0.0
        cr_cost_usd = 0.0

    cost_usd = input_cost_usd + cw_cost_usd + cr_cost_usd + output_cost_usd
    no_cache_input_usd = (total_input * base_in_usd) / 1e6
    cache_savings_usd = no_cache_input_usd - (
        input_cost_usd + cw_cost_usd + cr_cost_usd)

    return {
        'costUsd': _round(cost_usd),
        'costCny': _round(cost_usd * rate),
        'pricingSource': pricing_source,
        'pricingSnapshot': {
            'model': model_id, 'provider': provider_id,
            'source': pricing_source, 'currency': currency,
            'selectedPromptTokens': total_input,
            'tierId': selected_tier.get('id') if selected_tier else None,
            'maxPromptTokens': (selected_tier.get('maxPromptTokens')
                                if selected_tier else None),
            'rates': {'input': base_in, 'output': out_p,
                      'cacheWriteMul': cw_mul, 'cacheReadMul': cr_mul,
                      **({'cacheWrite1hMul': cw_1h_mul}
                         if cache_write_1h else {})},
        },
        'inputTokens': uncached,
        'outputTokens': out,
        'totalInputTokens': total_input,
        'cacheWriteTokens': cache_write,
        **({'cacheWrite1hTokens': cache_write_1h} if cache_write_1h else {}),
        'cacheReadTokens': cache_read,
        'thinkingTokens': think_tok,
        'inputCostCny': _round(input_cost_usd * rate, 6),
        'outputCostCny': _round(output_cost_usd * rate, 6),
        'cacheWriteCostCny': _round(cw_cost_usd * rate, 6),
        'cacheReadCostCny': _round(cr_cost_usd * rate, 6),
        # USD per-component sub-costs — consumed by the billing adapter
        # (lib/billing/cost.compute_request_cost) so the wallet debit and the
        # displayed cost share ONE arithmetic core and can never drift.
        'inputCostUsd': _round(input_cost_usd, 9),
        'outputCostUsd': _round(output_cost_usd, 9),
        'cacheWriteCostUsd': _round(cw_cost_usd, 9),
        'cacheReadCostUsd': _round(cr_cost_usd, 9),
        'cacheSavingsCny': _round(
            cache_savings_usd * rate if cache_savings_usd > 0 else 0, 6),
        'cacheSavingsUsd': _round(
            cache_savings_usd if cache_savings_usd > 0 else 0, 6),
        # Present ONLY when the resolved pricing was peak-scaled (all four
        # components already carry the multiplier) — transparency for
        # "why did this round cost 2x" without re-deriving the schedule.
        **({'peakMultiplier': peak_mul} if peak_mul else {}),
    }


def compute_api_rounds_cost(
    api_rounds,
    *,
    fallback_model_id: str = '',
    fallback_provider_id: Optional[str] = None,
    use_frozen_costs: bool = False,
) -> Optional[dict]:
    """Sum an API-round ledger without flattening model/pricing identity.

    A turn may contain main-model, fallback-model, and compaction-model calls.
    Merging their usage first and pricing the result under the final model is
    mathematically wrong even when the token totals are exact. This helper
    prices every round under its own model/provider/tier, then folds only the
    resulting money and normalized token components for the top-level display.
    It does not mutate the round ledger.
    """
    costs: list[dict] = []
    models: set[str] = set()
    providers: set[str] = set()
    sources: set[str] = set()
    for round_entry in api_rounds or ():
        if not isinstance(round_entry, dict):
            continue
        usage = round_entry.get('usage')
        if not isinstance(usage, dict) or not usage:
            continue
        model = str(round_entry.get('model') or fallback_model_id or '')
        provider = (round_entry.get('provider_id')
                    or round_entry.get('providerId')
                    or fallback_provider_id)
        if use_frozen_costs:
            if 'cost' not in round_entry:
                raise ValueError('frozen cost aggregation requires every round snapshot')
            cost = round_entry.get('cost')
            if cost is not None and not isinstance(cost, dict):
                raise ValueError('frozen round cost must be a canonical mapping')
        else:
            cost = compute_cost(
                usage, model_id=model, provider_id=provider)
        if not cost:
            continue
        costs.append(cost)
        if model:
            models.add(model)
        if provider:
            providers.add(str(provider))
        if cost.get('pricingSource'):
            sources.add(str(cost['pricingSource']))
    if not costs:
        return None

    token_keys = (
        'inputTokens', 'outputTokens', 'totalInputTokens',
        'cacheWriteTokens', 'cacheReadTokens', 'thinkingTokens',
    )
    usd_component_keys = (
        'inputCostUsd', 'outputCostUsd',
        'cacheWriteCostUsd', 'cacheReadCostUsd',
    )
    cny_component_keys = (
        'inputCostCny', 'outputCostCny',
        'cacheWriteCostCny', 'cacheReadCostCny',
    )

    def total(key: str) -> float:
        return sum(float(cost.get(key) or 0.0) for cost in costs)

    aggregate = {
        'costUsd': _round(sum(total(key) for key in usd_component_keys)),
        'costCny': _round(sum(total(key) for key in cny_component_keys)),
        'pricingSource': 'api_round_aggregate',
        'pricingSnapshot': {
            'kind': 'apiRoundAggregate',
            'rounds': len(costs),
            'models': sorted(models),
            'providers': sorted(providers),
            'sources': sorted(sources),
        },
        **{key: int(total(key)) for key in token_keys},
        **{key: _round(total(key), 9) for key in usd_component_keys},
        **{key: _round(total(key), 6) for key in cny_component_keys},
        'cacheSavingsUsd': _round(total('cacheSavingsUsd'), 6),
        'cacheSavingsCny': _round(total('cacheSavingsCny'), 6),
    }
    return aggregate


# ── lib/turn_trace_contract.py @ 1fa505314 ────────────────────


def _sanitize_trace_value(value, *, depth=0):
    """Return a small JSON-safe diagnostic value without content payloads."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:400]
    if depth >= 5:
        return _bounded_trace_text(value, 200)
    if isinstance(value, Mapping):
        out = {}
        for key, child in list(value.items())[:32]:
            name = _bounded_trace_text(key, 100)
            if not name or name.startswith('_'):
                continue
            out[name] = _sanitize_trace_value(child, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_trace_value(child, depth=depth + 1)
            for child in list(value)[:32]
        ]
    return _bounded_trace_text(value, 200)


def _bounded_trace_rows(rows, maximum, *, keep_recent=False):
    clean = [
        _sanitize_trace_value(row)
        for row in (rows or [])
        if isinstance(row, Mapping)
    ]
    if len(clean) <= maximum:
        return clean, 0
    if keep_recent:
        return clean[-maximum:], len(clean) - maximum
    head = maximum // 2
    tail = maximum - head
    return clean[:head] + clean[-tail:], len(clean) - maximum


def compact_trace_document(document, *, source=None):
    """Bound one live or terminal trace while preserving aggregate truth.

    Row detail is diagnostic/reconstructible; ``summary`` and top-level clocks
    remain exact.  When compaction is necessary the dropped counts make the
    loss explicit instead of presenting a deceptively complete waterfall.
    """
    if not isinstance(document, Mapping):
        return {}
    scalar_fields = (
        'version', 'taskId', 'eventsAvailable', 'eventLogAvailable', 'status',
        'running', 'coverage', 'coverageReason', 'clockSource', 'tStart', 'tEnd', 'totalMs',
    )
    out = {
        key: _sanitize_trace_value(document.get(key))
        for key in scalar_fields if key in document
    }
    if source or document.get('source'):
        out['source'] = _bounded_trace_text(
            source or document.get('source'), 40)
    if document.get('detailCompacted'):
        out['detailCompacted'] = True
    if document.get('compacted'):
        out['compacted'] = True
    dropped_over_budget = 0
    if isinstance(document.get('summary'), Mapping):
        out['summary'] = _sanitize_trace_value(document['summary'])
        if isinstance(document['summary'].get('overBudget'), list):
            over_budget_rows, dropped_over_budget = _bounded_trace_rows(
                document['summary']['overBudget'], TRACE_MAX_SPANS)
            out['summary']['overBudget'] = over_budget_rows

    spans, dropped_spans = _bounded_trace_rows(
        document.get('spans'), TRACE_MAX_SPANS)
    gaps, dropped_gaps = _bounded_trace_rows(
        document.get('gaps'), TRACE_MAX_GAPS)
    statuses, dropped_statuses = _bounded_trace_rows(
        document.get('statusHistory'), TRACE_MAX_STATUS_ENTRIES,
        keep_recent=True)
    observations, dropped_observations = _bounded_trace_rows(
        document.get('clientObservations'), TRACE_MAX_CLIENT_OBSERVATIONS,
        keep_recent=True)
    if spans or 'spans' in document:
        out['spans'] = spans
    if gaps or 'gaps' in document:
        out['gaps'] = gaps
    if statuses or 'statusHistory' in document:
        out['statusHistory'] = statuses
    if observations or 'clientObservations' in document:
        out['clientObservations'] = observations

    dropped_fields = (
        ('droppedSpans', dropped_spans),
        ('droppedGaps', dropped_gaps),
        ('statusDroppedCount', dropped_statuses),
        ('clientObservationDroppedCount', dropped_observations),
        ('overBudgetDroppedCount', dropped_over_budget),
    )
    for key, newly_dropped in dropped_fields:
        total = min(
            TRACE_MAX_COUNTER,
            max(0, int(document.get(key) or 0)) + newly_dropped,
        )
        if total:
            out[key] = total
            out['compacted'] = True

    def encoded_size():
        return len(json.dumps(
            out, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))

    # A live trace is re-compacted on every projected event and normally sits
    # far below the cap; one measurement then proves every guard below is a
    # no-op, so the document is not re-serialized once per guard.
    if encoded_size() <= TRACE_MAX_PERSISTED_BYTES:
        if any(out.get(key) for key in _TRACE_COMPACTION_MARKERS):
            out['compacted'] = True
        return out

    # The shape caps normally stay well below 96 KiB. This final byte guard is
    # deliberately deterministic for unusually rich attempt diagnostics.
    for key, floor, dropped_key in (
        ('spans', 24, 'droppedSpans'),
        ('statusHistory', 16, 'statusDroppedCount'),
        ('gaps', 16, 'droppedGaps'),
        ('clientObservations', 16, 'clientObservationDroppedCount'),
    ):
        while encoded_size() > TRACE_MAX_PERSISTED_BYTES \
                and len(out.get(key) or []) > floor:
            rows = out[key]
            remove = max(1, len(rows) // 4)
            if key in {'statusHistory', 'clientObservations'}:
                del rows[:remove]
            else:
                middle = max(1, (len(rows) - remove) // 2)
                del rows[middle:middle + remove]
            out[dropped_key] = min(
                TRACE_MAX_COUNTER,
                int(out.get(dropped_key) or 0) + remove,
            )
            out['compacted'] = True

    if encoded_size() > TRACE_MAX_PERSISTED_BYTES:
        # Preserve identities/verdicts but shed verbose attributes before any
        # remaining incident receipt. This is evidence degradation, never an
        # authority or completion change.
        for span in out.get('spans') or []:
            if isinstance(span, dict) and span.get('attrs'):
                span['attrs'] = {}
        out['detailCompacted'] = True
        out['compacted'] = True

    # The normal floors favor a useful cross-section of each evidence lane,
    # but all lanes can simultaneously contain maximum-length strings. Continue
    # deterministically below those preference floors until the byte contract
    # is actually true. Aggregate clocks remain untouched; each loss is counted.
    for key, floor, dropped_key in (
        ('gaps', 0, 'droppedGaps'),
        ('spans', 1, 'droppedSpans'),
        ('statusHistory', 1, 'statusDroppedCount'),
        ('clientObservations', 1, 'clientObservationDroppedCount'),
    ):
        while encoded_size() > TRACE_MAX_PERSISTED_BYTES \
                and len(out.get(key) or []) > floor:
            rows = out[key]
            remove = max(1, min(len(rows) - floor, len(rows) // 4))
            if key in {'statusHistory', 'clientObservations'}:
                del rows[:remove]
            else:
                middle = max(0, (len(rows) - remove) // 2)
                del rows[middle:middle + remove]
            out[dropped_key] = min(
                TRACE_MAX_COUNTER,
                int(out.get(dropped_key) or 0) + remove,
            )
            out['detailCompacted'] = True
            out['compacted'] = True

    # ``summary.overBudget`` is a diagnostic worklist, not part of the disjoint
    # aggregate clocks. Trim it only after row evidence; exact bucket totals are
    # retained. This also protects the hard cap from 256 maximum-length names.
    summary = out.get('summary')
    over_budget = (
        summary.get('overBudget')
        if isinstance(summary, dict) else None
    )
    while encoded_size() > TRACE_MAX_PERSISTED_BYTES \
            and isinstance(over_budget, list) and over_budget:
        del over_budget[max(0, len(over_budget) // 2)]
        out['overBudgetDroppedCount'] = min(
            TRACE_MAX_COUNTER,
            int(out.get('overBudgetDroppedCount') or 0) + 1,
        )
        out['detailCompacted'] = True
        out['compacted'] = True

    if encoded_size() > TRACE_MAX_PERSISTED_BYTES:
        # Absolute fail-safe for corrupt/future over-wide detail. Preserve only
        # the closed aggregate-clock contract and top-level identity/verdicts.
        for key, dropped_key in (
            ('gaps', 'droppedGaps'),
            ('spans', 'droppedSpans'),
            ('statusHistory', 'statusDroppedCount'),
            ('clientObservations', 'clientObservationDroppedCount'),
        ):
            removed = len(out.get(key) or [])
            if removed:
                out[key] = []
                out[dropped_key] = min(
                    TRACE_MAX_COUNTER,
                    int(out.get(dropped_key) or 0) + removed,
                )
        if isinstance(summary, dict):
            remaining_over_budget = len(summary.get('overBudget') or [])
            if remaining_over_budget:
                out['overBudgetDroppedCount'] = min(
                    TRACE_MAX_COUNTER,
                    int(out.get('overBudgetDroppedCount') or 0)
                    + remaining_over_budget,
                )
            aggregate_keys = (
                'totalMs', 'llmMs', 'toolMs', 'waitMs', 'compactionMs',
                'approvalWaitMs', 'flowStepMs', 'orchestrationMs',
                'unattributedMs', 'ttftMs',
                'admissionWaitMs', 'retryWaitMs', 'spawnWaitMs',
                'startupMs', 'roundPrepMs', 'roundSettleMs', 'finalizeMs',
            )
            out['summary'] = {
                key: summary[key]
                for key in aggregate_keys if key in summary
            }
            out['summary']['overBudget'] = []
        out['detailCompacted'] = True
        out['compacted'] = True
    if any(out.get(key) for key in _TRACE_COMPACTION_MARKERS):
        out['compacted'] = True
    return out


# ── lib/tasks_pkg/turn_trace.py @ 1fa505314 ────────────────────


def task_status_history(task):
    """Return a detached bounded status history for a Turn projection."""
    if not isinstance(task, Mapping):
        return [], 0
    history = task.get(_TASK_STATUS_HISTORY_KEY)
    if not isinstance(history, list):
        return [], max(0, int(task.get(_TASK_STATUS_DROPPED_KEY) or 0))
    detached = [
        _sanitize_trace_value(entry)
        for entry in history[-TRACE_MAX_STATUS_ENTRIES:]
        if isinstance(entry, Mapping)
    ]
    dropped = max(0, int(task.get(_TASK_STATUS_DROPPED_KEY) or 0))
    dropped += max(0, len(history) - len(detached))
    return detached, dropped


def project_running_trace_status(projection, task):
    """Attach the bounded server phase ledger without disturbing client receipts."""
    next_projection = dict(projection or {})
    task_id = str(task.get('id') or task.get('taskId') or '')
    previous = next_projection.get('timingTrace')
    if isinstance(previous, Mapping) \
            and str(previous.get('taskId') or '') != task_id:
        # A regenerate/continue attempt must never inherit the previous
        # attempt's timing evidence merely because it reuses the Turn row.
        next_projection.pop('timingTrace', None)
        previous = None
    history, dropped = task_status_history(task)
    if not history and not dropped:
        return next_projection
    trace = dict(previous) if isinstance(previous, Mapping) else {}
    trace.update({
        'version': TRACE_CONTRACT_VERSION,
        'taskId': task_id,
        'running': True,
        'status': 'running',
        'statusHistory': history,
    })
    if dropped:
        trace['statusDroppedCount'] = dropped
    next_projection['timingTrace'] = compact_trace_document(
        trace, source=trace.get('source') or 'live-projection')
    return next_projection


# ── lib/turn_activity_timeline.py @ 1fa505314 ────────────────────


def _trim_entries(
    entries: list[dict[str, Any]], dropped_count: int,
) -> tuple[list[dict[str, Any]], int]:
    """Apply hard row/byte budgets, retaining diagnostics preferentially."""

    def serialized_size() -> int:
        document = {
            'blockId': 'activity-timeline',
            'version': ACTIVITY_TIMELINE_VERSION,
            'entries': entries,
            **({'droppedCount': dropped_count} if dropped_count else {}),
        }
        return len(json.dumps(
            document, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))

    def removal_index() -> int:
        return _eviction_index(entries)

    while len(entries) > ACTIVITY_TIMELINE_MAX_ENTRIES:
        entries.pop(removal_index())
        dropped_count = min(_COUNT_MAX, dropped_count + 1)
    if not entries:
        return entries, dropped_count
    # Size the document once, then track each removal by its exact encoded
    # length (the row plus one comma separator; removing the last row ends
    # the loop, so its missing separator never matters). droppedCount is the
    # only other field that changes; its key appears once, its digits grow.
    size = serialized_size()
    while entries and size > ACTIVITY_TIMELINE_MAX_JSON_BYTES:
        removable = removal_index()
        row_bytes = len(json.dumps(
            entries[removable], ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))
        entries.pop(removable)
        previous_dropped = dropped_count
        dropped_count = min(_COUNT_MAX, dropped_count + 1)
        size -= row_bytes + 1
        if previous_dropped:
            size += len(str(dropped_count)) - len(str(previous_dropped))
        elif dropped_count:
            size += len(',"droppedCount":') + len(str(dropped_count))
    return entries, dropped_count


def normalize_activity_timeline(raw: Any) -> dict[str, Any] | None:
    """Return the public, bounded activity document or ``None`` when empty."""
    if not isinstance(raw, Mapping):
        return None
    entries = [
        normalized for item in (raw.get('entries') or [])
        if (normalized := _normalize_entry(item)) is not None
    ] if isinstance(raw.get('entries'), list) else []
    if not entries:
        return None
    dropped_count = _nonnegative_int(
        raw.get('droppedCount'), maximum=_COUNT_MAX,
    ) or 0
    entries, dropped_count = _trim_entries(entries, dropped_count)
    return {
        'blockId': 'activity-timeline',
        'version': ACTIVITY_TIMELINE_VERSION,
        'entries': entries,
        **({'droppedCount': dropped_count} if dropped_count else {}),
    }


def _fold_phase(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    phase = _text(raw_event.get('phase'), 80)
    status_code = _nonnegative_int(raw_event.get('statusCode'))
    has_http_error = bool(status_code and status_code >= 400)
    if phase not in {Phase.RETRYING, Phase.COMPACTING} and not has_http_error:
        # The phase channel is the stream's live status text (startup beats,
        # thinking, waiting-for-model): transient by contract and already
        # shown by the live-status surface. Only retry/compaction cycles and
        # HTTP error beats are durable timeline facts.
        return
    parent_span = _parent_request(raw_event, task)
    detail = _diagnostic_text(raw_event.get('detail') or phase)
    detail_key = _text(raw_event.get('detailKey'), 160)
    detail_args = _safe_args(raw_event.get('detailArgs'))
    model = _text(raw_event.get('model') or task.get('model'), 160)
    reason_key = _text(detail_args.get('reasonKey'), 160)
    occurrence_duration_ms = None
    if raw_event.get('backoff_s') is not None:
        try:
            occurrence_duration_ms = max(
                0, round(float(raw_event.get('backoff_s') or 0) * 1000))
        except (TypeError, ValueError, OverflowError):
            occurrence_duration_ms = 0
    span_id = _stable_span(
        'status', task.get('_attemptId'), parent_span, phase, detail_key,
        reason_key, status_code, _llm_round_anchor(raw_event, task),
        raw_event.get('anchorBlockId'), raw_event.get('anchorToolRoundNum'),
    )
    warning = phase == Phase.RETRYING or has_http_error
    anchors = (capture_compaction_boundary(raw_event, task)
               if phase == Phase.COMPACTING else _activity_anchors(raw_event, task))
    if phase == Phase.COMPACTING:
        # A retry/resume can compact twice at the same model round.
        span_id = _stable_span('compaction-start', anchors.get('taskId'), anchors.get('attemptId'), seq)

        # Reactive recovery archives before L2 starts. Bind its still-pending,
        # unwitnessed placeholder rather than leave a permanent phase ghost.
        # Witnessed or settled archives cannot consume a later start.
        pending = [entry for entry in entries
                   if _same_producer(entry, anchors)
                   and entry.get('reasonCode') == 'context_compaction'
                   and entry.get('status') == 'running'
                   and entry.get('anchorPosition') == 'producer-start'
                   and not entry.get('anchorBlockId')
                   and entry.get('durationMs') == 0
                   and entry.get('startedAt') == entry.get('occurredAt')]
        if len(pending) == 1:
            pending[0].update({**anchors, 'startedAt': now, 'endedAt': now,
                               'durationMs': 0})
            return
    _append_or_update(entries, {
        'id': _entry_id(task, seq, raw_event=raw_event),
        **anchors,
        'spanId': span_id,
        'parentSpanId': parent_span,
        'seq': seq,
        'occurredAt': now,
        'startedAt': now,
        'endedAt': now,
        **({'durationMs': occurrence_duration_ms,
            'timingMode': 'occurrences'}
           if occurrence_duration_ms is not None else {}),
        'kind': 'status',
        'status': 'waiting' if phase == Phase.RETRYING else 'running',
        'severity': 'warning' if warning else 'info',
        'summary': detail,
        'summaryKey': detail_key,
        'summaryArgs': detail_args,
        'phase': phase,
        'model': model,
        'reasonCode': reason_key or phase,
        'statusCode': status_code,
        'roundNum': _round_number(raw_event),
        'llmRound': _llm_round_anchor(raw_event, task),
    }, coalesce=True)


def fold_activity_timeline(
    previous: Any,
    raw_event: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Fold one registered runtime fact into the durable timeline projection."""
    existing = normalize_activity_timeline(previous)
    entries = [dict(item) for item in (existing or {}).get('entries', [])]
    dropped_count = int((existing or {}).get('droppedCount') or 0)
    event_type = str(raw_event.get('type') or '')
    seq = _nonnegative_int(raw_event.get('seq'))
    if seq is None:
        seq = min(
            _INTEGER_MAX,
            (entries[-1].get('seq', -1) + 1) if entries else 0,
        )
    fallback_time = _now_ms() if now_ms is None else int(now_ms)
    occurred_at = _event_time(raw_event, fallback_time)

    if event_type == EventType.MODEL_REQUEST_START:
        # Request bookkeeping lives in the event registry and turn trace; a
        # timeline row is earned only by a failed/aborted completion.  The
        # round is still tracked so later diagnostics inherit their anchor.
        _track_llm_round(raw_event, task)
        return existing
    if event_type == EventType.MODEL_REQUEST_COMPLETE:
        _fold_model_complete(entries, raw_event, task, seq, occurred_at)
    elif event_type in {
        EventType.TOOL_START, EventType.TOOL_PROGRESS,
        EventType.TOOL_RESULT, EventType.TOOL_COMPLETE,
    }:
        _fold_tool_event(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.TOOL_SCHEMA_REJECTED:
        _fold_schema_rejection(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.PHASE:
        _fold_phase(entries, raw_event, task, seq, occurred_at)
    elif event_type in {EventType.COMPACTION, EventType.COMPACTION_DONE}:
        _fold_compaction_event(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.MODEL_FALLBACK:
        _fold_model_fallback(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.ERROR:
        # The chat Turn authority treats the legacy ERROR frame as terminal
        # (newer fatal paths normally use DONE + error). Keep the projection
        # aligned with that boundary so a settled Turn has no open spans.
        _fold_error(entries, raw_event, task, seq, occurred_at)
        _close_open_entries(
            entries, identity=_producer_identity(raw_event, task), status='failed', severity='error', ended_at=occurred_at,
        )
    elif event_type == EventType.DONE:
        finish_reason = str(raw_event.get('finishReason') or '').lower()
        terminal_error = bool(
            raw_event.get('error') or task.get('error')
            or finish_reason == 'error'
        )
        if terminal_error:
            _fold_error(entries, raw_event, task, seq, occurred_at)
        terminal_aborted = finish_reason in {'abort', 'aborted', 'interrupted'}
        _close_open_entries(
            entries, identity=_producer_identity(raw_event, task),
            status=('failed' if terminal_error else
                    'aborted' if terminal_aborted else 'succeeded'),
            severity=('error' if terminal_error else
                      'warning' if terminal_aborted else None),
            ended_at=occurred_at,
        )
    elif event_type == 'aborted':
        _close_open_entries(
            entries, identity=_producer_identity(raw_event, task), status='aborted', severity='warning', ended_at=occurred_at,
        )
    # 'budget_warning' is historical: no longer emitted, still rendered for
    # pre-removal transcripts.
    elif event_type in {EventType.RETRY_RESET, 'budget_warning'}:
        detail = _diagnostic_text(
            raw_event.get('detail') or raw_event.get('content') or event_type,
        )
        _append_or_update(entries, {
            'id': _entry_id(task, seq, raw_event=raw_event),
            **_activity_anchors(raw_event, task),
            'spanId': _stable_span(
                'system', task.get('_attemptId'), event_type,
                raw_event.get('kind'), raw_event.get('limit'),
            ),
            'seq': seq,
            'occurredAt': occurred_at,
            'startedAt': occurred_at,
            'endedAt': occurred_at,
            'kind': 'system',
            'status': 'waiting' if event_type == EventType.RETRY_RESET
            else 'running',
            'severity': 'warning',
            'summary': detail,
            'reasonCode': _text(
                raw_event.get('kind') or raw_event.get('limit') or event_type,
                160,
            ),
        }, coalesce=True)
    else:
        return existing

    entries, dropped_count = _trim_entries(entries, dropped_count)
    return normalize_activity_timeline({
        'entries': entries,
        'droppedCount': dropped_count,
    })


# ── lib/turn_projection_patch.py @ 1fa505314 ────────────────────


def _normalize_tool_rounds(value: Any) -> Any:
    """Remove the internal pending-results sentinel from public rounds.

    Executors use ``results=None`` to distinguish a running tool from a
    completed tool that returned no rows.  ``TurnToolRound.results`` is an
    optional array on the public wire, so publishing that internal sentinel
    makes an otherwise valid live snapshot fail its generated contract.  An
    omitted key preserves the pending meaning; a real list, including an
    empty completed result, remains byte-for-byte intact.
    """
    if not isinstance(value, list):
        return value
    from lib.background_command_projection import normalize_handoff_round
    normalized: list[Any] = []
    for item in value:
        if not isinstance(item, Mapping):
            normalized.append(item)
            continue
        round_record = dict(item)
        if round_record.get('results') is None:
            round_record.pop('results', None)
        normalized.append(normalize_handoff_round(round_record))
    return normalized


def normalize_projection_document(raw: Any) -> dict[str, Any]:
    """Return the canonical JSON document stored in a turn projection.

    Callers often start from the legacy-compatible derived message shape,
    which intentionally overlays identity/runtime fields on top of the stored
    projection.  Persisting that overlay would create two disagreeing copies
    of turn identity.  This boundary strips it and normalizes text-only input.
    """
    if isinstance(raw, str):
        return {'content': raw}
    if not isinstance(raw, Mapping):
        return {'content': ''}
    source = dict(raw)
    result = dict(source)
    from lib.background_command_projection import bound_projection_completion_previews
    bound_projection_completion_previews(result)
    for identity_key in _PROJECTION_IDENTITY_KEYS:
        result.pop(identity_key, None)
    if result.get('providerId') is None and result.get('provider_id') is not None:
        result['providerId'] = result.get('provider_id')
    result.pop('provider_id', None)
    if 'content' not in result and 'text' in result:
        result['content'] = result.get('text') or ''
    result.setdefault('content', '')
    # Task carriers use None before any billed response. The public contract
    # represents unknown usage by absence, never null or invented zero tokens.
    # Apply at the shared authority boundary for root, child and old rows alike.
    if result.get('usage') is None:
        result.pop('usage', None)
    if not isinstance(result.get('routeSnapshot'), Mapping):
        model_id = str(result.get('model') or '').strip()
        provider_id = str(result.get('providerId') or '').strip()
        if model_id or provider_id:
            from lib.model_routing import legacy_route_snapshot
            result['routeSnapshot'] = legacy_route_snapshot(
                model_id=model_id,
                provider_id=provider_id,
                route_id='',
            )
    if 'toolRounds' in result:
        result['toolRounds'] = _normalize_tool_rounds(result['toolRounds'])

    if isinstance(result.get('segments'), list):
        # Segment-owned rounds override toolRounds; normalize both read shapes.
        result['segments'] = [
            {**segment, '_round': _normalize_tool_rounds([segment['_round']])[0]}
            if isinstance(segment, Mapping) and isinstance(segment.get('_round'), Mapping)
            else segment for segment in result['segments']
        ]
    origin = _normalize_origin(source)
    translation = _normalize_translation(source)
    context_snapshot = _normalize_context(source)
    compaction = _normalize_compaction(source)
    image_generation = _normalize_image_generation(source)
    activity_timeline = normalize_activity_timeline(
        source.get('activityTimeline'))
    # A tag in arbitrary assistant prose is not execution authority. The Plan
    # task terminal boundary explicitly mints ``proposedPlan``; normalization
    # only verifies such a sidecar against the visible tagged transcript.
    raw_proposed_plan = source.get('proposedPlan')
    proposed_plan = (
        proposed_plan_document(raw_proposed_plan, content=source.get('content'))
        if isinstance(raw_proposed_plan, Mapping) else None
    )
    plan_execution = plan_execution_document(source.get('planExecution'))
    if origin is not None:
        result['origin'] = origin
    if translation is not None:
        result['translation'] = translation
    if context_snapshot is not None:
        result['contextSnapshot'] = context_snapshot
    if compaction is not None:
        result['compaction'] = compaction
    if image_generation is not None:
        result['imageGeneration'] = image_generation
    if activity_timeline is not None:
        result['activityTimeline'] = activity_timeline
    else:
        result.pop('activityTimeline', None)
    if proposed_plan is not None:
        result['proposedPlan'] = proposed_plan
    else:
        result.pop('proposedPlan', None)
    if plan_execution is not None:
        result['planExecution'] = plan_execution
    else:
        result.pop('planExecution', None)
    rolled_back = _normalize_rolled_back(source.get('rolledBack'))
    if rolled_back is not None:
        result['rolledBack'] = rolled_back
    else:
        result.pop('rolledBack', None)
    return {
        key: value for key, value in result.items()
        if key in PUBLIC_PROJECTION_FIELDS
    }


def build_projection_patch(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    base_revision: int,
    target_revision: int,
) -> dict[str, Any]:
    """Return a deterministic patch from ``before`` to ``after``.

    Paths are arrays rather than JSON Pointer strings, avoiding a second
    escaping grammar.  Growing strings and lists use append operations; this
    is the load-bearing property for cumulative content and ``toolRounds``.
    Every patch names both revisions so a client can fail closed and request
    an authoritative snapshot when an event gap is detected.
    """

    previous = dict(before or {})
    current = dict(after or {})
    operations: list[dict[str, Any]] = []
    _diff_value(previous, current, [], operations)
    return {
        "version": PROJECTION_PATCH_VERSION,
        "baseRevision": int(base_revision),
        "targetRevision": int(target_revision),
        "operations": operations,
    }


def _diff_value(
    before: Any,
    after: Any,
    path: list[str | int],
    operations: list[dict[str, Any]],
) -> None:
    if before == after:
        return

    if isinstance(before, str) and isinstance(after, str):
        if after.startswith(before):
            suffix = after[len(before):]
            if suffix:
                operations.append({
                    "op": "append_text", "path": list(path), "value": suffix,
                })
            return
        operations.append({"op": "set", "path": list(path), "value": after})
        return

    if isinstance(before, Mapping) and isinstance(after, Mapping):
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys - after_keys, key=str):
            operations.append({"op": "remove", "path": [*path, str(key)]})
        for key in sorted(after_keys - before_keys, key=str):
            operations.append({
                "op": "set", "path": [*path, str(key)], "value": after[key],
            })
        for key in sorted(before_keys & after_keys, key=str):
            _diff_value(before[key], after[key], [*path, str(key)], operations)
        return

    if (_is_json_sequence(before) and _is_json_sequence(after)):
        before_list = list(before)
        after_list = list(after)
        start, evicted = _evicted_run(before_list, after_list)
        for _ in range(evicted):
            operations.append({"op": "remove", "path": [*path, start]})
        del before_list[start:start + evicted]
        shared = min(len(before_list), len(after_list))
        for index in range(shared):
            _diff_value(
                before_list[index], after_list[index], [*path, index], operations,
            )
        if len(after_list) < len(before_list):
            operations.append({
                "op": "truncate", "path": list(path), "length": len(after_list),
            })
        elif len(after_list) > len(before_list):
            operations.append({
                "op": "append", "path": list(path),
                "value": after_list[len(before_list):],
            })
        return

    operations.append({"op": "set", "path": list(path), "value": after})


# ── lib/turn_projection_segments.py @ 1fa505314 ────────────────────


def _preserved_notes(value: Any) -> list[dict[str, Any]]:
    """Salvage display-only notes even when a sibling block needs rebuilding."""
    from lib.tasks_pkg.segments._types import NOTE_KINDS, NOTE_ACTIONS
    notes = []
    seen = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, Mapping) or item.get('type') != 'system_note':
            continue
        if not isinstance(item.get('text'), str):
            continue
        note = dict(item)
        if note.get('noteKind') not in NOTE_KINDS:
            note['noteKind'] = 'system-intervention'
        if note.get('action', 'prompt') not in NOTE_ACTIONS:
            note['action'] = 'prompt'
        identity = note.get('blockId')
        identity = identity if isinstance(identity, str) and identity else None
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        notes.append(note)
    return notes


def _valid_segments(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    segments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") not in _SEGMENT_TYPES:
            return None
        segment = dict(item)
        segment_type = segment["type"]
        if segment_type in {"text", "thinking", "system_note"}:
            if not isinstance(segment.get("text"), str):
                return None
        else:
            result = segment.get("result")
            if not isinstance(result, Mapping):
                return None
            # Legacy mirrors committed before the generated contract typed
            # ``TurnToolResult.status`` as a string stamped ``status: None``
            # on early-announced tools. The sibling round owns the verdict
            # (the browser reconstructs it via resultFromRound), so the
            # read boundary drops the invalid field instead of failing the
            # whole conversation snapshot closed (2026-09-20, conv
            # mu5p3vn9x32wcd: one null-status mirror 500'd every snapshot).
            if "status" in result and not isinstance(result.get("status"), str):
                segment["result"] = {
                    key: item_value for key, item_value in result.items()
                    if key != "status"
                }
        segments.append(segment)
    return segments


def _stable_block_ids(
    segments: list[dict[str, Any]],
    *,
    reserved_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    claimed: dict[str, int] = {
        block_id: 1 for block_id in (reserved_ids or set())
    }
    normalized: list[dict[str, Any]] = []
    for position, source in enumerate(segments):
        segment = dict(source)
        declared = segment.get("blockId")
        preferred = (
            declared.strip()
            if isinstance(declared, str) and declared.strip()
            else _fallback_block_id(segment, position)
        )
        occurrence = claimed.get(preferred, 0) + 1
        claimed[preferred] = occurrence
        segment["blockId"] = (
            preferred if occurrence == 1 else f"{preferred}~{occurrence}"
        )
        segment.pop("_round", None)
        normalized.append(segment)
    return normalized


def _segments_missing_tool_round(
    segments: list[dict[str, Any]], tool_rounds: Any,
) -> bool:
    """True when a live tool round has no ``tool_use`` segment yet.

    ``task['segments']`` is a CHECKPOINT-era assembly: rounds appended after
    the last checkpoint (≥5s cadence) are absent from it even though they are
    already in ``toolRounds``.  Ordinary rounds heal at the next checkpoint,
    but human-wait rounds (ask_human / write-approval / stdin) BLOCK the
    executor — no later checkpoint ever runs — so their interactive card
    (question + answer UI, rendered only from a segment's tool block) would
    never appear.  The authority boundary repairs the gap. Running turns
    append only the missing tool blocks after their current live prefix;
    settled turns can safely use finished-turn re-assembly.

    One-directional by design: segments may legitimately be a SUPERSET of
    ``toolRounds`` (manual compaction folds cold rounds out of the round list
    while their render blocks stay), so only a round MISSING from the segment
    ids triggers repair — never a count mismatch.  Synthetic inbox rows
    and rounds without a toolCallId never become segments, so they must not
    count as gaps (that would repair on every read, forever).
    """
    if not isinstance(tool_rounds, list) or not tool_rounds:
        return False
    covered: dict[str, int] = {}
    for segment in segments:
        if segment.get("type") != "tool_use":
            continue
        call_id = str(segment.get("id") or "").strip()
        if call_id:
            covered[call_id] = covered.get(call_id, 0) + 1
    seen_rounds: dict[str, int] = {}
    for round_record in tool_rounds:
        if not isinstance(round_record, Mapping):
            continue
        if is_synthetic_inbox_round(round_record):
            continue
        call_id = str(round_record.get("toolCallId") or "").strip()
        if not call_id:
            continue
        occurrence = seen_rounds.get(call_id, 0) + 1
        seen_rounds[call_id] = occurrence
        if occurrence > covered.get(call_id, 0):
            return True
    return False


def _synchronize_terminal_blocks(
    segments: list[dict[str, Any]],
    projection: Mapping[str, Any],
    *,
    actor: str,
    status: str,
    ensure_assistant_text_placeholder: bool = True,
) -> None:
    content = str(projection.get("content") or "")
    thinking = str(projection.get("thinking") or "")
    terminal_text_index = next((
        index for index in range(len(segments) - 1, -1, -1)
        if segments[index].get("type") == "text"
        and (segments[index].get("terminal")
             or segments[index].get("deliverable"))
    ), None)
    if terminal_text_index is None and (
        content or (ensure_assistant_text_placeholder and actor != "human")
    ) and not _live_content_already_narrated(segments, content, status):
        segments.append({
            "type": "text",
            "blockId": "text:terminal",
            "text": content,
            "deliverable": True,
            "terminal": True,
        })
        terminal_text_index = len(segments) - 1
    if terminal_text_index is not None:
        terminal_text = segments[terminal_text_index]
        terminal_text["text"] = content
        terminal_text["deliverable"] = True
        terminal_text["terminal"] = True
        if status in _RESUMABLE_TURN_STATUSES:
            terminal_text["resumable"] = True
        else:
            terminal_text.pop("resumable", None)

    terminal_thinking_index = next((
        index for index in range(len(segments) - 1, -1, -1)
        if segments[index].get("type") == "thinking"
        and segments[index].get("terminal")
    ), None)
    if terminal_thinking_index is not None:
        segments[terminal_thinking_index]["text"] = thinking
    elif thinking:
        thinking_segment = {
            "type": "thinking",
            "blockId": "thinking:terminal",
            "text": thinking,
            "deliverable": False,
            "terminal": True,
        }
        if terminal_text_index is None:
            segments.append(thinking_segment)
        else:
            segments.insert(terminal_text_index, thinking_segment)


def _synchronize_compacted_tool_segment_results(
    segments: list[dict[str, Any]], tool_rounds: Any,
) -> list[dict[str, Any]]:
    """Prevent a durable compacted round's segment mirror restoring old bytes."""
    if not isinstance(tool_rounds, list) or not any(
        isinstance(round_record, Mapping)
        and _round_has_explicit_payload_compaction(round_record)
        for round_record in tool_rounds
    ):
        return segments
    rounds_by_id = _unique_rounds_by_call_id(tool_rounds)
    segment_id_counts = _tool_segment_id_counts(segments)
    if not rounds_by_id or not segment_id_counts:
        return segments
    changed = False
    synchronized: list[dict[str, Any]] = []
    for source in segments:
        call_id = (
            str(source.get("id") or "").strip()
            if source.get("type") == "tool_use"
            else ""
        )
        round_record = rounds_by_id.get(call_id)
        if (
            not call_id
            or segment_id_counts.get(call_id) != 1
            or round_record is None
            or not _round_has_explicit_payload_compaction(round_record)
            or not _tool_segment_matches_round(source, round_record)
        ):
            synchronized.append(source)
            continue
        round_result = _segment_result_from_round(round_record)
        if "content" not in round_result:
            synchronized.append(source)
            continue
        result = dict(source["result"])
        result["content"] = round_result["content"]
        if "status" in round_result:
            result["status"] = round_result["status"]
        if result == source["result"]:
            synchronized.append(source)
            continue
        segment = dict(source)
        segment["result"] = result
        synchronized.append(segment)
        changed = True
    return synchronized if changed else segments


def projection_with_stable_segments(
    raw_projection: Any,
    *,
    actor: str = "assistant",
    status: str = "completed",
) -> dict[str, Any]:
    """Return one projection whose render blocks all have durable identity."""
    projection = normalize_projection_document(raw_projection)
    reserved_ids = _normalize_sidecar_blocks(projection)
    normalized_actor = str(actor or "assistant")
    normalized_status = str(status or "")
    segments = _valid_segments(projection.get("segments"))
    preserved_notes = _preserved_notes(projection.get('segments'))
    if segments is not None:
        notes_by_id = {
            note['blockId']: note for note in preserved_notes
            if isinstance(note.get('blockId'), str) and note['blockId']
        }
        seen_notes = set()
        normalized_segments = []
        for item in segments:
            if item.get('type') == 'system_note':
                identity = item.get('blockId')
                identity = identity if isinstance(identity, str) and identity else None
                if identity and identity in seen_notes:
                    continue
                seen_notes.add(identity)
                item = notes_by_id.get(identity, item)
            normalized_segments.append(item)
        segments = normalized_segments
    if segments is not None and _segments_missing_tool_round(
        segments, projection.get("toolRounds"),
    ):
        if normalized_status == "running":
            # A complete tool call can be announced before parsing attaches
            # the current thinking/text to its round. Materialize that live
            # prefix first, then append only the missing tools; finished-turn
            # assembly would incorrectly put those tools before the prefix.
            _synchronize_terminal_blocks(
                segments,
                projection,
                actor=normalized_actor,
                status=normalized_status,
                ensure_assistant_text_placeholder=False,
            )
            segments = _append_missing_live_tool_segments(
                segments, projection.get("toolRounds"),
            )
        else:
            segments = None
    if segments is None:
        task = {
            "content": str(projection.get("content") or ""),
            "thinking": str(projection.get("thinking") or ""),
        }
        assembled = assemble_segments(
            task,
            merged=(list(projection.get("toolRounds") or [])
                    if isinstance(projection.get("toolRounds"), list) else []),
        )
        segments = segments_to_json(assembled)
        for note in preserved_notes:
            lr = note.get('llmRound')
            index = next((i for i, segment in enumerate(segments)
                          if segment.get('terminal') or (
                              isinstance(lr, int)
                              and isinstance(segment.get('llmRound'), int)
                              and segment['llmRound'] > lr
                              and (not note.get('attemptId') or
                                   note.get('attemptId') == segment.get('attemptId')))), len(segments))
            segments.insert(index, note)
    _synchronize_terminal_blocks(
        segments,
        projection,
        actor=normalized_actor,
        status=normalized_status,
    )
    segments = _synchronize_compacted_tool_segment_results(
        segments, projection.get("toolRounds"),
    )
    segments = _drop_narrated_live_terminal_mirror(
        segments, projection, normalized_status,
    )
    projection["segments"] = _stable_block_ids(
        segments, reserved_ids=reserved_ids,
    )
    return projection
