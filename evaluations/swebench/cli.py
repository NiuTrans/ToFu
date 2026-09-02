from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from evaluations.codex_kimi_proxy.codex_contract import CODEX_VERSION

from .artifacts import prepare_output_root
from .audit import audit_run
from .constants import (
    BENCHMARKS,
    DEFAULT_AGENT_BACKEND,
    DEFAULT_BENCHMARK,
    ISOLATED_BACKENDS,
    default_output_root,
)
from .harbor_runner import HarborRunSpec, resume_harbor_run, start_harbor_run
from .images import (
    load_definitions,
    prepare_caches,
    prepare_definitions,
    prepare_image_store,
)
from .official import grade_predictions
from .preflight import harbor_checks, official_checks, render_checks
from .rootless_qemu import RootlessQemuSettings
from .codex_kimi_runtime import (
    CODEX_KIMI_AGENT,
    CODEX_KIMI_PROFILE_ID,
    CodexKimiBaselineSettings,
)
from .tofu_kimi_runtime import (
    TOFU_KIMI_AGENT,
    TOFU_KIMI_PROFILE_ID,
    TofuKimiCandidateSettings,
)


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-root",
        type=Path,
        default=default_output_root(),
        help="Artifact root (default: TOFU_EVAL_ROOT or ~/.local/state/tofu-evals/agent-benchmarks)",
    )


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _add_rootless_qemu(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rootless-base-disk",
        type=Path,
        default=_env_path("ROOTLESS_VM_BASE_DISK"),
    )
    parser.add_argument(
        "--rootless-image-store",
        type=Path,
        default=_env_path("ROOTLESS_VM_IMAGE_STORE"),
    )
    parser.add_argument(
        "--rootless-qemu",
        type=Path,
        default=_env_path("ROOTLESS_VM_QEMU"),
    )
    parser.add_argument(
        "--rootless-qemu-img",
        type=Path,
        default=_env_path("ROOTLESS_VM_QEMU_IMG"),
    )
    parser.add_argument("--rootless-state-root", type=Path)
    parser.add_argument("--rootless-cache-root", type=Path)
    parser.add_argument("--rootless-egress-max-gib", type=int, default=4)
    parser.add_argument("--rootless-egress-concurrency", type=int, default=16)
    parser.add_argument("--rootless-vm-cpus", type=int, default=2)


def _rootless_settings(args: argparse.Namespace) -> RootlessQemuSettings | None:
    if args.backend != "rootless-qemu":
        return None
    if args.rootless_base_disk is None or args.rootless_image_store is None:
        return None
    return RootlessQemuSettings(
        base_disk=args.rootless_base_disk,
        image_store=args.rootless_image_store,
        qemu_path=args.rootless_qemu,
        qemu_img_path=args.rootless_qemu_img,
        state_root=args.rootless_state_root,
        prepared_cache_root=args.rootless_cache_root,
        egress_max_gib=args.rootless_egress_max_gib,
        egress_global_concurrency=args.rootless_egress_concurrency,
        vm_cpus=args.rootless_vm_cpus,
    )


def _codex_kimi_settings(
    args: argparse.Namespace,
) -> tuple[str, str | None, CodexKimiBaselineSettings | None]:
    if args.agent not in {CODEX_KIMI_PROFILE_ID, CODEX_KIMI_AGENT}:
        return args.agent, args.agent_version, None
    if args.codex_binary is None or not args.codex_sha256 \
            or not args.kimi_provider_face or not args.kimi_slot_id:
        raise ValueError(
            "codex-kimi requires binary/hash plus --kimi-provider-face and "
            "--kimi-slot-id (or their TOFU_* environment variables)"
        )
    return (
        CODEX_KIMI_AGENT,
        args.agent_version or CODEX_VERSION,
        CodexKimiBaselineSettings(
            codex_binary=args.codex_binary,
            codex_sha256=args.codex_sha256,
            provider_face=args.kimi_provider_face,
            provider_slot_id=args.kimi_slot_id,
            agent_timeout_seconds=args.codex_agent_timeout_seconds,
            upstream_base_url_env=args.kimi_base_url_env,
            upstream_api_key_env=args.kimi_api_key_env,
        ),
    )


