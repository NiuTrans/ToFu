"""Compile immutable task locks into the exact release BenchmarkRecordV2.

The compiler is intentionally pure once its catalog inputs are loaded.  It
does not download datasets, call a model, or infer missing tasks.  A formal
manifest exists only when all 1,845 task identities and their content locks are
present and the release matrix validator accepts the exact family shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lib.benchmark_contract import (
    CONTRACT_VERSION_V2,
    RELEASE_TASK_MATRIX_V2,
    build_manifest_v2,
    validate_release_task_matrix_v2,
)

from evaluations.swebench.constants import (
    BENCHMARKS,
    SWEBENCH_VERIFIED_DATASET_REF,
    TBENCH21_DATASET_REF,
    swebench_verified_task_digests,
    terminal_bench_21_task_digests,
)
from evaluations.swebench.images import load_definitions

from .contract import CUSTOM_PACK_SPECS, FrozenTaskPack, load_all_custom_packs


RELEASE_MATRIX_CONTRACT = "tofu-long-agent-release-matrix/v1"


class ReleaseMatrixError(ValueError):
    """The supplied catalogs cannot prove the preregistered release shape."""


@dataclass(frozen=True)
class SoftwareTaskLock:
    name: str
    sha256: str


@dataclass(frozen=True)
class CompiledReleaseMatrix:
    release_id: str
    sha256: str
    task_table: tuple[dict[str, Any], ...]
    components: tuple[dict[str, Any], ...]

    @property
    def task_count(self) -> int:
        return len(self.task_table)

    def dataset_snapshot(self) -> dict[str, Any]:
        return {
            "id": self.release_id,
            "sha256": self.sha256,
            "frozen": True,
            "releaseMatrix": True,
            "contractVersion": RELEASE_MATRIX_CONTRACT,
            "taskCount": self.task_count,
            "components": [dict(row) for row in self.components],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "contractVersion": RELEASE_MATRIX_CONTRACT,
            "releaseId": self.release_id,
            "sha256": self.sha256,
            "taskCount": self.task_count,
            "components": [dict(row) for row in self.components],
            "tasks": [dict(row) for row in self.task_table],
        }


def _digest(value: Any, field: str) -> str:
    text = str(value or "").lower().removeprefix("sha256:")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ReleaseMatrixError(f"{field} must be a SHA-256 digest")
    return text


def _software_locks(rows: Iterable[Any], *, expected: int,
                    dataset: str) -> list[SoftwareTaskLock]:
    locks: list[SoftwareTaskLock] = []
    for row in rows:
        if isinstance(row, SoftwareTaskLock):
            name, digest = row.name, row.sha256
        elif isinstance(row, dict):
            name = str(row.get("name") or row.get("taskId") or "")
            digest = row.get("sha256") or row.get("ref")
        else:
            name = str(getattr(row, "name", "") or "")
            digest = getattr(row, "sha256", None) or getattr(row, "ref", None)
        if not name:
            raise ReleaseMatrixError(f"{dataset} task name is empty")
        locks.append(SoftwareTaskLock(
            name=name, sha256=_digest(digest, f"{dataset} task {name}")))
    locks.sort(key=lambda item: item.name)
    if len(locks) != expected:
        raise ReleaseMatrixError(
            f"{dataset} requires exactly {expected} tasks, got {len(locks)}")
    if len({item.name for item in locks}) != len(locks):
        raise ReleaseMatrixError(f"{dataset} task names must be unique")
    return locks


def _component_digest(rows: Iterable[dict[str, Any]]) -> str:
    payload = json.dumps(
        list(rows), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pack_map(packs: Iterable[FrozenTaskPack]) -> dict[
        tuple[str, str], FrozenTaskPack]:
    result: dict[tuple[str, str], FrozenTaskPack] = {}
    for pack in packs:
        key = (pack.family, pack.dataset)
        if key in result:
            raise ReleaseMatrixError(f"duplicate frozen task pack: {key}")
        result[key] = pack
    expected = {(spec.family, spec.dataset) for spec in CUSTOM_PACK_SPECS}
    if set(result) != expected:
        raise ReleaseMatrixError(
            f"custom pack set mismatch: expected={sorted(expected)}, "
            f"observed={sorted(result)}")
    return result


def compile_release_matrix(
    *, release_id: str, swebench_tasks: Iterable[Any],
    terminal_task_digests: dict[str, str],
    custom_packs: Iterable[FrozenTaskPack],
) -> CompiledReleaseMatrix:
    """Compile the exact 500 + 89×5 + 900 preregistered task table."""
    if not str(release_id or "").strip():
        raise ReleaseMatrixError("release_id is required")
    swe_count = RELEASE_TASK_MATRIX_V2[
        ("software_engineering", "swe-bench-verified")]
    terminal_trials = RELEASE_TASK_MATRIX_V2[
        ("software_engineering", "terminal-bench-2.1")]
    if terminal_trials % 5:
        raise ReleaseMatrixError("Terminal-Bench release count is not 5 trials/task")
    terminal_sources = terminal_trials // 5
    pinned_terminal = terminal_bench_21_task_digests()
    if terminal_task_digests != pinned_terminal:
        raise ReleaseMatrixError(
            "Terminal-Bench task digest catalog differs from the pinned lock")
    swe_locks = _software_locks(
        swebench_tasks, expected=swe_count, dataset="swe-bench-verified")
    pinned_swe_locks = _software_locks(
        ({"name": name, "ref": digest}
         for name, digest in swebench_verified_task_digests().items()),
        expected=swe_count, dataset="swe-bench-verified")
    if swe_locks != pinned_swe_locks:
        raise ReleaseMatrixError(
            "SWE-bench task digest catalog differs from the pinned lock")
    terminal_locks = _software_locks(
        ({"name": name, "ref": digest}
         for name, digest in terminal_task_digests.items()),
        expected=terminal_sources, dataset="terminal-bench-2.1")
    packs = _pack_map(custom_packs)

    tasks: list[dict[str, Any]] = []
    swe_rows = []
    for lock in swe_locks:
        row = {
            "taskId": f"swe-bench-verified:{lock.name}",
            "family": "software_engineering",
            "dataset": "swe-bench-verified",
            "sourceTaskId": lock.name,
            "sourceSha256": lock.sha256,
            "trialIndex": 1,
        }
        tasks.append(row)
        swe_rows.append(row)

    terminal_rows = []
    for lock in terminal_locks:
        for trial_index in range(1, 6):
            row = {
                "taskId": (
                    f"terminal-bench-2.1:{lock.name}:trial-{trial_index}"),
                "family": "software_engineering",
                "dataset": "terminal-bench-2.1",
                "sourceTaskId": lock.name,
                "sourceSha256": lock.sha256,
                "trialIndex": trial_index,
            }
            tasks.append(row)
            terminal_rows.append(row)

    components: list[dict[str, Any]] = [
        {
            "dataset": "swe-bench-verified",
            "sourceRevision": SWEBENCH_VERIFIED_DATASET_REF,
            "taskCount": len(swe_rows),
            "sha256": _component_digest(swe_rows),
        },
        {
            "dataset": "terminal-bench-2.1",
            "sourceRevision": TBENCH21_DATASET_REF,
            "sourceTaskCount": len(terminal_locks),
            "trialsPerTask": 5,
            "taskCount": len(terminal_rows),
            "sha256": _component_digest(terminal_rows),
        },
    ]
    for spec in CUSTOM_PACK_SPECS:
        pack = packs[(spec.family, spec.dataset)]
        if len(pack.tasks) != spec.task_count:
            raise ReleaseMatrixError(
                f"{spec.dataset} requires exactly {spec.task_count} tasks")
        pack_rows = []
        for task in pack.tasks:
            if task.family != spec.family or task.dataset != spec.dataset:
                raise ReleaseMatrixError(
                    f"{spec.dataset} contains a cross-family task")
            row = {
                "taskId": task.task_id,
                "family": spec.family,
                "dataset": spec.dataset,
                "sourceSha256": task.sha256,
                "packSha256": pack.manifest_sha256,
                "worldVersion": pack.world_version,
                "oracleType": task.oracle_type,
                "trialIndex": 1,
            }
            tasks.append(row)
            pack_rows.append(row)
        components.append({
            "dataset": spec.dataset,
            "family": spec.family,
            "worldVersion": pack.world_version,
            "backend": {
                "id": pack.backend_id,
                "sha256": pack.backend_sha256,
            },
            "taskCount": len(pack_rows),
            "manifestSha256": pack.manifest_sha256,
            "sha256": _component_digest(pack_rows),
        })

    try:
        validate_release_task_matrix_v2(tasks)
    except ValueError as exc:
        raise ReleaseMatrixError(str(exc)) from exc
    if len({row["taskId"] for row in tasks}) != len(tasks):
        raise ReleaseMatrixError("release task IDs are not globally unique")
    canonical = {
        "contractVersion": RELEASE_MATRIX_CONTRACT,
        "releaseId": str(release_id),
        "components": components,
        "tasks": tasks,
    }
    matrix_sha = _component_digest([canonical])
    return CompiledReleaseMatrix(
        release_id=str(release_id), sha256=matrix_sha,
        task_table=tuple(tasks), components=tuple(components))


def compile_release_matrix_from_paths(
    *, release_id: str, swebench_definitions_root: Path,
    custom_packs_root: Path,
) -> CompiledReleaseMatrix:
    """Load fully verified external assets, then invoke the pure compiler."""
    swebench = load_definitions(
        BENCHMARKS["swebench-verified"], swebench_definitions_root)
    return compile_release_matrix(
        release_id=release_id,
        swebench_tasks=swebench,
        terminal_task_digests=terminal_bench_21_task_digests(),
        custom_packs=load_all_custom_packs(custom_packs_root),
    )


def create_release_benchmark_manifest(
    *, matrix: CompiledReleaseMatrix, run_id: str, harness: dict,
    agent: dict, provider_face: str, provider_slot_id: str, thinking: str,
    experiment_arm: str,
    pair_id: str, comparison_role: str,
    tool_permissions: dict, prompt_digest: str, tool_schema_digest: str,
    sandbox: dict, retry_rule: dict, artifact_limits: dict,
    timeout_seconds: int,
    maximum_infrastructure_failure_rate: float,
    environment: dict | None = None,
) -> dict[str, Any]:
    """Bind a compiled dataset lock to one immutable benchmark run manifest."""
    manifest = build_manifest_v2(
        run_id=run_id, harness=harness, agent=agent,
        provider_face=provider_face, provider_slot_id=provider_slot_id,
        thinking=thinking,
        experiment_arm=experiment_arm, pair_id=pair_id,
        comparison_role=comparison_role,
        tool_permissions=tool_permissions, prompt_digest=prompt_digest,
        tool_schema_digest=tool_schema_digest,
        dataset_snapshot=matrix.dataset_snapshot(),
        task_table=matrix.task_table, sandbox=sandbox,
        retry_rule=retry_rule, artifact_limits=artifact_limits,
        timeout_seconds=timeout_seconds,
        maximum_infrastructure_failure_rate=(
            maximum_infrastructure_failure_rate),
        environment=environment,
    )
    if manifest.get("contractVersion") != CONTRACT_VERSION_V2:
        raise ReleaseMatrixError("benchmark manifest did not resolve to v2")
    return manifest


__all__ = [
    "CompiledReleaseMatrix", "RELEASE_MATRIX_CONTRACT", "ReleaseMatrixError",
    "SoftwareTaskLock", "compile_release_matrix",
    "compile_release_matrix_from_paths", "create_release_benchmark_manifest",
]
