"""TB2.1 画布四副本切换（saveExperimentConfig 全量 read-modify-write）。

把 fork 出的画布从 donor 的 benchmark（如 canary13）切换到 TB2.1
（runSpecSetId 32373），并按 EDS 采样。这是 ``experiment_set_canvas_params``
（7 个嵌套参数）覆盖不了的操作——benchmarkList 有四份副本（surface /
submitInfoList / extraData / extraJson[JSON 字符串]），必须原子改齐，否则
surface update 返回成功但实际没生效（memory: V2-eval 精确题集切换须统一四层）。

实现模式沿用 362610-config.py：native ``_req`` + ``_require_experiment_owner``
（fail-closed，改别人的画布会被拒）+ preview 物化 ``runSpecFlowConfigList`` +
``saveExperimentConfig`` 全量写回 + read-back 验证。

TB2.1 物化配置（2026-09-30 实测 preview 362610）：
- runSpecSetId=32373, runSpecId=798614, dataSubSetId=804885, flowId=94, repeatRunTime=0
- ``runSpecAnchorBindMapJson`` 可空（真实 agent 绑定走 ``globalAnchorBindMapJson``）
"""
from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from typing import Any

# llm-mcp native API（与 362610-config.py 同一套）
_LLM_MCP_SRC = "/path/to/your/project"
if _LLM_MCP_SRC not in sys.path:
    sys.path.insert(0, _LLM_MCP_SRC)

# TB2.1 registered benchmark 身份（platform_metadata benchmarks 实测）
TB21_RUNSPECSET_ID = 32373
TB21_RUNSPECSET_NAME = "TB_Pipeline_terminalbench_2.1"
TB21_FLOW_ID = 94  # TB Pipeline flow

# donor 画布的 modelMeta 参数（362610-config.py 验证值）
DONOR_MODEL_META_ID = 284099
DONOR_MODEL_META_BIND_GROUP_ID = 67993

# 证据链强制超时（7d）
EVIDENCE_TIMEOUTS = {
    "flowJobSubmitStateTimeOut": 10080,
    "flowJobSubmittedStateTimeOut": 10080,
    "flowJobRunningStateTimeOut": 10080,
    "newEvalFlowHangKillSeconds": 604800,
}


@dataclass(frozen=True)
class Tb21SwitchSpec:
    """一次 TB2.1 切换的目标配置。"""

    canvas_id: int
    node_id: int
    model_id: int
    agent_alias: str  # agentix@<branch>@simple@tofu
    tb_git_branch: str
    evaluate_name: str  # MT/<name>
    eds: int  # EDS 采样数（20 冒烟 / 1000000 全量）
    agent_workers: int  # cpuJob agent 副本数
    tb21_runspecset_id: int = TB21_RUNSPECSET_ID
    tb21_runspecset_name: str = TB21_RUNSPECSET_NAME
    max_agent_timeout_sec: int = 604800
    global_timeout_multiplier: int = 1


@dataclass(frozen=True)
class Tb21SwitchResult:
    ok: bool
    canvas_id: int
    benchmark_id: int
    runspec_id: int
    dataset_id: int
    agent_workers: int
    eds: int
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


def _req_ok(auth, path, **kwargs):
    from llm_mcp.tools.experiment import _req
    response = _req(auth, path, **kwargs)
    code = response.get("code", 0)
    if code not in (0, 200, "0", "200"):
        raise RuntimeError(f"platform business error on {path}: {response}")
    return response.get("data")


def _materialize_tb21(auth, bench_template: dict, model_id: int) -> dict:
    """preview 物化 TB2.1 的 runSpecFlowConfigList（runSpecId/dataSubSetId）。"""
    tb21_bench = copy.deepcopy(bench_template)
    tb21_bench["runSpecSetId"] = TB21_RUNSPECSET_ID
    tb21_bench["rootRunSpecSetId"] = TB21_RUNSPECSET_ID
    tb21_bench["runSpecSetName"] = TB21_RUNSPECSET_NAME
    previews = _req_ok(auth, "/webApi/evalFlowJob/preview", method="POST", body={
        "benchmarkList": [tb21_bench],
        "modelId": model_id,
        "modelMetaId": DONOR_MODEL_META_ID,
        "modelMetaBindGroupId": DONOR_MODEL_META_BIND_GROUP_ID,
        "flowVersionList": [],
        "useMasterStatCache": False,
        "cpuJobWorkerCount": 0,
        "gpuJobWorkerCount": 0,
        "otherGpuJobWorkerCount": 0,
    })["evalFlowJobPreviewList"]
    if len(previews) != 1:
        raise RuntimeError(f"expected 1 TB2.1 preview, got {len(previews)}")
    return previews[0]


