"""收分器：从 dolphinfs trajectories 归集 per-trial reward 并按题审计。

判分权威是 inner ``result.json`` 的 ``verifier_result.rewards.reward``（顶层
783B 的 ``reward_stats`` 只是按 trial_name 聚合的元信息）。``experiment_eval_result``
只给汇总分，per-trial 必须读 dolphinfs。

轨迹根路径模板（实测 871428 确认）::

    <output_root>/<evaluate_name去MT前缀>/
      agentix@<owner_prefix>/<branch>@simple@tofu/
        trajectories/instance_<task>_<ts>_<hash>/
          result.json                              # 顶层 783B 元信息
          instance_<task_short>__<rand>/result.json  # inner 判分权威

审计规则（tofu release 铁律，任一不满足即拒封，不用 latest/keep-best 补数）：

- 每题恰好一次有效 attempt：同题多个顶层 trial 目录 → 拒封（除非显式允许多 attempt
  的正式 release，此时要求 attempt 数 == expected_attempts）。
- reward 只信 inner ``verifier_result.rewards.reward``；无 inner → 空分（setup 失败，
  单列，既非 0 也非 1）。
- ``exception_info.exception_type`` 是独立维度（AgentTimeoutError ≠ reward 0）。
- 三层证据：画布 agent 字段 / setup stdout ``[AGENTIX_COMMIT_FULL]`` /
  inner ``agent_info.version`` 三者一致才归档。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_OUTPUT_ROOT = Path(
    "/path/to/your/data"
)

# instance_<task>_<yyyymmdd>_<hhmmss>_<hash>
_TRIAL_DIR_RE = re.compile(
    r"^instance_(?P<task>.+)_(?P<ts>\d{8}_\d{6})_(?P<hash>[0-9a-f]{8})$"
)

REWARD_EMPTY = "empty"  # setup 失败/无 inner result.json
REWARD_RUNNING = "running"  # inner 存在但缺 verifier_result


@dataclass(frozen=True)
class TrialScore:
    """单个 attempt（一个顶层 trial 目录）的判分结果。"""

    task_id: str  # 顶层目录解析出的题 id（去掉 ts/hash）
    trial_dir: Path
    reward: float | None  # None 表示空分（无 inner / 无 verifier_result）
    reward_kind: str  # "numeric" | REWARD_EMPTY | REWARD_RUNNING
    exception_type: str | None
    agent_version: str | None  # inner agent_info.version
    environment_type: str | None  # inner config.environment.type
    inner_result_path: Path | None


@dataclass
class TaskScore:
    """一道题的所有 attempt 归集 + 审计结论。"""

    task_id: str
    trials: list[TrialScore] = field(default_factory=list)
    audit_ok: bool = True
    audit_reason: str = ""

    @property
    def valid_trials(self) -> list[TrialScore]:
        return [t for t in self.trials if t.reward_kind == "numeric"]


@dataclass(frozen=True)
class AttemptAudit:
    """整个 run 的审计结论。"""

    ok: bool
    n_tasks: int
    n_trials: int
    n_empty: int
    n_running: int
    rejected_tasks: tuple[str, ...]  # 审计失败的题 id
    reason: str


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _parse_trial_dir(trial_dir: Path) -> tuple[str, str] | None:
    """从顶层 trial 目录名解析 (task_id, ts)。不匹配返回 None。"""
    match = _TRIAL_DIR_RE.match(trial_dir.name)
    if not match:
        return None
    return match.group("task"), match.group("ts")


def _find_inner_result(trial_dir: Path) -> Path | None:
    """在顶层 trial 目录下找 inner result.json（嵌套一层 instance_* 目录）。"""
    for child in sorted(trial_dir.iterdir()):
        if child.is_dir() and child.name.startswith("instance_"):
            candidate = child / "result.json"
            if candidate.is_file():
                return candidate
    return None


def _score_one_trial(trial_dir: Path) -> TrialScore | None:
    parsed = _parse_trial_dir(trial_dir)
    if parsed is None:
        return None
    task_id, _ts = parsed

    inner_path = _find_inner_result(trial_dir)
    if inner_path is None:
        return TrialScore(
            task_id=task_id,
            trial_dir=trial_dir,
            reward=None,
            reward_kind=REWARD_EMPTY,
            exception_type=None,
            agent_version=None,
            environment_type=None,
            inner_result_path=None,
        )

    inner = _read_json(inner_path)
    if inner is None:
        return TrialScore(
            task_id=task_id,
            trial_dir=trial_dir,
            reward=None,
            reward_kind=REWARD_EMPTY,
            exception_type=None,
            agent_version=None,
            environment_type=None,
            inner_result_path=inner_path,
        )

    verifier_result = inner.get("verifier_result") or {}
    rewards = verifier_result.get("rewards") or {}
    reward = rewards.get("reward")
    exception_info = inner.get("exception_info") or {}
    exception_type = exception_info.get("exception_type")
    agent_info = inner.get("agent_info") or {}
    agent_version = agent_info.get("version")
    config = inner.get("config") or {}
    environment = config.get("environment") or {}
    environment_type = environment.get("type")

    if reward is None:
        kind = REWARD_RUNNING
        numeric = None
    else:
        kind = "numeric"
        numeric = float(reward)

    return TrialScore(
        task_id=task_id,
        trial_dir=trial_dir,
        reward=numeric,
        reward_kind=kind,
        exception_type=exception_type,
        agent_version=agent_version,
        environment_type=environment_type,
        inner_result_path=inner_path,
    )


def resolve_trajectory_root(
    evaluate_name: str,
    agent_alias: str,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> Path:
    """把画布 evaluate_name + agent 别名解析成 trajectories 根目录。

    ``evaluate_name`` 形如 ``MT/<name>`` 或 ``<name>``（去 MT 前缀）。
    ``agent_alias`` 形如 ``agentix@<owner>/<branch>@simple@tofu``；dolphinfs 把
    ``@`` 分段拆成两级目录：``agentix@<owner_prefix>/<branch>@simple@tofu``。

    实测 871428::

        evaluate_name = MT/longcat-flash-tofu-v0174-streamfix-...-ckpt_00000588
        agent         = agentix@ruanjunhao/tofu-streamfix-20260908@simple@tofu
        → <output_root>/longcat-flash-tofu-v0174-streamfix-...-ckpt_00000588/
            agentix@ruanjunhao/tofu-streamfix-20260908@simple@tofu/trajectories
    """
    name = evaluate_name
    if name.startswith("MT/"):
        name = name[len("MT/"):]
    name = name.rstrip("/")

    alias = agent_alias
    if alias.startswith("agentix@"):
        alias = alias[len("agentix@"):]
    # alias = <owner>/<branch>@simple@tofu → agentix@<owner> / <branch>@simple@tofu
    if "/" in alias:
        owner, rest = alias.split("/", 1)
        agent_dir = f"agentix@{owner}"
        return output_root / name / agent_dir / rest / "trajectories"
    return output_root / name / f"agentix@{alias}" / "trajectories"


def collect_scores(
    trajectory_root: Path,
    *,
    expected_attempts: int = 1,
    require_unique_attempt: bool = True,
) -> tuple[dict[str, TaskScore], AttemptAudit]:
    """归集 trajectories 根下所有 trial，按题分组并审计。

    ``expected_attempts``：正式 release 的每题 attempt 数（TB2.1 = 5）。canary
    传 1。``require_unique_attempt=True`` 时同题多个顶层目录即拒封（用于单 attempt
    canary）；正式 release 传 False 并核对 attempt 数 == expected_attempts。
    """
    tasks: dict[str, TaskScore] = {}
    n_trials = 0
    n_empty = 0
    n_running = 0
    rejected: list[str] = []

    if not trajectory_root.is_dir():
        return {}, AttemptAudit(
            ok=False,
            n_tasks=0,
            n_trials=0,
            n_empty=0,
            n_running=0,
            rejected_tasks=(),
            reason=f"trajectory root not found: {trajectory_root}",
        )

    for trial_dir in sorted(trajectory_root.iterdir()):
        if not trial_dir.is_dir():
            continue
        trial = _score_one_trial(trial_dir)
        if trial is None:
            continue
        n_trials += 1
        if trial.reward_kind == REWARD_EMPTY:
            n_empty += 1
        elif trial.reward_kind == REWARD_RUNNING:
            n_running += 1
        task = tasks.setdefault(trial.task_id, TaskScore(task_id=trial.task_id))
        task.trials.append(trial)

    for task_id, task in tasks.items():
        n_dirs = len(task.trials)
        if require_unique_attempt and n_dirs != 1:
            task.audit_ok = False
            task.audit_reason = (
                f"expected exactly 1 attempt, found {n_dirs} trial dirs"
            )
            rejected.append(task_id)
        elif not require_unique_attempt and n_dirs != expected_attempts:
            task.audit_ok = False
            task.audit_reason = (
                f"expected {expected_attempts} attempts, found {n_dirs} trial dirs"
            )
            rejected.append(task_id)

    ok = not rejected
    reason = "ok" if ok else f"{len(rejected)} task(s) failed attempt audit"
    return tasks, AttemptAudit(
        ok=ok,
        n_tasks=len(tasks),
        n_trials=n_trials,
        n_empty=n_empty,
        n_running=n_running,
        rejected_tasks=tuple(sorted(rejected)),
        reason=reason,
    )
