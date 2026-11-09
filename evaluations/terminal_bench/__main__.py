"""CLI: ``python -m evaluations.terminal_bench run|summarize``.

``run`` freezes its configuration in ``<work-dir>/run.json`` on first use; a
rerun with the same work dir resumes from that file (only ``--concurrency``
and ``--max-attempts`` may change) and never recomputes measured trials.
New runs carry the benchmark-source guard (``source_guard`` true in
``run.json``); a run frozen before it existed resumes without it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evaluations.terminal_bench.gateway import INGRESS_BRANCH
from evaluations.terminal_bench.harness import PROTOCOLS, list_tasks

# Context window both arms are told: the value the 2026-11-01/02 Tofu runs routed
# with (Harbor TofuKernel envelope), so both harnesses share one model fact.
GLM53_CONTEXT_TOKENS = 262_144
# Context windows per run model, from the production model-routing offering the
# run replicates. Opus 5.5's offering and Pi's built-in catalog both say 1 M.
RUN_MODEL_CONTEXT_TOKENS = {"claude-opus-5.5": 1_000_000}
TB21_TASK_CHECKSUMS = Path(__file__).resolve().parents[1] / "terminal_bench_21_task_checksums.json"


def verify_task_identity(tasks_dir: Path, names: list[str]) -> dict:
    """Harbor ``Task.checksum`` (dirhash sha256 of the task directory) against the TB2.1 pins."""
    from dirhash import dirhash

    pins = json.loads(TB21_TASK_CHECKSUMS.read_text(encoding="utf-8"))
    mismatched = [name for name in names
                  if dirhash(tasks_dir / name, "sha256") != pins["task_checksums"].get(name)]
    return {"dataset_commit": pins["dataset_commit"], "checked": len(names), "mismatched": mismatched}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evaluations.terminal_bench")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run or resume a native Terminal-Bench evaluation")
    run.add_argument("--work-dir", type=Path, required=True)
    run.add_argument("--concurrency", type=int, required=True, help="simultaneous sandbox sessions")
    run.add_argument("--max-attempts", type=int, default=3, help="attempts per trial for unmeasured outcomes")
    run.add_argument("--tasks-dir", type=Path)
    run.add_argument("--tasks", help="comma-separated task names (default: every task in --tasks-dir)")
    run.add_argument("--arms", default="tofu,opencode",
                     help="comma-separated arms: tofu, opencode, pi, claude-code")
    run.add_argument("--protocol", choices=sorted(PROTOCOLS), default="vendor-6h")
    run.add_argument("--model", default="glm-5.3-flash", help="wire model name the agents send")
    run.add_argument("--relay-url", help="model relay base URL reachable from sandbox containers")
    run.add_argument("--anthropic-url",
                     help="Anthropic bridge base URL reachable from sandbox containers (claude-code arm)")
    run.add_argument("--files-url", help="artifact base URL (default: <relay-url>/files)")
    run.add_argument("--artifacts-manifest", type=Path)
    run.add_argument("--endpoint", help="sandboxGateway endpoint .../system/<cluster>")
    run.add_argument("--image", help="sandbox VM image, e.g. rootfs://harbor-pipeline-dind-v5")
    run.add_argument("--branch", default=INGRESS_BRANCH, help="ingress branch with the native exec channel")
    run.add_argument("--proxy", help="upstream egress proxy for the VM and the trial egress filter")
    run.add_argument("--max-alive-min", type=int, default=540)
    run.add_argument("--context-tokens", type=int,
                     help="context window every arm is told (default: the run model's production offering)")
    run.add_argument("--poll-interval-sec", type=int, default=30)
    run.add_argument("--run-label", help="relay accounting label (default: work dir name)")
    summarize = commands.add_parser("summarize", help="rewrite summary.json from trial states")
    summarize.add_argument("--work-dir", type=Path, required=True)
    return parser


def frozen_config(args) -> dict:
    run_file = args.work_dir / "run.json"
    if run_file.is_file():
        config = json.loads(run_file.read_text(encoding="utf-8"))
        config["max_attempts"] = args.max_attempts
        return config
    if not args.tasks_dir or not args.relay_url or not args.artifacts_manifest or not args.endpoint \
            or not args.image or not args.proxy:
        raise SystemExit("a new run needs --tasks-dir --relay-url --artifacts-manifest --endpoint --image --proxy")
    if "claude-code" in args.arms.split(",") and not args.anthropic_url:
        raise SystemExit("the claude-code arm needs --anthropic-url (python -m evaluations.terminal_bench.anthropic_bridge)")
    tasks_dir = args.tasks_dir.resolve()
    names = [task.name for task in list_tasks(tasks_dir, args.tasks.split(",") if args.tasks else None)]
    identity = verify_task_identity(tasks_dir, names)
    if identity["mismatched"]:
        raise SystemExit(f"tasks differ from the pinned Terminal-Bench 2.1 checksums "
                         f"({identity['dataset_commit']}): {identity['mismatched'][:10]}")
    config = {
        "tasks_dir": str(tasks_dir),
        "tasks": names,
        "task_identity": identity,
        "arms": args.arms.split(","),
        "protocol": args.protocol,
        "model": args.model,
        "relay_url": args.relay_url.rstrip("/"),
        "files_url": (args.files_url or args.relay_url.rstrip("/") + "/files").rstrip("/"),
        "artifacts_manifest": str(args.artifacts_manifest.resolve()),
        "endpoint": args.endpoint.rstrip("/"),
        "image": args.image,
        "branch": args.branch,
        "proxy": args.proxy,
        "max_alive_min": args.max_alive_min,
        "max_attempts": args.max_attempts,
        "context_tokens": (args.context_tokens if args.context_tokens is not None
                           else RUN_MODEL_CONTEXT_TOKENS.get(args.model, GLM53_CONTEXT_TOKENS)),
        "poll_interval_sec": args.poll_interval_sec,
        "run_label": args.run_label or args.work_dir.name,
        "anthropic_url": args.anthropic_url.rstrip("/") if args.anthropic_url else None,
        "source_guard": True,
    }
    args.work_dir.mkdir(parents=True, exist_ok=True)
    run_file.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from evaluations.terminal_bench.runner import RunConfig, Runner, summarize

    if args.command == "summarize":
        print(json.dumps(summarize(args.work_dir)["arms"], indent=2))
        return 0
    config = frozen_config(args)
    run_config = RunConfig(
        work_dir=args.work_dir.resolve(), tasks_dir=Path(config["tasks_dir"]), tasks=config["tasks"],
        arms=config["arms"], protocol=config["protocol"], model=config["model"],
        relay_url=config["relay_url"], files_url=config["files_url"],
        artifacts_manifest=Path(config["artifacts_manifest"]), endpoint=config["endpoint"],
        image=config["image"], branch=config["branch"], proxy=config["proxy"],
        max_alive_min=config["max_alive_min"], max_attempts=config["max_attempts"],
        context_tokens=config["context_tokens"], poll_interval_sec=config["poll_interval_sec"],
        run_label=config["run_label"], anthropic_url=config.get("anthropic_url"),
        source_guard=bool(config.get("source_guard")))
    summary = Runner(run_config).run(args.concurrency)
    print(json.dumps(summary["arms"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
