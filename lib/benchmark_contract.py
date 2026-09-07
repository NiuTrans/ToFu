"""Versioned JSONL contract for reproducible agent benchmark runs.

The file format is append-only: one ``manifest`` record followed by task
records and optional infrastructure-attempt records.  Agent failures are final;
only records classified as ``infrastructure`` are retryable under the manifest's
predeclared limit.
"""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from statistics import NormalDist
from dataclasses import dataclass
from typing import Any, Iterable

from lib.log import get_logger


logger = get_logger(__name__)
CONTRACT_VERSION = 'tofu-benchmark/v1'
CONTRACT_VERSION_V2 = 'tofu-benchmark/v2'
KIMI_K3_PRICE_CARD = {
    'currency': 'USD',
    'inputUsdPerMillion': 2.76,
    'outputUsdPerMillion': 13.81,
    'cacheReadUsdPerMillion': 0.276,
    'cacheReadMultiplier': 0.10,
    'snapshot': 'kimi-k3-2026-08-24',
}
RELEASE_TASK_MATRIX_V2 = {
    ('software_engineering', 'swe-bench-verified'): 500,
    ('software_engineering', 'terminal-bench-2.1'): 89 * 5,
    ('integrated_multi_tool', 'frozen-integrated-tools'): 200,
    ('long_continuity', 'frozen-continuity'): 200,
    ('frozen_research', 'frozen-source-packs'): 200,
    ('long_writing', 'frozen-writing'): 200,
    ('fault_recovery', 'frozen-fault-recovery'): 100,
}
DEFAULT_HARD_BUDGET_USD = 1500.0
DEFAULT_PAUSE_BUDGET_USD = 1200.0
_RECORD_TYPES = frozenset({'manifest', 'task', 'attempt', 'summary'})
_PROMPT_PROFILE_ARM_EXPECTATIONS = {
    'prompt_lean_kimi': 'lean',
    'prompt_ablate_url': 'lean_no_url',
    'prompt_ablate_safety': 'lean_no_safety',
    'prompt_ablate_tools': 'lean_no_tools',
    'prompt_ablate_output': 'lean_no_output',
    'prompt_ablate_autonomy': 'lean_no_autonomy',
    'combined_v2': 'lean',
}
_ORCHESTRATION_EVIDENCE_ARMS = frozenset({
    'orchestration_v2', 'combined_v2',
})


class BenchmarkContractError(ValueError):
    pass


@dataclass(frozen=True)
class BenchmarkRecordV2:
    """Validated immutable wrapper for one v2 JSONL record."""

    value: dict[str, Any]

    def __post_init__(self) -> None:
        detached = json.loads(json.dumps(
            self.value, ensure_ascii=False, allow_nan=False,
            sort_keys=True, default=str))
        validate_record(detached)
        if detached.get('contractVersion') != CONTRACT_VERSION_V2:
            raise BenchmarkContractError('BenchmarkRecordV2 requires v2')
        object.__setattr__(self, 'value', detached)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.value, ensure_ascii=False))


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


def build_manifest_v2(
    *, run_id: str, harness: dict, agent: dict, provider_face: str,
    provider_slot_id: str, thinking: str, experiment_arm: str,
    tool_permissions: dict,
    prompt_digest: str,
    tool_schema_digest: str, dataset_snapshot: dict,
    task_table: Iterable[dict], sandbox: dict, retry_rule: dict,
    artifact_limits: dict,
    timeout_seconds: int, maximum_infrastructure_failure_rate: float,
    price_card: dict | None = None, environment: dict | None = None,
    pair_id: str = '', comparison_role: str = '',
) -> dict:
    """Build a cost-unlimited but shape/time/retry-preregistered v2 manifest."""
    tasks = [dict(row) for row in task_table]
    record = {
        'contractVersion': CONTRACT_VERSION_V2,
        'recordType': 'manifest',
        'runId': str(run_id),
        'createdAt': int(time.time() * 1000),
        'harness': dict(harness),
        'agent': dict(agent),
        'model': 'kimi-k3',
        'providerFace': str(provider_face),
        'providerSlotId': str(provider_slot_id),
        'thinking': str(thinking),
        'experimentArm': str(experiment_arm),
        'pairId': str(pair_id),
        'comparisonRole': str(comparison_role),
        'toolPermissions': dict(tool_permissions),
        'promptDigest': str(prompt_digest),
        'toolSchemaDigest': str(tool_schema_digest),
        'datasetSnapshot': dict(dataset_snapshot),
        'taskIds': [str(row.get('taskId') or '') for row in tasks],
        'tasks': tasks,
        'priceCard': dict(price_card or KIMI_K3_PRICE_CARD),
        'sandbox': dict(sandbox),
        'retryRule': dict(retry_rule),
        'artifactLimits': dict(artifact_limits),
        'limits': {
            'timeoutSeconds': max(1, int(timeout_seconds)),
            'maximumInfrastructureFailureRate': float(
                maximum_infrastructure_failure_rate),
            'costBudgetUsd': None,
            'costBudgetPolicy': 'unlimited_preregistered_shape',
        },
        'comparisonControls': {
            'codexVersion': '0.149.1',
            'codexEphemeral': True,
            'codexIgnoreUserConfig': True,
            'remoteCompactionV2': False,
            'compactEndpointRequestsInvalidateTrial': True,
            'proxyAdditionalModelCalls': 0,
            'formalLatencyUsesCodexFavoredCorrectedValue': True,
        },
        'environment': dict(environment or environment_snapshot()),
    }
    validate_record(record)
    return record


