"""Offline Terminal-Bench paired registration and evidence reports.

Entry points: register_comparison, validate_registration, compare_runs.
Harbor remains the execution authority; audit_run remains the evidence gate.
Reports retain bounded, hashed references, never copy prompts or credentials.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
from typing import Any

from .artifacts import atomic_write_json, create_run_dir, output_guard_status, utc_now
from .audit import audit_run
from .constants import BENCHMARKS, terminal_bench_21_task_digests
from .opencode_runtime import OPENCODE_AGENT
from .tofu_kimi_runtime import TOFU_KIMI_AGENT

SCHEMA = 'tofu.tb21-comparison/v2'
MAX_FILE_BYTES = 64 * 1024 * 1024
# The native-trace reader streams line by line, so its bound costs read time,
# not memory. Legitimate long trials (300+ billed rounds) exceed the generic
# 64 MiB file bound (TB2.1 v10: a 96 MiB events.jsonl blocked the whole
# paired report).
MAX_TRACE_BYTES = 512 * 1024 * 1024
MAX_RUN_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 10000
_MUTABLE_MANIFEST_KEYS = {
    'status', 'updated_at', 'exit_code', 'failure_type',
    'release_attempts_closed', 'release_attempt_close_failure_type',
}


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f'not a bounded regular evidence file: {path}')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'expected evidence object: {path}')
    return value


def _load_run(path: Path) -> tuple[Path, dict, dict, Path]:
    if path.is_symlink():
        raise ValueError('run root must not be a symlink')
    root = path.expanduser().resolve(strict=True)
    manifest, config = _read(root / 'manifest.json'), _read(root / 'job-config.json')
    from .artifacts import validate_run_id
    name = validate_run_id(str(manifest.get('run_id') or ''))
    if config.get('job_name') != name or manifest.get('benchmark') != 'terminal-bench-2.1':
        raise ValueError('comparison requires a run-owned Terminal-Bench 2.1 job')
    job = root / 'jobs' / name
    if job.resolve().is_relative_to(root) is False or job.is_symlink():
        raise ValueError('job must remain inside its run')
    return root, manifest, config, job


def _identity(manifest: dict, config: dict) -> str:
    return _sha({'manifest': {k: v for k, v in manifest.items()
                             if k not in _MUTABLE_MANIFEST_KEYS}, 'jobConfig': config})


def _shared(manifest: dict, config: dict) -> dict:
    agents = config.get('agents') or []
    agent = agents[0] if len(agents) == 1 else {}
    if manifest.get('agent') == TOFU_KIMI_AGENT:
        runtime = manifest.get('tofu_kimi_runtime') or {}
        context_tokens = runtime.get('contextWindowTokens')
        output_tokens = (runtime.get('runtimeConfig') or {}).get('maxTokens')
    else:
        runtime = manifest.get('opencode_runtime') or {}
        context_tokens = runtime.get('contextTokens')
        output_tokens = runtime.get('outputTokens')
    return {
        **{key: manifest.get(key) for key in (
            'benchmark', 'dataset', 'dataset_source_revision', 'benchmark_source_commit',
            'models', 'provider_face', 'provider_slot_id', 'backend', 'sandbox_identity',
            'harbor_source_commit', 'harbor_binary_sha256', 'attempts_per_task',
        )},
        'datasets': config.get('datasets'), 'timeout_multiplier': config.get('timeout_multiplier'),
        'trial_concurrency': config.get('n_concurrent_trials'),
        'agent_concurrency': agent.get('n_concurrent'),
        'runtime_resource_environment': manifest.get('runtime_resource_environment', {}),
        'agent_timeout_sec': (agent.get('kwargs') or {}).get('timeout_sec'),
        'harbor_agent_timeout_override_sec': agent.get('override_timeout_sec') or 'task_default',
        'harbor_agent_max_timeout_sec': agent.get('max_timeout_sec') or 'unset',
        'retry': config.get('retry'),
        'context_tokens': context_tokens, 'max_output_tokens': output_tokens,
    }


def _tasks(config: dict) -> list[str]:
    lock = terminal_bench_21_task_digests()
    datasets = config.get('datasets') or []
    if len(datasets) != 1 or datasets[0].get('n_tasks') is not None:
        raise ValueError('use the full dataset or explicit task_names, not a task limit')
    selected = datasets[0].get('task_names')
    tasks = sorted(lock if selected is None else selected)
    if not tasks or len(tasks) != len(set(tasks)) or set(tasks) - lock.keys():
        raise ValueError('task selection must be nonempty, unique and pinned')
    return tasks


def _compatibility(baseline: tuple, candidate: tuple) -> list[str]:
    _, bm, bc, _ = baseline
    _, cm, cc, _ = candidate
    issues = []
    a, b = _shared(bm, bc), _shared(cm, cc)
    for key in a:
        if a[key] is None or a[key] != b[key]:
            issues.append(f'shared_config:{key}')
    for role, manifest, config in [('baseline', bm, bc), ('candidate', cm, cc)]:
        shared = _shared(manifest, config)
        for key in ('context_tokens', 'max_output_tokens'):
            if type(shared[key]) is not int or shared[key] < 1:
                issues.append(f'{role}:explicit_{key}_required')
        if manifest.get('agent') != {'baseline': OPENCODE_AGENT, 'candidate': TOFU_KIMI_AGENT}[role]:
            issues.append(f'{role}:agent_identity')
        if manifest.get('dataset') != BENCHMARKS['terminal-bench-2.1'].dataset:
            issues.append(f'{role}:dataset_pin')
        if manifest.get('project_dirty') is not False or manifest.get('project_revision') in (None, '', 'unknown'):
            issues.append(f'{role}:source_identity')
        if (config.get('retry') or {}).get('max_retries') != 0:
            issues.append(f'{role}:destructive_internal_retry')
        if len(manifest.get('models') or []) != 1:
            issues.append(f'{role}:one_model_required')
        if config.get('n_attempts') != manifest.get('attempts_per_task'):
            issues.append(f'{role}:attempt_config_drift')
    return issues


def _create_json(path: Path, value: dict) -> None:
    # Exclusive publication is deliberate: a protocol cannot be edited after
    # seeing outcomes. A failed write leaves a fail-closed, unusable record.
    encoded = (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2,
                          allow_nan=False) + '\n').encode()
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def register_comparison(baseline: Path, candidate: Path, *, output_root: Path,
                        run_id: str, target_pp: float = 10, seed: int = 1729) -> Path:
    """Freeze two prepared Harbor runs before either has dispatched a trial."""
    arms = {'baseline': _load_run(baseline), 'candidate': _load_run(candidate)}
    if arms['baseline'][0] == arms['candidate'][0]:
        raise ValueError('comparison arms must be distinct')
    with ExitStack() as stack:
        for root in sorted(run[0] for run in arms.values()):
            lock = stack.enter_context((root / '.launcher.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Re-read after claiming the same locks as Harbor dispatch.
        arms = {role: _load_run(run[0]) for role, run in arms.items()}
        return _register_prepared(arms, output_root=output_root, run_id=run_id,
                                  target_pp=target_pp, seed=seed)


def _register_prepared(arms: dict, *, output_root: Path, run_id: str,
                       target_pp: float, seed: int) -> Path:
    issues = _compatibility(arms['baseline'], arms['candidate'])
    if issues:
        raise ValueError('incomparable prepared runs: ' + ', '.join(issues))
    if not math.isfinite(target_pp) or not 0 < target_pp <= 100:
        raise ValueError('target_pp must be finite and in (0, 100]')
    task_names = _tasks(arms['baseline'][2])
    for root, manifest, config, job in arms.values():
        if manifest.get('status') != 'prepared' or job.exists() or (root / 'command.json').exists():
            raise ValueError('registration requires unstarted --dry-run runs; historical results stay diagnostic')
        if (root / 'comparison-registration.json').exists():
            raise ValueError('run already belongs to a comparison')
        if _tasks(config) != task_names:
            raise ValueError('task selections differ')
    ok, detail = output_guard_status(output_root)
    if not ok:
        raise ValueError(detail)
    out = create_run_dir(output_root, run_id)
    protocol = {
        'schema': SCHEMA, 'kind': 'preregistered-pair', 'createdAt': utc_now(),
        'scope': 'local-rootless-fixed-model-native-harness',
        'samplingPolicy': 'Each native harness configuration is frozen separately; equal wire sampling is not claimed.',
        'targetPercentagePoints': target_pp, 'bootstrapSeed': seed,
        'bootstrapSamples': 10000, 'confidence': 0.95,
        'metric': 'task-macro-mean-binary-reward', 'taskNames': task_names,
        'attemptsPerTask': arms['baseline'][1]['attempts_per_task'],
        'shared': _shared(arms['baseline'][1], arms['baseline'][2]),
        'arms': {role: {'runRoot': str(root), 'identitySha256': _identity(m, c)}
                 for role, (root, m, c, _) in arms.items()},
    }
    _create_json(out / 'protocol.json', protocol)
    binding = {'schema': SCHEMA, 'protocolPath': str(out / 'protocol.json'),
               'protocolSha256': _sha(protocol)}
    for root, _, _, _ in arms.values():
        _create_json(root / 'comparison-registration.json', binding)
    return out / 'protocol.json'


def validate_registration(run_dir: Path, manifest: dict, config: dict) -> dict | None:
    """Called by the existing Harbor launcher before a registered dispatch."""
    path = run_dir / 'comparison-registration.json'
    if not path.exists() and not path.is_symlink():
        return None
    binding = _read(path)
    protocol = _read(Path(str(binding.get('protocolPath') or '')))
    if (binding.get('schema') != SCHEMA or protocol.get('schema') != SCHEMA
            or binding.get('protocolSha256') != _sha(protocol)):
        raise ValueError('comparison protocol digest drifted')
    arm = [a for a in protocol['arms'].values() if a['runRoot'] == str(run_dir.resolve())]
    if len(arm) != 1 or arm[0]['identitySha256'] != _identity(manifest, config):
        raise ValueError('registered run configuration/source identity drifted')
    return protocol


class EvidenceIndex:
    """One bounded pass; paths+hashes are receipts, raw content stays private."""
    def __init__(self, root: Path):
        self.root = root
        self.files: list[dict] = []
        self.total = 0
        self.unstable = False
        self.signatures: dict[Path, tuple[int, int]] = {}

    def add(self, path: Path) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError('evidence symlink or escaped path')
        before = path.stat()
        if not path.is_file() or before.st_size > MAX_TRACE_BYTES:
            raise ValueError('evidence exceeds per-file bound or is not regular')
        if len(self.files) >= MAX_FILES or self.total + before.st_size > MAX_RUN_BYTES:
            raise ValueError('evidence exceeds run file/byte budget')
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_TRACE_BYTES or self.total + size > MAX_RUN_BYTES:
                    raise ValueError('growing evidence exceeds byte budget')
                digest.update(chunk)
        after = path.stat()
        self.unstable |= (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        self.total += size
        self.signatures[path] = (after.st_size, after.st_mtime_ns)
        self.files.append({'path': str(path.relative_to(self.root)),
                           'bytes': size, 'sha256': digest.hexdigest()})

    def changed(self) -> bool:
        for path, expected in self.signatures.items():
            try:
                stat = path.stat()
            except OSError:
                return True
            if (stat.st_size, stat.st_mtime_ns) != expected:
                return True
        return self.unstable


def _elapsed(value: dict) -> float | None:
    try:
        seconds = (datetime.fromisoformat(value['finished_at']) -
                   datetime.fromisoformat(value['started_at'])).total_seconds()
        return seconds if seconds >= 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def _trace_facts(trial: Path) -> dict:
    """Measure observed boundaries; absent tools are not proof of model thinking."""
    path = trial / 'agent/tofu-kimi-evidence/events.jsonl'
    if not path.exists():
        return {'available': False}
    if path.is_symlink() or path.stat().st_size > MAX_TRACE_BYTES:
        raise ValueError('native trace exceeds file bound or is a symlink')
    counts: Counter = Counter()
    models: set[str] = set()
    completed_ms = 0.0
    max_delivery_lag_ms = 0.0
    delayed_rounds = 0
    tool_finishes = 0
    last_call = None
    seen_bytes = 0
    with path.open('rb') as stream:
        for number, raw in enumerate(stream, 1):
            seen_bytes += len(raw)
            if seen_bytes > MAX_TRACE_BYTES:
                raise ValueError('growing native trace exceeds file bound')
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                return {'available': True, 'malformedLine': number}
            if not isinstance(row, dict) or not isinstance(row.get('event'), dict):
                return {'available': True, 'malformedLine': number}
            event = row['event']
            typ = event.get('type')
            if typ in {'model_request_start', 'model_request_complete', 'custom_tool_call',
                       'tool_start', 'tool_result', 'round_usage'}:
                counts[typ] += 1
            if typ == 'model_request_complete':
                model = event.get('model')
                if isinstance(model, str) and len(models) < 32:
                    models.add(model[:128])
                value = event.get('durationMs')
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    completed_ms += value
                tool_finishes += event.get('finishReason') in {'tool_calls', 'tool_use'}
            if typ == 'round_usage':
                dispatch = (event.get('usage') or {}).get('_dispatch') or {}
                completed_at = dispatch.get('stream_completed_at_unix_ns')
                observed_at = row.get('observedAtUnixNs')
                if type(completed_at) is int and type(observed_at) is int and observed_at >= completed_at > 0:
                    lag_ms = (observed_at - completed_at) / 1_000_000
                    max_delivery_lag_ms = max(max_delivery_lag_ms, lag_ms)
                    delayed_rounds += lag_ms > 120_000
            if typ == 'custom_tool_call':
                arguments = event.get('arguments') or {}
                command = arguments.get('command') or arguments.get('validation_command')
                last_call = {'line': number, 'eventSeq': event.get('seq'),
                             'observedAtUnixNs': row.get('observedAtUnixNs'),
                             'callId': event.get('callId'), 'toolName': event.get('toolName'),
                             'requestedTimeoutSeconds': arguments.get('timeout_sec'),
                             'commandSha256': hashlib.sha256(command.encode()).hexdigest()
                             if isinstance(command, str) else None}
    return {
        'available': True, 'path': str(path), 'counts': dict(counts),
        'observedModels': sorted(models), 'completedModelRequestMs': completed_ms,
        'maxObservedDeliveryLagMs': max_delivery_lag_ms,
        'modelRoundsObservedOver120SecondsLate': delayed_rounds,
        'modelToolCallFinishes': tool_finishes, 'lastClientHandoff': last_call,
        'toolCallWithoutObservedClientHandoff': bool(tool_finishes and not counts['custom_tool_call']),
        'interpretation': 'Observed events only. Delivery lag is provider stream completion to observer receipt, not model reasoning time or proof of a specific blocking cause.',
    }


def _trial(path: Path, result: dict, expected_model: str) -> dict:
    task = str(result.get('task_name') or '')
    cfg = result.get('config') or {}
    identity = result.get('task_id') or {}
    ref = (cfg.get('task') or {}).get('ref')
    reward = ((result.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    exception = result.get('exception_info') or {}
    error = exception.get('exception_type')
    message = str(exception.get('exception_message') or '')
    numeric = type(reward) in (int, float) and math.isfinite(reward) and reward in (0, 1)
    issues = []
    if terminal_bench_21_task_digests().get(task) != ref or identity.get('ref') != ref:
        issues.append('task_digest_mismatch')
    if (cfg.get('agent') or {}).get('model_name') != expected_model:
        issues.append('configured_model_mismatch')
    if not numeric:
        issues.append('missing_or_invalid_reward')
    if error and error != 'AgentTimeoutError':
        issues.append('unresolved_execution_exception')
    if not (result.get('verifier') or {}).get('finished_at'):
        issues.append('verifier_not_finished')
    if 'production runtime rejected its owned custom tool result' in message:
        diagnosis = 'harness_client_result_rejected'
    elif error == 'EnvironmentStartTimeoutError':
        diagnosis = 'environment_start_timeout'
    elif error == 'AgentTimeoutError':
        diagnosis = 'agent_timeout_scored' if numeric else 'agent_timeout_unscored'
    elif error:
        diagnosis = str(error)
    else:
        diagnosis = 'passed' if numeric and reward == 1 else 'verifier_failed' if numeric else 'unscored'
    trace = _trace_facts(path.parent)
    return {
        'task': task, 'trial': path.parent.name, 'resultPath': str(path),
        'taskRef': ref, 'taskChecksum': result.get('task_checksum'),
        'reward': float(reward) if numeric else None,
        'eligible': not issues, 'issues': issues, 'diagnosis': diagnosis,
        'exceptionType': error, 'agentSeconds': _elapsed(result.get('agent_execution') or {}),
        'verifierSeconds': _elapsed(result.get('verifier') or {}),
        'inputTokens': (result.get('agent_result') or {}).get('n_input_tokens'),
        'outputTokens': (result.get('agent_result') or {}).get('n_output_tokens'),
        'trace': trace,
    }


def _observe(run: tuple) -> dict:
    root, manifest, config, job = run
    index = EvidenceIndex(root)
    for name in ('manifest.json', 'job-config.json', 'comparison-registration.json'):
        if (root / name).exists():
            index.add(root / name)
    tasks = _tasks(config)
    rows = []
    if job.exists():
        # No recursive traversal of VM disks, downloads, caches or provider envs.
        for trial in sorted(job.iterdir()):
            if trial.is_symlink():
                raise ValueError('trial must not be a symlink')
            if not trial.is_dir():
                continue
            if (trial / 'result.json').exists():
                rows.append(_trial(trial / 'result.json', _read(trial / 'result.json'),
                                   str((manifest.get('models') or [''])[0])))
            for name in ('result.json', 'config.json', 'exception.txt'):
                if (trial / name).exists():
                    index.add(trial / name)
            for directory in ('agent', 'verifier'):
                base = trial / directory
                if not base.exists():
                    continue
                for current, dirs, files in os.walk(base, followlinks=False):
                    if any((Path(current) / name).is_symlink() for name in dirs):
                        raise ValueError('artifact directory symlink')
                    for name in sorted(files):
                        path = Path(current) / name
                        if path.suffix in {'.json', '.jsonl', '.txt', '.log'}:
                            index.add(path)
        if (job / 'result.json').exists():
            index.add(job / 'result.json')
    try:
        audit = audit_run(root, allow_errors=True)
        failures = [c['name'] for c in audit['checks'] if not c['ok']]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        failures = ['audit_unavailable:' + type(exc).__name__]
    by_task: dict[str, list] = defaultdict(list)
    for row in rows:
        by_task[row['task']].append(row)
    attempts = manifest.get('attempts_per_task')
    if type(attempts) is not int or not 1 <= attempts <= 100:
        raise ValueError('attempts per task must be in 1..100')
    means = {}
    for task in tasks:
        group = by_task.get(task, [])
        if len(group) == attempts and all(row['eligible'] for row in group):
            means[task] = sum(row['reward'] for row in group) / attempts
    surplus = {t: len(group) for t, group in by_task.items()
               if t not in tasks or len(group) > attempts}
    # A final re-read catches ordinary concurrent writers. Active runs remain
    # diagnostic even if no file changed during the bounded snapshot pass.
    index.unstable |= manifest != _read(root / 'manifest.json') or config != _read(root / 'job-config.json')
    return {
        'runRoot': str(root), 'manifestIdentitySha256': _identity(manifest, config),
        'sharedConfiguration': _shared(manifest, config),
        'reasoningEffort': manifest.get('reasoning_effort'),
        'sourceRevision': manifest.get('project_revision'), 'sourceDirty': manifest.get('project_dirty'),
        'status': manifest.get('status'), 'expectedTasks': len(tasks),
        'attemptsPerTask': attempts, 'observedTrials': len(rows),
        'numericRewards': sum(row['reward'] is not None for row in rows),
        'observedSuccesses': sum(row['reward'] == 1 for row in rows),
        'eligibleTrials': sum(row['eligible'] for row in rows),
        'completeTasks': len(means), 'taskMeans': means, 'surplus': surplus,
        'fullDomain': _full_domain(by_task, tasks, attempts),
        'diagnoses': dict(sorted(Counter(row['diagnosis'] for row in rows).items())),
        'auditFailures': failures, 'snapshotChanged': index.changed(),
        'unresolvedTasks': [{'task': t, 'observedTrials': len(by_task.get(t, [])),
                             'eligibleTrials': sum(r['eligible'] for r in by_task.get(t, [])),
                             'requiredTrials': attempts,
                             'evidence': [r['resultPath'] for r in by_task.get(t, [])]}
                            for t in tasks if t not in means],
        'evidenceBytes': index.total, 'evidence': index.files,
        'evidenceIndexSha256': _sha(index.files), 'trials': rows,
    }


def _full_domain(by_task: dict[str, list], selected: list[str], attempts: int) -> dict:
    """Keep every pinned task in the denominator, including absent evidence.

    The current success rate measures delivered, verified successes. Unscored
    and missing attempts remain explicit; they are never classified as model
    failures. Duplicate attempts invalidate that task rather than select wins.
    """
    ledger = []
    for task in sorted(terminal_bench_21_task_digests()):
        group = by_task.get(task, [])
        ambiguous = len(group) > attempts or (group and task not in selected)
        accepted = [] if ambiguous else [row for row in group if row['eligible']]
        passed = sum(row['reward'] == 1 for row in accepted)
        failed = len(accepted) - passed
        unscored = attempts if ambiguous else len(group) - len(accepted)
        missing = 0 if ambiguous else attempts - len(group)
        state = ('ambiguous' if ambiguous else 'not_selected' if task not in selected
                 else 'missing' if not group else 'unscored' if unscored
                 else 'partial' if missing else 'complete')
        ledger.append({
            'task': task, 'state': state, 'requiredTrials': attempts,
            'observedTrials': len(group), 'passed': passed, 'scoredFailed': failed,
            'unscored': unscored, 'missing': missing,
            'evidence': [row['resultPath'] for row in group],
        })
    totals = {key: sum(row[key] for row in ledger)
              for key in ('passed', 'scoredFailed', 'unscored', 'missing')}
    denominator = len(ledger) * attempts
    complete = not (totals['unscored'] or totals['missing'])
    return {
        'taskCount': len(ledger), 'attemptsPerTask': attempts,
        'requiredTrials': denominator, **totals,
        'successRatePercent': totals['passed'] / denominator * 100,
        'scoredCoveragePercent': (totals['passed'] + totals['scoredFailed']) / denominator * 100,
        'allTrialsScored': complete,
        'scoreStatus': 'complete_observation' if complete else 'provisional_incomplete',
        'tasks': ledger,
    }


def _bootstrap(deltas: list[float], seed: int, samples: int = 10000) -> list[float] | None:
    if len(deltas) < 2:
        return None
    rng = random.Random(seed)
    # Repeated trials are clustered inside the task mean, not treated as
    # independent tasks. This avoids 445 pseudo-independent observations.
    draws = sorted(sum(rng.choices(deltas, k=len(deltas))) / len(deltas) * 100
                   for _ in range(samples))
    return [draws[int(samples * 0.025)], draws[int(samples * 0.975) - 1]]


def verify_report(path: Path) -> dict:
    """Rehash referenced evidence without rerunning or rescoring any trial."""
    report = _read(path)
    if report.get('schema') not in {SCHEMA, 'tofu.tb21-comparison/v1'} or report.get('kind') != 'paired-report':
        raise ValueError('unsupported paired report')
    failures = []
    for role in ('baseline', 'candidate'):
        arm = report['arms'][role]
        descriptors = arm['evidence']
        if len(descriptors) > MAX_FILES or _sha(descriptors) != arm['evidenceIndexSha256']:
            raise ValueError('evidence index digest or file bound violated')
        root = Path(arm['runRoot'])
        if root.is_symlink() or not root.is_dir():
            raise ValueError('evidence run root is missing or a symlink')
        index = EvidenceIndex(root.resolve())
        for expected in descriptors:
            relative = Path(expected['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('invalid evidence reference')
            try:
                index.add(root / relative)
                if index.files[-1] != expected:
                    failures.append({'arm': role, 'path': str(relative), 'cause': 'changed'})
            except (OSError, ValueError):
                failures.append({'arm': role, 'path': str(relative), 'cause': 'missing_or_invalid'})
        if index.changed():
            failures.append({'arm': role, 'cause': 'changed_during_verification'})
    return {'ok': not failures, 'failures': failures}


def render_report(report: dict) -> str:
    """Compact human entry point; complete receipts stay in report.json."""
    lines = [
        '# GLM / Terminal-Bench 2.1 全域评测证据', '',
        f"状态：`{report['decision']}`。目标：候选领先 {report['targetPercentagePoints']:g} 个百分点。",
        '本地 QEMU 结果；两边各自固定的 harness 配置，不声称采样参数完全一致或官方榜单成绩。', '',
        '统计域固定为锁定的全部 89 题；分母为 89 × 每题预定次数，未完成和未评分也占分母。',
        '当前通过率 = 已验证成功次数 / 全域预定次数。未评分保留故障状态，不冒充已验证失败。', '',
        '| 运行 | 全域当前通过率 | 成功 | 已评分失败 | 未评分 | 缺失 | 预定次数 |',
        '|---|---:|---:|---:|---:|---:|---:|',
    ]
    for role, arm in report['arms'].items():
        full = arm['fullDomain']
        lines.append(f"| {role} | {full['successRatePercent']:.2f}% | {full['passed']} | "
                     f"{full['scoredFailed']} | {full['unscored']} | {full['missing']} | {full['requiredTrials']} |")
    lines += ['', '运行尚未闭合或证据审计未通过时，当前通过率是进度观测，不作为最终提升结论。',
              f"全域当前通过率差：{report['fullDomain']['currentDeltaPercentagePoints']:+.2f} 个百分点。",
              f"全域 95% bootstrap 区间：{report['fullDomain']['taskClusterBootstrap95CI']}。",
              '', '正式分数阻塞项：', '']
    lines += [f'- `{item}`' for item in report['blockers']] or ['- 无']
    lines += ['', '失败边界统计：', '', '| 运行 | 观察到的结果/异常 | 数量 |', '|---|---|---:|']
    for role, arm in report['arms'].items():
        lines += [f'| {role} | {key} | {value} |' for key, value in arm['diagnoses'].items()]
    lines += ['', '全部 89 题账本（每格为 成功 / 已评分失败 / 未评分 / 缺失）：', '',
              '| 任务 | OpenCode | Tofu |', '|---|---|---|']
    for baseline, candidate in zip(report['arms']['baseline']['fullDomain']['tasks'],
                                   report['arms']['candidate']['fullDomain']['tasks'], strict=True):
        cells = [' / '.join(str(row[key]) for key in ('passed', 'scoredFailed', 'unscored', 'missing'))
                 for row in (baseline, candidate)]
        lines.append(f"| {baseline['task']} | {cells[0]} | {cells[1]} |")
    lines += ['', '开发复测优先项（用于诊断，不能作为最终验收集）：', '']
    for row in report['arms']['candidate']['trials']:
        if row['diagnosis'] == 'harness_client_result_rejected' or row['trace'].get('toolCallWithoutObservedClientHandoff'):
            reason = ('模型已返回工具调用，但未观察到客户端交接' if row['trace'].get('toolCallWithoutObservedClientHandoff')
                      else '客户端工具结果交接被拒绝')
            lines.append(f"- `{row['task']}`：{reason}。[原始结果]({row['resultPath']})")
    lines += ['', '完整逐题记录、未完成任务、配置身份、原始文件哈希见 [report.json](report.json)。',
              '修复后另建运行并预注册；保留失败 trial，不从历史多次尝试中挑选成功结果。', '']
    return '\n'.join(lines)


def compare_runs(baseline: Path, candidate: Path, *, output_root: Path,
                 run_id: str, target_pp: float = 10, seed: int = 1729) -> tuple[dict, Path]:
    arms = {'baseline': _load_run(baseline), 'candidate': _load_run(candidate)}
    if arms['baseline'][0] == arms['candidate'][0]:
        raise ValueError('comparison arms must be distinct')
    issues = _compatibility(arms['baseline'], arms['candidate'])
    protocols = []
    for role, (root, m, c, _) in arms.items():
        try:
            protocols.append(validate_registration(root, m, c))
        except (OSError, ValueError, KeyError, TypeError):
            issues.append(f'{role}:registration_invalid')
            protocols.append(None)
    protocol = protocols[0] if protocols[0] is not None and protocols[0] == protocols[1] else None
    if protocol is None:
        issues.append('pair_not_preregistered')
    else:
        target_pp, seed = protocol['targetPercentagePoints'], protocol['bootstrapSeed']
    if not math.isfinite(target_pp) or not 0 < target_pp <= 100:
        raise ValueError('target_pp must be finite and in (0, 100]')
    observed = {role: _observe(run) for role, run in arms.items()}
    a, b = observed['baseline'], observed['candidate']
    full_tasks = sorted(terminal_bench_21_task_digests())
    all_scored = all(obs['fullDomain']['allTrialsScored'] for obs in observed.values())
    deltas = [b['taskMeans'][t] - a['taskMeans'][t] for t in full_tasks] if all_scored else []
    delta = sum(deltas) / len(deltas) * 100 if deltas else None
    for role, obs in observed.items():
        if obs['expectedTasks'] != 89 or obs['attemptsPerTask'] != 5 or obs['completeTasks'] != 89:
            issues.append(f'{role}:incomplete_89x5')
        if obs['surplus']:
            issues.append(f'{role}:surplus_trials')
        if obs['auditFailures']:
            issues.append(f'{role}:audit_failed')
        if obs['snapshotChanged']:
            issues.append(f'{role}:snapshot_changed')
        if obs['status'] != 'succeeded':
            issues.append(f'{role}:launcher_not_succeeded')
    interval = _bootstrap(deltas, seed)
    report = {
        'schema': SCHEMA, 'kind': 'paired-report', 'createdAt': utc_now(),
        'analyzerSources': {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in {
                'comparison': Path(__file__),
                'harborAudit': Path(__file__).with_name('audit.py'),
                'tofuProjection': Path(__file__).parent.parent / 'long_agent_release/tofu_projection.py',
                'taskDigestLock': Path(__file__).with_name('terminal_bench_21_task_digests.json'),
            }.items()
        },
        'scope': 'local-rootless-fixed-model-native-harness',
        'samplingPolicy': 'Per-arm native defaults/configuration; equal wire sampling is not established.',
        'metric': 'full-domain-task-macro-mean-binary-reward', 'officialLeaderboardResult': False,
        'protocolSha256': _sha(protocol) if protocol else None,
        'targetPercentagePoints': target_pp, 'bootstrapSeed': seed,
        'bootstrapSamples': 10000, 'confidence': 0.95,
        'decision': 'diagnostic_only' if issues else 'target_confirmed' if interval and interval[0] >= target_pp
                    else 'point_target_met' if delta is not None and delta >= target_pp else 'target_not_met',
        'releaseEligible': not issues, 'blockers': sorted(set(issues)),
        'finalDeltaPercentagePoints': delta if not issues else None,
        'fullDomain': {
            'taskCount': len(full_tasks), 'allTrialsScored': all_scored,
            'currentDeltaPercentagePoints': (b['fullDomain']['successRatePercent']
                                             - a['fullDomain']['successRatePercent']),
            'taskClusterBootstrap95CI': interval,
            'denominatorPolicy': 'All pinned tasks times planned attempts; missing and unscored attempts remain in the denominator.',
            'inferencePolicy': 'No subset comparison. Confidence interval requires scored outcomes for every planned attempt in both arms.',
        },
        'arms': observed,
    }
    ok, detail = output_guard_status(output_root)
    if not ok:
        raise ValueError(detail)
    out = create_run_dir(output_root, run_id)
    atomic_write_json(out / 'report.json', report)
    summary = out / 'report.md'
    summary.write_text(render_report(report), encoding='utf-8')
    summary.chmod(0o600)
    return report, out
