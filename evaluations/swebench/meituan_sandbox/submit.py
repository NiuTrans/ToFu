"""提交器：把画布 TerminalBenchPipeline 提交为 yourprovider-sandbox 远程 run。

全部走封装好的 ``llm`` MCP 工具（不依赖原始 webApi 脚本）。网络调用通过
``CanvasClient`` 协议注入，便于离线 dry-run 与单测；真实实现见
``evaluations/swebench/yourprovider_sandbox/mcp_client``（MCP 工具桥）。

提交序列（每步 read-back 校验；顺序即证据链）：

1. ``check_login`` + ``whoami``：SSO 探活 + owner 校验（SSO 会话会过期且无法无人值守
   刷新 → 长效 pipeline 必须探活 + 失效告警）。
2. 翻转 donor 画布 cache flag：``_ENV_RUN_SPEC_SET_DISABLE_CACHE`` /
   ``_ENV_RUN_SPEC_SET_DISABLE_SCORE_CACHE`` → ``"True"``（fork 继承 False → judge
   COMPLETED 但分数被丢弃 ``not upload eval result``；其他字段字节级不变）。
3. ``experiment_fork(base_id, model_id)``。
4. ``experiment_get`` / ``experiment_get_node_config`` 校验模型绑定（防 fork 继承
   ``llm_debug`` 的 ~21min force-kill 队列）。
5. ``experiment_set_canvas_params`` 重放锚点（fork 会重置 agent/tb 锚点为模板值）。
6. ``experiment_get_canvas_params`` read-back（update→run 有传播竞态）。
7. ``experiment_run(experiment_id)`` 启动（``/preview`` 只物化、
   ``/saveExperimentConfig`` 只存配置，真正启动是 ``experiment_run``）。

**坑**：``experiment_run_node(dry_run=false)`` 可能返回 schema 假错但服务端已启动——
盲重试双开画布烧配额。启动后必须 ``experiment_get`` 确认 node status。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# 证据链强制参数（tofu release 铁律映射到画布字段）
EVIDENCE_CANVAS_PARAMS: dict[str, Any] = {
    # 禁平台层重试：retries erase evidence
    "repeatRunTime": 0,
    # agent 超时 7d（秒）
    "max_agent_timeout_sec": 604800,
    "global_timeout_multiplier": 1,
    # flow 超时 7d（分钟）
    "flowJobSubmitStateTimeOut": 10080,
    "flowJobSubmittedStateTimeOut": 10080,
    "flowJobRunningStateTimeOut": 10080,
    # hang-kill 7d（秒；UI 显示分钟但 API 是秒）
    "newEvalFlowHangKillSeconds": 604800,
}

# donor 画布必须翻转的两个 cache flag（fork 继承 False → 分数被丢弃）
DONOR_CACHE_FLAGS: dict[str, str] = {
    "_ENV_RUN_SPEC_SET_DISABLE_CACHE": "True",
    "_ENV_RUN_SPEC_SET_DISABLE_SCORE_CACHE": "True",
}

# 资源（已拍板，不做 tofu 侧参数化；TB2.1 纯 CPU，模型走 API）
RESOURCE_CODE = "longcat_harness"
SCHEDULE_PRIORITY = "p0"
CPU_QUEUE = "root.hldy_training_cluster.your-user.cpu_weight_convert"
GPU_QUEUE = "root.shxs_training_cluster.your-user.autoresearch_agent"


@dataclass(frozen=True)
class MassConfig:
    """TB2.1 mass 配置（89 题 × 5 attempt = 445 trial，纯 CPU）。

    Sizing 依据（hope skill 实测系数）：Agent worker ≈ 4核/8GiB，AM ≈ 20-30核，
    Group ≈ 4核/16GiB。worker=64 → ~300 核，`cpu_weight_convert`（1002 核空闲）充裕；
    理论 ~7h 压回 8h 派发截止内（worker=32 理论 ~14h 会超）。
    """

    task_count: int = 89
    attempts: int = 5
    agent_workers: int = 64
    group_workers: int = 1
    gpu_cards: int = 0  # TB2.1 无模型 GPU serving，不占用 96 卡上限

    @property
    def total_trials(self) -> int:
        return self.task_count * self.attempts

    @property
    def estimated_cpu_cores(self) -> int:
        # Agent workers × 4 核 + Group × 4 核 + AM ~25 核
        return self.agent_workers * 4 + self.group_workers * 4 + 25

    @property
    def estimated_memory_gib(self) -> int:
        return self.agent_workers * 8 + self.group_workers * 16 + 16


@dataclass
class SubmitPlan:
    """一次提交的完整计划（dry-run 可序列化输出，不落盘）。"""

    base_canvas_id: str
    model_id: str
    agent_alias: str  # agentix@<branch>@simple@tofu（含三 SHA）
    tb_git_branch: str
    evaluate_name: str  # MT/<name>
    mass: MassConfig
    canvas_params: dict[str, Any] = field(default_factory=dict)
    donor_cache_flags: dict[str, str] = field(
        default_factory=lambda: dict(DONOR_CACHE_FLAGS)
    )
    resource_code: str = RESOURCE_CODE
    schedule_priority: str = SCHEDULE_PRIORITY
    cpu_queue: str = CPU_QUEUE
    gpu_queue: str = GPU_QUEUE

    def merged_canvas_params(self) -> dict[str, Any]:
        """证据链强制参数 + 调用方覆盖（强制参数不可被覆盖）。"""
        merged = dict(self.canvas_params)
        for key, value in EVIDENCE_CANVAS_PARAMS.items():
            if key in merged and merged[key] != value:
                raise ValueError(
                    f"evidence-chain canvas param {key!r} is pinned to {value!r} "
                    f"(retries erase evidence); got {merged[key]!r}"
                )
            merged[key] = value
        return merged


class CanvasClient(Protocol):
    """画布 MCP 工具桥（真实实现走 llm MCP；测试注入 stub）。"""

    def check_login(self) -> bool: ...

    def whoami(self) -> str: ...

    def set_canvas_params(self, canvas_id: str, params: dict[str, Any]) -> None: ...

    def get_canvas_params(self, canvas_id: str) -> dict[str, Any]: ...

    def fork(self, base_id: str, name: str, model_id: str) -> str: ...

    def get(self, canvas_id: str) -> dict[str, Any]: ...

    def run(self, canvas_id: str) -> str: ...


def build_submit_plan(
    *,
    base_canvas_id: str,
    model_id: str,
    tofu_commit: str,
    agentix_branch: str,
    tb_git_branch: str,
    evaluate_name: str,
    mass: MassConfig | None = None,
    canvas_params: dict[str, Any] | None = None,
) -> SubmitPlan:
    """构造提交计划。agent 别名固定 ``agentix@<branch>@simple@tofu``。"""
    agent_alias = f"agentix@{agentix_branch}@simple@tofu"
    if not evaluate_name.startswith("MT/"):
        evaluate_name = f"MT/{evaluate_name}"
    plan = SubmitPlan(
        base_canvas_id=base_canvas_id,
        model_id=model_id,
        agent_alias=agent_alias,
        tb_git_branch=tb_git_branch,
        evaluate_name=evaluate_name,
        mass=mass or MassConfig(),
        canvas_params=dict(canvas_params or {}),
    )
    # 触发 merged 校验（早失败）
    plan.merged_canvas_params()
    return plan


@dataclass(frozen=True)
class SubmitResult:
    """提交结果。"""

    canvas_id: str
    flow_id: str
    evaluate_name: str
    agent_alias: str
    owner: str


def execute_submit(client: CanvasClient, plan: SubmitPlan) -> SubmitResult:
    """按提交序列执行（每步 read-back 校验）。

    顺序即证据链；任何一步校验失败立即抛错（不盲重试，防 run_node 假错双开）。
    """
    # 1. SSO 探活 + owner 校验
    if not client.check_login():
        raise RuntimeError(
            "SSO session expired and cannot be refreshed unattended; "
            "re-run a CIBA login (大象 push) and retry"
        )
    owner = client.whoami()

    # 2. 翻转 donor 画布 cache flag（其他字段字节级不变由 set_canvas_params 保证）
    client.set_canvas_params(plan.base_canvas_id, dict(plan.donor_cache_flags))
    donor_params = client.get_canvas_params(plan.base_canvas_id)
    for key, value in plan.donor_cache_flags.items():
        if donor_params.get(key) != value:
            raise RuntimeError(
                f"donor canvas cache flag {key!r} read-back mismatch: "
                f"expected {value!r}, got {donor_params.get(key)!r} "
                f"(judge result will be dropped if False)"
            )

    # 3. fork
    canvas_id = client.fork(plan.base_canvas_id, plan.evaluate_name, plan.model_id)

    # 4. 校验模型绑定
    info = client.get(canvas_id)
    bound = info.get("modelId") or info.get("model_id")
    if bound is not None and str(bound) != str(plan.model_id):
        raise RuntimeError(
            f"model binding mismatch after fork: expected {plan.model_id}, "
            f"got {bound} (fork may have inherited a stale 模型信息 node)"
        )

    # 5. 重放锚点（fork 会重置 agent/tb 锚点为模板值）+ 证据链强制参数
    anchor_params = {
        "agent": plan.agent_alias,
        "tb_git_branch": plan.tb_git_branch,
        "evaluateName": plan.evaluate_name,
        **plan.merged_canvas_params(),
    }
    client.set_canvas_params(canvas_id, anchor_params)

    # 6. read-back（update→run 有传播竞态）
    read_back = client.get_canvas_params(canvas_id)
    if read_back.get("agent") != plan.agent_alias:
        raise RuntimeError(
            f"agent anchor read-back mismatch: expected {plan.agent_alias!r}, "
            f"got {read_back.get('agent')!r} (propagation race; retry set)"
        )

    # 7. 启动
    flow_id = client.run(canvas_id)

    return SubmitResult(
        canvas_id=canvas_id,
        flow_id=flow_id,
        evaluate_name=plan.evaluate_name,
        agent_alias=plan.agent_alias,
        owner=owner,
    )
