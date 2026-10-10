"""Flow child accounting: settled invocation -> phase -> distinct run summary.

No pricing, provider calls, or transcript reconstruction lives here. Each API
row is bounded by vocabulary (not by dropping paid calls). Invocation totals
already include retries/compaction; their breakdowns are NEVER added again.
Durable run/header integration must use a separate summary, not phase zero.
"""
from __future__ import annotations

from copy import deepcopy
import math

from lib.cost import accumulate_usage_into, normalize_usage, split_input_tokens


_ROUND_FIELDS = (
    'round', 'model', 'provider_id', 'providerId', 'resolvedModel', 'tag',
    'kind', 'responseAuthoring', 'requestId', 'requestTag',
)
_USAGE_FIELDS = frozenset({
    'prompt_tokens', 'input_tokens', 'completion_tokens', 'output_tokens',
    'total_tokens', 'cache_write_tokens', 'cache_creation_input_tokens',
    'cache_read_tokens', 'cache_read_input_tokens', 'cached_tokens',
    'effectiveCachedTokens', 'prompt_cache_hit_tokens',
    'cached_content_token_count', 'reasoning_tokens', 'thinking_tokens',
})
_DETAIL_FIELDS = {
    'prompt_tokens_details': ('cached_tokens', 'audio_tokens'),
    'completion_tokens_details': ('reasoning_tokens', 'audio_tokens',
                                  'accepted_prediction_tokens', 'rejected_prediction_tokens'),
    'input_tokens_details': ('cached_tokens', 'audio_tokens'),
    'output_tokens_details': ('reasoning_tokens', 'audio_tokens'),
    'cache_creation': ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'),
}
_COST_AMOUNTS = (
    'costUsd', 'costCny', 'inputTokens', 'outputTokens', 'totalInputTokens',
    'cacheWriteTokens', 'cacheReadTokens', 'thinkingTokens', 'inputCostUsd',
    'outputCostUsd', 'cacheWriteCostUsd', 'cacheReadCostUsd', 'inputCostCny',
    'outputCostCny', 'cacheWriteCostCny', 'cacheReadCostCny', 'cacheSavingsUsd',
    'cacheSavingsCny',
)


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and value >= 0 and (not isinstance(value, float) or math.isfinite(value)))


def _counters(value):
    if not isinstance(value, dict):
        return {}
    selected = {key: value[key] for key in _USAGE_FIELDS if key in value}
    for key, fields in _DETAIL_FIELDS.items():
        details = value.get(key)
        if isinstance(details, dict):
            selected[key] = {name: details[name] for name in fields if name in details}
    result = {}
    accumulate_usage_into(result, selected)
    return result


def _cost(value):
    """Keep stamped amounts/provenance; never resolve today's prices."""
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in (*_COST_AMOUNTS, 'peakMultiplier')
              if _number(value.get(key))}
    if not any(key in result for key in ('costUsd', 'costCny')):
        return None
    if isinstance(value.get('pricingSource'), str):
        result['pricingSource'] = value['pricingSource'][:160]
    snapshot = value.get('pricingSnapshot')
    if isinstance(snapshot, dict):
        clean = {}
        for key in ('model', 'provider', 'source', 'currency', 'tierId', 'kind',
                    'selectedPromptTokens', 'maxPromptTokens', 'rounds'):
            item = snapshot.get(key)
            if isinstance(item, str) or item is None or _number(item):
                if key in snapshot:
                    clean[key] = item
        rates = snapshot.get('rates')
        if isinstance(rates, dict):
            clean['rates'] = {key: rates[key] for key in (
                'input', 'output', 'cacheWriteMul', 'cacheReadMul')
                if _number(rates.get(key))}
        result['pricingSnapshot'] = clean
    return result


def _sum_costs(costs, *, source):
    if not costs or not any(all(key in row for row in costs)
                            for key in ('costUsd', 'costCny')):
        return None
    # A currency/component absent from one snapshot is unknown, not zero.
    return {
        **{key: sum(row[key] for row in costs) for key in _COST_AMOUNTS
           if all(key in row for row in costs)},
        'pricingSource': source,
    }


