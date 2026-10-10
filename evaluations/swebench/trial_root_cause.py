"""Classify every trial in a Harbor run directory by failure root cause.

Reads only artifacts already on disk (result.json, exception.txt, agent
evidence, opencode proxy records) and assigns each trial exactly one primary
root-cause class, so harness losses are separated from model behavior losses
before we tune anything.

Usage:
    python3 -m evaluations.swebench.trial_root_cause \
        --run-root <runs>/<run-id> [--proxy-trials <dir>] [--json out.json]

Classes (prefix HARNESS_/INFRA_ = our bug or environment, MODEL_ = model):
    INFRA_VM_BOOT_TIMEOUT        environment start exceeded its timeout
    HARNESS_VERIFIER_TIMEOUT     verifier phase exceeded its timeout
    HARNESS_REWARD_MISSING       verifier ran but reward file never appeared
    HARNESS_HANDOFF_TIMEOUT      guest command outlived the host-side handoff
                                 wait; the late result was rejected
    HARNESS_EVIDENCE_RECONCILE   opencode wrapper could not reconcile evidence
    HARNESS_RUNTIME_OTHER        any other RuntimeError (message retained)
    MODEL_TIMEOUT_BUT_SCORED     agent hit the wall clock yet verifier reward > 0
    MODEL_REASONING_STARVED      agent timeout with <=3 resolved tool calls
    MODEL_TIMEOUT_LOOPING        agent timeout after real work, no score
    MODEL_MONOLOGUE_NO_TOOLCALL  clean exit, zero tool calls (single request)
    MODEL_TASK_FAILURE           clean exit with tool calls, reward == 0
    PASS                         reward > 0 without an exception
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

STARVED_TOOL_CALL_CEILING = 3


def _reward(result: dict):
    vr = result.get("verifier_result") or {}
    rew = vr.get("reward")
    if rew is None and isinstance(vr.get("rewards"), dict):
        rew = vr["rewards"].get("reward")
    return rew


def _trial_wall_seconds(trial_dir: str) -> float:
    try:
        start = os.path.getmtime(os.path.join(trial_dir, "lock.json"))
    except OSError:
        start = os.path.getmtime(os.path.join(trial_dir, "config.json"))
    end = os.path.getmtime(os.path.join(trial_dir, "result.json"))
    return round(end - start, 1)


def _tofu_evidence(trial_dir: str) -> dict:
    ev_dir = os.path.join(trial_dir, "agent", "tofu-kimi-evidence")
    out = {"tool_calls": None, "last_call": None, "last_timeout_sec": None,
           "last_round": None}
    audit_path = os.path.join(ev_dir, "tool-audit.json")
    if os.path.exists(audit_path):
        try:
            calls = (json.load(open(audit_path)).get("calls") or [])
            out["tool_calls"] = len(calls)
        except (OSError, json.JSONDecodeError):
            pass
    events_path = os.path.join(ev_dir, "events.jsonl")
    if os.path.exists(events_path):
        last_call = None
        try:
            with open(events_path) as fh:
                for line in fh:
                    try:
                        ev = json.loads(line).get("event") or {}
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "custom_tool_call":
                        last_call = ev
        except OSError:
            pass
        if last_call:
            args = last_call.get("arguments") or {}
            out["last_call"] = str(args.get("command", ""))[:200]
            out["last_timeout_sec"] = args.get("timeout_sec")
            out["last_round"] = last_call.get("roundNum")
    return out


def _opencode_requests(result: dict, proxy_trials: str | None):
    token = (((result.get("agent_result") or {}).get("metadata") or {})
             .get("opencodeEvidence") or {}).get("trialToken")
    if not token or not proxy_trials:
        return None
    path = os.path.join(proxy_trials, f"{token}.jsonl")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return sum(1 for _ in fh)
    except OSError:
        return None


def classify_trial(trial_dir: str, proxy_trials: str | None = None) -> dict:
    result_path = os.path.join(trial_dir, "result.json")
    result = json.load(open(result_path))
    exc = result.get("exception_info") or {}
    exc_type = exc.get("exception_type")
    exc_msg = exc.get("exception_message") or ""
    reward = _reward(result)
    is_opencode = "opencode" in (result.get("config", {}).get("agent", {})
                                 .get("name", "") or "")
    evidence = _tofu_evidence(trial_dir) if not is_opencode else {
        "requests": _opencode_requests(result, proxy_trials)}

    rec = {
        "task": (result.get("task_name") or "").replace("terminal-bench/", ""),
        "trial": result.get("trial_name"),
        "exception": exc_type,
        "reward": reward,
        "wall_s": _trial_wall_seconds(trial_dir),
        "evidence": evidence,
    }

    if exc_type == "EnvironmentStartTimeoutError":
        rec["root_cause"] = "INFRA_VM_BOOT_TIMEOUT"
    elif exc_type == "VerifierTimeoutError":
        rec["root_cause"] = "HARNESS_VERIFIER_TIMEOUT"
    elif exc_type == "RewardFileNotFoundError":
        rec["root_cause"] = "HARNESS_REWARD_MISSING"
    elif exc_type == "RuntimeError" and "rejected its owned custom tool result" in exc_msg:
        rec["root_cause"] = "HARNESS_HANDOFF_TIMEOUT"
    elif exc_type == "RuntimeError" and "could not be reconciled" in exc_msg:
        rec["root_cause"] = "HARNESS_EVIDENCE_RECONCILE"
    elif exc_type == "RuntimeError":
        rec["root_cause"] = "HARNESS_RUNTIME_OTHER"
        rec["detail"] = exc_msg[:200]
    elif exc_type == "AgentTimeoutError":
        if reward and reward > 0:
            rec["root_cause"] = "MODEL_TIMEOUT_BUT_SCORED"
        else:
            calls = evidence.get("tool_calls")
            if calls is not None and calls <= STARVED_TOOL_CALL_CEILING:
                rec["root_cause"] = "MODEL_REASONING_STARVED"
            elif calls is None and evidence.get("requests") == 1:
                rec["root_cause"] = "MODEL_MONOLOGUE_NO_TOOLCALL"
            else:
                rec["root_cause"] = "MODEL_TIMEOUT_LOOPING"
    elif exc_type:
        rec["root_cause"] = "UNCLASSIFIED_EXCEPTION"
        rec["detail"] = f"{exc_type}: {exc_msg[:160]}"
    elif reward and reward > 0:
        rec["root_cause"] = "PASS"
    else:
        if evidence.get("requests") == 1:
            rec["root_cause"] = "MODEL_MONOLOGUE_NO_TOOLCALL"
        elif evidence.get("tool_calls") == 0:
            rec["root_cause"] = "MODEL_MONOLOGUE_NO_TOOLCALL"
        else:
            rec["root_cause"] = "MODEL_TASK_FAILURE"
    return rec


def analyze_run(run_root: str, proxy_trials: str | None = None) -> dict:
    jobs = os.path.join(run_root, "jobs")
    if os.path.isdir(jobs):
        run_root = os.path.join(jobs, os.listdir(jobs)[0])
    trials = []
    for name in sorted(os.listdir(run_root)):
        trial_dir = os.path.join(run_root, name)
        if os.path.isdir(trial_dir) and os.path.exists(
                os.path.join(trial_dir, "result.json")):
            try:
                trials.append(classify_trial(trial_dir, proxy_trials))
            except (OSError, json.JSONDecodeError) as err:
                trials.append({"task": name, "root_cause": "UNREADABLE",
                               "detail": str(err)[:160]})
    counts = collections.Counter(t["root_cause"] for t in trials)
    scored = sum(1 for t in trials if (t.get("reward") or 0) > 0)
    harness_lost = sorted(
        t["task"] for t in trials
        if t["root_cause"].startswith(("HARNESS_", "INFRA_")))
    return {"run_root": run_root, "trials": len(trials), "scored": scored,
            "counts": dict(counts.most_common()),
            "harness_lost_tasks": harness_lost, "records": trials}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", action="append", required=True,
                        help="run dir (…/runs/<id>); repeatable for arms")
    parser.add_argument("--proxy-trials", default=None,
                        help="opencode proxy trials dir (request counting)")
    parser.add_argument("--json", default=None, help="write full JSON report")
    args = parser.parse_args(argv)

    report = {"arms": []}
    for run_root in args.run_root:
        analysis = analyze_run(run_root, args.proxy_trials)
        report["arms"].append(analysis)
        print(f"\n== {os.path.basename(run_root.rstrip('/'))}: "
              f"{analysis['trials']} trials, scored {analysis['scored']}")
        for cause, n in analysis["counts"].items():
            print(f"  {cause:32s} {n}")
        if analysis["harness_lost_tasks"]:
            print("  harness/infra-lost tasks:",
                  ", ".join(analysis["harness_lost_tasks"]))
    if len(report["arms"]) == 2:
        a, b = report["arms"]
        by_a = {t["task"]: t for t in a["records"]}
        by_b = {t["task"]: t for t in b["records"]}
        print("\n== divergent tasks (arm A lost to harness/infra, arm B scored)")
        for task in sorted(set(by_a) & set(by_b)):
            ta, tb = by_a[task], by_b[task]
            if (ta["root_cause"].startswith(("HARNESS_", "INFRA_"))
                    and (tb.get("reward") or 0) > 0):
                print(f"  {task}: A={ta['root_cause']} "
                      f"B=reward {tb['reward']}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nfull report: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
