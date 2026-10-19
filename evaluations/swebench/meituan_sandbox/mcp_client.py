"""``CanvasClient``/``JobsClient`` 的真实 llm MCP 桥。

把协议方法映射到 ``mcp__llm__*`` 工具。本模块不直接 import MCP（那是 tofu 宿主
的工具面），而是通过一个 ``call_tool`` 可调用注入——宿主把 ``execute_tools``
的单次调用能力传进来。这样本模块可在无 MCP 的单测里用 stub ``call_tool`` 验证
参数构造与响应解析。

字段层级（2026-09-30 实测 362610 node_config 核实）：

- agent / tb_git_branch / proxy / sandbox_endpoint / max_agent_timeout_sec /
  global_timeout_multiplier / eds → ``experiment_set_canvas_params``（嵌套层，
  覆盖所有 nodeParam 副本）。
- flow 超时（flowJobSubmitStateTimeOut 等）与资源（cpuJobQueue/gpuJobQueue/
  mlpResourceExperiment/mlpSchedulePriority）→ nodeParam 顶层，
  ``experiment_update_params`` 的 field= 键。
- donor cache flag（_ENV_RUN_SPEC_SET_DISABLE_CACHE 等）→
  ``benchmarkList[].envGlobalAliasParams[]``，362610 已为 "True"；fork 继承 False
  时需翻转（skill 实测：False → judge COMPLETED 但分数被丢弃）。
- worker 并发 → ``submitInfoList[].resourceConfigList[rootAnchor='agent'].workers``，
  get_node_config 会折叠为 ``_omitted``，需原生 getExperimentConfigDetail 全量
  read-modify-write（本桥暂不覆盖，worker 继承 donor）。
"""
from __future__ import annotations

from typing import Any, Callable

# call_tool(tool_name, arguments) -> dict（已解析的 MCP 结构化结果）
CallTool = Callable[[str, dict[str, Any]], dict[str, Any]]


def _require_ok(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise RuntimeError(f"{tool} failed: {result}")
    return result


class McpCanvasClient:
    """真实画布 client（提交器用）。"""

    def __init__(self, call_tool: CallTool) -> None:
        self._call = call_tool

    def check_login(self) -> bool:
        result = _require_ok("check_login", self._call("mcp__llm__check_login", {}))
        return bool(result.get("logged_in"))

    def whoami(self) -> str:
        result = _require_ok("whoami", self._call("mcp__llm__whoami", {}))
        subject = result.get("subject") or result.get("mis")
        if not subject:
            raise RuntimeError(f"whoami returned no subject: {result}")
        return str(subject)

    def set_canvas_params(self, canvas_id: str, params: dict[str, Any]) -> None:
        """嵌套参数重写（agent/tb_git_branch/超时等）。

        只转发 ``experiment_set_canvas_params`` 支持的键；其余键（flow 超时、
        cache flag、worker）由调用方走 update_params / 全量路径。dry_run=False
        直接生效（提交序列后续 read-back 校验）。
        """
        supported = {
            "agent", "proxy", "sandbox_endpoint", "tb_git_branch",
            "max_agent_timeout_sec", "global_timeout_multiplier", "eds",
        }
        args: dict[str, Any] = {"experiment_id": canvas_id, "dry_run": False}
        for key, value in params.items():
            if key in supported:
                args[key] = value
        if len(args) <= 2:
            return  # 没有 set_canvas_params 能处理的键
        _require_ok("experiment_set_canvas_params",
                    self._call("mcp__llm__experiment_set_canvas_params", args))

    def get_canvas_params(self, canvas_id: str) -> dict[str, Any]:
        result = _require_ok(
            "experiment_get_canvas_params",
            self._call("mcp__llm__experiment_get_canvas_params",
                       {"experiment_id": canvas_id}),
        )
        return dict(result.get("data", {}).get("current", {}))

    def fork(self, base_id: str, name: str, model_id: str) -> str:
        result = _require_ok(
            "experiment_fork",
            self._call("mcp__llm__experiment_fork", {
                "base_id": base_id,
                "name": name,
                "model_id": model_id,
                "dry_run": False,
            }),
        )
        data = result.get("data", {})
        new_id = data.get("experimentId") or data.get("experiment_id") or data.get("id")
        if not new_id:
            raise RuntimeError(f"fork returned no experiment id: {result}")
        return str(new_id)

    def get(self, canvas_id: str) -> dict[str, Any]:
        result = _require_ok(
            "experiment_get",
            self._call("mcp__llm__experiment_get",
                       {"experiment_id_or_name_or_url": canvas_id}),
        )
        return dict(result.get("data", {}))

    def run(self, canvas_id: str) -> str:
        result = _require_ok(
            "experiment_run",
            self._call("mcp__llm__experiment_run",
                       {"ids": [canvas_id], "dry_run": False}),
        )
        data = result.get("data", {})
        flow_id = data.get("flowId") or data.get("flow_id") or data.get("taskId")
        return str(flow_id) if flow_id else ""


class McpJobsClient:
    """真实 eval jobs client（监控器用）。"""

    def __init__(self, call_tool: CallTool, experiment_id: str) -> None:
        self._call = call_tool
        self._experiment_id = experiment_id

    def status(self, flow_id: str) -> dict[str, Any]:
        result = _require_ok(
            "experiment_eval_jobs",
            self._call("mcp__llm__experiment_eval_jobs", {
                "experiment_id_or_name_or_url": self._experiment_id,
                "job_id": flow_id,
            }),
        )
        data = result.get("data", {})
        # 平台状态字段：evalFlowJobStatus / status（大小写平台语义已在外层归一）
        status = data.get("status") or data.get("evalFlowJobStatus") or ""
        return {"status": status, "raw": data}