def build_task_record_v2(
    *, run_id: str, dataset: str, family: str, task_id: str,
    agent: dict, provider_face: str, provider_slot_id: str, thinking: str,
    experiment_arm: str,
    oracle: dict, rounds: list, context_blocks: list, tool_schemas: list,
    tool_results: list, compactions: list, call_graph: list, retries: list,
    cost: dict, latency: dict, incidents: list | None = None,
    judges: list | None = None, infrastructure_error: dict | None = None,
    final_output_digest: str = '', environment: dict | None = None,
    orchestration_decisions: list | None = None,
    artifacts: list | None = None,
    completed_at: int | None = None,
) -> dict:
    """Build exhaustive per-task v2 evidence without charging simulators."""
    if completed_at is not None and (
        isinstance(completed_at, bool)
        or not isinstance(completed_at, int)
        or completed_at < 0
    ):
        raise BenchmarkContractError(
            'task completed_at must be a non-negative integer timestamp')
    record = {
        'contractVersion': CONTRACT_VERSION_V2,
        'recordType': 'task',
        'runId': str(run_id),
        'completedAt': (
            int(time.time() * 1000)
            if completed_at is None else int(completed_at)
        ),
        'dataset': str(dataset),
        'family': str(family),
        'taskId': str(task_id),
        'agent': dict(agent),
        'model': 'kimi-k3',
        'providerFace': str(provider_face),
        'providerSlotId': str(provider_slot_id),
        'thinking': str(thinking),
        'experimentArm': str(experiment_arm),
        'oracle': dict(oracle),
        'rounds': list(rounds),
        # v1-compatible alias for readers that only aggregate usage arrays.
        'roundUsage': [dict(row.get('usage') or {}) for row in rounds],
        'contextBlocks': list(context_blocks),
        'toolSchemas': list(tool_schemas),
        'toolResults': list(tool_results),
        'compactions': list(compactions),
        'callGraph': list(call_graph),
        'retries': list(retries),
        'orchestrationDecisions': list(orchestration_decisions or []),
        'cost': {
            **dict(cost),
            'agentCostIncludesFailedAndCompactionCalls': True,
            'simulatorAndJudgeExcluded': True,
        },
        'latency': dict(latency),
        'incidents': list(incidents or []),
        'judges': list(judges or []),
        'artifacts': list(artifacts or []),
        'infrastructureError': (dict(infrastructure_error)
                                if infrastructure_error else None),
        'finalOutputDigest': str(final_output_digest),
    }
    if environment is not None:
        record['environment'] = dict(environment)
    validate_record(record)
    return record


def validate_record(record: Any) -> None:
    if not isinstance(record, dict):
        raise BenchmarkContractError('record must be a JSON object')
    version = record.get('contractVersion')
    if version not in {CONTRACT_VERSION, CONTRACT_VERSION_V2}:
        raise BenchmarkContractError(
            'contractVersion must be tofu-benchmark/v1 or tofu-benchmark/v2')
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
        if version == CONTRACT_VERSION:
            hard = float(limits.get('hardBudgetUsd') or 0)
            pause = float(limits.get('pauseBudgetUsd') or 0)
            if hard <= 0 or pause <= 0 or pause >= hard:
                raise BenchmarkContractError(
                    'budget limits require 0 < pauseBudgetUsd < hardBudgetUsd')
        else:
            _validate_manifest_v2(record)
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
        if version == CONTRACT_VERSION_V2:
            _validate_task_v2(record)


