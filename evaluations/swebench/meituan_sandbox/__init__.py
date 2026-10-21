"""yourprovider-sandbox 远程评测通路：提交（submit）、监控（watch）、收分（score）。

与 ``start_harbor_run``（本地 Harbor 执行）平行的远程提交通路。tofu 作为画布
TerminalBenchPipeline 的可编程提交器 + 结果归集器，agent 链路复用已验证的
``agentix@<branch>@simple@tofu`` kernel。设计见
``docs/evaluations/MEITUAN_SANDBOX_BACKEND.md``。

不进 ``ISOLATED_BACKENDS``：远程 run 不产本地 trial 树/manifest。
"""
from __future__ import annotations

from .score import (
    AttemptAudit,
    TaskScore,
    TrialScore,
    collect_scores,
    resolve_trajectory_root,
)
from .submit import (
    DONOR_CACHE_FLAGS,
    EVIDENCE_CANVAS_PARAMS,
    MassConfig,
    SubmitPlan,
    SubmitResult,
    build_submit_plan,
    execute_submit,
)
from .watch import (
    TERMINAL_ALL,
    TERMINAL_BAD,
    TERMINAL_OK,
    WatchResult,
    watch_flow,
)

__all__ = [
    "AttemptAudit",
    "DONOR_CACHE_FLAGS",
    "EVIDENCE_CANVAS_PARAMS",
    "MassConfig",
    "SubmitPlan",
    "SubmitResult",
    "TERMINAL_ALL",
    "TERMINAL_BAD",
    "TERMINAL_OK",
    "TaskScore",
    "TrialScore",
    "WatchResult",
    "build_submit_plan",
    "collect_scores",
    "execute_submit",
    "resolve_trajectory_root",
    "watch_flow",
]
