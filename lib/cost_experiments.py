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

import copy
import json
import math
import os
import re
import threading
import time
from typing import Any

from lib.config_dir import config_path
from lib.experiments.builtin_context_cost import (
    CONTROL_POLICY,
    OPTIMIZED_POLICY,
    context_cost_spec,
)
from lib.experiments.contracts import (
    ExperimentContractError,
    validate_resolved_spec,
)
from lib.experiments.service import (
    analyze_experiment,
    assign_experiment,
    compile_experiment_application,
    compile_metric_extractor,
)
from lib.experiments.registry import registry as experiment_registry
from lib.log import get_logger

logger = get_logger(__name__)


_DEFAULT_EXPERIMENT_ID = 'context-cost-v1'
_ARM_POLICIES = {
    'control': CONTROL_POLICY,
    'optimized': OPTIMIZED_POLICY,
}
_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,80}$')
_LIFECYCLES = {'draft', 'running', 'sealed'}


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
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError('fractional value')
    except (TypeError, ValueError, OverflowError) as exc:
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

    lifecycle_raw = raw.get('lifecycle')
    lifecycle_invalid_reason = ''
    if lifecycle_raw is not None and (
            not isinstance(lifecycle_raw, str)
            or lifecycle_raw not in _LIFECYCLES):
        if strict:
            raise ValueError('lifecycle must be draft, running, or sealed')
        logger.error('[CostExperiment] invalid lifecycle=%r; disabling',
                     lifecycle_raw)
        lifecycle_invalid_reason = 'invalid_lifecycle'
        enabled = False
        lifecycle = 'draft'
    elif lifecycle_raw == 'sealed' and enabled:
        if strict:
            raise ValueError(
                'a sealed experiment cannot be enabled; choose a new experiment_id')
        lifecycle_invalid_reason = 'sealed_experiment_reopened'
        enabled = False
        lifecycle = 'sealed'
    else:
        # Lifecycle is server-owned transition state. A stale settings snapshot
        # may submit running+off or draft+on; the atomic transition validator
        # derives the durable state after normalizing that requested operation.
        lifecycle = (
            'running' if enabled
            else ('sealed' if lifecycle_raw == 'sealed' else 'draft')
        )

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

    traffic_percent = _bounded_number(
        raw.get('traffic_percent'), field='traffic_percent', default=10,
        minimum=0, maximum=100, strict=strict)
    treatment_percent = _bounded_number(
        raw.get('treatment_percent'), field='treatment_percent', default=50,
        minimum=0, maximum=100, strict=strict)
    empty_arm = enabled and treatment_percent in (0, 100)
    if empty_arm and strict:
        raise ValueError(
            'treatment_percent must be between 1 and 99 when enabled')
    min_sample_size = _bounded_number(
        raw.get('min_sample_size'), field='min_sample_size', default=20,
        minimum=2, maximum=10_000, strict=strict)
    started_at_ms = _bounded_number(
        raw.get('started_at_ms'), field='started_at_ms', default=0,
        minimum=0, maximum=9_999_999_999_999, strict=strict)
    sealed_at_ms = _bounded_number(
        raw.get('sealed_at_ms'), field='sealed_at_ms', default=0,
        minimum=0, maximum=9_999_999_999_999, strict=strict)
    try:
        current_spec = context_cost_spec(
            experiment_id=experiment_id,
            traffic_percent=traffic_percent,
            treatment_percent=treatment_percent,
            minimum_sample_size=min_sample_size,
        )
    except (ExperimentContractError, RuntimeError, ValueError) as exc:
        if strict:
            raise ValueError(f'experiment capability resolution failed: {exc}') from exc
        logger.error('[CostExperiment] capability resolution failed; disabling: %s',
                     exc, exc_info=True)
        return {
            'enabled': False,
            'lifecycle': 'sealed' if lifecycle == 'running' else lifecycle,
            'experiment_id': experiment_id,
            'traffic_percent': traffic_percent,
            'treatment_percent': treatment_percent,
            'min_sample_size': min_sample_size,
            'started_at_ms': started_at_ms,
            'sealed_at_ms': sealed_at_ms,
            'assignment_unit': 'conversation',
            'sticky': True,
            'arms': {name: dict(policy)
                     for name, policy in _ARM_POLICIES.items()},
            'invalid_reason': 'capability_resolution_failed',
        }

    configured_spec = raw.get('spec')
    persisted_spec = None
    invalid_reason = (
        lifecycle_invalid_reason
        or ('empty_experiment_arm' if empty_arm else '')
    )
    stale_derived_fields = False
    if isinstance(configured_spec, dict):
        # A changed experiment ID intentionally starts a new immutable run;
        # derived fields returned by an older settings GET are then stale.
        if str(configured_spec.get('experimentId') or '') == experiment_id:
            try:
                persisted_spec = validate_resolved_spec(configured_spec)
            except ExperimentContractError as exc:
                if strict:
                    raise ValueError(f'invalid persisted experiment spec: {exc}') from exc
                invalid_reason = 'invalid_persisted_spec'
                logger.error('[CostExperiment] invalid persisted spec; disabling: %s',
                             exc)
        else:
            stale_derived_fields = True

    supplied_digest = (
        '' if stale_derived_fields
        else str(raw.get('spec_digest') or '').strip()
    )
    if persisted_spec is not None:
        persisted_digest = persisted_spec['specDigest']
        if supplied_digest and supplied_digest != persisted_digest:
            if strict:
                raise ValueError('spec_digest does not match the persisted spec')
            invalid_reason = 'invalid_persisted_spec'
        supplied_digest = persisted_digest

    current_digest = current_spec['specDigest']
    if supplied_digest and supplied_digest != current_digest:
        if strict:
            raise ValueError(
                'experiment specification changed; choose a new experiment_id')
        invalid_reason = invalid_reason or 'strategy_spec_changed'
        logger.error(
            '[CostExperiment] spec drift for experiment=%s; disabling old run',
            experiment_id,
        )

    effective_spec = persisted_spec or current_spec
    result = {
        'enabled': enabled,
        'lifecycle': lifecycle,
        'experiment_id': experiment_id,
        'traffic_percent': traffic_percent,
        'treatment_percent': treatment_percent,
        'min_sample_size': min_sample_size,
        'started_at_ms': started_at_ms,
        'sealed_at_ms': sealed_at_ms,
        'assignment_unit': 'conversation',
        'sticky': True,
        'arms': {name: dict(policy) for name, policy in _ARM_POLICIES.items()},
        'contract_version': effective_spec['contractVersion'],
        'spec_digest': effective_spec['specDigest'],
        'resolved_spec_digest': current_digest,
        'spec': effective_spec,
    }
    if invalid_reason:
        result['enabled'] = False
        if result['lifecycle'] == 'running':
            result['lifecycle'] = 'sealed'
        result['invalid_reason'] = invalid_reason
    return result