def _required_mapping(record: dict, field: str) -> dict:
    value = record.get(field)
    if not isinstance(value, dict):
        raise BenchmarkContractError(f'{field} must be an object')
    return value


def _sha256(value: Any) -> bool:
    text = str(value or '').lower()
    return len(text) == 64 and all(char in '0123456789abcdef' for char in text)


def validate_release_task_matrix_v2(task_table: Iterable[dict]) -> dict:
    """Reject a claimed full release matrix whose frozen shape drifted."""
    counts: dict[tuple[str, str], int] = {}
    task_ids: set[str] = set()
    for row in task_table:
        if not isinstance(row, dict):
            raise BenchmarkContractError('release task rows must be objects')
        task_id = str(row.get('taskId') or '')
        family = str(row.get('family') or '')
        dataset = str(row.get('dataset') or '')
        if not task_id or not family or not dataset:
            raise BenchmarkContractError(
                'release tasks require taskId, family, and dataset')
        if task_id in task_ids:
            raise BenchmarkContractError(f'duplicate release taskId: {task_id}')
        task_ids.add(task_id)
        key = (family, dataset)
        counts[key] = counts.get(key, 0) + 1
    if counts != RELEASE_TASK_MATRIX_V2:
        raise BenchmarkContractError(
            f'release task matrix mismatch: observed={counts!r}')
    return {
        'contractVersion': CONTRACT_VERSION_V2,
        'tasks': len(task_ids),
        'shape': {f'{family}/{dataset}': count
                  for (family, dataset), count in sorted(counts.items())},
    }