def _coverage(total, reported):
    return ('empty' if not total else 'missing' if not reported
            else 'complete' if total == reported else 'partial')


def _token_totals(usages):
    totals = dict.fromkeys(('uncachedInputTokens', 'totalInputTokens', 'outputTokens',
                           'cacheWriteTokens', 'cacheReadTokens', 'thinkingTokens'), 0)
    for usage in usages:
        normalized = normalize_usage(usage)
        uncached, total = split_input_tokens(usage)
        for key, value in (
            ('uncachedInputTokens', uncached), ('totalInputTokens', total),
            ('outputTokens', normalized['output']),
            ('cacheWriteTokens', normalized['cache_write']),
            ('cacheReadTokens', normalized['cache_read']),
            ('thinkingTokens', normalized['thinking']),
        ):
            totals[key] += value
    return totals


def project_child_accounting(source: dict, *, invocation_id: str = '') -> dict:
    """Copy settled facts, preserving absent usage vs explicit numeric zero.

    A ledgerIndex is scoped to the canonical child ID, NOT the model's round
    number: discarded attempts can share a round. Reprojection is idempotent.
    Compaction calls may be capped upstream; expose that gap, never price their
    merged tokens under a guessed model. Missing rows are not zero-cost calls.
    """
    receipt = source.get('usageAccounting')
    receipt = receipt if isinstance(receipt, dict) else {}
    invocation_id = invocation_id or str(receipt.get('invocationId') or '')
    result = {}
    usage = _counters(source.get('usage'))
    if usage:
        result['usage'] = usage
    rounds = source.get('apiRounds')
    recorded = []
    if isinstance(rounds, list):
        for index, raw in enumerate(rounds):
            row = raw if isinstance(raw, dict) else {}
            clean = {key: row[key] for key in _ROUND_FIELDS
                     if isinstance(row.get(key), (str, bool, int))}
            if invocation_id:
                clean.update(invocationId=invocation_id, ledgerIndex=index)
            counters = _counters(row.get('usage'))
            if counters:
                clean['usage'] = counters
            clean['usageStatus'] = 'reported' if counters else 'missing'
            raw_usage = row.get('usage')
            dispatch = raw_usage.get('_dispatch') if isinstance(raw_usage, dict) else None
            if isinstance(dispatch, dict):
                for key in ('provider_id', 'requestId', 'requestTag'):
                    if key not in clean and isinstance(dispatch.get(key), str):
                        clean[key] = dispatch[key]
                if not clean.get('resolvedModel') and isinstance(dispatch.get('model'), str):
                    clean['resolvedModel'] = dispatch['model']
            cost = _cost(row.get('cost'))
            if cost is not None:
                clean['cost'] = cost
            recorded.append(clean)
        result['apiRounds'] = recorded
    compaction = source.get('compactionUsage')
    if isinstance(compaction, dict):
        clean = _counters(compaction)
        if _number(compaction.get('n_calls')):
            clean['n_calls'] = compaction['n_calls']
        if clean:
            result['compactionUsage'] = clean
    compaction_calls = result.get('compactionUsage', {}).get('n_calls', 0)
    compaction_rows = sum(row.get('kind') == 'compaction' for row in recorded)
    truncated = compaction_calls > compaction_rows
    reported = [row['usage'] for row in recorded if row.get('usage')]
    priced = [row['cost'] for row in recorded if row.get('cost') is not None]
    # A phase snapshot takes precedence over its call breakdown. Only snapshots
    # are summed; projection must never reprice historical or failed children.
    cost = _cost(source.get('cost'))
    if cost is None:
        cost = _sum_costs(priced, source='flow_child_round_snapshot_aggregate')
    represented_usage = {}
    for counters in reported:
        accumulate_usage_into(represented_usage, counters)
    # Auxiliary accounting may appear in the invocation total without an API
    # row; do not call the row subtotal a complete bill in that case.
    usage_covered = bool(usage) and represented_usage == usage
    round_cost_status = _coverage(len(recorded), len(priced))
    cost_status = (receipt.get('costStatus') if receipt else None)
    if cost_status not in {'complete', 'partial', 'missing', 'empty'}:
        cost_status = ('complete' if _cost(source.get('cost')) is not None else
                       'partial' if priced and (truncated or not usage_covered or cost is None) else
                       'missing' if not priced else round_cost_status)
    if cost is not None and cost_status == 'complete':
        result['cost'] = cost
    accounting = {
        'invocationId': invocation_id,
        'usageStatus': 'reported' if usage else 'missing',
        'recordedApiRoundCount': len(recorded),
        'roundsWithUsage': len(reported),
        'roundsWithCost': len(priced),
        'apiRoundsStatus': 'reported' if isinstance(rounds, list) else 'missing',
        'compactionIncludedInUsage': True,
        'compactionRoundDetailTruncated': truncated,
        'costStatus': cost_status,
        'tokenTotalsStatus': ('complete' if recorded and len(reported) == len(recorded)
                              and not truncated and usage_covered
                              else 'partial' if reported else 'missing'),
    }
    if cost is not None:
        accounting['recordedCost'] = cost
    if reported:
        # Normalize PER REQUEST. Invocation usage may already mix vendor cache
        # conventions, so normalizing that merged dictionary is not safe.
        accounting['recordedTokenTotals'] = _token_totals(reported)
    result['usageAccounting'] = accounting
    return result


