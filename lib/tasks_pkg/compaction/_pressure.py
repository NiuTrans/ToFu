"""One bounded L1 candidate before L2, sharing its target and cache economics.

Normal L1 never edits a warm prefix. Under pressure this owner evaluates a
detached superseded-snapshot/tool/image candidate. Adopt only if it resolves the target and
repays a prefix rewrite, or safety requires the space. A rejected candidate
changes no messages/events/cache state. No LLM, durable reads, or new cache.
"""

from copy import deepcopy

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


def retain_oversized_tool_batch(messages, task, *, target_tokens,
                                schema_tokens, measure) -> bool:
    """Shrink unconsumed observations only after retaining their exact bytes.

    This mandatory path keeps the assistant call and every result in place.
    Failed retention, incomplete batches, cancellation and an overfull candidate
    leave the live prompt untouched. Durable tool history is never rewritten.
    """
    if not isinstance(task, dict) or task.get('aborted'):
        return False
    from lib.tasks_pkg.manager import task_user_id
    from lib.tasks_pkg.compaction._layer2._anchor import _apiform_tool_rounds
    from lib.tool_results.tool_history_pairing import adjacent_tool_call_result_pairs
    from lib.tasks_pkg.compaction._budget import enforce_round_aggregate_budget_v2
    from lib.tools.result_value import TrustedResultText
    import json

    spans = _apiform_tool_rounds(messages)
    if not spans:
        return False
    start, end = spans[-1]
    calls = messages[start].get('tool_calls') or []
    if len(adjacent_tool_call_result_pairs(messages[start:end])) != len(calls):
        return False
    try:
        owner = task_user_id(task)
    except (ValueError, TypeError):
        return False
    functions = {call['id']: call.get('function', {}).get('name', '') for call in calls}
    rows = list(task.get('_checkpointToolRounds') or ()) + list(task.get('toolRounds') or ())
    results, origins = {}, {}
    for index in range(start + 1, end):
        message = messages[index]
        content = message.get('content')
        if not isinstance(content, str):
            # Images and opaque provider state retain their ordinary boundary.
            return False
        call_id = message.get('tool_call_id')
        row = next((row for row in reversed(rows) if isinstance(row, dict)
                    and row.get('toolCallId') == call_id
                    and content in (row.get('toolContent'), row.get('toolModelContent'))), {})
        evidence = row.get('toolResultEvidence') or {}
        if (isinstance(evidence, dict)
                and evidence.get('resultContractVersion') == 'tofu.tool-result/v2'):
            # Only executor-owned metadata can authorize an existing envelope.
            try:
                value = json.loads(content)
                import re
                ref = evidence.get('artifactRef')
                if (isinstance(value, dict) and isinstance(ref, str)
                        and re.fullmatch(r'tool-result:[0-9a-f]{64}', ref)
                        and value.get('artifactRef') == ref):
                    content = TrustedResultText(content, envelope=True)
            except ValueError:
                pass
        key = str(index)
        origins[key] = row
        results[key] = (content, functions.get(call_id, ''), str(call_id or ''))
    budget = max(1, (int(target_tokens) - int(schema_tokens)) // 4)
    reduced = enforce_round_aggregate_budget_v2(
        results, user_id=owner, model=str((task.get('config') or {}).get('model') or ''),
        token_budget=budget, require_retention=True,
        recovery_by_call_id={key: {'policy': 'artifact'} for key in results})
    candidate = deepcopy(messages)
    for key, (content, _, _) in reduced.items():
        if (origins[key].get('status') in {'error', 'failed'}
                and content != messages[int(key)]['content']):
            value = json.loads(content)
            value.update(status='error', executionStatus='error')
            content = json.dumps(value, ensure_ascii=False)
        candidate[int(key)]['content'] = content
    if candidate == messages or task.get('aborted'):
        return False
    after = measure(candidate)
    if after > target_tokens or task.get('aborted'):
        return False
    messages[:] = candidate
    task['_lastRetainedToolBatch'] = {
        'targetTokens': int(target_tokens), 'candidateTokens': int(after),
        'results': end - start - 1,
    }
    return True


def try_pressure_l1(messages, task, *, target_tokens, total_tokens,
                    schema_tokens=0, mandatory=False, round_num=0,
                    remaining_api_rounds=None, measure=None) -> bool:
    if total_tokens <= target_tokens or (task or {}).get('aborted'):
        return False
    has_tools = any(message.get('role') == 'tool' for message in messages)
    if not has_tools and not any(message.get('_contextComposer') for message in messages):
        return False
    from lib.context_experiment_flags import normalize_context_experiment_flags
    if normalize_context_experiment_flags(
            (task or {}).get('config') or {})['compaction']['evidenceLedger']:
        # This opt-in transform writes recovery artifacts. A speculative
        # candidate cannot promise its final reference bytes without I/O;
        # retain the existing L2 path instead of producing speculative files.
        return False
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    from lib.tasks_pkg.compaction._tokens import _count_tokens_authoritative
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key

    # Reserve most of the target for non-tool context. The newest complete
    # tool batch remains protected by the existing L1 step regardless of size.
    from lib.tasks_pkg.compaction._constants import MICRO_HOT_TAIL_TOKENS
    hot_budget = min(MICRO_HOT_TAIL_TOKENS,
                     max(0, (target_tokens - schema_tokens) // 4))
    options = dict(steps=['compact_tool_results', 'strip_cold_images'],
                   ignore_cache_prefix=True,
                   constant_overrides={'MICRO_HOT_TAIL_TOKENS': hot_budget})
    from lib.tasks_pkg.context_composer import compact_context_snapshots
    snapshots = compact_context_snapshots(messages)
    snapshots_changed = snapshots != messages
    candidate = deepcopy(snapshots)
    saved = (micro_compact(candidate, task=dict(task) if task is not None else None,
                           _dry_run=True, **options) if has_tools else 0)
    if not saved and not snapshots_changed:
        return False
    after = (measure(candidate) if measure else
             _count_tokens_authoritative(candidate, task)[0])
    # The caller's exact tool surface may not yet be in the task counter.
    # Never under-budget a candidate on that account.
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    if measure is None:
        after = max(after, _estimate_total_tokens(candidate) + schema_tokens)
    accepted = after <= target_tokens and after < total_tokens
    payback = None
    if accepted and not mandatory:
        from lib.tasks_pkg.compaction._cache_economics import (
            _proactive_cache_economics, _proactive_payback_policy)
        economics = _proactive_cache_economics(
            task, tokens_before=total_tokens, candidate_tokens=after)
        horizon, _ = _proactive_payback_policy(
            task, current_round=round_num,
            remaining_api_rounds=remaining_api_rounds)
        payback = economics['payback_rounds']
        from lib.tasks_pkg.compaction._constants import _AUTO_COMPACT_MIN_REDUCTION_RATIO
        accepted = ((total_tokens - after) / max(1, total_tokens)
                    >= _AUTO_COMPACT_MIN_REDUCTION_RATIO
                    and (economics['cache_read_tokens'] <= 0 or payback <= horizon))
    evidence = dict(round=int(round_num or 0), mandatory=bool(mandatory),
                    beforeTokens=int(total_tokens), candidateTokens=int(after),
                    targetTokens=int(target_tokens), accepted=bool(accepted))
    if payback is not None and payback != float('inf'):
        evidence['paybackRounds'] = payback
    if task is not None:
        task['_lastPressureL1'] = evidence
    if not accepted or (task or {}).get('aborted'):
        return False
    # Repeat the deterministic transform only after adoption; rejected trials
    # never emit tool_compacted or mark an expected provider cache break.
    first_change = next((i for i, (old, new) in enumerate(zip(messages, candidate))
                         if old != new), min(len(messages), len(candidate)))
    if snapshots_changed:
        messages[:] = snapshots
    if has_tools:
        micro_compact(messages, task=task, **options)
    owner = compaction_owner_key(task)
    if owner and task is not None:
        try:
            from lib.tasks_pkg.manager import task_user_id
            from lib.tasks_pkg.cache_tracking._roi import notify_compaction
            notify_compaction(owner, user_id=task_user_id(task),
                              intended_rewrite_index=first_change)
        except Exception as exc:
            # Cache-break diagnostics cannot roll back an accepted projection.
            logger.debug('[PressureL1] cache notification failed: %s', exc)
    if task is not None:
        task.pop('_autoCompactRetryAfterTokens', None)
        task.pop('_autoCompactRetryWitness', None)
    try:
        audit_log('context_pressure_l1', conv=owner,
                  task=str((task or {}).get('id') or '')[:16], **evidence)
    except Exception as exc:
        logger.debug('[PressureL1] audit unavailable: %s', exc)
    return True