def _validate_manifest_v2(record: dict) -> None:
    for field in ('harness', 'agent', 'datasetSnapshot', 'priceCard',
                  'sandbox', 'retryRule', 'toolPermissions', 'artifactLimits'):
        _required_mapping(record, f'{field}')
    for field in ('providerFace', 'providerSlotId', 'thinking',
                  'experimentArm', 'promptDigest', 'toolSchemaDigest'):
        if not str(record.get(field) or '').strip():
            raise BenchmarkContractError(f'manifest.{field} is required')
    for field in ('promptDigest', 'toolSchemaDigest'):
        if not _sha256(record.get(field)):
            raise BenchmarkContractError(
                f'manifest.{field} must be a SHA-256 digest')
    if record.get('model') != 'kimi-k3':
        raise BenchmarkContractError('v2 comparison model must be kimi-k3')
    if not isinstance(record.get('tasks'), list) or not record['tasks']:
        raise BenchmarkContractError('manifest.tasks must be a non-empty list')
    if any(not isinstance(row, dict) for row in record['tasks']):
        raise BenchmarkContractError('every manifest task must be an object')
    if any(not str(row.get('family') or '')
           or not str(row.get('dataset') or '') for row in record['tasks']):
        raise BenchmarkContractError(
            'every manifest task requires family and dataset')
    task_ids = [str(row.get('taskId') or '') for row in record['tasks']]
    if any(not value for value in task_ids):
        raise BenchmarkContractError('every manifest task requires taskId')
    if len(set(task_ids)) != len(task_ids):
        raise BenchmarkContractError('manifest taskIds must be unique')
    if task_ids != [str(value) for value in record.get('taskIds') or ()]:
        raise BenchmarkContractError('manifest taskIds must match tasks in order')
    harness = record['harness']
    agent = record['agent']
    if not str(harness.get('name') or '') or not str(harness.get('version') or ''):
        raise BenchmarkContractError('v2 harness requires name and version')
    if not (_sha256(harness.get('binarySha256'))
            or _sha256(harness.get('commitSha256'))):
        raise BenchmarkContractError('v2 harness requires a SHA-256 identity')
    if not str(agent.get('name') or '') or not str(agent.get('version') or ''):
        raise BenchmarkContractError('v2 agent requires name and version')
    if not (_sha256(agent.get('binarySha256'))
            or _sha256(agent.get('commitSha256'))):
        raise BenchmarkContractError('v2 agent requires a SHA-256 identity')
    dataset = record['datasetSnapshot']
    if not str(dataset.get('id') or '') or not _sha256(dataset.get('sha256')):
        raise BenchmarkContractError('datasetSnapshot requires id and sha256')
    if dataset.get('diagnosticOnly') is not True and dataset.get('frozen') is not True:
        raise BenchmarkContractError('release datasetSnapshot must be frozen')
    pair_id = str(record.get('pairId') or '')
    comparison_role = str(record.get('comparisonRole') or '')
    if bool(pair_id) != bool(comparison_role):
        raise BenchmarkContractError(
            'v2 pairId and comparisonRole must be supplied together')
    if comparison_role and comparison_role not in {'baseline', 'candidate'}:
        raise BenchmarkContractError(
            'v2 comparisonRole must be baseline or candidate')
    if dataset.get('releaseMatrix') is True and not pair_id:
        raise BenchmarkContractError(
            'release matrix manifests require pairId and comparisonRole')
    if dataset.get('releaseMatrix') is True and comparison_role == 'baseline':
        if agent.get('name') != 'codex' or agent.get('version') != '0.149.1':
            raise BenchmarkContractError(
                'release baseline must identify Codex 0.149.1')
    if record['priceCard'] != KIMI_K3_PRICE_CARD:
        raise BenchmarkContractError('v2 comparison requires the frozen Kimi price card')
    artifact_limits = record['artifactLimits']
    artifact_limit_fields = (
        'maximumArtifactBytes', 'maximumTaskArtifactBytes',
        'maximumRunArtifactBytes',
    )
    artifact_limit_values = []
    for field in artifact_limit_fields:
        value = artifact_limits.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BenchmarkContractError(
                f'artifactLimits.{field} must be a positive integer')
        artifact_limit_values.append(value)
    if artifact_limit_values != sorted(artifact_limit_values):
        raise BenchmarkContractError(
            'artifact limits must satisfy artifact <= task <= run')
    retry = record['retryRule']
    raw_maximum_retries = retry.get('maxInfrastructureRetries')
    try:
        maximum_retries = int(raw_maximum_retries)
    except (TypeError, ValueError) as exc:
        raise BenchmarkContractError(
            'retryRule.maxInfrastructureRetries must be an integer') from exc
    if isinstance(raw_maximum_retries, bool) \
            or not isinstance(raw_maximum_retries, int) \
            or maximum_retries < 0 \
            or retry.get('retryableFailureClasses') != ['infrastructure']:
        raise BenchmarkContractError(
            'only preregistered infrastructure retries are allowed')
    if dataset.get('releaseMatrix') is True:
        validate_release_task_matrix_v2(record['tasks'])
    controls = _required_mapping(record, 'comparisonControls')
    required_controls = {
        'codexVersion': '0.149.1',
        'codexEphemeral': True,
        'codexIgnoreUserConfig': True,
        'remoteCompactionV2': False,
        'compactEndpointRequestsInvalidateTrial': True,
        'proxyAdditionalModelCalls': 0,
        'formalLatencyUsesCodexFavoredCorrectedValue': True,
    }
    if any(controls.get(key) != value
           for key, value in required_controls.items()):
        raise BenchmarkContractError(
            'v2 comparison controls do not match the frozen Codex contract')
    limits = record['limits']
    if limits.get('costBudgetUsd') is not None:
        raise BenchmarkContractError('v2 evaluation cost budget must be unlimited')
    if limits.get('costBudgetPolicy') != 'unlimited_preregistered_shape':
        raise BenchmarkContractError('v2 cost policy must freeze trial shape')
    timeout = limits.get('timeoutSeconds')
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise BenchmarkContractError('v2 timeoutSeconds must be a positive integer')
    try:
        failure_rate = float(
            limits.get('maximumInfrastructureFailureRate', -1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise BenchmarkContractError(
            'maximumInfrastructureFailureRate must be a number') from exc
    if not math.isfinite(failure_rate):
        raise BenchmarkContractError(
            'maximumInfrastructureFailureRate must be finite')
    if not 0 <= failure_rate <= 1:
        raise BenchmarkContractError(
            'maximumInfrastructureFailureRate must be between 0 and 1')


def _validate_task_v2(record: dict) -> None:
    if not str(record.get('family') or ''):
        raise BenchmarkContractError('task.family is required')
    completed_at = record.get('completedAt')
    if isinstance(completed_at, bool) or not isinstance(completed_at, int) \
            or completed_at < 0:
        raise BenchmarkContractError(
            'task.completedAt must be a non-negative integer timestamp')
    for field in ('rounds', 'contextBlocks', 'toolSchemas', 'toolResults',
                  'compactions', 'callGraph', 'retries', 'incidents', 'judges',
                  'orchestrationDecisions', 'artifacts'):
        if not isinstance(record.get(field), list):
            raise BenchmarkContractError(f'task.{field} must be a list')
    agent = _required_mapping(record, 'agent')
    if not str(agent.get('name') or '') or not str(agent.get('version') or ''):
        raise BenchmarkContractError('v2 task agent requires name and version')
    if not (_sha256(agent.get('binarySha256'))
            or _sha256(agent.get('commitSha256'))):
        raise BenchmarkContractError('v2 task agent requires a SHA-256 identity')
    if not str(record.get('providerFace') or '') \
            or not str(record.get('providerSlotId') or '') \
            or not str(record.get('thinking') or ''):
        raise BenchmarkContractError(
            'v2 task providerFace, providerSlotId, and thinking are required')
    _validate_task_prompt_profile_adoption(record)
    _validate_task_orchestration_adoption(record)
    _required_mapping(record, 'cost')
    if any(not isinstance(row, dict) for field in (
            'rounds', 'contextBlocks', 'toolSchemas', 'toolResults',
            'compactions', 'callGraph', 'retries', 'incidents', 'judges',
            'orchestrationDecisions', 'artifacts')
           for row in record[field]):
        raise BenchmarkContractError('v2 task evidence rows must be objects')
    oracle = record['oracle']
    if not str(oracle.get('type') or ''):
        raise BenchmarkContractError('task.oracle.type is required')
    cost = record['cost']
    if cost.get('agentCostIncludesFailedAndCompactionCalls') is not True \
            or cost.get('simulatorAndJudgeExcluded') is not True:
        raise BenchmarkContractError('v2 task cost accounting flags are required')
    try:
        agent_cost = float(cost.get('agentCostUsd'))
    except (TypeError, ValueError, OverflowError) as exc:
        raise BenchmarkContractError('v2 task agentCostUsd is required') from exc
    if not math.isfinite(agent_cost) or agent_cost < 0:
        raise BenchmarkContractError(
            'v2 task agentCostUsd must be finite and non-negative')
    latency = _required_mapping(record, 'latency')
    for field in ('rawWallMs', 'oracleReadyMs', 'queueMs', 'ttftMs',
                  'modelMs', 'toolMs', 'translationCpuMs', 'proxyCpuMs',
                  'codexFavoredCorrectedWallMs'):
        value = float(latency.get(field, -1))
        if not math.isfinite(value) or value < 0:
            raise BenchmarkContractError(f'task.latency.{field} is invalid')
    raw_wall = float(latency['rawWallMs'])
    oracle_ready = float(latency['oracleReadyMs'])
    if abs(raw_wall - oracle_ready) > 1.0:
        raise BenchmarkContractError(
            'task.latency.rawWallMs must measure task start to oracle-ready')
    translation_cpu = float(latency['translationCpuMs'])
    proxy_cpu = float(latency['proxyCpuMs'])
    if translation_cpu > proxy_cpu + 1e-9:
        raise BenchmarkContractError(
            'task.latency.translationCpuMs cannot exceed proxyCpuMs')
    corrected = float(latency['codexFavoredCorrectedWallMs'])
    expected_corrected = max(0.0, raw_wall - translation_cpu)
    if abs(corrected - expected_corrected) > 1.0:
        raise BenchmarkContractError(
            'codexFavoredCorrectedWallMs must subtract translationCpuMs')
    if record.get('model') != 'kimi-k3':
        raise BenchmarkContractError('v2 comparison model must be kimi-k3')


def _validate_task_prompt_profile_adoption(record: dict) -> None:
    """Fail closed when a prompt experiment task lacks model-visible proof."""
    arm = str(record.get('experimentArm') or '')
    expected = _PROMPT_PROFILE_ARM_EXPECTATIONS.get(arm)
    if not expected:
        return
    evidence_rows: list[dict] = []
    for row in record.get('rounds') or []:
        if isinstance(row, dict) and isinstance(row.get('promptProfile'), dict):
            evidence_rows.append(row['promptProfile'])
    for row in record.get('contextBlocks') or []:
        if not isinstance(row, dict):
            continue
        provenance = row.get('provenance') or {}
        if isinstance(provenance, dict) and isinstance(
                provenance.get('promptProfile'), dict):
            evidence_rows.append(provenance['promptProfile'])
    from lib.context_telemetry import prompt_profile_evidence_matches

    if not evidence_rows or not all(
            prompt_profile_evidence_matches(
                evidence, expected_profile=expected, model='kimi-k3')
            for evidence in evidence_rows):
        raise BenchmarkContractError(
            f'v2 task arm {arm} requires applied prompt profile {expected}')


def _validate_task_orchestration_adoption(record: dict) -> None:
    """Require explainable, truth-preserving runtime evidence for v2 arms."""
    arm = str(record.get('experimentArm') or '')
    if arm not in _ORCHESTRATION_EVIDENCE_ARMS:
        return
    decisions = record.get('orchestrationDecisions') or []
    if not decisions:
        raise BenchmarkContractError(
            f'v2 task arm {arm} requires orchestration decision evidence')
    from lib.orchestration_adoption import (
        validate_public_orchestration_decision)
    for index, decision in enumerate(decisions):
        try:
            validate_public_orchestration_decision(decision)
        except (TypeError, ValueError) as exc:
            raise BenchmarkContractError(
                f'v2 task orchestration decision {index} is invalid: {exc}'
            ) from exc


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
                            confidence: float = 0.95,
                            noninferiority_margin: float = 0.0) -> dict:
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
    margin = float(noninferiority_margin)
    if not math.isfinite(margin) or margin < 0 or margin > 1:
        raise BenchmarkContractError(
            'noninferiority_margin must be between 0 and 1')
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
    noninferiority_established = lower >= -margin
    return {
        'pairs': count,
        'candidateResolved': sum(candidate_values),
        'baselineResolved': sum(baseline_values),
        'difference': round(estimate, 6),
        'oneSidedConfidence': float(confidence),
        'lowerBound': round(lower, 6),
        'noninferiorityMargin': margin,
        'candidateOnlyWins': candidate_only,
        'baselineOnlyWins': baseline_only,
        'intervalMethod': 'paired_bonferroni_wilson',
        'observedTieOrBetter': estimate >= 0,
        'nonInferiorityEstablished': noninferiority_established,
        'conclusion': (
            'quality_noninferior' if noninferiority_established else
            'observed_tie_not_statistically_established' if estimate >= 0 else
            'candidate_lower'),
    }


def acceptance_decision(*, candidate_oracles: Iterable[bool],
                        baseline_oracles: Iterable[bool],
                        candidate_public_cost_usd: float,
                        baseline_public_cost_usd: float,
                        candidate_p90_latency_ms: float,
                        baseline_p90_latency_ms: float,
                        quality_noninferiority_margin: float = 0.05) -> dict:
    """Evaluate the frozen confirmation gates without weakening quality."""
    numeric_inputs = {
        'candidate_public_cost_usd': candidate_public_cost_usd,
        'baseline_public_cost_usd': baseline_public_cost_usd,
        'candidate_p90_latency_ms': candidate_p90_latency_ms,
        'baseline_p90_latency_ms': baseline_p90_latency_ms,
    }
    for field, raw in numeric_inputs.items():
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BenchmarkContractError(f'{field} must be a number') from exc
        if not math.isfinite(value) or value < 0:
            raise BenchmarkContractError(
                f'{field} must be a finite non-negative number')
    quality = paired_quality_interval(
        candidate_oracles,
        baseline_oracles,
        noninferiority_margin=quality_noninferiority_margin,
    )
    candidate_successes = quality['candidateResolved']
    baseline_successes = quality['baselineResolved']
    candidate_cost_per_success = (
        float(candidate_public_cost_usd) / candidate_successes
        if candidate_successes else None)
    baseline_cost_per_success = (
        float(baseline_public_cost_usd) / baseline_successes
        if baseline_successes else None)
    observed_quality_gate = candidate_successes >= baseline_successes
    quality_gate = bool(quality['nonInferiorityEstablished'])
    cost_gate = bool(
        candidate_cost_per_success is not None
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
            'resolvedNotLower': observed_quality_gate,
            'qualityNoninferiorityEstablished': quality_gate,
            'costPerSuccessNotHigher': cost_gate,
            'p90LatencyWithin20Percent': latency_gate,
        },
        'releaseEligible': quality_gate and cost_gate and latency_gate,
    }