def _set_anchor_param(global_map: list, name: str, value: Any) -> None:
    """在 globalAnchorBindMapJson 里把 agent anchor 的某个 contextParam 改为 value。"""
    for _anchor, binds in global_map:
        for bind in binds:
            for param in bind.get("contextParams", []):
                if param.get("name") == name:
                    param["value"] = value
                    param["source"] = "Job"


def _rewrite_benchmark_aliases(bench: dict, eds: int) -> None:
    """把 benchmark 的 envGlobalAliasParams 设为证据链值（cache off + EDS 采样）。"""
    want = {
        "_ENV_RUN_SPEC_SET_DISABLE_CACHE": "True",
        "_ENV_RUN_SPEC_SET_DISABLE_SCORE_CACHE": "True",
        "_ENV_RUN_SPEC_SET_EVAL_DATA_SIZE": str(eds),
    }
    aliases = bench.setdefault("envGlobalAliasParams", [])
    by_name = {a["name"]: a for a in aliases}
    for name, value in want.items():
        if name in by_name:
            by_name[name]["value"] = value
        else:
            aliases.append({
                "name": name,
                "value": value,
                "type": "bool" if "DISABLE" in name else "evalDataSize",
                "source": "GlobalAliasJob",
                **({"overrideFlag": "ALL_SUB"} if "DISABLE" in name else {}),
            })
    # evalDataSize 顶层同步
    bench["evalDataSize"] = eds


