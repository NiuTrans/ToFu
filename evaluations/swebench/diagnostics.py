"""Read-only, bounded Harbor diagnostic adapter for any agent and numeric metric.

Entry point: diagnose_run. Catalog pins define expected coverage, not observed
successes. Optional OpenCode wire receipts add observations, never alter scores.
No raw prompts, provider URLs, exception messages or credentials are exported.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from itertools import islice
from datetime import datetime, timezone
from pathlib import Path

from evaluations.catalog import BENCHMARKS
from evaluations.evidence import TrialObservation, summarize_trials
from evaluations.model_request import request_parameter_receipt
from evaluations.native_failure import native_failure_receipt
from .artifacts import validate_run_id

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_TRIALS = 10_000


class EvidenceReader:
    def __init__(self, root: Path):
        self.root = root
        self.remaining = MAX_TOTAL_BYTES
        self.receipts: dict[str, str] = {}
        self.slices: dict[str, dict] = {}

    def tail_events(self, path: Path) -> list[dict]:
        """Read only the last 64 KiB; attest the slice, not the whole transcript."""
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("symlink evidence is not supported")
        if not path.is_file():
            raise ValueError("native event evidence must be a regular file")
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            offset = max(0, size - min(64 * 1024, self.remaining))
            stream.seek(offset)
            raw = stream.read(min(64 * 1024, self.remaining))
        if not raw:
            raise ValueError("empty event tail or exhausted evidence byte budget")
        self.remaining -= len(raw)
        self.slices[str(relative)] = {"offset": offset, "bytes": len(raw),
                                     "sha256": hashlib.sha256(raw).hexdigest()}
        lines = raw.splitlines()
        if offset:
            lines = lines[1:]  # first line may have started before the slice
        return [json.loads(line) for line in lines if line.strip()]

    def read(self, path: Path, *, jsonl: bool = False):
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("symlink evidence is not supported")
        if not path.is_file() or path.stat().st_size > min(MAX_FILE_BYTES, self.remaining):
            raise ValueError("evidence exceeds regular-file or byte budget")
        with path.open("rb") as stream:
            raw = stream.read(min(MAX_FILE_BYTES, self.remaining) + 1)
        if len(raw) > min(MAX_FILE_BYTES, self.remaining):
            raise ValueError("evidence grew beyond byte budget")
        self.remaining -= len(raw)
        self.receipts[str(relative)] = hashlib.sha256(raw).hexdigest()
        if jsonl:
            return [json.loads(line) for line in raw.splitlines() if line.strip()]
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected an evidence object")
        return value


def diagnose_run(path: Path, *, reference_profile: str | None = None) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    if path.is_symlink():
        raise ValueError("run root cannot be a symlink")
    root = path.expanduser().resolve(strict=True)
    reader = EvidenceReader(root)
    manifest = reader.read(root / "manifest.json")
    config = reader.read(root / "job-config.json")
    run_id = validate_run_id(manifest.get("run_id", ""))
    if config.get("job_name") != run_id:
        raise ValueError("run identity does not match job config")
    definition = BENCHMARKS.get(manifest.get("benchmark"))
    if definition is None:
        raise ValueError("benchmark requires a registered task lock")
    datasets = config.get("datasets") or []
    if len(datasets) != 1 or datasets[0].get("n_tasks") is not None:
        raise ValueError("diagnosis requires an explicit task selection or full task lock")
    locked = definition.task_digests()
    selected = datasets[0].get("task_names")
    tasks = list(locked) if selected is None else selected
    if not isinstance(tasks, list) or not tasks or any(not isinstance(t, str) for t in tasks) \
            or len(tasks) != len(set(tasks)) or set(tasks) - locked.keys():
        raise ValueError("task selection does not match pinned benchmark")
    models = manifest.get("models")
    attempts = config.get("n_attempts")
    if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m for m in models) \
            or len(set(models)) != len(models) or type(attempts) is not int or not 1 <= attempts <= 100:
        raise ValueError("invalid model/attempt registration")
    if len(tasks) * len(models) * attempts > MAX_TRIALS:
        raise ValueError("registered coverage exceeds diagnostic trial budget")
    expected = {(task, model): attempts for task in tasks for model in models}
    job = root / "jobs" / run_id
    if job.is_symlink() or (root / "jobs").is_symlink():
        raise ValueError("job directory cannot be a symlink")
    trials = []
    native_failures: Counter = Counter()
    wire_parameters: Counter = Counter()
    entries = list(islice(job.iterdir(), MAX_TRIALS + 101)) if job.exists() else []
    if len(entries) > MAX_TRIALS + 100:
        raise ValueError("job directory exceeds entry budget")
    for directory in sorted(entries):
        if directory.is_symlink():
            raise ValueError("trial directory cannot be a symlink")
        result_path = directory / "result.json"
        if not directory.is_dir() or not result_path.exists():
            continue
        if len(trials) >= MAX_TRIALS:
            raise ValueError("trial count exceeds budget")
        issues, observations = [], []
        task, model, rewards, exception = "", "", {}, None
        try:
            result = reader.read(result_path)
            task = result.get("task_name", "")
            model = ((result.get("config") or {}).get("agent") or {}).get("model_name", "")
            rewards = (result.get("verifier_result") or {}).get("rewards") or {}
            exception = (result.get("exception_info") or {}).get("exception_type")
            if not isinstance(task, str) or not isinstance(model, str) or not isinstance(rewards, dict) \
                    or (exception is not None and not isinstance(exception, str)):
                raise ValueError("invalid trial fields")
            task_config = (result.get("config") or {}).get("task") or {}
            if task_config.get("ref") != locked.get(task) or task_config.get("name") != task:
                issues.append("task_identity_mismatch")
            if not result.get("finished_at"):
                issues.append("missing_trial_completion")
            native_path = directory / "agent/tofu-kimi-evidence/runtime-evidence.json"
            if native_path.exists():
                native = reader.read(native_path)
                rounds = native.get("apiRounds") or []
                if not isinstance(rounds, list):
                    raise ValueError("native rounds must be a list")
                for row in rounds:
                    if not isinstance(row, dict):
                        raise ValueError("invalid native round")
                    if row.get("kind") == "compaction":
                        continue
                    usage = row.get("usage") or {}
                    parameters = usage.get("_request_parameters")
                    if isinstance(parameters, dict):
                        receipt = request_parameter_receipt(parameters)
                        wire_parameters[json.dumps(receipt, sort_keys=True)] += 1
                    else:
                        observations.append("wire_request_parameters_unrecorded")
                if native.get("status") == "error":
                    events_path = native_path.with_name("events.jsonl")
                    if events_path.exists():
                        events = reader.tail_events(events_path)
                        done = [row.get("event", row) for row in events
                                if isinstance(row, dict)]
                        done = [event for event in done if isinstance(event, dict)
                                and event.get("type") == "done"]
                        if done:
                            failure = native_failure_receipt(done[-1].get("error"))
                            if failure:
                                label = failure.get("reason", failure["kind"])
                                native_failures[label] += 1
                                observations.append("native_failure:" + label)
            metrics_path = directory / "agent/opencode-evidence/proxy-metrics.jsonl"
            if metrics_path.exists():
                metrics = reader.read(metrics_path, jsonl=True)
                calls = [row for row in metrics if row.get("event") == "chatCompletion"]
                if any(row.get("status") != "completed" for row in calls):
                    observations.append("provider_or_transport_failure")
                if any("length" in (row.get("finishReasons") or []) for row in calls):
                    observations.append("model_output_limit_reached")
                if calls and "length" in (calls[-1].get("finishReasons") or []):
                    observations.append("last_model_response_truncated")
                if calls and any("requestParameters" not in row for row in calls):
                    observations.append("wire_request_parameters_unrecorded")
                for row in calls:
                    if isinstance(row.get("requestParameters"), dict):
                        receipt = request_parameter_receipt(row["requestParameters"])
                        wire_parameters[json.dumps(receipt, sort_keys=True)] += 1
        except (OSError, ValueError, TypeError, AttributeError):
            issues.append("unreadable_or_invalid_evidence")
            task = task if isinstance(task, str) else ""
            model = model if isinstance(model, str) else ""
            rewards = rewards if isinstance(rewards, dict) else {}
            exception = exception if isinstance(exception, str) else None
        trials.append(TrialObservation(directory.name, task, model, rewards, exception,
                                       tuple(issues), tuple(observations)))
    report = summarize_trials(trials, expected=expected)
    report.update(run_id=run_id, manifest_status=manifest.get("status"),
                  snapshot_is_atomic=False, snapshot_started_at=started_at,
                  snapshot_finished_at=datetime.now(timezone.utc).isoformat(),
                  evidence_sha256=reader.receipts)
    report.update(
        native_failure_counts=dict(sorted(native_failures.items())),
        observed_request_parameters=[{"parameters": json.loads(key), "count": count}
                                     for key, count in sorted(wire_parameters.items())],
        evidence_slices=reader.slices,
    )
    if reference_profile is not None:
        if reference_profile != "glm-5.3-tb21" or manifest.get("benchmark") != "terminal-bench-2.1" \
                or models != ["glm-5.3-m17"]:
            raise ValueError("GLM-5.3 reference comparison requires its registered TB 2.1 model/run")
        from evaluations.reference_profiles import compare_glm53_reference
        report["reference_comparison"] = compare_glm53_reference(config, report)
    return report
