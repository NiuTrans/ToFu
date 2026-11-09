"""收分器（yourprovider_sandbox.score）的离线单元测试。

场景覆盖 871428 真实轨迹实证到的全部形态：

- 单 attempt 正常判分（numeric 0/1）
- 同题多 attempt 拒封（含 reward 不一致的危险样本：flipt-io 同题一 0 一 1）
- 无 inner result.json → 空分
- inner 缺 verifier_result → running
- 路径解析（MT 前缀 / agent 别名 @ 分段拆两级目录）

用合成轨迹（tmp_path），不依赖真实 dolphinfs——CI 可能没挂载。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluations.swebench.yourprovider_sandbox.score import (
    REWARD_EMPTY,
    REWARD_RUNNING,
    collect_scores,
    resolve_trajectory_root,
)

pytestmark = pytest.mark.unit


def _write_trial(
    trajectories: Path,
    task: str,
    ts: str,
    hash8: str,
    *,
    reward: float | None,
    exception_type: str | None = None,
    agent_version: str | None = "ruanjunhao/branch@simple@tofu",
    environment_type: str | None = "docker",
    with_inner: bool = True,
) -> Path:
    trial_dir = trajectories / f"instance_{task}_{ts}_{hash8}"
    trial_dir.mkdir(parents=True)
    if with_inner:
        inner_dir = trial_dir / f"instance_{task[:20]}__rand"
        inner_dir.mkdir()
        inner = {
            "task_name": task,
            "verifier_result": (
                {"rewards": {"reward": reward}, "failure_reason": None}
                if reward is not None
                else {}
            ),
            "exception_info": (
                {"exception_type": exception_type} if exception_type else None
            ),
            "agent_info": {"name": "agentix", "version": agent_version},
            "config": {"environment": {"type": environment_type}},
        }
        (inner_dir / "result.json").write_text(json.dumps(inner), encoding="utf-8")
    return trial_dir


def test_resolve_trajectory_root_strips_mt_and_splits_alias(tmp_path: Path) -> None:
    root = resolve_trajectory_root(
        "MT/longcat-flash-tofu-v0174-streamfix-ckpt_00000588",
        "agentix@ruanjunhao/tofu-streamfix-20260908@simple@tofu",
        output_root=tmp_path,
    )
    assert root == (
        tmp_path
        / "longcat-flash-tofu-v0174-streamfix-ckpt_00000588"
        / "agentix@ruanjunhao"
        / "tofu-streamfix-20260908@simple@tofu"
        / "trajectories"
    )


def test_single_attempt_numeric_scores(tmp_path: Path) -> None:
    traj = tmp_path / "trajectories"
    _write_trial(traj, "task-a", "20260909_010000", "aaaaaaaa", reward=1.0)
    _write_trial(traj, "task-b", "20260909_010100", "bbbbbbbb", reward=0.0)

    tasks, audit = collect_scores(traj, require_unique_attempt=True)
    assert audit.ok, audit.reason
    assert audit.n_tasks == 2
    assert audit.n_trials == 2
    assert tasks["task-a"].valid_trials[0].reward == 1.0
    assert tasks["task-b"].valid_trials[0].reward == 0.0


def test_duplicate_attempt_with_inconsistent_reward_rejected(tmp_path: Path) -> None:
    """871428 实证的危险样本：同题两 attempt 一 0 一 1 → 必须拒封。"""
    traj = tmp_path / "trajectories"
    _write_trial(traj, "flipt", "20260909_012627", "1c14b580", reward=0.0)
    _write_trial(traj, "flipt", "20260909_012627", "47846f13", reward=1.0)

    tasks, audit = collect_scores(traj, require_unique_attempt=True)
    assert not audit.ok
    assert audit.rejected_tasks == ("flipt",)
    assert not tasks["flipt"].audit_ok
    assert "expected exactly 1 attempt" in tasks["flipt"].audit_reason


def test_empty_trial_when_inner_missing(tmp_path: Path) -> None:
    traj = tmp_path / "trajectories"
    _write_trial(traj, "setup-fail", "20260909_010000", "cccccccc", reward=None, with_inner=False)

    tasks, audit = collect_scores(traj, require_unique_attempt=True)
    assert audit.ok
    assert audit.n_empty == 1
    trial = tasks["setup-fail"].trials[0]
    assert trial.reward_kind == REWARD_EMPTY
    assert trial.reward is None


def test_running_trial_when_verifier_missing(tmp_path: Path) -> None:
    traj = tmp_path / "trajectories"
    _write_trial(traj, "hung", "20260909_010000", "dddddddd", reward=None, with_inner=True)

    tasks, audit = collect_scores(traj, require_unique_attempt=True)
    assert audit.ok
    assert audit.n_running == 1
    trial = tasks["hung"].trials[0]
    assert trial.reward_kind == REWARD_RUNNING


def test_exception_type_is_separate_dimension(tmp_path: Path) -> None:
    traj = tmp_path / "trajectories"
    _write_trial(
        traj, "timeout", "20260909_010000", "eeeeeeee",
        reward=1.0, exception_type="AgentTimeoutError",
    )
    tasks, _audit = collect_scores(traj, require_unique_attempt=True)
    trial = tasks["timeout"].trials[0]
    assert trial.reward == 1.0  # AgentTimeoutError ≠ reward 0
    assert trial.exception_type == "AgentTimeoutError"


def test_release_mode_requires_expected_attempts(tmp_path: Path) -> None:
    traj = tmp_path / "trajectories"
    for i in range(5):
        _write_trial(traj, "full", f"20260909_01010{i}", f"{i:08x}", reward=1.0)
    _write_trial(traj, "short", "20260909_010200", "ffffffff", reward=0.0)

    _tasks, audit = collect_scores(
        traj, expected_attempts=5, require_unique_attempt=False
    )
    assert not audit.ok
    assert audit.rejected_tasks == ("short",)


def test_missing_root_rejected(tmp_path: Path) -> None:
    tasks, audit = collect_scores(tmp_path / "nonexistent", require_unique_attempt=True)
    assert not audit.ok
    assert "not found" in audit.reason
    assert tasks == {}
