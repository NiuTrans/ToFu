"""CLI for fail-closed release-matrix preflight and manifest compilation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .manifest import (
    RELEASE_MATRIX_CONTRACT,
    compile_release_matrix_from_paths,
    create_release_benchmark_manifest,
)
from .harbor_codex_export import export_codex_harbor_run
from .harbor_tofu_export import export_tofu_harbor_run
from .report import analyze_release_pair
from .run_store import (
    RUN_STORE_CONTRACT,
    audit_release_attempts,
    audit_release_pair,
    audit_release_run,
    claim_release_task_attempts,
    fail_release_execution_before_dispatch,
    fail_release_task_attempt,
    finalize_release_run,
    initialize_release_run,
    load_release_record,
    record_release_task,
    release_task_retry_evidence,
    store_run_artifact,
)


RELEASE_CONFIG_VERSION = "tofu-long-agent-release-config/v2"
_CONFIG_FIELDS = frozenset({
    "contractVersion", "releaseId", "runId", "harness", "agent",
    "providerFace", "providerSlotId", "thinking", "experimentArm", "pairId",
    "comparisonRole", "toolPermissions", "promptDigest",
    "toolSchemaDigest", "sandbox", "retryRule", "timeoutSeconds",
    "maximumInfrastructureFailureRate", "artifactLimits", "environment",
})
_REQUIRED_CONFIG_FIELDS = _CONFIG_FIELDS - {"environment"}


def _load_config(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("release config must be a regular non-symlink file")
    try:
        def object_pairs(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError(
                        f"release config contains duplicate key {key!r}")
                result[key] = item
            return result

        def invalid_constant(value):
            raise ValueError(
                f"release config contains non-finite number {value}")

        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=invalid_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid release config: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("release config must be a JSON object")
    if value.get("contractVersion") != RELEASE_CONFIG_VERSION:
        raise ValueError("release config version mismatch")
    missing = sorted(_REQUIRED_CONFIG_FIELDS - set(value))
    unknown = sorted(set(value) - _CONFIG_FIELDS)
    if missing or unknown:
        raise ValueError(
            f"release config fields mismatch: missing={missing}, unknown={unknown}")
    for field in ("harness", "agent", "toolPermissions", "sandbox",
                  "retryRule", "artifactLimits"):
        if not isinstance(value.get(field), dict):
            raise ValueError(f"release config {field} must be an object")
    if "environment" in value and not isinstance(value["environment"], dict):
        raise ValueError("release config environment must be an object")
    timeout = value.get("timeoutSeconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("release config timeoutSeconds must be positive")
    failure_rate = value.get("maximumInfrastructureFailureRate")
    if isinstance(failure_rate, bool) or not isinstance(failure_rate, (int, float)) \
            or not math.isfinite(float(failure_rate)) \
            or not 0 <= float(failure_rate) <= 1:
        raise ValueError(
            "release config maximumInfrastructureFailureRate must be 0..1")
    return value


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> str:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ValueError("manifest output must be a regular file")
        if path.read_bytes() == payload:
            return "unchanged"
        raise FileExistsError(
            f"refusing to overwrite immutable benchmark manifest: {path}")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
    return "created"


def _matrix(args: argparse.Namespace, *, release_id: str):
    return compile_release_matrix_from_paths(
        release_id=release_id,
        swebench_definitions_root=args.swebench_definitions_root,
        custom_packs_root=args.custom_packs_root,
    )


def _report(matrix, *, status: str = "valid") -> dict[str, Any]:
    return {
        "contractVersion": RELEASE_MATRIX_CONTRACT,
        "status": status,
        "releaseId": matrix.release_id,
        "sha256": matrix.sha256,
        "taskCount": matrix.task_count,
        "components": [dict(row) for row in matrix.components],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluations.long_agent_release",
        description=(
            "Validate and compile the frozen 1,845-task Kimi/Codex matrix"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser(
        "preflight", help="Verify all frozen task assets without writing")
    preflight.add_argument("--release-id", required=True)
    preflight.add_argument(
        "--swebench-definitions-root", type=Path, required=True)
    preflight.add_argument("--custom-packs-root", type=Path, required=True)
    preflight.add_argument("--json", action="store_true")

    manifest = sub.add_parser(
        "manifest", help="Create one immutable tofu-benchmark/v2 manifest")
    manifest.add_argument("--config", type=Path, required=True)
    manifest.add_argument(
        "--swebench-definitions-root", type=Path, required=True)
    manifest.add_argument("--custom-packs-root", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)

    run_init = sub.add_parser(
        "run-init", help="Initialize one immutable per-arm evidence store")
    run_init.add_argument("--manifest", type=Path, required=True)
    run_init.add_argument("--run-root", type=Path, required=True)

    artifact = sub.add_parser(
        "store-artifact", help="Store one task artifact by content digest")
    artifact.add_argument("--run-root", type=Path, required=True)
    artifact.add_argument("--task-id", required=True)
    artifact.add_argument("--kind", required=True)
    artifact.add_argument("--source", type=Path, required=True)

    record = sub.add_parser(
        "record-task", help="Commit one manifest-bound v2 task record")
    record.add_argument("--run-root", type=Path, required=True)
    record.add_argument("--record", type=Path, required=True)

    attempt_start = sub.add_parser(
        "attempt-start",
        help="Preclaim task attempts before any paid runner dispatch",
    )
    attempt_start.add_argument("--run-root", type=Path, required=True)
    attempt_start.add_argument(
        "--task-id", action="append", required=True, dest="task_ids")
    attempt_start.add_argument("--execution-id", required=True)
    attempt_start.add_argument("--runner-kind", required=True)

    attempt_fail = sub.add_parser(
        "attempt-fail",
        help="Close one infrastructure attempt with retained cost evidence",
    )
    attempt_fail.add_argument("--run-root", type=Path, required=True)
    attempt_fail.add_argument("--task-id", required=True)
    attempt_fail.add_argument("--execution-id", required=True)
    attempt_fail.add_argument("--code", required=True)
    attempt_fail.add_argument(
        "--evidence", type=Path, required=True,
        help=(
            "JSON object with modelUsages, paidToolCostUsd, artifacts, "
            "and noPaidCalls"
        ),
    )

    attempt_status = sub.add_parser(
        "attempt-status", help="Audit pre-dispatch claims and outcomes")
    attempt_status.add_argument("--run-root", type=Path, required=True)
    attempt_status.add_argument("--require-complete", action="store_true")

    attempt_retries = sub.add_parser(
        "attempt-retries",
        help="Project retained failed attempts into the final task retry rows",
    )
    attempt_retries.add_argument("--run-root", type=Path, required=True)
    attempt_retries.add_argument("--task-id", required=True)

    attempt_fail_execution = sub.add_parser(
        "attempt-fail-execution",
        help="Close every claim when dispatch made provably zero paid calls",
    )
    attempt_fail_execution.add_argument("--run-root", type=Path, required=True)
    attempt_fail_execution.add_argument("--execution-id", required=True)
    attempt_fail_execution.add_argument("--code", required=True)

    status = sub.add_parser(
        "run-status", help="Audit task completeness and artifact digests")
    status.add_argument("--run-root", type=Path, required=True)
    status.add_argument("--require-complete", action="store_true")

    finalize = sub.add_parser(
        "run-finalize", help="Assemble immutable manifest-ordered JSONL")
    finalize.add_argument("--run-root", type=Path, required=True)

    pair = sub.add_parser(
        "pair-status", help="Audit frozen controls across both paired arms")
    pair.add_argument("--baseline-root", type=Path, required=True)
    pair.add_argument("--candidate-root", type=Path, required=True)
    pair.add_argument("--require-complete", action="store_true")

    harbor_export = sub.add_parser(
        "export-codex-harbor",
        help="Project one audited formal Harbor Codex slice into a release run",
    )
    harbor_export.add_argument("--harbor-run-dir", type=Path, required=True)
    harbor_export.add_argument("--run-root", type=Path, required=True)

    tofu_harbor_export = sub.add_parser(
        "export-tofu-harbor",
        help=(
            "Project one audited formal Harbor production-Tofu slice into "
            "a release run"),
    )
    tofu_harbor_export.add_argument(
        "--harbor-run-dir", type=Path, required=True)
    tofu_harbor_export.add_argument("--run-root", type=Path, required=True)

    pair_report = sub.add_parser(
        "pair-report",
        help="Create an immutable diagnostic or full conjunctive pair report",
    )
    pair_report.add_argument("--baseline-root", type=Path, required=True)
    pair_report.add_argument("--candidate-root", type=Path, required=True)
    pair_report.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run-init":
            report = initialize_release_run(
                args.run_root, load_release_record(args.manifest))
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "store-artifact":
            artifact = store_run_artifact(
                args.run_root, task_id=args.task_id,
                kind=args.kind, source=args.source)
            print(json.dumps({
                "contractVersion": RUN_STORE_CONTRACT,
                "status": "stored",
                "taskId": args.task_id,
                "artifact": artifact,
            }, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "record-task":
            report = record_release_task(
                args.run_root, load_release_record(args.record))
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "attempt-start":
            report = claim_release_task_attempts(
                args.run_root, task_ids=args.task_ids,
                execution_id=args.execution_id,
                runner_kind=args.runner_kind,
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "attempt-fail":
            evidence = load_release_record(args.evidence)
            report = fail_release_task_attempt(
                args.run_root, task_id=args.task_id,
                execution_id=args.execution_id, code=args.code,
                model_usages=evidence.get("modelUsages"),
                paid_tool_cost_usd=evidence.get("paidToolCostUsd"),
                artifacts=evidence.get("artifacts"),
                no_paid_calls=evidence.get("noPaidCalls"),
                task_started_at_unix_ms=evidence.get("taskStartedAtUnixMs"),
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "attempt-status":
            report = audit_release_attempts(
                args.run_root, require_complete=args.require_complete)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["valid"] and (
                report["complete"] or not args.require_complete) else 2

        if args.command == "attempt-retries":
            retries = release_task_retry_evidence(
                args.run_root, task_id=args.task_id)
            print(json.dumps({
                "contractVersion": RUN_STORE_CONTRACT,
                "taskId": args.task_id,
                "retries": retries,
            }, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "attempt-fail-execution":
            report = fail_release_execution_before_dispatch(
                args.run_root, execution_id=args.execution_id, code=args.code)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "run-status":
            report = audit_release_run(
                args.run_root, require_complete=args.require_complete)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["valid"] and (
                report["complete"] or not args.require_complete) else 2

        if args.command == "run-finalize":
            report = finalize_release_run(args.run_root)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "pair-status":
            report = audit_release_pair(
                baseline_root=args.baseline_root,
                candidate_root=args.candidate_root,
                require_complete=args.require_complete)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["valid"] and (
                report["pairReady"] or not args.require_complete) else 2

        if args.command == "export-codex-harbor":
            report = export_codex_harbor_run(
                harbor_run_dir=args.harbor_run_dir,
                release_run_root=args.run_root,
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "export-tofu-harbor":
            report = export_tofu_harbor_run(
                harbor_run_dir=args.harbor_run_dir,
                release_run_root=args.run_root,
            )
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "pair-report":
            report = analyze_release_pair(
                baseline_root=args.baseline_root,
                candidate_root=args.candidate_root,
            )
            status = _write_immutable(args.output, _canonical_bytes(report))
            print(json.dumps({
                "contractVersion": report["contractVersion"],
                "status": status,
                "output": str(args.output.expanduser().resolve()),
                "fullFrozenMatrix": report["fullFrozenMatrix"],
                "releaseEligible": report["releaseDecision"]["releaseEligible"],
                "claim": report["claim"],
            }, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "preflight":
            matrix = _matrix(args, release_id=args.release_id)
            report = _report(matrix)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"release matrix valid: {matrix.task_count} tasks; "
                    f"sha256={matrix.sha256}")
            return 0

        config = _load_config(args.config)
        matrix = _matrix(args, release_id=str(config["releaseId"]))
        benchmark_manifest = create_release_benchmark_manifest(
            matrix=matrix, run_id=str(config["runId"]),
            harness=config["harness"], agent=config["agent"],
            provider_face=str(config["providerFace"]),
            provider_slot_id=str(config["providerSlotId"]),
            thinking=str(config["thinking"]),
            experiment_arm=str(config["experimentArm"]),
            pair_id=str(config["pairId"]),
            comparison_role=str(config["comparisonRole"]),
            tool_permissions=config["toolPermissions"],
            prompt_digest=str(config["promptDigest"]),
            tool_schema_digest=str(config["toolSchemaDigest"]),
            sandbox=config["sandbox"], retry_rule=config["retryRule"],
            artifact_limits=config["artifactLimits"],
            timeout_seconds=int(config["timeoutSeconds"]),
            maximum_infrastructure_failure_rate=float(
                config["maximumInfrastructureFailureRate"]),
            environment=config.get("environment"),
        )
        payload = _canonical_bytes(benchmark_manifest)
        write_status = _write_immutable(args.output, payload)
        report = {
            **_report(matrix, status=write_status),
            "output": str(args.output.expanduser().resolve()),
            "manifestSha256": hashlib.sha256(payload).hexdigest(),
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, TypeError, ValueError) as exc:
        contract_version = (
            RUN_STORE_CONTRACT
            if args.command in {
                "run-init", "store-artifact", "record-task", "run-status",
                "run-finalize", "pair-status",
                "attempt-start", "attempt-fail", "attempt-status",
                "attempt-fail-execution", "attempt-retries",
                "export-codex-harbor",
                "export-tofu-harbor",
                "pair-report",
            }
            else RELEASE_MATRIX_CONTRACT
        )
        print(json.dumps({
            "contractVersion": contract_version,
            "status": "invalid",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, sort_keys=True))
        return 2


__all__ = ["RELEASE_CONFIG_VERSION", "build_parser", "main"]
