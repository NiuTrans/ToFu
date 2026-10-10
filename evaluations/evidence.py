"""Harness-neutral diagnostic evidence, independent of execution and scoring.

Adapters provide observations; this module counts missing/invalid evidence and
preserves arbitrary numeric metrics. It never substitutes zero for a failed
execution, retries a task, selects the best attempt, or certifies a score.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import math


@dataclass(frozen=True)
class TrialObservation:
    identity: str
    task: str
    model: str
    rewards: dict = field(default_factory=dict)
    exception_type: str | None = None
    evidence_issues: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()


def summarize_trials(trials: list[TrialObservation], *, expected: dict[tuple[str, str], int]) -> dict:
    """Count exact (task, model) coverage without imposing a benchmark metric."""
    if not expected or any(not task or not model or type(count) is not int or count < 1
                           for (task, model), count in expected.items()):
        raise ValueError("expected coverage requires nonempty task/model identities and positive counts")
    identities = Counter(t.identity for t in trials)
    coverage = Counter((t.task, t.model) for t in trials)
    exceptions: Counter = Counter()
    issues: Counter = Counter()
    observations: Counter = Counter()
    histograms: dict[str, Counter] = {}
    rows = []
    for trial in trials:
        row_issues = list(trial.evidence_issues)
        if not trial.identity or identities[trial.identity] != 1:
            row_issues.append("duplicate_or_missing_trial_identity")
        if (trial.task, trial.model) not in expected:
            row_issues.append("unexpected_task_or_model")
        rewards = {}
        for name, value in trial.rewards.items():
            if not isinstance(name, str) or not name or type(value) not in (float, int) \
                    or (type(value) is float and not math.isfinite(value)):
                row_issues.append("invalid_reward")
                continue
            rewards[name] = value
            label = str(int(value)) if type(value) is float and value.is_integer() else str(value)
            histograms.setdefault(name, Counter())[label] += 1
        issues.update(set(row_issues))
        observations.update(set(trial.observations))
        if trial.exception_type:
            exceptions[trial.exception_type] += 1
        rows.append({**asdict(trial), "rewards": rewards,
                     "evidence_issues": sorted(set(row_issues))})
    missing = [{"task": task, "model": model, "count": count - coverage[task, model]}
               for (task, model), count in sorted(expected.items()) if coverage[task, model] < count]
    excess = [{"task": task, "model": model, "count": count - expected.get((task, model), 0)}
              for (task, model), count in sorted(coverage.items())
              if count > expected.get((task, model), 0)]
    return {
        "schema": "evaluation.diagnostics/v1", "diagnostic_only": True,
        "formal_score": None,
        "expected_trials": sum(expected.values()), "observed_trials": len(trials),
        "trials_with_rewards": sum(bool(row["rewards"]) for row in rows),
        "trials_without_rewards": sum(not row["rewards"] for row in rows),
        "missing_trials": missing, "excess_trials": excess,
        "exception_counts": dict(sorted(exceptions.items())),
        "evidence_issue_counts": dict(sorted(issues.items())),
        "observation_counts": dict(sorted(observations.items())),
        "observed_reward_histograms": {k: dict(sorted(v.items())) for k, v in sorted(histograms.items())},
        "trials": rows,
    }