def acceptance_decision_v2(
    *, candidate_by_family: dict[str, Iterable[bool]],
    baseline_by_family: dict[str, Iterable[bool]],
    task_table: Iterable[dict],
    candidate_agent_cost_usd: float, baseline_agent_cost_usd: float,
    candidate_p90_oracle_ready_ms: float, baseline_p90_oracle_ready_ms: float,
    candidate_critical_incidents: int,
    judge_passes: dict[str, bool],
    infrastructure_failure_rate: float,
    maximum_infrastructure_failure_rate: float,
    candidate_orchestration_adoption: dict,
) -> dict:
    """Evaluate the frozen v2 Codex comparison gates without averaging harm."""
    release_tasks = [dict(row) for row in task_table]
    validate_release_task_matrix_v2(release_tasks)
    expected_family_counts: dict[str, int] = {}
    for row in release_tasks:
        family = str(row['family'])
        expected_family_counts[family] = expected_family_counts.get(family, 0) + 1
    numeric = {
        'candidate_agent_cost_usd': candidate_agent_cost_usd,
        'baseline_agent_cost_usd': baseline_agent_cost_usd,
        'candidate_p90_oracle_ready_ms': candidate_p90_oracle_ready_ms,
        'baseline_p90_oracle_ready_ms': baseline_p90_oracle_ready_ms,
        'infrastructure_failure_rate': infrastructure_failure_rate,
        'maximum_infrastructure_failure_rate': maximum_infrastructure_failure_rate,
    }
    for field, raw in numeric.items():
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BenchmarkContractError(f'{field} must be a number') from exc
        if not math.isfinite(value) or value < 0:
            raise BenchmarkContractError(
                f'{field} must be a finite non-negative number')
    if float(infrastructure_failure_rate) > 1 \
            or float(maximum_infrastructure_failure_rate) > 1:
        raise BenchmarkContractError('infrastructure failure rates must be <= 1')
    if int(candidate_critical_incidents) < 0:
        raise BenchmarkContractError('candidate_critical_incidents must be non-negative')
    if set(candidate_by_family) != set(baseline_by_family) \
            or not candidate_by_family:
        raise BenchmarkContractError(
            'candidate and baseline must contain the same non-empty families')
    if set(candidate_by_family) != set(expected_family_counts):
        raise BenchmarkContractError(
            'release outcomes must contain the preregistered task families')
    candidate_all: list[bool] = []
    baseline_all: list[bool] = []
    family_results: dict[str, Any] = {}
    for family in sorted(candidate_by_family):
        candidate = [bool(value) for value in candidate_by_family[family]]
        baseline = [bool(value) for value in baseline_by_family[family]]
        if len(candidate) != len(baseline) or not candidate:
            raise BenchmarkContractError(
                f'family {family!r} requires paired non-empty outcomes')
        if len(candidate) != expected_family_counts[family]:
            raise BenchmarkContractError(
                f'family {family!r} outcome count does not match release matrix')
        candidate_rate = sum(candidate) / len(candidate)
        baseline_rate = sum(baseline) / len(baseline)
        regression = baseline_rate - candidate_rate
        family_results[family] = {
            'pairs': len(candidate),
            'candidateRate': round(candidate_rate, 6),
            'baselineRate': round(baseline_rate, 6),
            'regression': round(regression, 6),
            'withinFivePercentagePoints': regression <= 0.05 + 1e-12,
        }
        candidate_all.extend(candidate)
        baseline_all.extend(baseline)
    quality = paired_quality_interval(
        candidate_all, baseline_all, confidence=0.95,
        noninferiority_margin=0.03)
    candidate_successes = sum(candidate_all)
    baseline_successes = sum(baseline_all)
    candidate_cost_per_success = (
        float(candidate_agent_cost_usd) / candidate_successes
        if candidate_successes else None)
    baseline_cost_per_success = (
        float(baseline_agent_cost_usd) / baseline_successes
        if baseline_successes else None)
    cost_ratio = (
        candidate_cost_per_success / baseline_cost_per_success
        if candidate_cost_per_success is not None
        and baseline_cost_per_success not in (None, 0) else None)
    latency_ratio = (
        float(candidate_p90_oracle_ready_ms)
        / float(baseline_p90_oracle_ready_ms)
        if float(baseline_p90_oracle_ready_ms) > 0 else None)
    required_judges = {'claude-opus-5', 'glm-5.3'}
    judges_gate = required_judges.issubset(judge_passes) and all(
        bool(judge_passes[name]) for name in required_judges)
    adoption = candidate_orchestration_adoption
    if not isinstance(adoption, dict) or adoption.get(
            'contractVersion') != 'tofu.orchestration-adoption-summary/v1':
        raise BenchmarkContractError(
            'candidate_orchestration_adoption must be a v1 summary')
    adoption_numeric: dict[str, int] = {}
    for field in ('taskRecords', 'tasksWithV2Decisions', 'v2Decisions',
                  'programTrajectories', 'agentTrajectories',
                  'falseAdoptionClaims'):
        raw = adoption.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise BenchmarkContractError(
                f'candidate_orchestration_adoption.{field} is invalid')
        adoption_numeric[field] = raw
    orchestration_gate = bool(
        adoption_numeric['taskRecords'] == len(release_tasks)
        and adoption_numeric['tasksWithV2Decisions'] == len(release_tasks)
        and adoption_numeric['v2Decisions'] > 0
        and adoption_numeric['programTrajectories'] > 0
        and adoption_numeric['agentTrajectories'] > 0
        and adoption_numeric['falseAdoptionClaims'] == 0)
    gates = {
        'qualityPointNotLower': quality['difference'] >= 0,
        'qualityLowerBoundAtLeastMinusThreePoints': (
            quality['lowerBound'] >= -0.03),
        'allFamiliesWithinFivePoints': all(
            row['withinFivePercentagePoints']
            for row in family_results.values()),
        'costPerSuccessAtMostEightyFivePercent': (
            cost_ratio is not None and cost_ratio <= 0.85),
        'p90OracleReadyAtMostEightyFivePercent': (
            latency_ratio is not None and latency_ratio <= 0.85),
        'zeroCriticalIncidents': int(candidate_critical_incidents) == 0,
        'bothBlindJudgesPass': judges_gate,
        'infrastructureFailureRateWithinPreregisteredLimit': (
            float(infrastructure_failure_rate)
            <= float(maximum_infrastructure_failure_rate)),
        # A wire-projected gateway is not adoption.  The full frozen run must
        # contain both a real program and a real agent trajectory before a
        # combined release can claim orchestration_v2 was enabled.
        'orchestrationActualAdoptionProven': orchestration_gate,
    }
    eligible = all(gates.values())
    return {
        'contractVersion': CONTRACT_VERSION_V2,
        'quality': quality,
        'families': family_results,
        'candidateAgentCostPerSuccessUsd': candidate_cost_per_success,
        'baselineAgentCostPerSuccessUsd': baseline_cost_per_success,
        'costPerSuccessRatio': cost_ratio,
        'p90OracleReadyRatio': latency_ratio,
        'judges': dict(judge_passes),
        'orchestrationAdoption': dict(adoption),
        'gates': gates,
        'releaseEligible': eligible,
        'claim': ('quality/cost/latency gates passed against Codex'
                  if eligible else
                  'not demonstrated; inspect failed gates and families'),
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
    'BenchmarkContractError', 'BenchmarkJsonlWriter', 'BenchmarkRecordV2',
    'CONTRACT_VERSION', 'CONTRACT_VERSION_V2', 'KIMI_K3_PRICE_CARD',
    'RELEASE_TASK_MATRIX_V2',
    'DEFAULT_HARD_BUDGET_USD', 'DEFAULT_PAUSE_BUDGET_USD', 'budget_status',
    'acceptance_decision', 'acceptance_decision_v2',
    'build_manifest', 'build_manifest_v2', 'build_task_record',
    'build_task_record_v2',
    'environment_snapshot',
    'infrastructure_retry_allowed', 'read_jsonl', 'validate_record',
    'validate_release_task_matrix_v2',
    'paired_quality_interval', 'public_price_cost_from_usage',
]
