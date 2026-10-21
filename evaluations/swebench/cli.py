from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from evaluations.codex_kimi_proxy.codex_contract import CODEX_VERSION
from evaluations.opencode_contract import OPENCODE_VERSION

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
from .opencode_runtime import (
    OPENCODE_AGENT,
    OPENCODE_PROFILE_ID,
    OpenCodeBaselineSettings,
)


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-root",
        type=Path,
        default=default_output_root(),
        help="Artifact root (default: TOFU_EVAL_ROOT or checkout/.eval-runs/benchmarks; never host /tmp)",
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
        model=str(args.models[0]),
        agent_timeout_seconds=args.tofu_agent_timeout_seconds,
        command_timeout_seconds=args.tofu_command_timeout_seconds,
        upstream_base_url_env=args.kimi_base_url_env,
        upstream_api_key_env=args.kimi_api_key_env,
        thinking_format=args.tofu_thinking_format,
        tool_surface=args.tofu_tool_surface,
        wire_proxy=args.tofu_wire_proxy,
    )
    settings.validate()
    return TOFU_KIMI_AGENT, agent_version, settings


def _opencode_settings(
    args: argparse.Namespace,
    *,
    agent: str,
    agent_version: str | None,
) -> tuple[str, str | None, OpenCodeBaselineSettings | None]:
    if agent not in {OPENCODE_PROFILE_ID, OPENCODE_AGENT}:
        return agent, agent_version, None
    if args.opencode_binary is None or not args.opencode_sha256 \
            or args.opencode_cache_seed is None \
            or not args.glm_provider_face or not args.glm_slot_id:
        raise ValueError(
            "opencode requires binary/hash/cache-seed plus --glm-provider-face "
            "and --glm-slot-id (or their TOFU_* environment variables)"
        )
    settings = OpenCodeBaselineSettings(
        opencode_binary=args.opencode_binary,
        opencode_sha256=args.opencode_sha256,
        opencode_cache_seed=args.opencode_cache_seed,
        model=str(args.models[0]),
        provider_face=args.glm_provider_face,
        provider_slot_id=args.glm_slot_id,
        agent_timeout_seconds=args.opencode_agent_timeout_seconds,
        context_tokens=args.opencode_context_tokens,
        output_tokens=args.opencode_output_tokens,
        upstream_base_url_env=args.glm_base_url_env,
        upstream_api_key_env=args.glm_api_key_env,
    )
    settings.validate(verify_binary=False)
    return OPENCODE_AGENT, args.agent_version or OPENCODE_VERSION, settings


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

    diagnose = sub.add_parser(
        "diagnose",
        help="Read-only coverage/evidence diagnosis of one run root (no harness load)")
    diagnose.add_argument("run_root")
    diagnose.add_argument("--reference-profile", choices=("glm-5.3-tb21",))

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
    run.add_argument('--agent-timeout-override-seconds', type=int,
                     help='Local GLM comparison: override Harbor task agent deadlines; must equal the native agent timeout')
    run.add_argument(
        "--verifier-timeout-multiplier",
        type=float,
        default=1.0,
        help=(
            "Scale every task's verifier phase deadline (Harbor-side) together "
            "with the guest verifier watchdog; compensates emulation slowdown "
            "without changing agent-phase budgets"
        ),
    )
    run.add_argument("--secret-env", action="append", default=[])
    run.add_argument("--agent-host", action="append", default=[])
    run.add_argument("--reasoning-effort")
    run.add_argument("--source-receipt", type=Path,
                     help="Verified Git-object source proof for an exported worker payload")
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
    run.add_argument("--tofu-tool-surface", choices=("exclusive", "production"),
                     default="exclusive", help="Freeze the candidate's actual tool surface")
    run.add_argument("--tofu-wire-proxy", action="store_true",
                     help="Retain secret-free actual provider request receipts")
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
    run.add_argument(
        "--opencode-binary",
        type=Path,
        default=_env_path("TOFU_OPENCODE_BINARY"),
        help="Pinned OpenCode binary for --agent opencode",
    )
    run.add_argument(
        "--opencode-sha256",
        default=os.environ.get("TOFU_OPENCODE_SHA256"),
        help="Expected SHA-256 for the pinned OpenCode binary",
    )
    run.add_argument(
        "--opencode-cache-seed",
        type=Path,
        default=_env_path("TOFU_OPENCODE_CACHE_SEED"),
        help="Pinned offline provider-package cache seed for --agent opencode",
    )
    run.add_argument(
        "--opencode-agent-timeout-seconds",
        type=int,
        default=3600,
    )
    run.add_argument('--opencode-context-tokens', type=int, default=200000)
    run.add_argument('--opencode-output-tokens', type=int, default=32768)
    run.add_argument(
        "--glm-base-url-env",
        default="GLM_CHAT_BASE_URL",
        help="Host-only environment variable containing the GLM Chat base URL",
    )
    run.add_argument(
        "--glm-api-key-env",
        default="GLM_API_KEY",
        help="Host-only environment variable containing the GLM API key",
    )
    run.add_argument(
        "--glm-provider-face",
        default=os.environ.get("TOFU_GLM_PROVIDER_FACE"),
        help="Non-secret provider face frozen into paired benchmark manifests",
    )
    run.add_argument(
        "--glm-slot-id",
        default=os.environ.get("TOFU_GLM_SLOT_ID"),
        help="Non-secret GLM slot identifier shared by both paired arms",
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
    for name, help_text in (
        ('comparison-register', 'Freeze two prepared TB 2.1 runs before dispatch'),
        ('compare', 'Write a bounded, traceable TB 2.1 paired evidence report'),
    ):
        compare = sub.add_parser(name, help=help_text)
        compare.add_argument('--baseline', type=Path, required=True)
        compare.add_argument('--candidate', type=Path, required=True)
        compare.add_argument('--output-root', type=Path, required=True)
        compare.add_argument('--run-id', required=True)
        compare.add_argument('--target-pp', type=float, default=10)
        compare.add_argument('--seed', type=int, default=1729)
        compare.add_argument('--reference-prices', type=Path,
                             help='Four USD-per-million rates, frozen at registration; not an invoice')
        if name == 'comparison-register':
            compare.add_argument('--infrastructure-retries', type=int, default=0)
        if name == 'compare':
            compare.add_argument('--require-target', action='store_true',
                                 help='Exit nonzero unless the 95%% lower bound reaches the target')
    verify = sub.add_parser('comparison-verify', help='Rehash original evidence referenced by a paired report')
    verify.add_argument('report', type=Path)
    msubmit = sub.add_parser(
        'yourprovider-submit',
        help='Plan a yourprovider-sandbox canvas submission (dry-run prints the plan)',
    )
    msubmit.add_argument('--base-canvas-id', required=True)
    msubmit.add_argument('--model-id', required=True)
    msubmit.add_argument('--tofu-commit', required=True)
    msubmit.add_argument('--agentix-branch', required=True)
    msubmit.add_argument('--tb-git-branch', required=True)
    msubmit.add_argument('--evaluate-name', required=True)
    msubmit.add_argument('--agent-workers', type=int, default=64)
    msubmit.add_argument('--attempts', type=int, default=5)
    msubmit.add_argument('--task-count', type=int, default=89)
    msubmit.add_argument('--json', action='store_true')
    msubmit.add_argument('--dry-run', action='store_true')

    mscore = sub.add_parser(
        'yourprovider-score',
        help='Collect per-trial rewards from dolphinfs trajectories and audit',
    )
    mscore.add_argument('--evaluate-name', required=True)
    mscore.add_argument('--agent-alias', required=True)
    mscore.add_argument('--output-root', type=Path, default=None)
    mscore.add_argument('--expected-attempts', type=int, default=1)
    mscore.add_argument('--release', action='store_true',
                        help='Audit for a formal release (expected-attempts per task)')
    mscore.add_argument('--json', action='store_true')
    return parser


def _print_audit(report: dict) -> None:
    for check in report["checks"]:
        status = "PASS" if check["ok"] else "FAIL"
        print(f"[{status}] {check['name']}: {check['detail']}")
    print(f"overall: {'PASS' if report['ok'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'comparison-verify':
            from .comparison import verify_report
            report = verify_report(args.report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['ok'] else 1
        if args.command == 'yourprovider-submit':
            from .yourprovider_sandbox.submit import MassConfig, build_submit_plan
            mass = MassConfig(
                task_count=args.task_count,
                attempts=args.attempts,
                agent_workers=args.agent_workers,
            )
            plan = build_submit_plan(
                base_canvas_id=args.base_canvas_id,
                model_id=args.model_id,
                tofu_commit=args.tofu_commit,
                agentix_branch=args.agentix_branch,
                tb_git_branch=args.tb_git_branch,
                evaluate_name=args.evaluate_name,
                mass=mass,
            )
            payload = {
                'base_canvas_id': plan.base_canvas_id,
                'model_id': plan.model_id,
                'agent_alias': plan.agent_alias,
                'tb_git_branch': plan.tb_git_branch,
                'evaluate_name': plan.evaluate_name,
                'mass': {
                    'task_count': plan.mass.task_count,
                    'attempts': plan.mass.attempts,
                    'total_trials': plan.mass.total_trials,
                    'agent_workers': plan.mass.agent_workers,
                    'gpu_cards': plan.mass.gpu_cards,
                    'estimated_cpu_cores': plan.mass.estimated_cpu_cores,
                    'estimated_memory_gib': plan.mass.estimated_memory_gib,
                },
                'canvas_params': plan.merged_canvas_params(),
                'donor_cache_flags': plan.donor_cache_flags,
                'resource_code': plan.resource_code,
                'schedule_priority': plan.schedule_priority,
                'cpu_queue': plan.cpu_queue,
                'gpu_queue': plan.gpu_queue,
                'dry_run': args.dry_run,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if args.dry_run:
                return 0
            print(
                'error: live submission requires the llm MCP canvas client; '
                'run with --dry-run to inspect the plan',
                file=sys.stderr,
            )
            return 2

        if args.command == 'yourprovider-score':
            from .yourprovider_sandbox.score import (
                DEFAULT_OUTPUT_ROOT,
                collect_scores,
                resolve_trajectory_root,
            )
            output_root = args.output_root or DEFAULT_OUTPUT_ROOT
            root = resolve_trajectory_root(
                args.evaluate_name, args.agent_alias, output_root=output_root
            )
            tasks, audit = collect_scores(
                root,
                expected_attempts=args.expected_attempts,
                require_unique_attempt=not args.release,
            )
            rewards = [
                tr.reward
                for t in tasks.values()
                for tr in t.trials
                if tr.reward is not None
            ]
            payload = {
                'trajectory_root': str(root),
                'audit': {
                    'ok': audit.ok,
                    'n_tasks': audit.n_tasks,
                    'n_trials': audit.n_trials,
                    'n_empty': audit.n_empty,
                    'n_running': audit.n_running,
                    'rejected_tasks': list(audit.rejected_tasks),
                    'reason': audit.reason,
                },
                'n_scored': len(rewards),
                'pass_rate': (sum(rewards) / len(rewards)) if rewards else None,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"trajectory root: {root}")
                print(f"audit: {'PASS' if audit.ok else 'FAIL'} ({audit.reason})")
                print(
                    f"tasks={audit.n_tasks} trials={audit.n_trials} "
                    f"scored={len(rewards)} empty={audit.n_empty} "
                    f"running={audit.n_running}"
                )
                if rewards:
                    print(f"pass rate: {sum(rewards) / len(rewards):.3f}")
                if audit.rejected_tasks:
                    print(f"rejected: {', '.join(audit.rejected_tasks)}")
            return 0 if audit.ok else 1
        if args.command in {'comparison-register', 'compare'}:
            from .comparison import compare_runs, register_comparison
            kwargs = dict(output_root=args.output_root, run_id=args.run_id,
                          target_pp=args.target_pp, seed=args.seed)
            if args.reference_prices:
                from .comparison import _read
                kwargs['reference_prices'] = _read(args.reference_prices)
            if args.command == 'comparison-register':
                kwargs['infrastructure_retry_limit'] = args.infrastructure_retries
                path = register_comparison(args.baseline, args.candidate, **kwargs)
                print(f'comparison protocol: {path}')
                return 0
            report, path = compare_runs(args.baseline, args.candidate, **kwargs)
            print(json.dumps({
                'report': str(path / 'report.json'), 'decision': report['decision'],
                'fullDomain': report['fullDomain'],
                'finalDeltaPercentagePoints': report['finalDeltaPercentagePoints'],
                'blockers': report['blockers'],
            }, ensure_ascii=False, indent=2))
            return int(args.require_target and report['decision'] != 'target_confirmed')

        if args.command == "prepare-rootless":
            definition = BENCHMARKS[args.benchmark]
            definition.validate_backend("rootless-qemu")
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

        if args.command == "diagnose":
            from .diagnostics import diagnose_run
            print(json.dumps(
                diagnose_run(Path(args.run_root), reference_profile=args.reference_profile),
                ensure_ascii=False, indent=2))
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
            agent, agent_version, opencode = _opencode_settings(
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
                        or opencode is not None
                    ) else 2)
                ),
                timeout_multiplier=args.timeout_multiplier,
                agent_timeout_override_seconds=args.agent_timeout_override_seconds,
                verifier_timeout_multiplier=args.verifier_timeout_multiplier,
                secret_env=tuple(args.secret_env),
                agent_hosts=tuple(args.agent_host),
                reasoning_effort=args.reasoning_effort,
                agent_version=agent_version,
                harbor_bin=args.harbor_bin,
                source_receipt=args.source_receipt,
                rootless_qemu=rootless_qemu,
                codex_kimi=codex_kimi,
                tofu_kimi=tofu_kimi,
                opencode=opencode,
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
