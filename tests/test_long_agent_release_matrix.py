"""Exact-shape and immutability tests for the 1,845-task release compiler."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from evaluations.long_agent_release.cli import _load_config, _write_immutable, main
from evaluations.long_agent_release.contract import (
    CUSTOM_PACK_SPECS,
    FROZEN_TASK_PACK_VERSION,
    FROZEN_TASK_VERSION,
    FrozenTask,
    FrozenTaskPack,
    FrozenTaskPackError,
    load_frozen_task_pack,
)
from evaluations.long_agent_release.manifest import (
    ReleaseMatrixError,
    compile_release_matrix,
    create_release_benchmark_manifest,
)
from evaluations.swebench.constants import (
    swebench_verified_task_digests,
    terminal_bench_21_task_digests,
)
from lib.benchmark_contract import (
    RELEASE_TASK_MATRIX_V2,
    BenchmarkContractError,
    BenchmarkRecordV2,
)


pytestmark = pytest.mark.unit


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _swebench_tasks() -> list[dict]:
    return [
        {"name": name, "ref": digest}
        for name, digest in swebench_verified_task_digests().items()
    ]


def _pack(spec, *, task_count: int | None = None) -> FrozenTaskPack:
    count = spec.task_count if task_count is None else task_count
    tasks = tuple(FrozenTask(
        task_id=f"{spec.dataset}/task-{index:03d}",
        family=spec.family,
        dataset=spec.dataset,
        sha256=_sha(f"{spec.dataset}:{index}"),
        oracle_type="exact",
        tags=("required",),
        path=Path(f"/private/{spec.dataset}/task-{index:03d}.json"),
    ) for index in range(count))
    return FrozenTaskPack(
        family=spec.family,
        dataset=spec.dataset,
        world_version=f"{spec.dataset}-world-v1",
        backend_id="frozen-mcp-v1",
        backend_sha256=_sha(f"{spec.dataset}:backend"),
        manifest_sha256=_sha(f"{spec.dataset}:manifest"),
        tasks=tasks,
    )


def _matrix():
    return compile_release_matrix(
        release_id="kimi-codex-release-2026-08",
        swebench_tasks=_swebench_tasks(),
        terminal_task_digests=terminal_bench_21_task_digests(),
        custom_packs=[_pack(spec) for spec in CUSTOM_PACK_SPECS],
    )


def _task_payload(spec, task_id: str) -> dict:
    shapes = {
        "integrated_multi_tool": {
            "toolNames": ["read_files", "web_search"],
            "dependencyEdges": 1,
            "expectedResultBytes": 8192,
        },
        "long_continuity": {
            "turns": 20, "hiddenFactCount": 1,
            "constraintChangeCount": 1,
        },
        "frozen_research": {
            "sourceCount": 2, "frozenSources": True,
            "requiresCitations": True,
        },
        "long_writing": {
            "revisionStages": 4, "factCount": 1,
            "formatConstraintCount": 1, "styleConstraintCount": 1,
        },
        "fault_recovery": {
            "injections": ["429"], "requiresRecoveryOracle": True,
        },
    }
    return {
        "contractVersion": FROZEN_TASK_VERSION,
        "taskId": task_id,
        "family": spec.family,
        "dataset": spec.dataset,
        "instructions": "Complete the frozen task.",
        "oracle": {"type": "exact", "definition": {"answer": 42}},
        "simulator": {
            "type": "state_machine", "definition": {"initial": "ready"},
        },
        "permissions": {"profile": "frozen-read-write"},
        "shape": shapes[spec.family],
        "tags": ["required"],
    }


def _write_small_pack(tmp_path: Path, *, tamper_hash: bool = False,
                      task_path: str | None = None):
    spec = replace(CUSTOM_PACK_SPECS[0], task_count=2)
    root = tmp_path / spec.dataset
    root.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(mode=0o700)
    rows = []
    for index in range(2):
        task_id = f"{spec.dataset}/task-{index}"
        path = tasks_dir / f"task-{index}.json"
        raw = json.dumps(
            _task_payload(spec, task_id), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode()
        path.write_bytes(raw)
        rows.append({
            "taskId": task_id,
            "path": (task_path if index == 0 and task_path is not None
                     else f"tasks/task-{index}.json"),
            "sha256": ("0" * 64 if index == 0 and tamper_hash
                       else hashlib.sha256(raw).hexdigest()),
        })
    manifest = {
        "contractVersion": FROZEN_TASK_PACK_VERSION,
        "family": spec.family,
        "dataset": spec.dataset,
        "taskCount": 2,
        "frozen": True,
        "worldVersion": "world-v1",
        "backend": {"id": "frozen-mcp", "sha256": _sha("backend")},
        "requiredTags": ["required"],
        "tasks": rows,
    }
    path = root / "pack.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, spec


def test_release_compiler_builds_exact_deterministic_1845_shape():
    matrix = _matrix()
    repeated = _matrix()

    assert matrix.task_count == sum(RELEASE_TASK_MATRIX_V2.values()) == 1845
    assert matrix.sha256 == repeated.sha256
    assert matrix.task_table == repeated.task_table
    assert matrix.dataset_snapshot()["releaseMatrix"] is True
    assert matrix.dataset_snapshot()["sha256"] == matrix.sha256
    terminal = [row for row in matrix.task_table
                if row["dataset"] == "terminal-bench-2.1"]
    assert len(terminal) == 445
    assert {row["trialIndex"] for row in terminal} == {1, 2, 3, 4, 5}
    assert len({row["taskId"] for row in matrix.task_table}) == 1845


def test_release_compiler_rejects_missing_pack_and_cardinality_drift():
    packs = [_pack(spec) for spec in CUSTOM_PACK_SPECS]
    with pytest.raises(ReleaseMatrixError, match="custom pack set mismatch"):
        compile_release_matrix(
            release_id="release", swebench_tasks=_swebench_tasks(),
            terminal_task_digests=terminal_bench_21_task_digests(),
            custom_packs=packs[:-1])

    packs[0] = _pack(CUSTOM_PACK_SPECS[0], task_count=199)
    with pytest.raises(ReleaseMatrixError, match="exactly 200"):
        compile_release_matrix(
            release_id="release", swebench_tasks=_swebench_tasks(),
            terminal_task_digests=terminal_bench_21_task_digests(),
            custom_packs=packs)

    with pytest.raises(ReleaseMatrixError, match="exactly 500"):
        compile_release_matrix(
            release_id="release", swebench_tasks=_swebench_tasks()[:-1],
            terminal_task_digests=terminal_bench_21_task_digests(),
            custom_packs=[_pack(spec) for spec in CUSTOM_PACK_SPECS])

    changed_swe = _swebench_tasks()
    changed_swe[0] = {
        **changed_swe[0], "ref": "sha256:" + _sha("changed-swe")}
    with pytest.raises(ReleaseMatrixError, match="SWE-bench.*pinned lock"):
        compile_release_matrix(
            release_id="release", swebench_tasks=changed_swe,
            terminal_task_digests=terminal_bench_21_task_digests(),
            custom_packs=[_pack(spec) for spec in CUSTOM_PACK_SPECS])

    changed_terminal = terminal_bench_21_task_digests()
    first = next(iter(changed_terminal))
    changed_terminal[first] = "sha256:" + _sha("changed")
    with pytest.raises(ReleaseMatrixError, match="pinned lock"):
        compile_release_matrix(
            release_id="release", swebench_tasks=_swebench_tasks(),
            terminal_task_digests=changed_terminal,
            custom_packs=[_pack(spec) for spec in CUSTOM_PACK_SPECS])


def test_compiled_matrix_binds_to_valid_benchmark_v2_manifest():
    matrix = _matrix()
    manifest = create_release_benchmark_manifest(
        matrix=matrix, run_id="release-run",
        harness={"name": "paired-harness", "version": "1",
                 "commitSha256": _sha("harness")},
        agent={"name": "tofu", "version": "test",
               "commitSha256": _sha("agent")},
        provider_face="meituan-chat", provider_slot_id="kimi-slot-fixture",
        thinking="high",
        experiment_arm="combined_v2", pair_id="release-pair",
        comparison_role="candidate",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest=_sha("prompt"), tool_schema_digest=_sha("tools"),
        sandbox={"kind": "rootless-qemu", "networkPolicy": "frozen"},
        retry_rule={"maxInfrastructureRetries": 1,
                    "retryableFailureClasses": ["infrastructure"]},
        artifact_limits={"maximumArtifactBytes": 64 * 1024 * 1024,
                         "maximumTaskArtifactBytes": 128 * 1024 * 1024,
                         "maximumRunArtifactBytes": 128 * 1024**3},
        timeout_seconds=3600, maximum_infrastructure_failure_rate=0.02,
        environment={"gitCommit": _sha("repo")},
    )

    assert manifest["contractVersion"] == "tofu-benchmark/v2"
    assert len(manifest["tasks"]) == 1845
    assert manifest["datasetSnapshot"]["sha256"] == matrix.sha256
    assert manifest["limits"]["costBudgetUsd"] is None
    assert manifest["comparisonControls"]["remoteCompactionV2"] is False

    wrong_baseline = {**manifest, "comparisonRole": "baseline"}
    with pytest.raises(BenchmarkContractError, match="Codex 0.149.1"):
        BenchmarkRecordV2(wrong_baseline)

    invalid_limits = json.loads(json.dumps(manifest))
    invalid_limits["artifactLimits"]["maximumArtifactBytes"] = (
        invalid_limits["artifactLimits"]["maximumRunArtifactBytes"] + 1)
    with pytest.raises(BenchmarkContractError, match="artifact <= task <= run"):
        BenchmarkRecordV2(invalid_limits)


def test_task_pack_loader_verifies_hash_paths_shape_and_coverage(tmp_path):
    path, spec = _write_small_pack(tmp_path)
    pack = load_frozen_task_pack(path, spec)

    assert len(pack.tasks) == 2
    assert pack.tasks[0].task_id == f"{spec.dataset}/task-0"
    assert pack.world_version == "world-v1"
    assert pack.manifest_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    bad_path, bad_spec = _write_small_pack(
        tmp_path / "bad-hash", tamper_hash=True)
    with pytest.raises(FrozenTaskPackError, match="SHA-256 mismatch"):
        load_frozen_task_pack(bad_path, bad_spec)


def test_task_pack_loader_rejects_escape_and_symlink(tmp_path):
    escaped, spec = _write_small_pack(
        tmp_path / "escape", task_path="../outside.json")
    with pytest.raises(FrozenTaskPackError, match="escapes"):
        load_frozen_task_pack(escaped, spec)

    path, spec = _write_small_pack(tmp_path / "symlink")
    manifest = json.loads(path.read_text())
    target = path.parent / "tasks" / "task-0.json"
    alias = path.parent / "tasks" / "alias.json"
    alias.symlink_to(target)
    manifest["tasks"][0]["path"] = "tasks/alias.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FrozenTaskPackError, match="symlink"):
        load_frozen_task_pack(path, spec)


def test_cli_preflight_fails_closed_when_real_assets_are_absent(
        tmp_path, capsys):
    missing_swe = tmp_path / "missing-swe"
    missing_custom = tmp_path / "missing-custom"
    exit_code = main([
        "preflight", "--release-id", "release",
        "--swebench-definitions-root", str(missing_swe),
        "--custom-packs-root", str(missing_custom), "--json",
    ])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert result["status"] == "invalid"
    assert "missing" in result["error"].lower() \
        or "invalid" in result["error"].lower()


def test_manifest_write_is_immutable_and_config_rejects_bool_timeout(tmp_path):
    output = tmp_path / "artifacts" / "manifest.json"
    assert _write_immutable(output, b'{"a":1}\n') == "created"
    assert _write_immutable(output, b'{"a":1}\n') == "unchanged"
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _write_immutable(output, b'{"a":2}\n')

    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "contractVersion": "tofu-long-agent-release-config/v2",
        "releaseId": "release", "runId": "run",
        "harness": {}, "agent": {}, "providerFace": "meituan-chat",
        "providerSlotId": "kimi-slot-fixture",
        "thinking": "high", "experimentArm": "combined_v2",
        "pairId": "pair", "comparisonRole": "candidate",
        "toolPermissions": {},
        "promptDigest": _sha("prompt"),
        "toolSchemaDigest": _sha("tools"), "sandbox": {},
        "retryRule": {}, "artifactLimits": {}, "timeoutSeconds": True,
        "maximumInfrastructureFailureRate": 0.02,
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="timeoutSeconds"):
        _load_config(config)
