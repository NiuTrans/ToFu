from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .audit import audit_run
from .constants import (
    BENCHMARKS,
    DEFAULT_AGENT_BACKEND,
    DEFAULT_BENCHMARK,
    ISOLATED_BACKENDS,
    default_output_root,
)
from .harbor_runner import HarborRunSpec, resume_harbor_run, start_harbor_run
from .official import grade_predictions
from .preflight import harbor_checks, official_checks, render_checks


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-root",
        type=Path,
        default=default_output_root(),
        help="Artifact root (default: TOFU_EVAL_ROOT or ~/.local/state/tofu-evals/agent-benchmarks)",
    )


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
    doctor.add_argument("--harbor-bin", default="harbor")
    doctor.add_argument("--official", action="store_true", help="Check upstream patch grader instead of Harbor")
    doctor.add_argument("--json", action="store_true")
    _add_output(doctor)

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
    run.add_argument("--max-retries", type=int, default=2)
    run.add_argument("--timeout-multiplier", type=float, default=1.0)
    run.add_argument("--secret-env", action="append", default=[])
    run.add_argument("--agent-host", action="append", default=[])
    run.add_argument("--reasoning-effort")
    run.add_argument("--agent-version")
    run.add_argument("--harbor-bin", default="harbor")
    run.add_argument("--run-id")
    run.add_argument("--dry-run", action="store_true")
    _add_output(run)

    resume = sub.add_parser("resume", help="Resume only unfinished Harbor trials")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--harbor-bin", default="harbor")

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
        if args.command == "doctor":
            checks = (
                official_checks(backend=args.backend, output_root=args.output_root)
                if args.official
                else harbor_checks(
                    backend=args.backend,
                    benchmark=args.benchmark,
                    output_root=args.output_root,
                    harbor_bin=args.harbor_bin,
                )
            )
            print(render_checks(checks, as_json=args.json))
            return 2 if any(check.failed for check in checks) else 0

        if args.command == "run":
            if not args.dry_run:
                checks = harbor_checks(
                    backend=args.backend,
                    benchmark=args.benchmark,
                    output_root=args.output_root,
                    harbor_bin=args.harbor_bin,
                )
                if any(check.failed for check in checks):
                    print(render_checks(checks), file=sys.stderr)
                    return 2
            spec = HarborRunSpec(
                agent=args.agent,
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
                    else (1 if args.backend == "singularity" else 16)
                ),
                agent_concurrency=args.agent_concurrency,
                max_retries=args.max_retries,
                timeout_multiplier=args.timeout_multiplier,
                secret_env=tuple(args.secret_env),
                agent_hosts=tuple(args.agent_host),
                reasoning_effort=args.reasoning_effort,
                agent_version=args.agent_version,
                harbor_bin=args.harbor_bin,
            )
            code, run_dir = start_harbor_run(spec, dry_run=args.dry_run)
            print(f"run directory: {run_dir}")
            return code

        if args.command == "resume":
            return resume_harbor_run(args.run_dir, harbor_bin=args.harbor_bin)

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
    except (FileExistsError, FileNotFoundError, ValueError, json.JSONDecodeError, BlockingIOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