def _tofu_kimi_settings(
    args: argparse.Namespace,
    *,
    agent: str,
    agent_version: str | None,
) -> tuple[str, str | None, TofuKimiCandidateSettings | None]:
    if agent not in {TOFU_KIMI_PROFILE_ID, TOFU_KIMI_AGENT}:
        return agent, agent_version, None
    if args.tofu_runtime_config is None \
            or not args.tofu_experiment_arm \
            or not args.kimi_provider_face or not args.kimi_slot_id:
        raise ValueError(
            "tofu-kimi requires --tofu-runtime-config, "
            "--tofu-experiment-arm, --kimi-provider-face, and --kimi-slot-id"
        )
    config_path = args.tofu_runtime_config.expanduser().resolve(strict=True)
    if not config_path.is_file():
        raise ValueError("--tofu-runtime-config must be a regular JSON file")
    runtime_config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(runtime_config, dict):
        raise ValueError("--tofu-runtime-config must contain a JSON object")
    if agent_version is None:
        from tofu_agent import __version__ as agent_version
    settings = TofuKimiCandidateSettings(
        provider_face=args.kimi_provider_face,
        provider_slot_id=args.kimi_slot_id,
        agent_version=agent_version,
        experiment_arm=args.tofu_experiment_arm,
        runtime_config=runtime_config,
        agent_timeout_seconds=args.tofu_agent_timeout_seconds,
        command_timeout_seconds=args.tofu_command_timeout_seconds,
        upstream_base_url_env=args.kimi_base_url_env,
        upstream_api_key_env=args.kimi_api_key_env,
        thinking_format=args.tofu_thinking_format,
    )
    settings.validate()
    return TOFU_KIMI_AGENT, agent_version, settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluations.swebench",
        description="Audited SWE-bench Verified and Terminal-Bench 2.1 orchestration",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Fail-closed runtime and artifact preflight")
    doctor.add_argument(
        "--backend",
        choices=ISOLATED_BACKENDS,
        default=os.environ.get("TOFU_EVAL_BACKEND", DEFAULT_AGENT_BACKEND),
    )
    doctor.add_argument("--benchmark", choices=tuple(BENCHMARKS), default=DEFAULT_BENCHMARK)
    doctor.add_argument("--task", action="append", default=[], dest="tasks")
    doctor.add_argument("--harbor-bin", default="harbor")
    doctor.add_argument("--official", action="store_true", help="Check upstream patch grader instead of Harbor")
    doctor.add_argument("--json", action="store_true")
    _add_output(doctor)
    _add_rootless_qemu(doctor)

    run = sub.add_parser("run", help="Run agent × model × task trials through Harbor")
    run.add_argument("--agent", required=True)
    run.add_argument("--model", action="append", required=True, dest="models")
    run.add_argument(
        "--backend",
        choices=ISOLATED_BACKENDS,
        default=os.environ.get("TOFU_EVAL_BACKEND", DEFAULT_AGENT_BACKEND),
    )
    run.add_argument("--benchmark", choices=tuple(BENCHMARKS), default=DEFAULT_BENCHMARK)
    run.add_argument("--task", action="append", default=[], dest="tasks")
    run.add_argument("--limit", type=int)
    run.add_argument(
        "--attempts",
        type=int,
        help="Trials per task (defaults: SWE-bench=1, Terminal-Bench 2.1=5)",
    )
    run.add_argument(
        "--concurrency",
        type=int,
        help="Concurrent trials (default: 1 for local Singularity, otherwise 16)",
    )
    run.add_argument("--agent-concurrency", type=int)
    run.add_argument(
        "--max-retries",
        type=int,
        help="Harbor retries (default 0 for codex-kimi, otherwise 2)",
    )
    run.add_argument("--timeout-multiplier", type=float, default=1.0)
    run.add_argument("--secret-env", action="append", default=[])
    run.add_argument("--agent-host", action="append", default=[])
    run.add_argument("--reasoning-effort")
    run.add_argument("--agent-version")
    run.add_argument(
        "--codex-binary",
        type=Path,
        default=_env_path("TOFU_CODEX_01491_BINARY"),
        help="Pinned Codex 0.149.1 binary for --agent codex-kimi",
    )
    run.add_argument(
        "--codex-sha256",
        default=os.environ.get("TOFU_CODEX_01491_SHA256"),
        help="Expected SHA-256 for the pinned Codex binary",
    )
    run.add_argument(
        "--codex-agent-timeout-seconds",
        type=int,
        default=3600,
        help="Pinned Codex execution timeout used by the release manifest",
    )
    run.add_argument(
        "--tofu-runtime-config",
        type=Path,
        help=(
            "Frozen non-secret AgentRuntime config JSON for --agent tofu-kimi"
        ),
    )
    run.add_argument(
        "--tofu-experiment-arm",
        help="Pre-registered candidate arm represented by the runtime config",
    )
    run.add_argument(
        "--tofu-agent-timeout-seconds",
        type=int,
        default=3600,
    )
    run.add_argument(
        "--tofu-command-timeout-seconds",
        type=int,
        default=480,
    )
    run.add_argument(
        "--tofu-thinking-format",
        default="",
        help="Optional public Kimi thinking-format adapter identifier",
    )
    run.add_argument(
        "--kimi-base-url-env",
        default="KIMI_CHAT_BASE_URL",
        help="Host-only environment variable containing the Kimi Chat base URL",
    )
    run.add_argument(
        "--kimi-api-key-env",
        default="KIMI_API_KEY",
        help="Host-only environment variable containing the Kimi API key",
    )
    run.add_argument(
        "--kimi-provider-face",
        default=os.environ.get("TOFU_KIMI_PROVIDER_FACE"),
        help="Non-secret provider face frozen into paired benchmark manifests",
    )
    run.add_argument(
        "--kimi-slot-id",
        default=os.environ.get("TOFU_KIMI_SLOT_ID"),
        help="Non-secret Kimi slot identifier shared by both paired arms",
    )
    run.add_argument("--harbor-bin", default="harbor")
    run.add_argument("--run-id")
    run.add_argument(
        "--release-run-root", type=Path,
        help=(
            "Initialized matching release run store to preclaim before paid "
            "formal dispatch; required for release-eligible export"
        ),
    )
    run.add_argument("--dry-run", action="store_true")
    _add_output(run)
    _add_rootless_qemu(run)

    prepare = sub.add_parser(
        "prepare-rootless",
        help="Download pinned task definitions and build resumable QEMU image caches",
    )
    prepare.add_argument("--benchmark", choices=tuple(BENCHMARKS), default=DEFAULT_BENCHMARK)
    prepare.add_argument(
        "--phase",
        choices=("definitions", "assets", "cache", "all"),
        default="all",
    )
    prepare.add_argument("--definitions-root", type=Path)
    prepare.add_argument(
        "--task",
        action="append",
        default=[],
        dest="tasks",
        help="Prepare only this exact Harbor task name; repeat as needed",
    )
    prepare.add_argument("--crane", default=os.environ.get("CRANE", "auto"))
    prepare.add_argument("--archive-tool", default="tar")
    prepare.add_argument(
        "--genisoimage",
        help="Optional external ISO writer; the default uses pinned pycdlib",
    )
    prepare.add_argument("--definition-workers", type=int, default=4)
    prepare.add_argument("--asset-workers", type=int, default=4)
    prepare.add_argument("--cache-workers", type=int, default=2)
    _add_output(prepare)
    _add_rootless_qemu(prepare)

    resume = sub.add_parser("resume", help="Resume only unfinished Harbor trials")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--harbor-bin", default="harbor")
    resume.add_argument(
        "--release-run-root", type=Path,
        help="Same private release store used by the tracked original run",
    )

    grade = sub.add_parser("grade", help="Grade patches with the unmodified upstream harness")
    grade.add_argument("--predictions", required=True, type=Path)
    grade.add_argument(
        "--backend",
        choices=("modal", "docker"),
        default=os.environ.get("TOFU_OFFICIAL_EVAL_BACKEND", "modal"),
    )
    grade.add_argument("--workers", type=int, default=4)
    grade.add_argument("--timeout", type=int, default=1800)
    grade.add_argument("--run-id")
    grade.add_argument("--dry-run", action="store_true")
    _add_output(grade)

    audit = sub.add_parser("audit", help="Verify cardinality, isolation config and terminal state")
    audit.add_argument("run_dir", type=Path)
    audit.add_argument("--allow-errors", action="store_true")
    audit.add_argument("--json", action="store_true")
    return parser