# The turn hot path (``_turn_prelude`` → ``apply_cost_experiment``) loads
# this block once per chat turn, so a naive ``read_json`` would pay a full
# read+parse of server_config.json on a network filesystem for every turn —
# including the default-disabled case, where the read exists only to learn
# "off".  Re-parse only when the file's mtime changes; the atomic-write
# store replaces the file on save, which always bumps the path mtime.
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: dict[str, Any] = {'mtime_ns': None, 'config': None}
_APPLICATION_CACHE_LOCK = threading.Lock()
_APPLICATION_CACHE: dict[str, Any] = {'key': None, 'apply': None}


def _load_cached_cost_experiment_config() -> dict:
    """Return the read-only process cache used by the turn hot path."""
    from lib.json_store import read_json

    path = config_path('server_config.json')
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError as exc:
        # Missing/unreadable file means "no experiment"; never let a stat
        # hiccup break the chat hot path.
        logger.debug('[CostExperiment] server_config stat failed: %s', exc)
        return normalize_cost_experiment_config({})
    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE
        if cached['mtime_ns'] == mtime_ns and cached['config'] is not None:
            return cached['config']
    saved = read_json(path, default={})
    raw = saved.get('cost_experiment') if isinstance(saved, dict) else {}
    config = normalize_cost_experiment_config(raw)
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE['mtime_ns'] = mtime_ns
        _CONFIG_CACHE['config'] = config
    return config


def load_cost_experiment_config() -> dict:
    """Load a detached active experiment block from shared server config."""
    return copy.deepcopy(_load_cached_cost_experiment_config())


def _compiled_cost_application(spec: dict):
    """Return a generation-aware application plan for the turn hot path."""
    providers = experiment_registry()
    key = (str(spec.get('specDigest') or ''), providers.generation)
    with _APPLICATION_CACHE_LOCK:
        if (_APPLICATION_CACHE['key'] == key
                and _APPLICATION_CACHE['apply'] is not None):
            return _APPLICATION_CACHE['apply']
        application = compile_experiment_application(
            spec, provider_registry=providers)
        _APPLICATION_CACHE['key'] = key
        _APPLICATION_CACHE['apply'] = application
        return application


