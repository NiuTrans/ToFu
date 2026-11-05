"""Score report for native Terminal-Bench runs.

Responsibility: combine comparable work dirs (fix-up dirs for an arm, or runs
of other arms), attach the contamination audit and human reviews, and compute
per-arm raw and strict pass rates over the main dir's planned task set. A
later dir's measured trial replaces an unmeasured one for the same arm/task.
Strict scoring counts confirmed contamination and every unmeasured trial as a
failure. A dir is refused unless it shares the main dir's ``COMPARABLE_FIELDS``
and dataset commit and plans only tasks of the main dir.

Entry point: ``python -m evaluations.terminal_bench.report --tasks-dir T
[--review review.json ...] [--output report.json] WORK_DIR [OTHER_DIR ...]``;
writes ``report.json`` into the first work dir unless ``--output`` is given.
``review.json`` maps ``"<arm>/<task>"`` to ``{"contaminated": bool, "note":
str}`` after reading the audit's cited calls; reviews that disagree on a trial
are refused. Dependencies: ``runner`` (state layout), ``audit``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluations.terminal_bench.audit import audit_trial, task_fingerprints
from evaluations.terminal_bench.runner import MEASURED, read_json

COMPARABLE_FIELDS = ("protocol", "model", "context_tokens", "image", "endpoint", "proxy")


def load_reviews(paths: list[Path]) -> dict:
    merged: dict[str, dict] = {}
    for path in paths:
        for key, verdict in json.loads(path.read_text(encoding="utf-8")).items():
            if key in merged and bool(merged[key].get("contaminated")) != bool(verdict.get("contaminated")):
                raise ValueError(f"{path}: verdict for {key} contradicts an earlier review")
            merged.setdefault(key, verdict)
    return merged


def plan_identity(plan: dict) -> dict:
    identity = {field: plan.get(field) for field in COMPARABLE_FIELDS}
    identity["task_identity.dataset_commit"] = (plan.get("task_identity") or {}).get("dataset_commit")
    return identity


def comparable_plans(work_dirs: list[Path]) -> tuple[dict, list[str]]:
    plans = [read_json(work_dir / "run.json") for work_dir in work_dirs]
    for work_dir, plan in zip(work_dirs, plans):
        if plan is None:
            raise ValueError(f"{work_dir} has no run.json")
    main, expected = plans[0], plan_identity(plans[0])
    arms: list[str] = []
    for work_dir, plan in zip(work_dirs, plans):
        for field, value in plan_identity(plan).items():
            if value != expected[field]:
                raise ValueError(f"{work_dir}: {field} {value!r} differs from {work_dirs[0]} ({expected[field]!r})")
        unplanned = sorted(set(plan["tasks"]) - set(main["tasks"]))
        if unplanned:
            raise ValueError(f"{work_dir} plans tasks outside {work_dirs[0]}: {unplanned}")
        arms += [arm for arm in plan["arms"] if arm not in arms]
    return main, arms


def latest_attempts(work_dir: Path) -> dict[tuple[str, str], tuple[Path, dict]]:
    latest: dict[tuple[str, str], tuple[int, Path, dict]] = {}
    for state_path in work_dir.glob("trials/*/*/attempt-*/state.json"):
        arm, task, attempt = state_path.parts[-4], state_path.parts[-3], int(state_path.parts[-2].split("-")[1])
        if (arm, task) not in latest or attempt > latest[(arm, task)][0]:
            latest[(arm, task)] = (attempt, state_path.parent, read_json(state_path))
    return {key: (path, state) for key, (_, path, state) in latest.items()}


def trial_row(directory: Path, state: dict, fingerprints: set[str]) -> dict:
    result = read_json(directory / "result.json") or {}
    phases = result.get("phases") or {}
    agent = phases.get("agent") or {}
    verifier = phases.get("verifier") or {}
    budget, duration = agent.get("budget_sec"), agent.get("duration_s")
    row = {
        "dir": str(directory), "phase": state.get("phase"), "reward": state.get("reward"),
        "agent_exit": agent.get("exit_code"), "agent_s": duration, "agent_timed_out": agent.get("timed_out"),
        # A harness that stops itself at its deadline exits without the runner's timeout flag.
        "agent_budget_exhausted": bool(agent.get("timed_out"))
        or (budget is not None and duration is not None and duration >= budget),
        "verifier_attempts": len(verifier.get("attempts") or []),
        "verifier_timed_out": result.get("verifier_timed_out"), "image": (result.get("image") or {}).get("id"),
        "install": ((phases.get("install") or {}).get("check_output") or "").strip(),
        "egress": result.get("egress"), "infra_error": result.get("infra_error") or state.get("error"),
    }
    if (directory / "trial" / "result.json").is_file():
        audit = audit_trial(directory / "trial", fingerprints)
        row["audit_flags"] = audit["flags"]
        row["audit_strong"] = audit["strong"]
        row["audit"] = {key: audit[key] for key in ("hidden_line_count", "local_hidden_line_count", "hidden_lines",
                                                    "canary_results", "source_calls", "denied_egress")}
    return row


def build_report(work_dirs: list[Path], tasks_dir: Path, review: dict) -> dict:
    config, arms = comparable_plans(work_dirs)
    planned = config["tasks"]
    merged: dict[tuple[str, str], tuple[Path, dict]] = {}
    for work_dir in work_dirs:
        for key, (directory, state) in latest_attempts(work_dir).items():
            current = merged.get(key)
            if current is None or (state.get("phase") in MEASURED and current[1].get("phase") not in MEASURED):
                merged[key] = (directory, state)
    fingerprints = {task: task_fingerprints(tasks_dir / task) for task in planned}
    report = {"identity": plan_identity(config), "task_identity": config.get("task_identity"),
              "work_dirs": [str(path) for path in work_dirs], "arms": {}, "trials": {}}
    for arm in arms:
        counts = {"planned": len(planned), "measured": 0, "passed_raw": 0, "contaminated_passes": 0,
                  "passed_strict": 0, "agent_timeouts": 0, "unmeasured": [], "no_reward": [],
                  "flagged_unreviewed": []}
        for task in planned:
            directory, state = merged.get((arm, task), (None, {}))
            row = trial_row(directory, state, fingerprints[task]) if directory else {"phase": "missing"}
            verdict = review.get(f"{arm}/{task}") or {}
            row["review"] = verdict
            report["trials"][f"{arm}/{task}"] = row
            if row["phase"] not in MEASURED:
                counts["unmeasured"].append(task)
                continue
            counts["measured"] += 1
            counts["agent_timeouts"] += row["agent_budget_exhausted"]
            if row["phase"] == "no_reward":
                counts["no_reward"].append(task)
            passed = (row.get("reward") or 0) >= 1.0
            counts["passed_raw"] += passed
            if passed and verdict.get("contaminated"):
                counts["contaminated_passes"] += 1
            elif passed:
                counts["passed_strict"] += 1
                if row.get("audit_strong") and "contaminated" not in verdict:
                    counts["flagged_unreviewed"].append(task)
        counts["raw_rate"] = round(counts["passed_raw"] / counts["planned"], 4)
        counts["strict_rate"] = round(counts["passed_strict"] / counts["planned"], 4)
        report["arms"][arm] = counts
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluations.terminal_bench.report")
    parser.add_argument("work_dirs", type=Path, nargs="+",
                        help="main work dir first (its run.json fixes the task set), then fix-up or other-arm dirs")
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--review", type=Path, action="append", default=[],
                        help="human contamination verdicts; repeat for each run's review")
    parser.add_argument("--output", type=Path, help="report path (default: <main work dir>/report.json)")
    args = parser.parse_args(argv)
    report = build_report(args.work_dirs, args.tasks_dir, load_reviews(args.review))
    output = args.output or args.work_dirs[0] / "report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["arms"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