def switch_to_tb21(auth, spec: Tb21SwitchSpec, *, dry_run: bool = True) -> Tb21SwitchResult:
    """把 fork 出的画布切换到 TB2.1（四副本原子）+ EDS 采样 + agent 锚点。

    dry_run=True 只读 + 构造计划不落盘；dry_run=False 真实 saveExperimentConfig +
    read-back 验证。owner 校验 fail-closed。
    """
    from llm_mcp.tools.experiment import _GET, _BASE_INFO, _require_experiment_owner
    from llm_mcp.native.sso import SsoAuth  # noqa: F401  (auth 由调用方传入)

    detail = _req_ok(auth, _GET, params={"experimentId": spec.canvas_id})
    base = _req_ok(auth, _BASE_INFO, method="POST", body={"experimentId": spec.canvas_id})
    _require_experiment_owner(auth, spec.canvas_id, base=base, detail=detail)

    cfg = detail["configDetail"]
    node = next(n for n in cfg["nodes"] if n["nodeId"] == spec.node_id)
    p = json.loads(node["nodeParam"])
    model_node = next(n for n in cfg["nodes"] if n["nodeType"] == 1)
    model = json.loads(model_node["nodeParam"])
    if model["modelId"] != spec.model_id:
        raise RuntimeError(
            f"model binding mismatch: canvas has {model['modelId']}, "
            f"spec wants {spec.model_id}"
        )

    # 物化 TB2.1
    plan = _materialize_tb21(auth, p["benchmarkList"][0], spec.model_id)
    runspec_flow = plan["runSpecFlowConfigList"]
    runspec_id = runspec_flow[0]["runSpecId"]
    dataset_id = runspec_flow[0]["dataSubSetId"]

    new = copy.deepcopy(p)
    new["evaluateName"] = spec.evaluate_name
    new.update(EVIDENCE_TIMEOUTS)
    new["max_agent_timeout_sec"] = spec.max_agent_timeout_sec
    new["global_timeout_multiplier"] = spec.global_timeout_multiplier

    # surface benchmarkList → TB2.1
    new_bench = copy.deepcopy(p["benchmarkList"][0])
    new_bench["runSpecSetId"] = spec.tb21_runspecset_id
    new_bench["rootRunSpecSetId"] = spec.tb21_runspecset_id
    new_bench["runSpecSetName"] = spec.tb21_runspecset_name
    _rewrite_benchmark_aliases(new_bench, spec.eds)
    new["benchmarkList"] = [new_bench]

    sub = new["submitInfoList"][0]
    sub["benchmarkList"] = copy.deepcopy(new["benchmarkList"])
    sub["runSpecFlowConfigList"] = copy.deepcopy(runspec_flow)
    for key in ("modelId", "modelMetaId", "taskUser", "sourceMetaId"):
        if key in plan:
            sub[key] = plan[key]
    sub.update(EVIDENCE_TIMEOUTS)
    sub["evaluateName"] = spec.evaluate_name
    sub["max_agent_timeout_sec"] = spec.max_agent_timeout_sec

    # agent 锚点（globalAnchorBindMapJson）
    gm = json.loads(sub["globalAnchorBindMapJson"])
    _set_anchor_param(gm, "agent", spec.agent_alias)
    _set_anchor_param(gm, "tb_git_branch", spec.tb_git_branch)
    _set_anchor_param(gm, "max_agent_timeout_sec", spec.max_agent_timeout_sec)
    sub["globalAnchorBindMapJson"] = json.dumps(gm, ensure_ascii=False)

    # worker 并发（resourceConfigList agent/cpuJob）
    for resource in sub.get("resourceConfigList", []):
        if resource.get("resourceType") == "cpuJob" and resource.get("workers", 1) > 1:
            resource["workers"] = spec.agent_workers

    # extraData / extraJson（第三、四副本）
    extra = copy.deepcopy(sub["extraData"])
    extra.update(EVIDENCE_TIMEOUTS)
    extra["benchmarkList"] = copy.deepcopy(new["benchmarkList"])
    sub["extraData"] = extra
    sub["extraJson"] = json.dumps(extra, ensure_ascii=False)

    evidence = {
        "canvas_id": spec.canvas_id,
        "node_id": spec.node_id,
        "benchmark_id": spec.tb21_runspecset_id,
        "runspec_id": runspec_id,
        "dataset_id": dataset_id,
        "eds": spec.eds,
        "agent_workers": spec.agent_workers,
        "agent_alias": spec.agent_alias,
        "tb_git_branch": spec.tb_git_branch,
        "evaluate_name": spec.evaluate_name,
    }

    if dry_run:
        return Tb21SwitchResult(
            ok=True,
            canvas_id=spec.canvas_id,
            benchmark_id=spec.tb21_runspecset_id,
            runspec_id=runspec_id,
            dataset_id=dataset_id,
            agent_workers=spec.agent_workers,
            eds=spec.eds,
            reason="dry_run",
            evidence=evidence,
        )

    # 真实写：saveExperimentConfig
    nodes = copy.deepcopy(cfg["nodes"])
    target = next(n for n in nodes if n["nodeId"] == spec.node_id)
    target["nodeParam"] = json.dumps(new, ensure_ascii=False)
    from llm_mcp.tools.experiment import _req
    response = _req(auth, "/webApi/model/experiment/saveExperimentConfig", method="POST", body={
        "experimentId": {
            "businessType": 0,
            "experimentId": spec.canvas_id,
            "experimentName": base["experimentConfig"]["experimentName"],
        },
        "nodes": nodes,
        "edges": cfg.get("edges", []),
        "slaTask": detail.get("slaTask", False),
        "feDisplayConfig": detail.get("feDisplayConfig", ""),
    })
    code = response.get("code", 0)
    if code not in (0, 200, "0", "200"):
        raise RuntimeError(f"saveExperimentConfig failed: {response}")

    # read-back 验证唯一 benchmark = TB2.1
    rb = _req_ok(auth, _GET, params={"experimentId": spec.canvas_id})
    saved = json.loads(
        next(n for n in rb["configDetail"]["nodes"] if n["nodeId"] == spec.node_id)["nodeParam"]
    )
    sb = saved["benchmarkList"][0]
    if sb["runSpecSetId"] != spec.tb21_runspecset_id:
        raise RuntimeError(
            f"read-back benchmark mismatch: expected {spec.tb21_runspecset_id}, "
            f"got {sb['runSpecSetId']} (四副本未原子生效)"
        )
    sub_rb = saved["submitInfoList"][0]
    rsf = sub_rb["runSpecFlowConfigList"][0]
    if rsf["runSpecSetId"] != spec.tb21_runspecset_id:
        raise RuntimeError(
            f"read-back runSpecFlowConfigList mismatch: {rsf['runSpecSetId']}"
        )

    return Tb21SwitchResult(
        ok=True,
        canvas_id=spec.canvas_id,
        benchmark_id=spec.tb21_runspecset_id,
        runspec_id=rsf["runSpecId"],
        dataset_id=rsf["dataSubSetId"],
        agent_workers=spec.agent_workers,
        eds=spec.eds,
        reason="saved_and_verified",
        evidence=evidence,
    )