def validate_cost_experiment_transition(previous_raw: Any,
                                        next_config: dict, *,
                                        now_ms: int | None = None) -> dict:
    """Keep one experiment ID bound to one immutable routing shape.

    Changing routing, provider code, or the fixed-horizon analysis plan under
    an existing ID would mix incompatible observations. Once persisted, any
    resolved-spec change therefore requires a fresh experiment ID. Stopping a
    running experiment seals that ID permanently, giving fixed-horizon analysis
    an irreversible boundary instead of a stop/restart peeking loop.
    """
    transition_time = int(now_ms or time.time() * 1000)
    current = normalize_cost_experiment_config(next_config, strict=True)
    if not isinstance(previous_raw, dict) or not previous_raw:
        current['lifecycle'] = 'running' if current['enabled'] else 'draft'
        current['started_at_ms'] = transition_time if current['enabled'] else 0
        current['sealed_at_ms'] = 0
        return current
    previous = normalize_cost_experiment_config(previous_raw)
    if previous['experiment_id'] != current['experiment_id']:
        current['lifecycle'] = 'running' if current['enabled'] else 'draft'
        current['started_at_ms'] = transition_time if current['enabled'] else 0
        current['sealed_at_ms'] = 0
        return current
    changed = [
        field for field in (
            'traffic_percent', 'treatment_percent', 'min_sample_size',
            'spec_digest',
        )
        if previous.get(field) != current.get(field)
    ]
    if changed:
        raise CostExperimentTransitionError(
            'change experiment_id before changing immutable experiment fields: '
            + ', '.join(changed))
    previous_lifecycle = previous.get('lifecycle') or (
        'running' if previous.get('enabled') else 'draft')
    if previous_lifecycle == 'sealed':
        if current['enabled']:
            raise CostExperimentTransitionError(
                'sealed experiment IDs cannot be restarted; choose a new '
                'experiment_id')
        current['lifecycle'] = 'sealed'
        current['started_at_ms'] = previous.get('started_at_ms', 0)
        current['sealed_at_ms'] = previous.get('sealed_at_ms', 0)
    elif previous_lifecycle == 'running':
        current['lifecycle'] = 'running' if current['enabled'] else 'sealed'
        current['started_at_ms'] = previous.get('started_at_ms', 0)
        current['sealed_at_ms'] = (
            previous.get('sealed_at_ms', 0)
            if current['enabled'] else max(
                transition_time, int(previous.get('started_at_ms') or 0)
            )
        )
    else:
        current['lifecycle'] = 'running' if current['enabled'] else 'draft'
        current['started_at_ms'] = transition_time if current['enabled'] else 0
        current['sealed_at_ms'] = 0
    return current


def _legacy_assignment(record: dict) -> dict:
    """Expose the historic snake-case ID while retaining the v1 contract."""
    return {**record, 'experiment_id': record.get('experimentId')}


def assign_cost_experiment(config: dict, conv_id: str, *, owner_id: Any) -> dict:
    """Return a deterministic conversation-level assignment record."""
    cfg = normalize_cost_experiment_config(config)
    base = {
        'contractVersion': cfg.get('contract_version'),
        'experimentId': cfg['experiment_id'],
        'experiment_id': cfg['experiment_id'],
        'specDigest': cfg.get('spec_digest'),
        'assignmentUnit': 'conversation',
        'status': 'off',
        'exposureStatus': 'not_applicable',
    }
    if not cfg['enabled']:
        return base
    conv_id = str(conv_id or '').strip()
    if not conv_id:
        return {**base, 'status': 'excluded', 'reason': 'missing_conversation_id'}
    try:
        return _legacy_assignment(assign_experiment(
            cfg['spec'], owner_id=owner_id, unit_id=conv_id))
    except ValueError as exc:
        logger.error('[CostExperiment] invalid assignment identity: %s', exc)
        return {
            **base,
            'status': 'excluded',
            'exposureStatus': 'not_applied',
            'reason': 'missing_owner_identity',
        }