class FlowUsageLedger:
    """One immutable terminal receipt per child, O(invocations), no raw rows.

    Exact replay is a no-op. A different receipt for the same identity is an
    integrity conflict, not an extra charge or an implicit billing correction.
    """

    def __init__(self):
        self._receipts: dict[str, dict] = {}

    def record(self, projection: dict) -> None:
        accounting = projection.get('usageAccounting') or {}
        identity = accounting.get('invocationId')
        if not identity:
            return
        receipt = deepcopy({
            'usageAccounting': accounting,
            **{key: projection[key] for key in ('usage', 'cost') if key in projection},
        })
        previous = self._receipts.get(identity)
        if previous is not None and previous != receipt:
            raise ValueError('Conflicting Flow child accounting receipt')
        self._receipts[identity] = receipt

    def summary(self) -> dict:
        receipts = list(self._receipts.values())
        accounts = [row['usageAccounting'] for row in receipts]
        reported = sum(row['usageStatus'] == 'reported' for row in accounts)
        priced = [row['recordedCost'] for row in accounts
                  if row.get('recordedCost') is not None]
        cost_status = _coverage(len(receipts), len(priced))
        if priced and any(row['costStatus'] != 'complete' for row in accounts):
            cost_status = 'partial'
        result = {
            'scope': 'flow_children',
            'invocations': len(receipts),
            'invocationsWithUsage': reported,
            'invocationsMissingUsage': len(receipts) - reported,
            'usageStatus': _coverage(len(receipts), reported),
            'recordedApiRoundCount': sum(row['recordedApiRoundCount'] for row in accounts),
            'roundsWithUsage': sum(row['roundsWithUsage'] for row in accounts),
            'roundsWithCost': sum(row['roundsWithCost'] for row in accounts),
            'invocationsWithCost': len(priced),
            'costStatus': cost_status,
            'compactionRoundDetailTruncated': any(
                row['compactionRoundDetailTruncated'] for row in accounts),
            'tokenTotalsStatus': ('empty' if not accounts else 'complete' if all(
                row['tokenTotalsStatus'] == 'complete' for row in accounts)
                else 'partial' if any(row.get('recordedTokenTotals') for row in accounts)
                else 'missing'),
        }
        totals = {}
        for row in accounts:
            accumulate_usage_into(totals, row.get('recordedTokenTotals'))
        if totals:
            result['recordedTokenTotals'] = totals
        cost = _sum_costs(priced, source='flow_child_snapshot_aggregate')
        if cost is not None:
            result['cost'] = cost
        elif priced:
            result['costStatus'] = 'partial'
        return result
