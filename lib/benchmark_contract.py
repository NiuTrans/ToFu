"""Versioned JSONL contract for reproducible agent benchmark runs.

The file format is append-only: one ``manifest`` record followed by task
records and optional infrastructure-attempt records.  Agent failures are final;
only records classified as ``infrastructure`` are retryable under the manifest's
predeclared limit.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable

from lib.log import get_logger


logger = get_logger(__name__)
CONTRACT_VERSION = 'tofu-benchmark/v1'
DEFAULT_HARD_BUDGET_USD = 1500.0
DEFAULT_PAUSE_BUDGET_USD = 1200.0
_RECORD_TYPES = frozenset({'manifest', 'task', 'attempt', 'summary'})


class BenchmarkContractError(ValueError):
    pass


def environment_snapshot(*, cwd: str | None = None,
                         extra: dict | None = None) -> dict:
    """Return a credential-free, reproducibility-oriented environment view."""
    root = os.path.abspath(cwd or os.getcwd())
    snapshot = {
        'capturedAt': int(time.time() * 1000),
        'cwd': root,
        'python': sys.version.split()[0],
        'platform': platform.platform(),
        'machine': platform.machine(),
    }
    try:
        snapshot['gitCommit'] = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=5).strip()
        snapshot['gitDirty'] = bool(subprocess.check_output(
            ['git', 'status', '--porcelain'], cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=5).strip())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug('[Benchmark] Git environment snapshot unavailable: %s', exc)
        snapshot['gitCommit'] = ''
        snapshot['gitDirty'] = None
    if extra:
        snapshot['extra'] = dict(extra)
    return snapshot


def build_manifest(*, run_id: str, dataset: str, tasks: Iterable[str],
                   agent: str, agent_version: str, model: str, effort: str,
                   experiment_arm: str, timeout_seconds: int,
                   network_policy: str, max_infra_retries: int = 1,
                   single_agent: bool = True,
                   hard_budget_usd: float = DEFAULT_HARD_BUDGET_USD,
                   pause_budget_usd: float = DEFAULT_PAUSE_BUDGET_USD,
                   environment: dict | None = None) -> dict:
    record = {
        'contractVersion': CONTRACT_VERSION,
        'recordType': 'manifest',
        'runId': str(run_id),
        'createdAt': int(time.time() * 1000),
        'dataset': str(dataset),
        'taskIds': [str(task_id) for task_id in tasks],
        'agent': {'name': str(agent), 'version': str(agent_version)},
        'model': str(model),
        'effort': str(effort),
        'experimentArm': str(experiment_arm),
        'limits': {
            'timeoutSeconds': max(1, int(timeout_seconds)),
            'networkPolicy': str(network_policy),
            'singleAgent': bool(single_agent),
            'maxInfrastructureRetries': max(0, int(max_infra_retries)),
            'hardBudgetUsd': float(hard_budget_usd),
            'pauseBudgetUsd': float(pause_budget_usd),
        },
        'environment': dict(environment or environment_snapshot()),
    }
    validate_record(record)
    return record


def build_task_record(*, run_id: str, dataset: str, task_id: str,
                      agent: str, agent_version: str, model: str,
                      effort: str, experiment_arm: str,
                      oracle_passed: bool | None, oracle_type: str,
                      final_patch: str, test_result: dict,
                      round_usage: list, prefix_fingerprints: list,
                      cost: dict, latency_ms: int,
                      infrastructure_error: dict | None = None,
                      environment: dict | None = None,
                      context_telemetry: dict | None = None,
                      compactions: list | None = None,
                      artifacts: list | None = None) -> dict:
    record = {
        'contractVersion': CONTRACT_VERSION,
        'recordType': 'task',
        'runId': str(run_id),
        'completedAt': int(time.time() * 1000),
        'dataset': str(dataset),
        'taskId': str(task_id),
        'agent': {'name': str(agent), 'version': str(agent_version)},
        'model': str(model),
        'effort': str(effort),
        'experimentArm': str(experiment_arm),
        'oracle': {'passed': oracle_passed, 'type': str(oracle_type)},
        'finalPatch': str(final_patch or ''),
        'tests': dict(test_result or {}),
        'roundUsage': list(round_usage or []),
        'prefixFingerprints': list(prefix_fingerprints or []),
        'cost': dict(cost or {}),
        'latencyMs': max(0, int(latency_ms or 0)),
        'infrastructureError': (dict(infrastructure_error)
                                if infrastructure_error else None),
        'contextTelemetry': dict(context_telemetry or {}),
        'compactions': list(compactions or []),
        'artifacts': list(artifacts or []),
    }
    if environment is not None:
        record['environment'] = dict(environment)
    validate_record(record)
    return record


def validate_record(record: Any) -> None:
    if not isinstance(record, dict):
        raise BenchmarkContractError('record must be a JSON object')
    if record.get('contractVersion') != CONTRACT_VERSION:
        raise BenchmarkContractError(
            f'contractVersion must be {CONTRACT_VERSION}')
    kind = record.get('recordType')
    if kind not in _RECORD_TYPES:
        raise BenchmarkContractError(
            f'recordType must be one of: {", ".join(sorted(_RECORD_TYPES))}')
    for field in ('runId',):
        if not str(record.get(field) or '').strip():
            raise BenchmarkContractError(f'{field} is required')
    if kind == 'manifest':
        if not isinstance(record.get('taskIds'), list):
            raise BenchmarkContractError('manifest.taskIds must be a list')
        limits = record.get('limits')
        if not isinstance(limits, dict):
            raise BenchmarkContractError('manifest.limits must be an object')
        hard = float(limits.get('hardBudgetUsd') or 0)
        pause = float(limits.get('pauseBudgetUsd') or 0)
        if hard <= 0 or pause <= 0 or pause >= hard:
            raise BenchmarkContractError(
                'budget limits require 0 < pauseBudgetUsd < hardBudgetUsd')
    elif kind == 'task':
        for field in ('dataset', 'taskId', 'model', 'experimentArm'):
            if not str(record.get(field) or '').strip():
                raise BenchmarkContractError(f'task.{field} is required')
        oracle = record.get('oracle')
        if not isinstance(oracle, dict) or oracle.get('passed') not in (
                True, False, None):
            raise BenchmarkContractError(
                'task.oracle.passed must be true, false, or null')
        if not isinstance(record.get('roundUsage'), list):
            raise BenchmarkContractError('task.roundUsage must be a list')


def infrastructure_retry_allowed(*, failure_class: str, attempt: int,
                                 max_infra_retries: int) -> bool:
    """Enforce the fixed retry rule: infra failures only, never agent failures."""
    return (str(failure_class).lower() == 'infrastructure'
            and int(attempt) <= int(max_infra_retries))


def public_price_cost_from_usage(usage: dict | None, pricing: dict) -> dict:
    """Apply one frozen public price card to provider-reported usage.

    Rates are USD per million tokens. ``outputTokens`` is assumed to include
    reasoning tokens, matching the Responses API billing representation; the
    reasoning count is still returned separately for observability.
    """
    from lib.cost import normalize_usage, split_input_tokens

    usage = usage if isinstance(usage, dict) else {}
    normalized = normalize_usage(usage)
    uncached, total_input = split_input_tokens(usage)

    def rate(*keys: str) -> float:
        for key in keys:
            if key in pricing:
                return max(0.0, float(pricing[key] or 0))
        return 0.0

    components = {
        'uncachedInputCostUsd': (
            uncached * rate('uncachedInputUsdPerMillion',
                            'inputUsdPerMillion') / 1_000_000),
        'cacheReadCostUsd': (
            normalized['cache_read']
            * rate('cacheReadUsdPerMillion') / 1_000_000),
        'cacheWriteCostUsd': (
            normalized['cache_write']
            * rate('cacheWriteUsdPerMillion') / 1_000_000),
        'outputCostUsd': (
            normalized['output']
            * rate('outputUsdPerMillion') / 1_000_000),
    }
    return {
        'costUsd': round(sum(components.values()), 9),
        **{key: round(value, 9) for key, value in components.items()},
        'promptTokens': int(total_input),
        'uncachedInputTokens': int(uncached),
        'cacheReadTokens': int(normalized['cache_read']),
        'cacheWriteTokens': int(normalized['cache_write']),
        'outputTokens': int(normalized['output']),
        'reasoningTokens': int(normalized['thinking']),
        'pricingSnapshot': dict(pricing),
    }


def paired_quality_interval(candidate: Iterable[bool],
                            baseline: Iterable[bool], *,
                            confidence: float = 0.95) -> dict:
    """Conservative one-sided score interval for paired resolved difference.

    Candidate-only wins and baseline-only wins are multinomial marginals.
    Bonferroni-combined one-sided Wilson bounds give a meaningful lower bound
    even when every observed pair ties; a naive paired-normal interval would
    have zero variance and make an unjustified population-level claim.
    """
    candidate_values = [bool(value) for value in candidate]
    baseline_values = [bool(value) for value in baseline]
    if len(candidate_values) != len(baseline_values) or not candidate_values:
        raise BenchmarkContractError(
            'paired quality vectors must have the same non-zero length')
    if not 0.5 < float(confidence) < 1:
        raise BenchmarkContractError('confidence must be between 0.5 and 1')
    differences = [int(left) - int(right)
                   for left, right in zip(candidate_values, baseline_values)]
    count = len(differences)
    estimate = sum(differences) / count
    alpha = 1.0 - float(confidence)
    z_value = NormalDist().inv_cdf(1.0 - alpha / 2.0)

    def wilson_bounds(successes: int) -> tuple[float, float]:
        proportion = successes / count
        z_squared = z_value ** 2
        denominator = 1.0 + z_squared / count
        center = (proportion + z_squared / (2.0 * count)) / denominator
        half_width = (z_value * (
            proportion * (1.0 - proportion) / count
            + z_squared / (4.0 * count ** 2)) ** 0.5 / denominator)
        return max(0.0, center - half_width), min(1.0, center + half_width)

    candidate_only = sum(value == 1 for value in differences)
    baseline_only = sum(value == -1 for value in differences)
    candidate_lower, _candidate_upper = wilson_bounds(candidate_only)
    _baseline_lower, baseline_upper = wilson_bounds(baseline_only)
    lower = max(-1.0, candidate_lower - baseline_upper)
    return {
        'pairs': count,
        'candidateResolved': sum(candidate_values),
        'baselineResolved': sum(baseline_values),
        'difference': round(estimate, 6),
        'oneSidedConfidence': float(confidence),
        'lowerBound': round(lower, 6),
        'candidateOnlyWins': candidate_only,
        'baselineOnlyWins': baseline_only,
        'intervalMethod': 'paired_bonferroni_wilson',
        'observedTieOrBetter': estimate >= 0,
        'nonInferiorityEstablished': lower >= 0,
        'conclusion': (
            'quality_noninferior' if lower >= 0 else
            'observed_tie_not_statistically_established' if estimate >= 0 else
            'candidate_lower'),
    }


def acceptance_decision(*, candidate_oracles: Iterable[bool],
                        baseline_oracles: Iterable[bool],
                        candidate_public_cost_usd: float,
                        baseline_public_cost_usd: float,
                        candidate_p90_latency_ms: float,
                        baseline_p90_latency_ms: float) -> dict:
    """Evaluate the frozen confirmation gates without weakening quality."""
    quality = paired_quality_interval(candidate_oracles, baseline_oracles)
    candidate_successes = quality['candidateResolved']
    baseline_successes = quality['baselineResolved']
    candidate_cost_per_success = (
        float(candidate_public_cost_usd) / candidate_successes
        if candidate_successes else None)
    baseline_cost_per_success = (
        float(baseline_public_cost_usd) / baseline_successes
        if baseline_successes else None)
    quality_gate = candidate_successes >= baseline_successes
    cost_gate = bool(
        quality_gate and candidate_cost_per_success is not None
        and baseline_cost_per_success is not None
        and candidate_cost_per_success <= baseline_cost_per_success)
    latency_ratio = (
        float(candidate_p90_latency_ms) / float(baseline_p90_latency_ms)
        if float(baseline_p90_latency_ms) > 0 else None)
    latency_gate = bool(latency_ratio is not None and latency_ratio <= 1.20)
    return {
        'quality': quality,
        'candidatePublicPriceCostPerSuccessUsd': candidate_cost_per_success,
        'baselinePublicPriceCostPerSuccessUsd': baseline_cost_per_success,
        'p90LatencyRatio': latency_ratio,
        'gates': {
            'resolvedNotLower': quality_gate,
            'costPerSuccessNotHigher': cost_gate,
            'p90LatencyWithin20Percent': latency_gate,
        },
        'releaseEligible': quality_gate and cost_gate and latency_gate,
    }


def budget_status(spent_usd: float, *, predicted_remaining_usd: float = 0,
                  hard_budget_usd: float = DEFAULT_HARD_BUDGET_USD,
                  pause_budget_usd: float = DEFAULT_PAUSE_BUDGET_USD) -> dict:
    spent = max(0.0, float(spent_usd or 0))
    predicted = max(0.0, float(predicted_remaining_usd or 0))
    hard = float(hard_budget_usd)
    pause = float(pause_budget_usd)
    if hard <= 0 or pause <= 0 or pause >= hard:
        raise BenchmarkContractError(
            'budget limits require 0 < pause_budget_usd < hard_budget_usd')
    if spent >= hard:
        action = 'stop'
    elif spent >= pause or spent + predicted > hard:
        action = 'pause_and_reforecast'
    else:
        action = 'continue'
    return {
        'spentUsd': spent,
        'predictedRemainingUsd': predicted,
        'projectedTotalUsd': spent + predicted,
        'pauseBudgetUsd': pause,
        'hardBudgetUsd': hard,
        'action': action,
        'mayStartNewTask': action == 'continue',
    }


class BenchmarkJsonlWriter:
    """Thread-safe, fsync-backed append writer for benchmark evidence."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        validate_record(record)
        payload = json.dumps(record, ensure_ascii=False,
                             separators=(',', ':'), sort_keys=True) + '\n'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            existing_run_id = None
            has_records = self.path.exists() and self.path.stat().st_size > 0
            if has_records:
                try:
                    with self.path.open('r', encoding='utf-8') as existing:
                        first = json.loads(existing.readline())
                    validate_record(first)
                except (json.JSONDecodeError, BenchmarkContractError) as exc:
                    raise BenchmarkContractError(
                        f'existing JSONL manifest is invalid: {exc}') from exc
                if first.get('recordType') != 'manifest':
                    raise BenchmarkContractError(
                        'first JSONL record must be a manifest')
                existing_run_id = first.get('runId')
            if not has_records and record.get('recordType') != 'manifest':
                raise BenchmarkContractError(
                    'first JSONL record must be a manifest')
            if has_records and record.get('recordType') == 'manifest':
                raise BenchmarkContractError(
                    'a JSONL run may contain only one manifest')
            if existing_run_id and record.get('runId') != existing_run_id:
                raise BenchmarkContractError(
                    'record.runId must match the manifest runId')
            with self.path.open('a', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())


def read_jsonl(path: str | os.PathLike[str]) -> list[dict]:
    records = []
    with Path(path).open('r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                validate_record(record)
            except (json.JSONDecodeError, BenchmarkContractError) as exc:
                raise BenchmarkContractError(
                    f'invalid JSONL record at line {line_number}: {exc}') from exc
            records.append(record)
    return records


__all__ = [
    'BenchmarkContractError', 'BenchmarkJsonlWriter', 'CONTRACT_VERSION',
    'DEFAULT_HARD_BUDGET_USD', 'DEFAULT_PAUSE_BUDGET_USD', 'budget_status',
    'acceptance_decision', 'build_manifest', 'build_task_record',
    'environment_snapshot',
    'infrastructure_retry_allowed', 'read_jsonl', 'validate_record',
    'paired_quality_interval', 'public_price_cost_from_usage',
]