def apply_cost_experiment(task: dict, request_config: dict, *,
                          experiment_config: dict | None = None) -> dict:
    """Apply the assigned arm to a shallow copy of the request config.

    Disabled experiments return the exact input object.  Explicit per-request
    MCP/working-set controls are never overwritten; the task is tagged as
    excluded so the report can explain why it was not sampled.
    """
    exp = (normalize_cost_experiment_config(experiment_config)
           if experiment_config is not None
           else _load_cached_cost_experiment_config())
    if not exp['enabled']:
        return request_config

    owner_id = task.get('_userId')
    if owner_id in (None, ''):
        assignment = {
            'contractVersion': exp.get('contract_version'),
            'experimentId': exp['experiment_id'],
            'experiment_id': exp['experiment_id'],
            'specDigest': exp.get('spec_digest'),
            'assignmentUnit': 'conversation',
            'status': 'excluded',
            'exposureStatus': 'not_applied',
            'reason': 'missing_owner_identity',
        }
        task['_costExperiment'] = assignment
        return request_config
    conv_id = str(task.get('convId') or '').strip()
    if not conv_id:
        assignment = {
            'contractVersion': exp.get('contract_version'),
            'experimentId': exp['experiment_id'],
            'experiment_id': exp['experiment_id'],
            'specDigest': exp.get('spec_digest'),
            'assignmentUnit': 'conversation',
            'status': 'excluded',
            'exposureStatus': 'not_applied',
            'reason': 'missing_conversation_id',
        }
        task['_costExperiment'] = assignment
        return request_config
    try:
        application = _compiled_cost_application(exp['spec'])
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.error('[CostExperiment] application plan unavailable: %s', exc,
                     exc_info=True)
        task['_costExperiment'] = {
            'contractVersion': exp.get('contract_version'),
            'experimentId': exp['experiment_id'],
            'experiment_id': exp['experiment_id'],
            'specDigest': exp.get('spec_digest'),
            'assignmentUnit': 'conversation',
            'status': 'application_failed',
            'exposureStatus': 'failed',
            'reason': 'experiment_capability_unavailable',
        }
        return request_config
    updated, assignment = application(
        owner_id=owner_id, unit_id=conv_id, request_config=request_config)
    assignment = _legacy_assignment(assignment)
    task['_costExperiment'] = assignment
    if assignment.get('exposureStatus') != 'applied':
        return request_config
    task['config'] = updated
    logger.debug('[CostExperiment] task=%s conv=%s experiment=%s arm=%s',
                 str(task.get('id') or '')[:8],
                 str(task.get('convId') or '')[:8],
                 assignment['experiment_id'], assignment['arm'])
    return dict(updated)


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
        for key in (
            'contractVersion', 'experimentId', 'experiment_id',
            'specDigest', 'assignmentUnit', 'assignmentAlgorithm',
            'subjectDigest', 'status', 'exposureStatus', 'exposedAt',
            'arm', 'reason', 'strategy', 'policy',
        )
        if key in assignment
    }
    if 'experimentId' not in outcome and outcome.get('experiment_id'):
        outcome['experimentId'] = outcome['experiment_id']
    if 'experiment_id' not in outcome and outcome.get('experimentId'):
        outcome['experiment_id'] = outcome['experimentId']
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