def _print_audit(report: dict) -> None:
    for check in report["checks"]:
        status = "PASS" if check["ok"] else "FAIL"
        print(f"[{status}] {check['name']}: {check['detail']}")
    print(f"overall: {'PASS' if report['ok'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-rootless":
            definition = BENCHMARKS[args.benchmark]
            output_root = prepare_output_root(args.output_root)
            definitions_root = args.definitions_root or (
                output_root / ".definitions" / args.benchmark
            )
            image_store = args.rootless_image_store or (
                output_root / ".image-store" / args.benchmark
            )
            cache_root = args.rootless_cache_root or (
                output_root / ".image-cache" / "rootless-qemu"
            )
            if args.phase in {"definitions", "all"}:
                tasks = prepare_definitions(
                    definition,
                    definitions_root,
                    workers=args.definition_workers,
                )
            else:
                tasks = load_definitions(definition, definitions_root)
            if args.tasks:
                requested = set(args.tasks)
                if len(requested) != len(args.tasks):
                    raise ValueError("--task values must be unique")
                by_name = {task.name: task for task in tasks}
                missing = sorted(requested - by_name.keys())
                if missing:
                    raise ValueError(f"unknown task definitions: {missing}")
                tasks = [by_name[name] for name in args.tasks]
            if args.phase in {"assets", "all"}:
                prepare_image_store(
                    definition,
                    tasks,
                    image_store,
                    crane=args.crane,
                    archive_tool=args.archive_tool,
                    genisoimage=args.genisoimage,
                    workers=args.asset_workers,
                    require_full=not args.tasks,
                )
            if args.phase in {"cache", "all"}:
                if args.rootless_base_disk is None:
                    raise ValueError("cache preparation requires --rootless-base-disk")
                prepare_caches(
                    tasks,
                    image_store=image_store,
                    cache_root=cache_root,
                    base_disk=args.rootless_base_disk,
                    qemu=args.rootless_qemu,
                    qemu_img=args.rootless_qemu_img,
                    workers=args.cache_workers,
                )
            print(json.dumps({
                "benchmark": args.benchmark,
                "phase": args.phase,
                "tasks": len(tasks),
                "definitions_root": str(Path(definitions_root).expanduser().resolve()),
                "image_store": str(Path(image_store).expanduser().resolve()),
                "cache_root": str(Path(cache_root).expanduser().resolve()),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.command == "doctor":
            rootless_qemu = _rootless_settings(args)
            checks = (
                official_checks(backend=args.backend, output_root=args.output_root)
                if args.official
                else harbor_checks(
                    backend=args.backend,
                    benchmark=args.benchmark,
                    output_root=args.output_root,
                    harbor_bin=args.harbor_bin,
                    rootless_qemu=rootless_qemu,
                    required_tasks=tuple(args.tasks),
                )
            )
            print(render_checks(checks, as_json=args.json))
            return 2 if any(check.failed for check in checks) else 0

        if args.command == "run":
            rootless_qemu = _rootless_settings(args)
            agent, agent_version, codex_kimi = _codex_kimi_settings(args)
            agent, agent_version, tofu_kimi = _tofu_kimi_settings(
                args, agent=agent, agent_version=agent_version)
            if not args.dry_run:
                checks = harbor_checks(
                    backend=args.backend,
                    benchmark=args.benchmark,
                    output_root=args.output_root,
                    harbor_bin=args.harbor_bin,
                    rootless_qemu=rootless_qemu,
                    required_tasks=tuple(args.tasks),
                )
                if any(check.failed for check in checks):
                    print(render_checks(checks), file=sys.stderr)
                    return 2
            spec = HarborRunSpec(
                agent=agent,
                models=tuple(args.models),
                backend=args.backend,
                output_root=args.output_root,
                run_id=args.run_id,
                benchmark=args.benchmark,
                task_ids=tuple(args.tasks),
                limit=args.limit,
                attempts=args.attempts,
                concurrency=(
                    args.concurrency
                    if args.concurrency is not None
                    else (
                        1
                        if args.backend == "singularity"
                        else (4 if args.backend == "rootless-qemu" else 16)
                    )
                ),
                agent_concurrency=(
                    args.agent_concurrency
                    if args.agent_concurrency is not None
                    else (4 if args.backend == "rootless-qemu" else None)
                ),
                max_retries=(
                    args.max_retries
                    if args.max_retries is not None
                    else (0 if (
                        codex_kimi is not None or tofu_kimi is not None
                    ) else 2)
                ),
                timeout_multiplier=args.timeout_multiplier,
                secret_env=tuple(args.secret_env),
                agent_hosts=tuple(args.agent_host),
                reasoning_effort=args.reasoning_effort,
                agent_version=agent_version,
                harbor_bin=args.harbor_bin,
                rootless_qemu=rootless_qemu,
                codex_kimi=codex_kimi,
                tofu_kimi=tofu_kimi,
                release_run_root=args.release_run_root,
            )
            code, run_dir = start_harbor_run(spec, dry_run=args.dry_run)
            print(f"run directory: {run_dir}")
            return code

        if args.command == "resume":
            return resume_harbor_run(
                args.run_dir, harbor_bin=args.harbor_bin,
                release_run_root=args.release_run_root,
            )

        if args.command == "grade":
            if not args.dry_run:
                checks = official_checks(backend=args.backend, output_root=args.output_root)
                if any(check.failed for check in checks):
                    print(render_checks(checks), file=sys.stderr)
                    return 2
            code, run_dir = grade_predictions(
                args.predictions,
                backend=args.backend,
                output_root=args.output_root,
                run_id=args.run_id,
                workers=args.workers,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
            print(f"run directory: {run_dir}")
            return code

        if args.command == "audit":
            report = audit_run(args.run_dir, allow_errors=args.allow_errors)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else "", end="" if args.json else "")
            if not args.json:
                _print_audit(report)
            elif report:
                print()
            return 0 if report["ok"] else 1
    except (
        BlockingIOError,
        FileExistsError,
        FileNotFoundError,
        PermissionError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
