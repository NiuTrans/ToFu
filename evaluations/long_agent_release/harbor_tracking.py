"""Bind formal Harbor dispatches to immutable release attempt claims.

The release store is claimed before Harbor can dispatch a paid model call.
The Harbor manifest keeps only public run/digest metadata; callers supply the
private release-store path again for resume and export validation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evaluations.swebench.constants import BENCHMARKS
from evaluations.swebench.codex_kimi_runtime import CODEX_KIMI_AGENT
from evaluations.swebench.tofu_kimi_runtime import TOFU_KIMI_AGENT
from evaluations.codex_kimi_proxy.codex_contract import CODEX_VERSION

from .run_store import (
    ATTEMPT_LEDGER_CONTRACT,
    ReleaseRunError,
    claim_release_task_attempts,
    load_release_manifest,
    validate_release_attempt_execution,
)


HARBOR_RELEASE_TRACKING_CONTRACT = "tofu-codex-harbor-release-tracking/v1"
HARBOR_TOFU_RELEASE_TRACKING_CONTRACT = (
    "tofu-production-harbor-release-tracking/v1"
)
_BENCHMARK_DATASETS = {
    "swebench-verified": "swe-bench-verified",
    "terminal-bench-2.1": "terminal-bench-2.1",
}


class HarborReleaseTrackingError(ValueError):
    """A Harbor slice is not bound to the claimed release tasks."""


def _sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _release_manifest_digest(manifest: dict[str, Any]) -> str:
    return _sha256(manifest)


def _dataset_config(
    harbor_manifest: dict[str, Any], job_config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    benchmark = str(harbor_manifest.get("benchmark") or "")
    try:
        definition = BENCHMARKS[benchmark]
        release_dataset = _BENCHMARK_DATASETS[benchmark]
    except KeyError as exc:
        raise HarborReleaseTrackingError(
            f"unsupported formal Harbor benchmark: {benchmark!r}") from exc
    datasets = job_config.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 1 \
            or not isinstance(datasets[0], dict):
        raise HarborReleaseTrackingError(
            "formal Harbor tracking requires exactly one dataset")
    dataset = datasets[0]
    expected_name, expected_ref = definition.dataset.rsplit("@", 1)
    if dataset.get("name") != expected_name \
            or dataset.get("ref", dataset.get("version")) != expected_ref:
        raise HarborReleaseTrackingError(
            "Harbor dataset identity drifted before release claim")
    if "n_tasks" in dataset:
        raise HarborReleaseTrackingError(
            "tracked Harbor slices forbid order-dependent --limit; use exact "
            "task names or the full frozen dataset")
    return release_dataset, dataset


def _validate_shared_release_binding(
    release_manifest: dict[str, Any], harbor_manifest: dict[str, Any],
    job_config: dict[str, Any], *, runtime: dict[str, Any],
) -> None:
    controls = {
        "model": "kimi-k3",
        "providerFace": runtime.get("providerFace"),
        "providerSlotId": runtime.get("providerSlotId"),
        "thinking": harbor_manifest.get("reasoning_effort"),
    }
    drift = [
        field for field, value in controls.items()
        if release_manifest.get(field) != value
    ]
    if drift:
        raise HarborReleaseTrackingError(
            f"release/Harbor model-provider controls drifted: {drift}")
    if release_manifest.get("harness") != harbor_manifest.get(
            "harness_identity") \
            or (harbor_manifest.get("harness_identity") or {}).get(
                "runnerProjectDirty") is not False:
        raise HarborReleaseTrackingError(
            "release/Harbor harness identity drifted before dispatch")
    if release_manifest.get("sandbox") != harbor_manifest.get(
            "sandbox_identity"):
        raise HarborReleaseTrackingError(
            "release/Harbor sandbox identity drifted before dispatch")
    if (release_manifest.get("limits") or {}).get("timeoutSeconds") \
            != runtime.get("agentTimeoutSeconds") \
            or float(job_config.get("timeout_multiplier", 1.0)) != 1.0:
        raise HarborReleaseTrackingError(
            "release/Harbor timeout controls drifted before dispatch")
    retry = job_config.get("retry") or {}
    if retry.get("max_retries") != 0:
        raise HarborReleaseTrackingError(
            "Harbor internal retries erase evidence and cannot be dispatched")


def _validate_codex_release_binding(
    release_manifest: dict[str, Any], harbor_manifest: dict[str, Any],
    job_config: dict[str, Any],
) -> None:
    runtime = harbor_manifest.get("codex_kimi_runtime")
    release_agent = release_manifest.get("agent") or {}
    if not isinstance(runtime, dict) \
            or harbor_manifest.get("agent") != CODEX_KIMI_AGENT \
            or release_manifest.get("comparisonRole") != "baseline" \
            or release_agent.get("name") != "codex" \
            or release_agent.get("version") != CODEX_VERSION \
            or release_agent.get("binarySha256") != runtime.get("codexSha256"):
        raise HarborReleaseTrackingError(
            "release store is not bound to this formal Codex runtime")
    _validate_shared_release_binding(
        release_manifest, harbor_manifest, job_config, runtime=runtime)


def _validate_tofu_release_binding(
    release_manifest: dict[str, Any], harbor_manifest: dict[str, Any],
    job_config: dict[str, Any],
) -> None:
    runtime = harbor_manifest.get("tofu_kimi_runtime")
    release_agent = release_manifest.get("agent") or {}
    environment = release_manifest.get("environment") or {}
    if not isinstance(runtime, dict) \
            or harbor_manifest.get("agent") != TOFU_KIMI_AGENT \
            or release_manifest.get("comparisonRole") != "candidate" \
            or release_agent.get("name") != "tofu" \
            or release_agent.get("version") != runtime.get("agentVersion"):
        raise HarborReleaseTrackingError(
            "release store is not bound to this formal production Tofu runtime")
    if release_manifest.get("experimentArm") != runtime.get("experimentArm"):
        raise HarborReleaseTrackingError(
            "release/Harbor candidate experiment arm drifted")
    if release_manifest.get("toolSchemaDigest") != runtime.get(
            "toolSchemaSha256"):
        raise HarborReleaseTrackingError(
            "release/Harbor candidate tool schema digest drifted")
    if release_manifest.get("promptDigest") != runtime.get(
            "promptContractSha256"):
        raise HarborReleaseTrackingError(
            "release/Harbor candidate prompt contract digest drifted")
    if not isinstance(environment, dict) \
            or environment.get("gitCommit") != harbor_manifest.get(
                "project_revision") \
            or environment.get("runtimeConfigSha256") != runtime.get(
                "runtimeConfigSha256"):
        raise HarborReleaseTrackingError(
            "release/Harbor candidate code or runtime config drifted")
    _validate_shared_release_binding(
        release_manifest, harbor_manifest, job_config, runtime=runtime)


def release_task_ids_for_harbor_slice(
    *, release_manifest: dict[str, Any], harbor_manifest: dict[str, Any],
    job_config: dict[str, Any],
) -> list[str]:
    """Resolve a Harbor task selection to exact release trial IDs."""

    dataset_name, dataset = _dataset_config(harbor_manifest, job_config)
    release_rows = [
        row for row in release_manifest.get("tasks") or []
        if row.get("dataset") == dataset_name
    ]
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in release_rows:
        source = str(row.get("sourceTaskId") or "")
        if not source:
            raise HarborReleaseTrackingError(
                f"release task lacks sourceTaskId: {row.get('taskId')}")
        by_source.setdefault(source, []).append(row)
    selected_raw = dataset.get("task_names")
    if selected_raw is None:
        selected_sources = sorted(by_source)
    elif not isinstance(selected_raw, list) or any(
            not isinstance(source, str) or not source for source in selected_raw):
        raise HarborReleaseTrackingError("Harbor task_names are invalid")
    else:
        selected_sources = list(selected_raw)
    if not selected_sources or len(set(selected_sources)) != len(selected_sources):
        raise HarborReleaseTrackingError(
            "Harbor release task selection must be unique and non-empty")
    unknown = sorted(set(selected_sources) - set(by_source))
    if unknown:
        raise HarborReleaseTrackingError(
            f"Harbor tasks are absent from the release manifest: {unknown[:3]}")
    attempts = job_config.get("n_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) \
            or attempts < 1:
        raise HarborReleaseTrackingError("Harbor n_attempts is invalid")
    task_ids: list[str] = []
    for source in selected_sources:
        rows = sorted(
            by_source[source], key=lambda row: int(row.get("trialIndex") or 0))
        indices = [row.get("trialIndex") for row in rows]
        if indices != list(range(1, attempts + 1)):
            raise HarborReleaseTrackingError(
                f"Harbor/release attempt shape drifted for source {source}")
        task_ids.extend(str(row["taskId"]) for row in rows)
    expected_trials = harbor_manifest.get("expected_trials")
    if isinstance(expected_trials, bool) or not isinstance(expected_trials, int) \
            or expected_trials != len(task_ids):
        raise HarborReleaseTrackingError(
            "Harbor expected_trials differs from the release claim")
    return task_ids


def claim_codex_harbor_release_attempts(
    *, release_run_root: Path, harbor_manifest: dict[str, Any],
    job_config: dict[str, Any],
) -> dict[str, Any]:
    """Claim a whole Harbor slice before its launcher is invoked."""

    release_manifest = load_release_manifest(release_run_root)
    _validate_codex_release_binding(
        release_manifest, harbor_manifest, job_config)
    execution_id = str(harbor_manifest.get("run_id") or "")
    task_ids = release_task_ids_for_harbor_slice(
        release_manifest=release_manifest,
        harbor_manifest=harbor_manifest,
        job_config=job_config,
    )
    try:
        claims = claim_release_task_attempts(
            release_run_root, task_ids=task_ids,
            execution_id=execution_id, runner_kind="harbor-codex",
        )
    except ReleaseRunError as exc:
        raise HarborReleaseTrackingError(
            "Harbor release attempt claim failed") from exc
    occurred = [int(row["occurredAt"]) for row in claims["claims"]]
    return {
        "contractVersion": HARBOR_RELEASE_TRACKING_CONTRACT,
        "attemptContractVersion": ATTEMPT_LEDGER_CONTRACT,
        "releaseRunId": release_manifest["runId"],
        "releaseManifestSha256": _release_manifest_digest(release_manifest),
        "executionId": execution_id,
        "runnerKind": "harbor-codex",
        "benchmark": harbor_manifest["benchmark"],
        "taskCount": len(task_ids),
        "taskIdsSha256": _sha256(task_ids),
        "claimedAtUnixMs": min(occurred),
    }


def validate_codex_harbor_release_attempts(
    *, release_run_root: Path, tracking: dict[str, Any],
    harbor_manifest: dict[str, Any], job_config: dict[str, Any],
    allow_oracle_ready: bool = False,
) -> dict[str, Any]:
    """Validate persisted tracking against current release-ledger state."""

    release_manifest = load_release_manifest(release_run_root)
    _validate_codex_release_binding(
        release_manifest, harbor_manifest, job_config)
    task_ids = release_task_ids_for_harbor_slice(
        release_manifest=release_manifest,
        harbor_manifest=harbor_manifest,
        job_config=job_config,
    )
    expected = {
        "contractVersion": HARBOR_RELEASE_TRACKING_CONTRACT,
        "attemptContractVersion": ATTEMPT_LEDGER_CONTRACT,
        "releaseRunId": release_manifest["runId"],
        "releaseManifestSha256": _release_manifest_digest(release_manifest),
        "executionId": str(harbor_manifest.get("run_id") or ""),
        "runnerKind": "harbor-codex",
        "benchmark": harbor_manifest.get("benchmark"),
        "taskCount": len(task_ids),
        "taskIdsSha256": _sha256(task_ids),
    }
    drift = [key for key, value in expected.items() if tracking.get(key) != value]
    claimed_at = tracking.get("claimedAtUnixMs")
    if drift or isinstance(claimed_at, bool) or not isinstance(claimed_at, int) \
            or claimed_at < 0:
        raise HarborReleaseTrackingError(
            f"Harbor release tracking metadata drifted: {drift}")
    try:
        execution = validate_release_attempt_execution(
            release_run_root, execution_id=expected["executionId"],
            task_ids=task_ids, allow_oracle_ready=allow_oracle_ready,
        )
    except ReleaseRunError as exc:
        raise HarborReleaseTrackingError(
            "Harbor execution no longer owns its release attempts") from exc
    if min(int(row["occurredAt"]) for row in execution["claims"]) \
            != claimed_at:
        raise HarborReleaseTrackingError(
            "Harbor tracking claim timestamp drifted")
    return {**execution, "taskIds": task_ids}


def claim_tofu_harbor_release_attempts(
    *, release_run_root: Path, harbor_manifest: dict[str, Any],
    job_config: dict[str, Any],
) -> dict[str, Any]:
    """Claim a production-Tofu Harbor slice before paid dispatch."""

    release_manifest = load_release_manifest(release_run_root)
    _validate_tofu_release_binding(
        release_manifest, harbor_manifest, job_config)
    execution_id = str(harbor_manifest.get("run_id") or "")
    task_ids = release_task_ids_for_harbor_slice(
        release_manifest=release_manifest,
        harbor_manifest=harbor_manifest,
        job_config=job_config,
    )
    try:
        claims = claim_release_task_attempts(
            release_run_root, task_ids=task_ids,
            execution_id=execution_id, runner_kind="harbor-tofu",
        )
    except ReleaseRunError as exc:
        raise HarborReleaseTrackingError(
            "Harbor release attempt claim failed") from exc
    occurred = [int(row["occurredAt"]) for row in claims["claims"]]
    return {
        "contractVersion": HARBOR_TOFU_RELEASE_TRACKING_CONTRACT,
        "attemptContractVersion": ATTEMPT_LEDGER_CONTRACT,
        "releaseRunId": release_manifest["runId"],
        "releaseManifestSha256": _release_manifest_digest(release_manifest),
        "executionId": execution_id,
        "runnerKind": "harbor-tofu",
        "benchmark": harbor_manifest["benchmark"],
        "taskCount": len(task_ids),
        "taskIdsSha256": _sha256(task_ids),
        "claimedAtUnixMs": min(occurred),
    }


def validate_tofu_harbor_release_attempts(
    *, release_run_root: Path, tracking: dict[str, Any],
    harbor_manifest: dict[str, Any], job_config: dict[str, Any],
    allow_oracle_ready: bool = False,
) -> dict[str, Any]:
    """Validate a production-Tofu claim against its immutable ledger."""

    release_manifest = load_release_manifest(release_run_root)
    _validate_tofu_release_binding(
        release_manifest, harbor_manifest, job_config)
    task_ids = release_task_ids_for_harbor_slice(
        release_manifest=release_manifest,
        harbor_manifest=harbor_manifest,
        job_config=job_config,
    )
    expected = {
        "contractVersion": HARBOR_TOFU_RELEASE_TRACKING_CONTRACT,
        "attemptContractVersion": ATTEMPT_LEDGER_CONTRACT,
        "releaseRunId": release_manifest["runId"],
        "releaseManifestSha256": _release_manifest_digest(release_manifest),
        "executionId": str(harbor_manifest.get("run_id") or ""),
        "runnerKind": "harbor-tofu",
        "benchmark": harbor_manifest.get("benchmark"),
        "taskCount": len(task_ids),
        "taskIdsSha256": _sha256(task_ids),
    }
    drift = [key for key, value in expected.items()
             if tracking.get(key) != value]
    claimed_at = tracking.get("claimedAtUnixMs")
    if drift or isinstance(claimed_at, bool) \
            or not isinstance(claimed_at, int) or claimed_at < 0:
        raise HarborReleaseTrackingError(
            f"Harbor release tracking metadata drifted: {drift}")
    try:
        execution = validate_release_attempt_execution(
            release_run_root, execution_id=expected["executionId"],
            task_ids=task_ids, allow_oracle_ready=allow_oracle_ready,
        )
    except ReleaseRunError as exc:
        raise HarborReleaseTrackingError(
            "Harbor execution no longer owns its release attempts") from exc
    if min(int(row["occurredAt"]) for row in execution["claims"]) \
            != claimed_at:
        raise HarborReleaseTrackingError(
            "Harbor tracking claim timestamp drifted")
    return {**execution, "taskIds": task_ids}


__all__ = [
    "HARBOR_RELEASE_TRACKING_CONTRACT",
    "HARBOR_TOFU_RELEASE_TRACKING_CONTRACT", "HarborReleaseTrackingError",
    "claim_codex_harbor_release_attempts",
    "claim_tofu_harbor_release_attempts",
    "release_task_ids_for_harbor_slice",
    "validate_codex_harbor_release_attempts",
    "validate_tofu_harbor_release_attempts",
]
