"""Command-line runner for the frozen context-efficiency benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from lib.benchmark_contract import (
    BenchmarkJsonlWriter,
    budget_status,
    build_manifest,
    build_task_record,
    environment_snapshot,
    infrastructure_retry_allowed,
    public_price_cost_from_usage,
    read_jsonl,
)

from .dataset import (
    DATASET_ID,
    SPLIT_SIZES,
    BenchmarkTask,
    load_manifest_tasks,
    write_frozen_manifest,
)
from .runtime import (
    BENCHMARK_MODEL_ID,
    BENCHMARK_PROVIDER_ID,
    BENCHMARK_PUBLIC_PRICING,
    CODEX_PACKAGE,
    PROJECT_ROOT,
    TOFU_SOURCE,
    EvaluationOutcome,
    InferenceOutcome,
    arm_config,
    ensure_container,
    evaluate_patch,
    run_codex,
    run_tofu,
    validate_runtime,
)


_PRINT_LOCK = threading.Lock()
_SUBSCRIPTION_PROVIDERS = frozenset({
    'oauth_codex',
    'chatgpt_codex_subscription',
})


def _print_event(payload: dict) -> None:
    with _PRINT_LOCK:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _version(command: list[str], fallback: str) -> str:
    try:
        output = subprocess.check_output(
            command, text=True, stderr=subprocess.STDOUT, timeout=10)
        return output.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        return fallback


def _tofu_version() -> str:
    try:
        from lib.version import __version__
        return str(__version__)
    except (ImportError, AttributeError):
        return 'unknown'


def _source_fingerprint(candidate_path: Path | None = None) -> str:
    digest = hashlib.sha256()
    # Model/provider identity is part of the treatment.  Without it, changing
    # benchmark egress could silently resume an incompatible JSONL run.
    digest.update(
        f'model={BENCHMARK_MODEL_ID}\nprovider={BENCHMARK_PROVIDER_ID}\n'.encode())
    roots = [Path(__file__).parent]
    explicit = [
        PROJECT_ROOT / 'lib' / 'benchmark_contract.py',
        TOFU_SOURCE / 'lib' / 'context_experiment_flags.py',
        TOFU_SOURCE / 'lib' / 'orchestration_adoption.py',
        TOFU_SOURCE / 'lib' / 'llm' / '_sse_core.py',
        TOFU_SOURCE / 'lib' / 'tools' / 'gateway.py',
        TOFU_SOURCE / 'lib' / 'tasks_pkg' / 'programmatic_escalation.py',
        TOFU_SOURCE / 'lib' / 'tasks_pkg' / 'tool_orchestration_policy.py',
        TOFU_SOURCE / 'lib' / 'tasks_pkg' / 'orchestrator'
        / '_round_request_prep.py',
        TOFU_SOURCE / 'lib' / 'tasks_pkg' / 'orchestrator'
        / '_tool_loop_breaker.py',
        TOFU_SOURCE / 'routes' / 'api_v1' / 'agent_run.py',
        TOFU_SOURCE / 'routes' / 'api_v1' / 'chat_direct.py',
        TOFU_SOURCE / 'static' / 'vite' / 'manifest.json',
    ]
    paths = []
    for root in roots:
        paths.extend(sorted(root.glob('*.py')))
    paths.extend(path for path in explicit if path.exists())
    if candidate_path is not None:
        paths.append(candidate_path)
    for path in paths:
        try:
            identity = path.relative_to(PROJECT_ROOT)
        except ValueError:
            identity = Path('tofu-source') / path.relative_to(TOFU_SOURCE)
        digest.update(str(identity).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_id(dataset_sha: str, stage: str, arm: str,
            candidate_path: Path | None) -> str:
    source_sha = _source_fingerprint(candidate_path)
    return f'ctxeff-v1-{dataset_sha[:12]}-{source_sha[:12]}-{stage}-{arm}'


def _agent_metadata(arm: str) -> tuple[str, str]:
    if arm.startswith('codex-'):
        binary = CODEX_PACKAGE / 'bin' / 'codex'
        return 'Codex', _version([str(binary), '--version'], 'codex-unknown')
    return 'Tofu', _tofu_version()


def _arm_effort(arm: str, candidate_path: Path | None = None) -> str:
    if arm.startswith('codex-'):
        return 'xhigh'
    try:
        return str(arm_config(arm, candidate_path).get(
            'thinkingDepth') or 'xhigh')
    except (OSError, ValueError, TypeError):
        return 'xhigh'


def _aggregate_usage(outcome: InferenceOutcome) -> dict:
    rows = [row.get('usage') or {} for row in outcome.round_usage
            if isinstance(row, dict)]
    if not rows:
        rows = [outcome.usage or {}]
    totals = {
        'prompt_tokens': 0,
        'completion_tokens': 0,
        'cache_read_tokens': 0,
        'cache_write_tokens': 0,
        'reasoning_tokens': 0,
    }
    for usage in rows:
        prompt_details = usage.get('prompt_tokens_details') or {}
        completion_details = usage.get('completion_tokens_details') or {}
        totals['prompt_tokens'] += int(
            usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
        totals['completion_tokens'] += int(
            usage.get('completion_tokens') or usage.get('output_tokens') or 0)
        totals['cache_read_tokens'] += int(
            usage.get('cache_read_tokens')
            or prompt_details.get('cached_tokens') or 0)
        totals['cache_write_tokens'] += int(
            usage.get('cache_write_tokens') or 0)
        totals['reasoning_tokens'] += int(
            usage.get('reasoning_tokens')
            or completion_details.get('reasoning_tokens') or 0)
    totals['total_tokens'] = (
        totals['prompt_tokens'] + totals['completion_tokens'])
    return totals


def _provider_estimate(outcome: InferenceOutcome) -> float:
    total = 0.0
    for row in outcome.round_usage:
        if not isinstance(row, dict):
            continue
        cost = row.get('cost') or {}
        total += float(cost.get('costUsd') or cost.get('cost_usd') or 0)
    return round(total, 9)


def _cost_record(outcome: InferenceOutcome) -> dict:
    usage = _aggregate_usage(outcome)
    public = public_price_cost_from_usage(
        usage, BENCHMARK_PUBLIC_PRICING)
    provider_estimate = _provider_estimate(outcome)
    subscription = outcome.provider_id in _SUBSCRIPTION_PROVIDERS
    actual = 0.0 if subscription else provider_estimate
    return {
        'billingMode': 'subscription' if subscription else 'api_or_unknown',
        'actualCostUsd': actual,
        'providerEstimatedCostUsd': provider_estimate,
        'publicApiShadowCostUsd': public['costUsd'],
        'publicPrice': public,
        'usage': usage,
        'cacheWriteObservation': (
            'provider_reported' if outcome.provider_id !=
            'chatgpt_codex_subscription' else 'not_exposed_by_codex_cli'),
    }


def _artifact(path: Path, kind: str) -> dict:
    try:
        rendered = str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        rendered = str(path.resolve())
    return {'type': kind, 'path': rendered, 'exists': path.exists()}


def _attempt_record(*, run_id: str, task: BenchmarkTask, arm: str,
                    attempt: int, error: str) -> dict:
    return {
        'contractVersion': 'tofu-benchmark/v1',
        'recordType': 'attempt',
        'runId': run_id,
        'taskId': task.instance_id,
        'experimentArm': arm,
        'attempt': attempt,
        'failureClass': 'infrastructure',
        'error': error,
    }


def _execute_once(task: BenchmarkTask, arm: str, run_dir: Path,
                  candidate_path: Path | None, timeout_s: int,
                  eval_timeout_s: int) -> tuple[InferenceOutcome,
                                                 EvaluationOutcome]:
    if arm.startswith('codex-'):
        inference = run_codex(
            task, run_dir, product=arm == 'codex-product',
            timeout_s=timeout_s)
    else:
        inference = run_tofu(
            task, run_dir, arm_config(arm, candidate_path),
            timeout_s=timeout_s)
    if inference.infrastructure_error and not inference.patch:
        evaluation = EvaluationOutcome(
            error='evaluation skipped after inference infrastructure failure',
            infrastructure_error=True)
    else:
        evaluation = evaluate_patch(
            task, inference.patch, run_dir, timeout_s=eval_timeout_s)
    return inference, evaluation


def _task_record(*, run_id: str, stage: str, task: BenchmarkTask,
                 arm: str, agent: str, agent_version: str,
                 effort: str,
                 inference: InferenceOutcome,
                 evaluation: EvaluationOutcome,
                 run_dir: Path) -> dict:
    infrastructure_error = (
        inference.infrastructure_error or evaluation.infrastructure_error)
    error_parts = [part for part in (inference.error, evaluation.error) if part]
    test_result = {
        'passed': evaluation.resolved,
        'patchApplies': evaluation.patch_applies,
        'failToPassPassed': evaluation.fail_to_pass_passed,
        'failToPassTotal': evaluation.fail_to_pass_total,
        'passToPassPassed': evaluation.pass_to_pass_passed,
        'passToPassTotal': evaluation.pass_to_pass_total,
        'evaluationLatencyMs': evaluation.duration_ms,
        'agentTerminalWithoutError': not bool(inference.error),
        'error': '; '.join(error_parts),
    }
    telemetry = dict(inference.context_telemetry)
    telemetry.update({
        'apiRounds': inference.api_rounds,
        'toolCalls': inference.tool_calls,
        'providerId': inference.provider_id,
    })
    infra = None
    if infrastructure_error:
        infra = {'class': 'infrastructure', 'message': '; '.join(error_parts)}
    artifacts = [
        _artifact(run_dir / 'model_patch.diff', 'final_patch'),
        _artifact(run_dir / 'inference.json', 'inference_metadata'),
        _artifact(run_dir / 'codex_events.jsonl', 'codex_events'),
        _artifact(run_dir / 'eval' / 'test_output.txt', 'test_output'),
    ]
    return build_task_record(
        run_id=run_id,
        dataset=f'{DATASET_ID}:{stage}',
        task_id=task.instance_id,
        agent=agent,
        agent_version=agent_version,
        model=BENCHMARK_MODEL_ID,
        effort=effort,
        experiment_arm=arm,
        oracle_passed=None if infrastructure_error else evaluation.resolved,
        oracle_type='swebench_multilingual_official_grader',
        final_patch=inference.patch,
        test_result=test_result,
        round_usage=inference.round_usage,
        prefix_fingerprints=inference.prefix_fingerprints,
        cost=_cost_record(inference),
        latency_ms=inference.latency_ms + evaluation.duration_ms,
        infrastructure_error=infra,
        context_telemetry=telemetry,
        compactions=inference.compactions,
        artifacts=artifacts,
    )


def _spent_in_tree(results_dir: Path) -> float:
    total = 0.0
    for path in results_dir.rglob('*.jsonl') if results_dir.exists() else []:
        try:
            records = read_jsonl(path)
        except (OSError, ValueError):
            continue
        total += sum(float((row.get('cost') or {}).get('actualCostUsd') or 0)
                     for row in records if row.get('recordType') == 'task')
    return total


def _run_task(*, task: BenchmarkTask, arm: str, artifacts_root: Path,
              candidate_path: Path | None, timeout_s: int,
              eval_timeout_s: int, max_infra_retries: int,
              writer: BenchmarkJsonlWriter, run_id: str, stage: str,
              agent: str, agent_version: str) -> dict:
    final_inference = InferenceOutcome()
    final_evaluation = EvaluationOutcome()
    final_dir = artifacts_root / task.instance_id / 'attempt-1'
    for attempt in range(1, max_infra_retries + 2):
        run_dir = artifacts_root / task.instance_id / f'attempt-{attempt}'
        _print_event({
            'event': 'task_started', 'stage': stage, 'arm': arm,
            'taskId': task.instance_id, 'attempt': attempt,
        })
        inference, evaluation = _execute_once(
            task, arm, run_dir, candidate_path, timeout_s, eval_timeout_s)
        final_inference, final_evaluation, final_dir = (
            inference, evaluation, run_dir)
        infra = inference.infrastructure_error or evaluation.infrastructure_error
        if not infra:
            break
        error = '; '.join(filter(None, (inference.error, evaluation.error)))
        writer.append(_attempt_record(
            run_id=run_id, task=task, arm=arm,
            attempt=attempt, error=error))
        if not infrastructure_retry_allowed(
                failure_class='infrastructure', attempt=attempt,
                max_infra_retries=max_infra_retries):
            break
        _print_event({
            'event': 'infrastructure_retry', 'taskId': task.instance_id,
            'arm': arm, 'attempt': attempt, 'error': error,
        })
    record = _task_record(
        run_id=run_id, stage=stage, task=task, arm=arm,
        agent=agent, agent_version=agent_version,
        effort=_arm_effort(arm, candidate_path),
        inference=final_inference, evaluation=final_evaluation,
        run_dir=final_dir)
    writer.append(record)
    _print_event({
        'event': 'task_completed', 'stage': stage, 'arm': arm,
        'taskId': task.instance_id,
        'oraclePassed': record['oracle']['passed'],
        'actualCostUsd': record['cost']['actualCostUsd'],
        'shadowCostUsd': record['cost']['publicApiShadowCostUsd'],
        'latencyMs': record['latencyMs'],
        'infrastructureError': bool(record['infrastructureError']),
    })
    return record


def _initialize_run(path: Path, *, tasks: list[BenchmarkTask], manifest: dict,
                    stage: str, arm: str, timeout_s: int,
                    max_infra_retries: int,
                    candidate_path: Path | None) -> tuple[
                        BenchmarkJsonlWriter, str, set[str]]:
    run_id = _run_id(manifest['datasetSha256'], stage, arm, candidate_path)
    writer = BenchmarkJsonlWriter(path)
    completed: set[str] = set()
    if path.exists() and path.stat().st_size:
        records = read_jsonl(path)
        if records[0]['runId'] != run_id:
            raise RuntimeError(
                f'existing run ID differs in {path}; use a new results path')
        completed = {
            row['taskId'] for row in records
            if row.get('recordType') == 'task'
        }
        return writer, run_id, completed
    agent, version = _agent_metadata(arm)
    env = environment_snapshot(cwd=str(PROJECT_ROOT), extra={
        'datasetSha256': manifest['datasetSha256'],
        'sourceFingerprint': _source_fingerprint(candidate_path),
        'pricing': BENCHMARK_PUBLIC_PRICING,
        'providerId': BENCHMARK_PROVIDER_ID,
        'billing': (
            'ChatGPT/Codex subscription; API price is a shadow conversion'
            if BENCHMARK_PROVIDER_ID in _SUBSCRIPTION_PROVIDERS else
            'provider-metered API; canonical price is an audit conversion'),
    })
    writer.append(build_manifest(
        run_id=run_id,
        dataset=f'{DATASET_ID}:{stage}',
        tasks=[task.instance_id for task in tasks],
        agent=agent,
        agent_version=version,
        model=BENCHMARK_MODEL_ID,
        effort=_arm_effort(arm, candidate_path),
        experiment_arm=arm,
        timeout_seconds=timeout_s,
        network_policy='repository network disabled; provider egress only',
        single_agent=(
            (arm_config(arm, candidate_path).get('orchestration') or {}).get(
                'multiAgent') in (None, 'off')
            if not arm.startswith('codex-') else True),
        max_infra_retries=max_infra_retries,
        environment=env,
    ))
    return writer, run_id, completed


def command_manifest(args: argparse.Namespace) -> int:
    manifest = write_frozen_manifest(args.output, seed=args.seed)
    counts = {name: len(rows) for name, rows in manifest['splits'].items()}
    _print_event({
        'event': 'manifest_written', 'path': str(args.output),
        'datasetSha256': manifest['datasetSha256'],
        'tasks': manifest['taskCount'], 'repos': manifest['repoCount'],
        'languages': manifest['languageCount'], 'splits': counts,
    })
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    errors = validate_runtime()
    if errors:
        _print_event({'event': 'preflight_failed', 'errors': errors})
        return 2
    tasks, _manifest = load_manifest_tasks(args.manifest, args.stage)
    task = tasks[args.task_index]
    container, error = ensure_container(task, timeout=args.container_timeout)
    if error:
        _print_event({
            'event': 'preflight_failed', 'taskId': task.instance_id,
            'error': error,
        })
        return 2
    result: dict[str, Any] = {
        'event': 'preflight_container_ready',
        'taskId': task.instance_id,
        'container': container,
    }
    if args.gold_oracle:
        run_dir = args.results_dir / 'preflight' / task.instance_id / 'gold'
        evaluation = evaluate_patch(
            task, task.patch, run_dir, timeout_s=args.eval_timeout)
        result['goldOracle'] = evaluation.__dict__
        _print_event(result)
        return 0 if evaluation.resolved else 3
    _print_event(result)
    return 0


def command_run(args: argparse.Namespace) -> int:
    runtime_errors = validate_runtime()
    if runtime_errors:
        raise RuntimeError('; '.join(runtime_errors))
    tasks, manifest = load_manifest_tasks(args.manifest, args.stage)
    if args.task_ids:
        requested_ids = [
            task_id.strip() for task_id in args.task_ids.split(',')
            if task_id.strip()
        ]
        by_id = {task.instance_id: task for task in tasks}
        missing = [task_id for task_id in requested_ids if task_id not in by_id]
        if missing:
            raise ValueError(
                'requested task IDs are not in the selected stage: '
                + ', '.join(missing))
        tasks = [by_id[task_id] for task_id in requested_ids]
    if args.limit is not None:
        tasks = tasks[:max(0, args.limit)]
    arms = [arm.strip() for arm in args.arms.split(',') if arm.strip()]
    if not arms:
        raise ValueError('at least one arm is required')
    if 'tofu-candidate' in arms and args.candidate_config is None:
        raise ValueError('tofu-candidate requires --candidate-config')
    args.results_dir.mkdir(parents=True, exist_ok=True)
    for arm in arms:
        agent, agent_version = _agent_metadata(arm)
        candidate = args.candidate_config if arm == 'tofu-candidate' else None
        run_id = _run_id(
            manifest['datasetSha256'], args.stage, arm, candidate)
        jsonl_path = args.results_dir / args.stage / f'{run_id}.jsonl'
        artifacts_root = (
            args.results_dir / 'artifacts' / args.stage / run_id)
        writer, run_id, completed = _initialize_run(
            jsonl_path, tasks=tasks, manifest=manifest, stage=args.stage,
            arm=arm, timeout_s=args.timeout,
            max_infra_retries=args.max_infra_retries,
            candidate_path=candidate)
        pending = [task for task in tasks if task.instance_id not in completed]
        _print_event({
            'event': 'arm_started', 'stage': args.stage, 'arm': arm,
            'runId': run_id, 'tasks': len(tasks), 'pending': len(pending),
            'results': str(jsonl_path),
        })
        status = budget_status(_spent_in_tree(args.results_dir))
        if not status['mayStartNewTask']:
            _print_event({'event': 'budget_pause', **status})
            return 4
        if args.workers == 1:
            for task in pending:
                _run_task(
                    task=task, arm=arm, artifacts_root=artifacts_root,
                    candidate_path=candidate, timeout_s=args.timeout,
                    eval_timeout_s=args.eval_timeout,
                    max_infra_retries=args.max_infra_retries,
                    writer=writer, run_id=run_id, stage=args.stage,
                    agent=agent, agent_version=agent_version)
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = [executor.submit(
                    _run_task,
                    task=task, arm=arm, artifacts_root=artifacts_root,
                    candidate_path=candidate, timeout_s=args.timeout,
                    eval_timeout_s=args.eval_timeout,
                    max_infra_retries=args.max_infra_retries,
                    writer=writer, run_id=run_id, stage=args.stage,
                    agent=agent, agent_version=agent_version,
                ) for task in pending]
                for future in as_completed(futures):
                    future.result()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the frozen Tofu/Codex context-efficiency benchmark')
    subparsers = parser.add_subparsers(dest='command', required=True)

    manifest = subparsers.add_parser('manifest')
    manifest.add_argument(
        '--output', type=Path,
        default=PROJECT_ROOT / 'benchmarks' / 'context_efficiency_manifest.json')
    manifest.add_argument('--seed', type=int, default=20260810)
    manifest.set_defaults(func=command_manifest)

    preflight = subparsers.add_parser('preflight')
    preflight.add_argument('--manifest', type=Path, required=True)
    preflight.add_argument('--stage', choices=SPLIT_SIZES, default='calibration')
    preflight.add_argument('--task-index', type=int, default=0)
    preflight.add_argument('--container-timeout', type=int, default=2400)
    preflight.add_argument('--eval-timeout', type=int, default=1800)
    preflight.add_argument('--gold-oracle', action='store_true')
    preflight.add_argument(
        '--results-dir', type=Path,
        default=PROJECT_ROOT / 'benchmarks' / 'context_efficiency_results')
    preflight.set_defaults(func=command_preflight)

    run = subparsers.add_parser('run')
    run.add_argument('--manifest', type=Path, required=True)
    run.add_argument('--stage', choices=SPLIT_SIZES, required=True)
    run.add_argument('--arms', required=True)
    run.add_argument('--candidate-config', type=Path)
    run.add_argument(
        '--results-dir', type=Path,
        default=PROJECT_ROOT / 'benchmarks' / 'context_efficiency_results')
    run.add_argument('--workers', type=int, default=1)
    run.add_argument('--timeout', type=int, default=1800)
    run.add_argument('--eval-timeout', type=int, default=1800)
    run.add_argument('--max-infra-retries', type=int, default=1)
    run.add_argument('--limit', type=int)
    run.add_argument(
        '--task-ids',
        help='comma-separated frozen task IDs to run in the requested order')
    run.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'context-efficiency benchmark failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