def task_outcome_report_rows(records: list) -> tuple[list, int]:
    """Project task-result outcome records into aggregator input rows.

    ``task_results`` (legacy table / sidecar ``storage_records`` namespace)
    already persists one ``metadata.costExperiment`` outcome per terminal
    task, so the report can scan THAT compact projection instead of hauling
    every conversation's full transcript.  The returned rows mirror the
    conversation-scan shape (``{'id', 'messages', 'updated_at'}``) so
    :func:`aggregate_cost_experiment_rows` consumes both sources unchanged.

    Each record is ``{'task_id', 'conv_id', 'completed_at', 'outcome'}``
    with ``outcome`` a dict or JSON string.  Unparseable records are logged,
    counted (second return value), and skipped.
    """
    rows: list = []
    invalid = 0
    for record in records or []:
        if not isinstance(record, dict):
            invalid += 1
            continue
        outcome = record.get('outcome')
        if isinstance(outcome, str):
            try:
                outcome = json.loads(outcome)
            except (json.JSONDecodeError, ValueError) as exc:
                invalid += 1
                logger.warning(
                    '[CostExperiment] report skipped malformed outcome '
                    'record: %s', exc)
                continue
        if not isinstance(outcome, dict) or not outcome:
            invalid += 1
            continue
        conv_id = str(record.get('conv_id') or record.get('task_id') or '')
        completed_at = _safe_int(record.get('completed_at'))
        rows.append({
            'id': conv_id,
            'messages': [{'role': 'assistant', 'costExperiment': outcome}],
            'updated_at': completed_at,
        })
    return rows, invalid


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
        '_assignment_units': set(),
        '_first_observation_by_unit': {},
        '_pending_order_by_unit': {},
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
    experiment_spec: dict | None = None,
    analysis_closed: bool = False,
    analysis_start_ms: int = 0,
    analysis_sealed_ms: int = 0,
    truncated: bool = False,
    source_invalid_rows: int = 0,
) -> dict:
    """Aggregate outcomes and run the spec-pinned promotion analyzer.

    Descriptive totals remain available for legacy observations, but promotion
    is refused unless every analyzed exposure carries the expected spec digest
    and application marker and the source is complete.
    """
    now_ms = int(now_ms or time.time() * 1000)
    days = max(1, min(90, int(days or 14)))
    analysis_start_ms = max(0, int(analysis_start_ms or 0))
    analysis_sealed_ms = max(0, int(analysis_sealed_ms or 0))
    cutoff = analysis_start_ms or (now_ms - days * 86_400_000)
    arms = {'control': _empty_arm(), 'optimized': _empty_arm()}
    invalid_rows = max(0, int(source_invalid_rows or 0))
    metric_extraction_errors = 0
    unversioned_outcomes = 0
    unverified_exposures = 0
    pending_exposure_units: set[str] = set()
    lifecycle_window_violations = 0
    observed_spec_digests: set[str] = set()
    unit_arms: dict[str, str] = {}
    unit_exposure_order: dict[str, tuple[int, int, str, str]] = {}
    cross_arm_units: set[str] = set()
    funnel_statuses: dict[str, int] = {}
    analysis_setup_error = ''
    metric_extractor = None
    try:
        resolved_spec = (
            validate_resolved_spec(experiment_spec)
            if isinstance(experiment_spec, dict)
            else context_cost_spec(
                experiment_id=experiment_id,
                traffic_percent=100,
                treatment_percent=50,
                minimum_sample_size=max(2, int(min_sample_size)),
            )
        )
        if resolved_spec['experimentId'] != experiment_id:
            raise ExperimentContractError(
                'report spec experimentId does not match the requested run')
        min_sample_size = int(
            resolved_spec['analysis']['minimumSampleSizePerArm'])
        metric_extractor = compile_metric_extractor(resolved_spec)
    except (ExperimentContractError, RuntimeError, ValueError) as exc:
        analysis_setup_error = str(exc)
        logger.error('[CostExperiment] report spec unavailable: %s', exc,
                     exc_info=True)
        # Keep descriptive reporting usable while making the decision invalid.
        resolved_spec = context_cost_spec(
            experiment_id=experiment_id,
            traffic_percent=100,
            treatment_percent=50,
            minimum_sample_size=max(2, int(min_sample_size)),
        )
        try:
            metric_extractor = compile_metric_extractor(resolved_spec)
        except (RuntimeError, TypeError, ValueError) as fallback_exc:
            logger.error('[CostExperiment] fallback metrics unavailable: %s',
                         fallback_exc, exc_info=True)

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
            outcome_id = str(
                outcome.get('experimentId')
                or outcome.get('experiment_id')
                or '')
            if outcome_id != experiment_id:
                continue
            completed_at = _safe_int(outcome.get('completedAt')) or row_updated
            if completed_at < cutoff or completed_at > now_ms + 86_400_000:
                continue
            status = str(outcome.get('status') or 'missing')
            funnel_statuses[status] = funnel_statuses.get(status, 0) + 1
            if status != 'assigned':
                continue
            arm_name = outcome.get('arm')
            if arm_name not in arms:
                invalid_rows += 1
                continue

            spec_digest = str(outcome.get('specDigest') or '')
            if spec_digest:
                observed_spec_digests.add(spec_digest)
            if (outcome.get('contractVersion') != 'tofu.experiment/v1'
                    or not spec_digest):
                unversioned_outcomes += 1
            subject_digest = str(outcome.get('subjectDigest') or '').strip()
            exposed_at = _safe_int(outcome.get('exposedAt'))
            exposure_in_lifecycle = bool(
                (not analysis_start_ms or exposed_at >= analysis_start_ms)
                and (not analysis_sealed_ms or exposed_at <= analysis_sealed_ms)
            )
            verified_exposure = bool(
                outcome.get('exposureStatus') == 'applied'
                and re.fullmatch(r'[a-f0-9]{64}', subject_digest)
                and exposed_at > 0
                and exposure_in_lifecycle
            )
            if not verified_exposure:
                unverified_exposures += 1
            if exposed_at > 0 and not exposure_in_lifecycle:
                lifecycle_window_violations += 1

            unit_id = subject_digest or conv_id
            if not unit_id:
                invalid_rows += 1
                continue
            order_key = (
                exposed_at or completed_at,
                completed_at,
                str(outcome.get('taskId') or ''),
                unit_id,
            )
            if (unit_id not in unit_exposure_order
                    or order_key < unit_exposure_order[unit_id]):
                unit_exposure_order[unit_id] = order_key
            previous_arm = unit_arms.setdefault(unit_id, str(arm_name))
            if previous_arm != arm_name:
                cross_arm_units.add(unit_id)

            arm = arms[arm_name]
            arm['_conversation_ids'].add(conv_id)
            arm['_assignment_units'].add(unit_id)
            if _safe_int(outcome.get('completedAt')) <= 0:
                # Running checkpoints and terminal-construction failures retain
                # the assignment record. They belong in the exposure denominator
                # but can never be silently treated as completed observations.
                pending_exposure_units.add(unit_id)
                previous_pending = arm['_pending_order_by_unit'].get(unit_id)
                if previous_pending is None or order_key < previous_pending:
                    arm['_pending_order_by_unit'][unit_id] = order_key
                continue
            arm['turns'] += 1
            metrics = outcome.get('metrics')
            quality = outcome.get('quality')
            if not isinstance(metrics, dict) or not isinstance(quality, dict):
                invalid_rows += 1
                metrics = metrics if isinstance(metrics, dict) else {}
                quality = quality if isinstance(quality, dict) else {}
            extraction_failed = False
            try:
                if metric_extractor is None:
                    raise RuntimeError('metric extraction plan is unavailable')
                extracted = metric_extractor(outcome)
            except (RuntimeError, TypeError, ValueError) as exc:
                extraction_failed = True
                metric_extraction_errors += 1
                logger.warning(
                    '[CostExperiment] metric extraction failed: %s', exc)
                extracted = {}
            cost_usd = extracted.get('tofu.context-cost/cost.usd')
            if cost_usd is None and extraction_failed:
                cost_usd = _safe_number(metrics.get('costUsd'))
            cost_cny = _safe_number(metrics.get('costCny'))
            oracle_passed = extracted.get(
                'tofu.context-cost/quality.oracle_passed')
            latency_value = extracted.get('tofu.context-cost/latency.ms')
            terminal_without_error = extracted.get(
                'tofu.context-cost/health.terminal_without_error')
            if extraction_failed:
                oracle_passed = quality.get('oraclePassed')
                latency_value = _safe_number(outcome.get('latencyMs'))
                terminal_without_error = quality.get('terminalWithoutError')
            public_cost_usd = _optional_number(
                metrics, 'publicPriceCostUsd')
            public_cost_cny = _optional_number(
                metrics, 'publicPriceCostCny')
            actual_cache_write_cost = _optional_number(
                metrics, 'actualCacheWriteCostUsd')
            public_cache_write_cost = _optional_number(
                metrics, 'publicPriceCacheWriteCostUsd')
            first_observation = {
                'orderKey': order_key,
                'costUsd': float(cost_usd) if cost_usd is not None else None,
                'quality': (float(oracle_passed)
                            if isinstance(oracle_passed, bool) else None),
                'latencyMs': (float(latency_value)
                              if latency_value is not None else None),
            }
            existing_first = arm['_first_observation_by_unit'].get(unit_id)
            if (existing_first is None
                    or order_key < existing_first['orderKey']):
                arm['_first_observation_by_unit'][unit_id] = first_observation
            conv_cost = arm['_conversation_costs'].setdefault(
                conv_id, {'turns': 0, 'pricedTurns': 0, 'costUsd': 0.0})
            conv_cost['turns'] += 1
            if cost_usd is None:
                arm['unpricedTurns'] += 1
            else:
                cost_usd = float(cost_usd)
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
            latency = _safe_int(latency_value)
            arm['latencyMs'] += latency
            if latency_value is not None:
                arm['latencySamples'] += 1
                arm['_latencies'].append(latency)
            arm['terminalWithoutError'] += int(terminal_without_error is True)
            if isinstance(oracle_passed, bool):
                arm['oracleEvaluated'] += 1
                if oracle_passed:
                    arm['oraclePassed'] += 1
                if cost_usd is not None:
                    arm['oracleActualPriced'] += 1
                    arm['oracleActualCostUsd'] += cost_usd
                if public_cost_usd is not None:
                    arm['oraclePublicPriced'] += 1
                    arm['oraclePublicPriceCostUsd'] += public_cost_usd
            arm['compactions'] += _safe_int(quality.get('compactions'))
            telemetry = outcome.get('telemetry') or {}
            if not isinstance(telemetry, dict):
                invalid_rows += 1
                telemetry = {}
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

    fixed_horizon = int(
        resolved_spec['analysis']['maximumAssignmentUnits'])
    ordered_units = sorted(
        unit_arms, key=lambda unit_id: unit_exposure_order.get(
            unit_id, (2**63 - 1, 2**63 - 1, '', unit_id)
        )
    )
    fixed_horizon_reached = len(ordered_units) >= fixed_horizon
    analysis_unit_ids = set(ordered_units[:fixed_horizon])

    public_arms: dict[str, dict] = {}
    analysis_arms: dict[str, dict] = {}
    pending_analysis_exposures = 0
    for name, raw in arms.items():
        turns = raw['turns']
        priced = raw['pricedTurns']
        prompt = raw['promptTokens']
        fully_priced = [
            value for value in raw['_conversation_costs'].values()
            if value['turns'] > 0 and value['turns'] == value['pricedTurns']
        ]
        fully_priced_cost = sum(value['costUsd'] for value in fully_priced)
        arm_analysis_unit_ids = sorted(
            unit_id for unit_id in raw['_assignment_units']
            if unit_id in analysis_unit_ids
        )
        analysis_observations = [
            raw['_first_observation_by_unit'][unit_id]
            for unit_id in arm_analysis_unit_ids
            if unit_id in raw['_first_observation_by_unit']
        ]
        pending_analysis_exposures += sum(
            1 for unit_id in arm_analysis_unit_ids
            if unit_id in raw['_pending_order_by_unit']
            and (
                unit_id not in raw['_first_observation_by_unit']
                or raw['_pending_order_by_unit'][unit_id]
                < raw['_first_observation_by_unit'][unit_id]['orderKey']
            )
        )
        fully_priced_units = [
            observation for observation in analysis_observations
            if observation['costUsd'] is not None
        ]
        quality_by_unit = [
            observation['quality'] for observation in analysis_observations
            if observation['quality'] is not None
        ]
        latency_by_unit = [
            observation['latencyMs'] for observation in analysis_observations
            if observation['latencyMs'] is not None
        ]
        assigned_units = len(arm_analysis_unit_ids)
        analysis_arms[name] = {
            'assignedUnits': assigned_units,
            'fullyPricedCosts': [value['costUsd']
                                 for value in fully_priced_units],
            'pricingCoverage': (
                len(fully_priced_units) / assigned_units
                if assigned_units else 0.0),
            'qualityByUnit': quality_by_unit,
            'latencyByUnit': latency_by_unit,
        }
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
               if not key.startswith('_')
               and key not in ('latencyMs', 'terminalWithoutError')},
            'conversations': len(raw['_conversation_ids']),
            'assignedUnits': len(raw['_assignment_units']),
            'analysisUnits': assigned_units,
            'fullyPricedConversations': len(fully_priced),
            'fullyPricedAssignmentUnits': len(fully_priced_units),
            'analysisCostPerFullyPricedAssignmentUnitUsd': (
                round(sum(value['costUsd'] for value in fully_priced_units)
                      / len(fully_priced_units), 6)
                if fully_priced_units else None),
            'assignmentUnitPricingCoverage': _rounded_ratio(
                len(fully_priced_units), assigned_units),
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
    all_observed_control_conv_cost = public_arms['control'][
        'costPerFullyPricedConversationUsd']
    all_observed_optimized_conv_cost = public_arms['optimized'][
        'costPerFullyPricedConversationUsd']
    all_observed_conversation_delta = None
    if (all_observed_control_conv_cost not in (None, 0)
            and all_observed_optimized_conv_cost is not None):
        all_observed_conversation_delta = round(
            (all_observed_optimized_conv_cost - all_observed_control_conv_cost)
            / all_observed_control_conv_cost * 100, 2)
    control_conv_cost = public_arms['control'][
        'analysisCostPerFullyPricedAssignmentUnitUsd']
    optimized_conv_cost = public_arms['optimized'][
        'analysisCostPerFullyPricedAssignmentUnitUsd']
    conversation_delta = None
    if control_conv_cost not in (None, 0) and optimized_conv_cost is not None:
        conversation_delta = round(
            (optimized_conv_cost - control_conv_cost)
            / control_conv_cost * 100, 2)
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

    analysis_payload = {
        'arms': analysis_arms,
        'analysisClosed': bool(analysis_closed),
        'analysisStartVerified': analysis_start_ms > 0,
        'analysisSealVerified': bool(
            analysis_closed and analysis_sealed_ms > 0),
        'analysisSealedAt': analysis_sealed_ms or None,
        'fixedHorizonReached': fixed_horizon_reached,
        'observedAssignmentUnits': len(ordered_units),
        'analyzedAssignmentUnits': len(analysis_unit_ids),
        'truncated': bool(truncated),
        'invalidRows': invalid_rows,
        'observedSpecDigests': sorted(observed_spec_digests),
        'unversionedOutcomes': unversioned_outcomes,
        'unverifiedExposures': unverified_exposures,
        'pendingExposures': pending_analysis_exposures,
        'crossArmUnits': len(cross_arm_units),
        'metricExtractionErrors': metric_extraction_errors,
    }
    analysis_error = analysis_setup_error
    decision: dict
    if not analysis_error:
        try:
            decision = analyze_experiment(resolved_spec, analysis_payload)
        except (RuntimeError, TypeError, ValueError) as exc:
            analysis_error = str(exc)
            logger.error('[CostExperiment] analyzer unavailable: %s', exc,
                         exc_info=True)
    if analysis_error:
        decision = {
            'contractVersion': 'tofu.experiment-decision/v1',
            'status': 'invalid_data',
            'dataValid': False,
            'sampleReady': False,
            'pricingReady': False,
            'qualityReady': False,
            'latencyReady': False,
            'srmReady': False,
            'analysisClosed': bool(analysis_closed),
            'analysisStartVerified': analysis_start_ms > 0,
            'analysisSealVerified': bool(
                analysis_closed and analysis_sealed_ms > 0),
            'fixedHorizonReached': fixed_horizon_reached,
            'maximumAssignmentUnits': fixed_horizon,
            'decisionEligible': False,
            'promotionEligible': False,
            'blockers': ['analyzer_unavailable'],
            'analysisError': analysis_error,
        }
    point_estimate_cheaper = bool(
        conversation_delta is not None and conversation_delta < 0)
    return {
        'contractVersion': 'tofu.experiment-report/v1',
        'experiment_id': experiment_id,
        'experimentId': experiment_id,
        'specDigest': resolved_spec['specDigest'],
        'windowDays': days,
        'assignmentUnit': 'conversation',
        'minSampleSize': int(min_sample_size),
        'maximumAssignmentUnits': fixed_horizon,
        'observedAssignmentUnits': len(ordered_units),
        'analyzedAssignmentUnits': len(analysis_unit_ids),
        'analysisClosed': bool(analysis_closed),
        'analysisStartVerified': analysis_start_ms > 0,
        'analysisSealVerified': bool(
            analysis_closed and analysis_sealed_ms > 0),
        'analysisStartedAt': analysis_start_ms or None,
        'analysisSealedAt': analysis_sealed_ms or None,
        'fixedHorizonReached': fixed_horizon_reached,
        'ready': bool(decision.get('decisionEligible')),
        'sampleReady': bool(decision.get('sampleReady')),
        'pricingReady': bool(decision.get('pricingReady')),
        'qualityReady': bool(decision.get('qualityReady')),
        'latencyReady': bool(decision.get('latencyReady')),
        'promotionEligible': bool(decision.get('promotionEligible')),
        'invalidRows': invalid_rows,
        'truncated': bool(truncated),
        'decision': decision,
        'funnel': {
            'outcomesByStatus': funnel_statuses,
            'unversionedOutcomes': unversioned_outcomes,
            'unverifiedExposures': unverified_exposures,
            'pendingExposures': len(pending_exposure_units),
            'pendingAnalysisExposures': pending_analysis_exposures,
            'lifecycleWindowViolations': lifecycle_window_violations,
            'crossArmUnits': len(cross_arm_units),
            'metricExtractionErrors': metric_extraction_errors,
        },
        'arms': public_arms,
        'comparison': {
            'costPerConversationDeltaPct': conversation_delta,
            'allObservedCostPerConversationDeltaPct': (
                all_observed_conversation_delta),
            'costPerPricedTurnDeltaPct': turn_delta,
            'oraclePassRateDelta': quality_delta,
            'publicPriceCostPerOracleSuccessDeltaPct': success_cost_delta,
            'publicCacheWriteCostPerTurnDeltaPct': cache_write_cost_delta,
            'pointEstimateOptimizedCheaper': point_estimate_cheaper,
            # Compatibility field now means evidence-backed, safe-to-promote.
            'optimizedIsCheaper': bool(decision.get('promotionEligible')),
        },
        'methodology': (
            'The first terminal exposure is the frozen observation for each '
            'assignment unit; deterministic clustered '
            'bootstrap intervals over the precommitted first assignment '
            'horizon and an SRM diagnostic. Enrollment must be irreversibly '
            'sealed before promotion. Provider-reported '
            'usage uses persisted price snapshots; missing prices are never '
            'zero and block promotion. terminalWithoutError is operational '
            'health only; semantic non-inferiority requires oraclePassed. '
            'Truncation, malformed rows, unverified exposure, or mixed spec '
            'digests invalidate the decision.'
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
    'task_outcome_report_rows',
    'validate_cost_experiment_transition',
]
