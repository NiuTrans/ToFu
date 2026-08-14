"""Conversation-sticky A/B policy and real-cost rollups.

The experiment is deliberately inert unless ``server_config.json`` contains
``cost_experiment.enabled=true``.  Assignment is deterministic by conversation
ID, so every turn in a conversation stays in one arm and cache/history state is
never mixed across variants.

The two policies are fixed here rather than accepting arbitrary request keys:

``control``
    Compatibility baseline: inline MCP schemas and context-window-only L2
    compaction (``workingSetTokens=0``).

``optimized``
    Current economic defaults: automatic progressive MCP disclosure and a
    128K working set.

Only the enrollment/split/sample controls are low-code editable.  Keeping the
arm payloads declarative and bounded prevents an admin typo from changing an
unrelated model/tool setting on production requests.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any

from lib.config_dir import config_path
from lib.log import get_logger

logger = get_logger(__name__)


_DEFAULT_EXPERIMENT_ID = 'context-cost-v1'
_ARM_POLICIES = {
    'control': {
        'mcpToolExposure': 'inline',
        'workingSetTokens': 0,
    },
    'optimized': {
        'mcpToolExposure': 'auto',
        'workingSetTokens': 128_000,
    },
}
_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,80}$')


class CostExperimentTransitionError(ValueError):
    """Raised when an existing experiment ID would change assignment rules."""


def _bounded_number(raw: Any, *, field: str, default: int,
                    minimum: int, maximum: int, strict: bool) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        if strict:
            raise ValueError(f'{field} must be an integer')
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError(f'{field} must be an integer') from exc
        logger.warning('[CostExperiment] invalid %s=%r; using %d',
                       field, raw, default)
        return default
    if value < minimum or value > maximum:
        if strict:
            raise ValueError(
                f'{field} must be between {minimum} and {maximum}')
        logger.warning('[CostExperiment] out-of-range %s=%r; using %d',
                       field, raw, default)
        return default
    return value


def normalize_cost_experiment_config(raw: Any, *, strict: bool = False) -> dict:
    """Return the complete, validated low-code experiment configuration.

    ``strict=True`` is used on the settings write path and rejects invalid
    controls.  Reads fail safe to the inert defaults so a malformed hand-edited
    config file can never turn an experiment on accidentally.
    """
    if not isinstance(raw, dict):
        if strict:
            raise ValueError('cost_experiment must be a JSON object')
        raw = {}

    enabled_raw = raw.get('enabled', False)
    if strict and not isinstance(enabled_raw, bool):
        raise ValueError('enabled must be a boolean')
    enabled = enabled_raw if isinstance(enabled_raw, bool) else False

    experiment_id = str(
        raw.get('experiment_id') or _DEFAULT_EXPERIMENT_ID).strip()
    if not _ID_RE.fullmatch(experiment_id):
        if strict:
            raise ValueError(
                'experiment_id must contain 1-80 letters, numbers, dot, '
                'underscore, or hyphen characters')
        logger.warning('[CostExperiment] invalid experiment_id=%r; using %s',
                       experiment_id, _DEFAULT_EXPERIMENT_ID)
        experiment_id = _DEFAULT_EXPERIMENT_ID

    return {
        'enabled': enabled,
        'experiment_id': experiment_id,
        'traffic_percent': _bounded_number(
            raw.get('traffic_percent'), field='traffic_percent', default=10,
            minimum=0, maximum=100, strict=strict),
        'treatment_percent': _bounded_number(
            raw.get('treatment_percent'), field='treatment_percent', default=50,
            minimum=0, maximum=100, strict=strict),
        'min_sample_size': _bounded_number(
            raw.get('min_sample_size'), field='min_sample_size', default=20,
            minimum=1, maximum=10_000, strict=strict),
        'assignment_unit': 'conversation',
        'sticky': True,
        'arms': {name: dict(policy) for name, policy in _ARM_POLICIES.items()},
    }


def load_cost_experiment_config() -> dict:
    """Load the active experiment block from the shared server config."""
    from lib.json_store import read_json

    saved = read_json(config_path('server_config.json'), default={})
    raw = saved.get('cost_experiment') if isinstance(saved, dict) else {}
    return normalize_cost_experiment_config(raw)


def validate_cost_experiment_transition(previous_raw: Any,
                                        next_config: dict) -> None:
    """Keep one experiment ID bound to one immutable routing shape.

    Changing enrollment or arm thresholds can move an existing conversation
    across variants even though assignment is hash-based. Once an experiment
    block has been persisted, those changes therefore require a fresh ID.
    Enable/disable and reporting sample-size changes remain safe in place.
    """
    if not isinstance(previous_raw, dict) or not previous_raw:
        return
    previous = normalize_cost_experiment_config(previous_raw)
    current = normalize_cost_experiment_config(next_config, strict=True)
    if previous['experiment_id'] != current['experiment_id']:
        return
    changed = [
        field for field in ('traffic_percent', 'treatment_percent')
        if previous[field] != current[field]
    ]
    if changed:
        raise CostExperimentTransitionError(
            'change experiment_id before changing routing fields: '
            + ', '.join(changed))


def _bucket(experiment_id: str, lane: str, conv_id: str) -> int:
    digest = hashlib.sha256(
        f'{experiment_id}\x00{lane}\x00{conv_id}'.encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big') % 10_000


def assign_cost_experiment(config: dict, conv_id: str) -> dict:
    """Return a deterministic conversation-level assignment record."""
    cfg = normalize_cost_experiment_config(config)
    base = {
        'experiment_id': cfg['experiment_id'],
        'assignmentUnit': 'conversation',
        'status': 'off',
    }
    if not cfg['enabled']:
        return base
    conv_id = str(conv_id or '').strip()
    if not conv_id:
        return {**base, 'status': 'excluded', 'reason': 'missing_conversation_id'}

    enrollment_bucket = _bucket(cfg['experiment_id'], 'enrollment', conv_id)
    if enrollment_bucket >= cfg['traffic_percent'] * 100:
        return {
            **base,
            'status': 'not_enrolled',
            'enrollmentBucket': enrollment_bucket,
        }

    arm_bucket = _bucket(cfg['experiment_id'], 'arm', conv_id)
    arm = ('optimized'
           if arm_bucket < cfg['treatment_percent'] * 100
           else 'control')
    return {
        **base,
        'status': 'assigned',
        'arm': arm,
        'enrollmentBucket': enrollment_bucket,
        'armBucket': arm_bucket,
        'policy': dict(cfg['arms'][arm]),
    }


def _has_request_policy_override(cfg: dict) -> bool:
    if 'mcpToolExposure' in cfg:
        return True
    compaction = cfg.get('compaction')
    return (isinstance(compaction, dict)
            and 'workingSetTokens' in compaction)


def apply_cost_experiment(task: dict, request_config: dict, *,
                          experiment_config: dict | None = None) -> dict:
    """Apply the assigned arm to a shallow copy of the request config.

    Disabled experiments return the exact input object.  Explicit per-request
    MCP/working-set controls are never overwritten; the task is tagged as
    excluded so the report can explain why it was not sampled.
    """
    exp = (normalize_cost_experiment_config(experiment_config)
           if experiment_config is not None
           else load_cost_experiment_config())
    if not exp['enabled']:
        return request_config

    assignment = assign_cost_experiment(exp, task.get('convId', ''))
    if _has_request_policy_override(request_config):
        assignment = {
            'experiment_id': exp['experiment_id'],
            'assignmentUnit': 'conversation',
            'status': 'excluded',
            'reason': 'request_override',
        }
        task['_costExperiment'] = assignment
        return request_config

    task['_costExperiment'] = assignment
    if assignment.get('status') != 'assigned':
        return request_config

    policy = assignment['policy']
    updated = dict(request_config)
    updated['mcpToolExposure'] = policy['mcpToolExposure']
    compaction = dict(request_config.get('compaction') or {})
    compaction['workingSetTokens'] = policy['workingSetTokens']
    updated['compaction'] = compaction
    task['config'] = updated
    logger.debug('[CostExperiment] task=%s conv=%s experiment=%s arm=%s',
                 str(task.get('id') or '')[:8],
                 str(task.get('convId') or '')[:8],
                 assignment['experiment_id'], assignment['arm'])
    return updated


def _safe_int(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return 0
        return max(0, int(value or 0))
    except (TypeError, ValueError) as exc:
        logger.debug('[CostExperiment] invalid integer metric %r: %s', value, exc)
        return 0


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        logger.debug('[CostExperiment] invalid numeric metric %r: %s', value, exc)
        return None
    return number if math.isfinite(number) else None


def _has_real_price_snapshot(cost: dict, model: str,
                             provider_id: str) -> tuple[bool, str]:
    """Classify a cost snapshot without changing legacy display arithmetic."""
    source = str(cost.get('pricingSource') or '')
    if source:
        return source not in ('default_estimate', 'qwen_default_estimate'), source

    # Older persisted snapshots predate provenance. Infer only when their
    # concrete model/provider resolves to a real row; model-less callers in
    # unit/back-compat paths may still supply an explicitly priced snapshot.
    if not model:
        return cost.get('costUsd') is not None, 'legacy_explicit'
    try:
        from lib.pricing import QWEN_PRICING_CNY, lookup_pricing
        if 'qwen' in model.lower():
            return model in QWEN_PRICING_CNY, (
                'legacy_qwen_tier' if model in QWEN_PRICING_CNY
                else 'legacy_qwen_default_estimate')
        matched = lookup_pricing(model, provider_id or None)
        return matched is not None, ('legacy_resolved' if matched
                                     else 'legacy_default_estimate')
    except Exception as exc:
        logger.warning('[CostExperiment] pricing provenance lookup failed for '
                       'model=%s provider=%s: %s', model, provider_id, exc)
        return False, 'provenance_error'


def _precise_cost(cost: dict, currency: str) -> float | None:
    """Prefer persisted component precision over the rounded display total."""
    suffix = 'Usd' if currency == 'usd' else 'Cny'
    keys = (
        f'inputCost{suffix}', f'outputCost{suffix}',
        f'cacheWriteCost{suffix}', f'cacheReadCost{suffix}',
    )
    if any(key in cost for key in keys):
        values = [_safe_number(cost.get(key)) for key in keys]
        if all(value is not None for value in values):
            return sum(values)  # component snapshots carry 6-9 decimals
    return _safe_number(cost.get('cost' + suffix))


def _context_rollup(round_context: list | None) -> dict:
    rows = [row for row in (round_context or []) if isinstance(row, dict)]
    result = {'contextRoundSamples': len(rows)}
    for field in ('stablePrefixTokens', 'toolSchemaTokens',
                  'rawToolResultTokens', 'modelToolResultTokens'):
        result[field] = sum(_safe_int(row.get(field)) for row in rows)
    return result


def _api_round_observations(api_rounds: list | None) -> list[dict]:
    """Project provider rounds into a small, uniform benchmark shape."""
    from lib.cost import normalize_usage, split_input_tokens

    observations: list[dict] = []
    for index, api_round in enumerate(api_rounds or []):
        if not isinstance(api_round, dict):
            continue
        usage = api_round.get('usage') or {}
        normalized = normalize_usage(usage)
        uncached, total_input = split_input_tokens(usage)
        observations.append({
            'round': _safe_int(api_round.get('round')) or index + 1,
            'latencyMs': _safe_int(
                api_round.get('latencyMs') or api_round.get('elapsedMs')),
            'uncachedInputTokens': _safe_int(uncached),
            'promptTokens': _safe_int(total_input),
            'cacheReadTokens': _safe_int(normalized['cache_read']),
            'cacheWriteTokens': _safe_int(normalized['cache_write']),
            'outputTokens': _safe_int(normalized['output']),
            'reasoningTokens': _safe_int(normalized['thinking']),
            'prefixFingerprint': str(
                usage.get('_wire_static') or usage.get('_wire_fp')
                or api_round.get('prefixFingerprint') or ''),
        })
    return observations


def _public_price_values(value: Any) -> tuple[float | None, float | None]:
    if value is None:
        return None, None
    if isinstance(value, dict):
        usd = (_safe_number(value.get('costUsd'))
               if value.get('costUsd') is not None else None)
        if usd is None:
            usd = (_safe_number(value.get('publicPriceCostUsd'))
                   if value.get('publicPriceCostUsd') is not None else None)
        cny = (_safe_number(value.get('costCny'))
               if value.get('costCny') is not None else None)
        if cny is None:
            cny = (_safe_number(value.get('publicPriceCostCny'))
                   if value.get('publicPriceCostCny') is not None else None)
        return usd, cny
    return _safe_number(value), None


def _optional_number(mapping: dict, key: str) -> float | None:
    value = mapping.get(key)
    return _safe_number(value) if value is not None else None


def _benchmark_assignment(task: dict | None) -> dict | None:
    """Synthesize a persistence tag for benchmark tasks outside production A/B."""
    if not isinstance(task, dict):
        return None
    cfg = task.get('config') if isinstance(task.get('config'), dict) else {}
    benchmark = task.get('_benchmark') or task.get('benchmark') or cfg.get('benchmark')
    if not isinstance(benchmark, dict) or not benchmark:
        return None
    return {
        'experiment_id': str(
            benchmark.get('runId') or benchmark.get('experimentId')
            or 'benchmark-v1'),
        'assignmentUnit': 'task',
        'status': 'assigned',
        'arm': str(benchmark.get('experimentArm') or 'benchmark'),
    }


def build_cost_experiment_outcome(
    assignment: dict | None,
    *,
    usage: dict | None,
    cost: dict | None,
    api_rounds: list | None,
    finish_reason: str | None,
    error: Any,
    elapsed_ms: int | float | None,
    compactions: int = 0,
    model: str = '',
    provider_id: str = '',
    completed_at_ms: int | None = None,
    oracle_passed: bool | None = None,
    oracle_type: str = '',
    dataset: str = '',
    benchmark_task_id: str = '',
    agent_version: str = '',
    effort: str = '',
    experiment_arm: str = '',
    public_price_cost: Any = None,
    context_rounds: list | None = None,
    compaction_events: list | None = None,
    tool_exposure: dict | None = None,
    mcp_searches: int = 0,
    mcp_search_misses: int = 0,
    evidence_archives: list | None = None,
    infrastructure_error: Any = None,
    task: dict | None = None,
) -> dict | None:
    """Attach observable outcome metrics to one assigned task.

    Token counts come from provider-reported usage (or the already-persisted
    cost snapshot derived from it).  ``costUsd``/``costCny`` remain ``None``
    when no price row was available; the report exposes this as unpriced
    coverage rather than silently treating a missing price as zero.
    """
    if not isinstance(assignment, dict):
        assignment = _benchmark_assignment(task)
    if not isinstance(assignment, dict):
        return None
    outcome = {
        key: assignment[key]
        for key in ('experiment_id', 'assignmentUnit', 'status', 'arm',
                    'reason', 'policy')
        if key in assignment
    }
    if assignment.get('status') != 'assigned':
        return outcome

    from lib.cost import normalize_usage, split_input_tokens

    usage = usage if isinstance(usage, dict) else {}
    cost = cost if isinstance(cost, dict) else {}
    normalized = normalize_usage(usage)
    uncached, total_input = split_input_tokens(usage)
    total_input = _safe_int(cost.get('totalInputTokens')) or total_input
    uncached = (_safe_int(cost.get('inputTokens'))
                if cost.get('inputTokens') is not None else uncached)
    output = (_safe_int(cost.get('outputTokens'))
              if cost.get('outputTokens') is not None
              else normalized['output'])
    cache_read = (_safe_int(cost.get('cacheReadTokens'))
                  if cost.get('cacheReadTokens') is not None
                  else normalized['cache_read'])
    cache_write = (_safe_int(cost.get('cacheWriteTokens'))
                   if cost.get('cacheWriteTokens') is not None
                   else normalized['cache_write'])
    reasoning = _safe_int(normalized['thinking'])
    priced, pricing_source = _has_real_price_snapshot(
        cost, str(model or ''), str(provider_id or ''))
    cost_usd = _precise_cost(cost, 'usd') if priced else None
    cost_cny = _precise_cost(cost, 'cny') if priced else None

    if isinstance(task, dict):
        task_cfg = task.get('config') if isinstance(task.get('config'), dict) else {}
        benchmark = task.get('_benchmark') or task.get('benchmark') or task_cfg.get('benchmark')
        benchmark = benchmark if isinstance(benchmark, dict) else {}
        if oracle_passed is None and isinstance(benchmark.get('oraclePassed'), bool):
            oracle_passed = benchmark['oraclePassed']
        oracle_type = oracle_type or str(benchmark.get('oracleType') or '')
        dataset = dataset or str(benchmark.get('dataset') or '')
        benchmark_task_id = benchmark_task_id or str(
            benchmark.get('taskId') or benchmark.get('task_id') or task.get('id') or '')
        agent_version = agent_version or str(
            benchmark.get('agentVersion') or task.get('agentVersion') or '')
        effort = effort or str(
            benchmark.get('effort') or task_cfg.get('reasoning_effort')
            or task_cfg.get('thinkingDepth') or task.get('thinkingDepth')
            or task_cfg.get('effort') or '')
        experiment_arm = experiment_arm or str(
            benchmark.get('experimentArm') or assignment.get('arm') or '')
        if public_price_cost is None:
            public_price_cost = (task.get('_publicPriceCost')
                                 or benchmark.get('publicPriceCost'))
        context_rounds = context_rounds or task.get('_contextTelemetryRounds')
        compaction_events = (compaction_events
                             or task.get('_contextCompactionEvents'))
        tool_exposure = tool_exposure or task.get('_toolExposureTelemetry')
        mcp_searches = mcp_searches or _safe_int(task.get('_mcpSearchCount'))
        mcp_search_misses = (mcp_search_misses
                             or _safe_int(task.get('_mcpSearchMissCount')))
        evidence_archives = (evidence_archives
                             or task.get('_contextEvidenceArchives'))
        if infrastructure_error is None:
            infrastructure_error = task.get('_infrastructureError')

    public_cost_usd, public_cost_cny = _public_price_values(public_price_cost)
    public_cost_dict = (public_price_cost
                        if isinstance(public_price_cost, dict) else {})
    context_metrics = _context_rollup(context_rounds)
    compaction_count = max(
        _safe_int(compactions),
        len([row for row in (compaction_events or [])
             if isinstance(row, dict)]),
    )

    finish = str(finish_reason or '')
    error_finishes = {
        'error', 'aborted', 'content_filter', 'premature_close',
        'abnormal_stop', 'budget_exceeded',
    }
    outcome.update({
        'completedAt': int(completed_at_ms or time.time() * 1000),
        'latencyMs': _safe_int(elapsed_ms),
        'model': str(model or ''),
        'provider_id': str(provider_id or ''),
        'metrics': {
            'costUsd': cost_usd,
            'costCny': cost_cny,
            'actualCostUsd': cost_usd,
            'actualCostCny': cost_cny,
            'publicPriceCostUsd': public_cost_usd,
            'publicPriceCostCny': public_cost_cny,
            'actualInputCostUsd': (
                _optional_number(cost, 'inputCostUsd') if priced else None),
            'actualOutputCostUsd': (
                _optional_number(cost, 'outputCostUsd') if priced else None),
            'actualCacheReadCostUsd': (
                _optional_number(cost, 'cacheReadCostUsd') if priced else None),
            'actualCacheWriteCostUsd': (
                _optional_number(cost, 'cacheWriteCostUsd') if priced else None),
            'publicPriceCacheWriteCostUsd': _optional_number(
                public_cost_dict, 'cacheWriteCostUsd'),
            'pricingSource': pricing_source,
            'cacheSavingsUsd': _safe_number(cost.get('cacheSavingsUsd')),
            'promptTokens': _safe_int(total_input),
            'uncachedInputTokens': _safe_int(uncached),
            'outputTokens': _safe_int(output),
            'reasoningTokens': reasoning,
            'cacheReadTokens': _safe_int(cache_read),
            'cacheWriteTokens': _safe_int(cache_write),
            'rounds': len(api_rounds or []),
            **context_metrics,
        },
        'quality': {
            # This is an operational proxy, not a semantic-quality score.
            'terminalWithoutError': bool(
                not error and finish not in error_finishes),
            'finishReason': finish,
            'compactions': compaction_count,
        },
        'telemetry': {
            'roundContext': [dict(row) for row in (context_rounds or [])
                             if isinstance(row, dict)],
            'apiRounds': _api_round_observations(api_rounds),
            'compactions': [dict(row) for row in (compaction_events or [])
                            if isinstance(row, dict)],
            'toolExposure': (dict(tool_exposure)
                             if isinstance(tool_exposure, dict) else {}),
            'mcpSearches': _safe_int(mcp_searches),
            'mcpSearchMisses': _safe_int(mcp_search_misses),
            'evidenceArchives': [dict(row) for row in (evidence_archives or [])
                                 if isinstance(row, dict)],
        },
    })
    optional_metadata = {
        'dataset': dataset,
        'taskId': benchmark_task_id,
        'agentVersion': agent_version,
        'effort': effort,
        'experimentArm': experiment_arm,
    }
    outcome.update({key: value for key, value in optional_metadata.items()
                    if value})
    if oracle_passed is not None:
        outcome['quality']['oraclePassed'] = bool(oracle_passed)
    if oracle_type:
        outcome['quality']['oracleType'] = str(oracle_type)
    if infrastructure_error is not None:
        outcome['quality']['infrastructureError'] = str(infrastructure_error)
    return outcome


def build_task_cost_experiment_outcome(task: dict) -> dict | None:
    """Build a terminal task outcome before its task-results row is written.

    The conversation sync later rebuilds from its persisted cost snapshot, but
    this early copy keeps cold replay useful even if that sync is interrupted.
    """
    assignment = task.get('_costExperiment')
    cost = task.get('cost') if isinstance(task.get('cost'), dict) else None
    if cost is None and task.get('usage'):
        from lib.cost import compute_cost
        cost = compute_cost(
            task.get('usage'),
            model_id=task.get('model') or '',
            provider_id=task.get('provider_id') or None,
        )
    return build_cost_experiment_outcome(
        assignment,
        usage=task.get('usage'),
        cost=cost,
        api_rounds=task.get('apiRounds'),
        finish_reason=task.get('finishReason'),
        error=task.get('error'),
        elapsed_ms=(time.time() - task.get('created_at', time.time())) * 1000,
        compactions=task.get('_costExperimentCompactions', 0),
        model=task.get('model') or '',
        provider_id=task.get('provider_id') or '',
        task=task,
    )


def _empty_arm() -> dict:
    return {
        'conversations': 0,
        'turns': 0,
        'pricedTurns': 0,
        'unpricedTurns': 0,
        'totalCostUsd': 0.0,
        'totalCostCny': 0.0,
        'publicPricedTurns': 0,
        'totalPublicPriceCostUsd': 0.0,
        'totalPublicPriceCostCny': 0.0,
        'totalActualCacheWriteCostUsd': 0.0,
        'totalPublicPriceCacheWriteCostUsd': 0.0,
        'actualCacheWriteCostSamples': 0,
        'publicCacheWriteCostSamples': 0,
        'promptTokens': 0,
        'uncachedInputTokens': 0,
        'outputTokens': 0,
        'reasoningTokens': 0,
        'cacheReadTokens': 0,
        'cacheWriteTokens': 0,
        'contextRoundSamples': 0,
        'stablePrefixTokens': 0,
        'toolSchemaTokens': 0,
        'rawToolResultTokens': 0,
        'modelToolResultTokens': 0,
        'rounds': 0,
        'latencyMs': 0,
        'latencySamples': 0,
        'terminalWithoutError': 0,
        'oracleEvaluated': 0,
        'oraclePassed': 0,
        'oracleActualPriced': 0,
        'oracleActualCostUsd': 0.0,
        'oraclePublicPriced': 0,
        'oraclePublicPriceCostUsd': 0.0,
        'compactions': 0,
        'mcpSearches': 0,
        'mcpSearchMisses': 0,
        'prefixTransitions': 0,
        'prefixMutations': 0,
        'evidenceRetained': 0,
        'evidenceLost': 0,
        'toolExposureSamples': 0,
        'availableTools': 0,
        'exposedTools': 0,
        'models': {},
        'providers': {},
        'pricingSources': {},
        '_conversation_ids': set(),
        '_conversation_costs': {},
        '_latencies': [],
    }


def _rounded_ratio(numerator: float, denominator: float,
                   places: int = 4) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, places)


def aggregate_cost_experiment_rows(
    rows: list,
    *,
    experiment_id: str,
    days: int,
    now_ms: int | None = None,
    min_sample_size: int = 20,
) -> dict:
    """Aggregate persisted assistant outcomes into a two-arm report."""
    now_ms = int(now_ms or time.time() * 1000)
    days = max(1, min(90, int(days or 14)))
    cutoff = now_ms - days * 86_400_000
    arms = {'control': _empty_arm(), 'optimized': _empty_arm()}
    invalid_rows = 0

    for row in rows or []:
        try:
            raw_messages = (row.get('messages') if isinstance(row, dict)
                            else row['messages'])
            messages = (json.loads(raw_messages)
                        if isinstance(raw_messages, str) else raw_messages)
            if not isinstance(messages, list):
                raise ValueError('messages is not a list')
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            invalid_rows += 1
            logger.warning('[CostExperiment] report skipped malformed row: %s',
                           exc)
            continue
        conv_id = str((row.get('id') if isinstance(row, dict)
                       else row['id']) or '')
        row_updated = _safe_int(
            row.get('updated_at') if isinstance(row, dict)
            else row['updated_at'])
        for message in messages:
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            outcome = message.get('costExperiment')
            if not isinstance(outcome, dict):
                continue
            if outcome.get('experiment_id') != experiment_id:
                continue
            if outcome.get('status') != 'assigned':
                continue
            arm_name = outcome.get('arm')
            if arm_name not in arms:
                continue
            completed_at = _safe_int(outcome.get('completedAt')) or row_updated
            if completed_at < cutoff or completed_at > now_ms + 86_400_000:
                continue

            arm = arms[arm_name]
            arm['_conversation_ids'].add(conv_id)
            arm['turns'] += 1
            metrics = outcome.get('metrics') or {}
            quality = outcome.get('quality') or {}
            cost_usd = _safe_number(metrics.get('costUsd'))
            cost_cny = _safe_number(metrics.get('costCny'))
            public_cost_usd = _optional_number(
                metrics, 'publicPriceCostUsd')
            public_cost_cny = _optional_number(
                metrics, 'publicPriceCostCny')
            actual_cache_write_cost = _optional_number(
                metrics, 'actualCacheWriteCostUsd')
            public_cache_write_cost = _optional_number(
                metrics, 'publicPriceCacheWriteCostUsd')
            conv_cost = arm['_conversation_costs'].setdefault(
                conv_id, {'turns': 0, 'pricedTurns': 0, 'costUsd': 0.0})
            conv_cost['turns'] += 1
            if cost_usd is None:
                arm['unpricedTurns'] += 1
            else:
                arm['pricedTurns'] += 1
                arm['totalCostUsd'] += cost_usd
                conv_cost['pricedTurns'] += 1
                conv_cost['costUsd'] += cost_usd
                if cost_cny is not None:
                    arm['totalCostCny'] += cost_cny
            if public_cost_usd is not None:
                arm['publicPricedTurns'] += 1
                arm['totalPublicPriceCostUsd'] += public_cost_usd
                if public_cost_cny is not None:
                    arm['totalPublicPriceCostCny'] += public_cost_cny
            if actual_cache_write_cost is not None:
                arm['actualCacheWriteCostSamples'] += 1
                arm['totalActualCacheWriteCostUsd'] += actual_cache_write_cost
            if public_cache_write_cost is not None:
                arm['publicCacheWriteCostSamples'] += 1
                arm['totalPublicPriceCacheWriteCostUsd'] += public_cache_write_cost
            for source, target in (
                ('promptTokens', 'promptTokens'),
                ('uncachedInputTokens', 'uncachedInputTokens'),
                ('outputTokens', 'outputTokens'),
                ('reasoningTokens', 'reasoningTokens'),
                ('cacheReadTokens', 'cacheReadTokens'),
                ('cacheWriteTokens', 'cacheWriteTokens'),
                ('contextRoundSamples', 'contextRoundSamples'),
                ('stablePrefixTokens', 'stablePrefixTokens'),
                ('toolSchemaTokens', 'toolSchemaTokens'),
                ('rawToolResultTokens', 'rawToolResultTokens'),
                ('modelToolResultTokens', 'modelToolResultTokens'),
                ('rounds', 'rounds'),
            ):
                arm[target] += _safe_int(metrics.get(source))
            latency = _safe_int(outcome.get('latencyMs'))
            arm['latencyMs'] += latency
            if outcome.get('latencyMs') is not None:
                arm['latencySamples'] += 1
                arm['_latencies'].append(latency)
            arm['terminalWithoutError'] += int(
                quality.get('terminalWithoutError') is True)
            if isinstance(quality.get('oraclePassed'), bool):
                arm['oracleEvaluated'] += 1
                if quality['oraclePassed']:
                    arm['oraclePassed'] += 1
                if cost_usd is not None:
                    arm['oracleActualPriced'] += 1
                    arm['oracleActualCostUsd'] += cost_usd
                if public_cost_usd is not None:
                    arm['oraclePublicPriced'] += 1
                    arm['oraclePublicPriceCostUsd'] += public_cost_usd
            arm['compactions'] += _safe_int(quality.get('compactions'))
            telemetry = outcome.get('telemetry') or {}
            arm['mcpSearches'] += _safe_int(telemetry.get('mcpSearches'))
            arm['mcpSearchMisses'] += _safe_int(
                telemetry.get('mcpSearchMisses'))
            fingerprints = [
                str(row.get('prefixFingerprint') or '')
                for row in telemetry.get('roundContext') or []
                if isinstance(row, dict) and row.get('prefixFingerprint')]
            if len(fingerprints) > 1:
                arm['prefixTransitions'] += len(fingerprints) - 1
                arm['prefixMutations'] += sum(
                    left != right for left, right in zip(
                        fingerprints, fingerprints[1:]))
            for event in telemetry.get('compactions') or []:
                if not isinstance(event, dict):
                    continue
                arm['evidenceRetained'] += len(
                    event.get('evidenceRetained') or [])
                arm['evidenceLost'] += len(event.get('evidenceLost') or [])
            exposure = telemetry.get('toolExposure') or {}
            if isinstance(exposure, dict) and exposure:
                arm['toolExposureSamples'] += 1
                arm['availableTools'] += _safe_int(
                    exposure.get('availableTools'))
                arm['exposedTools'] += _safe_int(exposure.get('exposedTools'))
            for field, target in (('model', 'models'),
                                  ('provider_id', 'providers')):
                value = str(outcome.get(field) or '')
                if value:
                    arm[target][value] = arm[target].get(value, 0) + 1
            pricing_source = str(metrics.get('pricingSource') or 'legacy')
            arm['pricingSources'][pricing_source] = (
                arm['pricingSources'].get(pricing_source, 0) + 1)

    public_arms: dict[str, dict] = {}
    for name, raw in arms.items():
        turns = raw['turns']
        priced = raw['pricedTurns']
        prompt = raw['promptTokens']
        fully_priced = [
            value for value in raw['_conversation_costs'].values()
            if value['turns'] > 0 and value['turns'] == value['pricedTurns']
        ]
        fully_priced_cost = sum(value['costUsd'] for value in fully_priced)
        latency_values = sorted(raw['_latencies'])
        p90_latency = (latency_values[max(
            0, math.ceil(0.9 * len(latency_values)) - 1)]
            if latency_values else None)
        oracle_successes = raw['oraclePassed']
        oracle_cost_per_success = None
        if (oracle_successes and raw['oracleEvaluated'] > 0
                and raw['oracleActualPriced'] == raw['oracleEvaluated']):
            oracle_cost_per_success = round(
                raw['oracleActualCostUsd'] / oracle_successes, 6)
        public_oracle_cost_per_success = None
        if (oracle_successes and raw['oracleEvaluated'] > 0
                and raw['oraclePublicPriced'] == raw['oracleEvaluated']):
            public_oracle_cost_per_success = round(
                raw['oraclePublicPriceCostUsd'] / oracle_successes, 6)
        public_arms[name] = {
            **{key: value for key, value in raw.items()
               if key not in ('_conversation_ids', '_conversation_costs',
                              '_latencies',
                              'latencyMs',
                              'terminalWithoutError')},
            'conversations': len(raw['_conversation_ids']),
            'fullyPricedConversations': len(fully_priced),
            'totalCostUsd': round(raw['totalCostUsd'], 6),
            'totalCostCny': round(raw['totalCostCny'], 6),
            'totalPublicPriceCostUsd': round(
                raw['totalPublicPriceCostUsd'], 6),
            'totalPublicPriceCostCny': round(
                raw['totalPublicPriceCostCny'], 6),
            'totalActualCacheWriteCostUsd': round(
                raw['totalActualCacheWriteCostUsd'], 6),
            'totalPublicPriceCacheWriteCostUsd': round(
                raw['totalPublicPriceCacheWriteCostUsd'], 6),
            'actualCacheWriteCostPerObservedTurnUsd': (
                round(raw['totalActualCacheWriteCostUsd']
                      / raw['actualCacheWriteCostSamples'], 6)
                if raw['actualCacheWriteCostSamples'] else None),
            'publicCacheWriteCostPerObservedTurnUsd': (
                round(raw['totalPublicPriceCacheWriteCostUsd']
                      / raw['publicCacheWriteCostSamples'], 6)
                if raw['publicCacheWriteCostSamples'] else None),
            'costPerPricedTurnUsd': (
                round(raw['totalCostUsd'] / priced, 6) if priced else None),
            'costPerFullyPricedConversationUsd': (
                round(fully_priced_cost / len(fully_priced), 6)
                if fully_priced else None),
            'pricingCoverage': _rounded_ratio(priced, turns),
            'publicPricingCoverage': _rounded_ratio(
                raw['publicPricedTurns'], turns),
            'promptTokensPerTurn': (
                round(prompt / turns, 1) if turns else None),
            'roundsPerTurn': (
                round(raw['rounds'] / turns, 2) if turns else None),
            'cacheReadRatio': _rounded_ratio(
                raw['cacheReadTokens'], prompt),
            'latencyAvgMs': (
                round(raw['latencyMs'] / raw['latencySamples'])
                if raw['latencySamples'] else None),
            'latencyP90Ms': p90_latency,
            'terminalWithoutErrorRate': _rounded_ratio(
                raw['terminalWithoutError'], turns),
            'oraclePassRate': _rounded_ratio(
                raw['oraclePassed'], raw['oracleEvaluated']),
            'costPerOracleSuccessUsd': oracle_cost_per_success,
            'publicPriceCostPerOracleSuccessUsd': (
                public_oracle_cost_per_success),
            'stablePrefixTokensPerContextRound': _rounded_ratio(
                raw['stablePrefixTokens'], raw['contextRoundSamples'], 1),
            'toolSchemaTokensPerContextRound': _rounded_ratio(
                raw['toolSchemaTokens'], raw['contextRoundSamples'], 1),
            'rawToolResultTokensPerContextRound': _rounded_ratio(
                raw['rawToolResultTokens'], raw['contextRoundSamples'], 1),
            'modelToolResultTokensPerContextRound': _rounded_ratio(
                raw['modelToolResultTokens'], raw['contextRoundSamples'], 1),
            'mcpSearchMissRate': _rounded_ratio(
                raw['mcpSearchMisses'], raw['mcpSearches']),
            'prefixMutationRate': _rounded_ratio(
                raw['prefixMutations'], raw['prefixTransitions']),
            'evidenceRetentionRate': _rounded_ratio(
                raw['evidenceRetained'],
                raw['evidenceRetained'] + raw['evidenceLost']),
            'availableToolsAvg': _rounded_ratio(
                raw['availableTools'], raw['toolExposureSamples'], 1),
            'exposedToolsAvg': _rounded_ratio(
                raw['exposedTools'], raw['toolExposureSamples'], 1),
            'compactionsPerTurn': _rounded_ratio(
                raw['compactions'], turns),
        }

    control_cost = public_arms['control']['costPerPricedTurnUsd']
    optimized_cost = public_arms['optimized']['costPerPricedTurnUsd']
    turn_delta = None
    if control_cost not in (None, 0) and optimized_cost is not None:
        turn_delta = round(
            (optimized_cost - control_cost) / control_cost * 100, 2)
    control_conv_cost = public_arms['control'][
        'costPerFullyPricedConversationUsd']
    optimized_conv_cost = public_arms['optimized'][
        'costPerFullyPricedConversationUsd']
    conversation_delta = None
    if control_conv_cost not in (None, 0) and optimized_conv_cost is not None:
        conversation_delta = round(
            (optimized_conv_cost - control_conv_cost)
            / control_conv_cost * 100, 2)
    ready = all(
        public_arms[name]['fullyPricedConversations'] >= int(min_sample_size)
        for name in ('control', 'optimized'))
    quality_ready = all(
        public_arms[name]['oracleEvaluated'] >= int(min_sample_size)
        for name in ('control', 'optimized'))
    control_quality = public_arms['control']['oraclePassRate']
    optimized_quality = public_arms['optimized']['oraclePassRate']
    quality_delta = None
    if control_quality is not None and optimized_quality is not None:
        quality_delta = round(optimized_quality - control_quality, 4)
    control_success_cost = public_arms['control'][
        'publicPriceCostPerOracleSuccessUsd']
    optimized_success_cost = public_arms['optimized'][
        'publicPriceCostPerOracleSuccessUsd']
    success_cost_delta = None
    if control_success_cost not in (None, 0) and optimized_success_cost is not None:
        success_cost_delta = round(
            (optimized_success_cost - control_success_cost)
            / control_success_cost * 100, 2)
    control_cache_write_cost = public_arms['control'][
        'publicCacheWriteCostPerObservedTurnUsd']
    optimized_cache_write_cost = public_arms['optimized'][
        'publicCacheWriteCostPerObservedTurnUsd']
    cache_write_cost_delta = None
    if (control_cache_write_cost not in (None, 0)
            and optimized_cache_write_cost is not None):
        cache_write_cost_delta = round(
            (optimized_cache_write_cost - control_cache_write_cost)
            / control_cache_write_cost * 100, 2)
    return {
        'experiment_id': experiment_id,
        'windowDays': days,
        'assignmentUnit': 'conversation',
        'minSampleSize': int(min_sample_size),
        'ready': ready,
        'qualityReady': quality_ready,
        'invalidRows': invalid_rows,
        'arms': public_arms,
        'comparison': {
            'costPerConversationDeltaPct': conversation_delta,
            'costPerPricedTurnDeltaPct': turn_delta,
            'oraclePassRateDelta': quality_delta,
            'publicPriceCostPerOracleSuccessDeltaPct': success_cost_delta,
            'publicCacheWriteCostPerTurnDeltaPct': cache_write_cost_delta,
            'optimizedIsCheaper': bool(
                conversation_delta is not None and conversation_delta < 0),
        },
        'methodology': (
            'Provider-reported usage multiplied by the persisted price '
            'snapshot; missing prices are counted as unpriced, never zero. '
            'terminalWithoutError is a run-health metric only. Semantic '
            'quality is measured exclusively by oraclePassed; public-price '
            'cost per success is emitted only with complete oracle pricing.'
        ),
    }


__all__ = [
    'CostExperimentTransitionError',
    'aggregate_cost_experiment_rows',
    'apply_cost_experiment',
    'assign_cost_experiment',
    'build_cost_experiment_outcome',
    'build_task_cost_experiment_outcome',
    'load_cost_experiment_config',
    'normalize_cost_experiment_config',
    'validate_cost_experiment_transition',
]
