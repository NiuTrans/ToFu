"""Harbor adapter must execute the real runtime contract, not a private loop."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


def _install_fake_harbor(monkeypatch) -> None:
    modules = {
        name: ModuleType(name)
        for name in (
            "harbor", "harbor.agents", "harbor.agents.base",
            "harbor.environments", "harbor.environments.base",
            "harbor.models", "harbor.models.agent",
            "harbor.models.agent.context",
        )
    }

    class BaseAgent:
        def __init__(self, logs_dir, model_name=None, **_kwargs):
            self.logs_dir = Path(logs_dir)
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            self.model_name = model_name
            self.context_id = "context-1"
            self.session_id = "session-1"

    class BaseEnvironment:
        pass

    class AgentContext:
        n_input_tokens = 0
        n_output_tokens = 0
        metadata = {}

    modules["harbor.agents.base"].BaseAgent = BaseAgent
    modules["harbor.environments.base"].BaseEnvironment = BaseEnvironment
    modules["harbor.models.agent.context"].AgentContext = AgentContext
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_adapter_uses_exclusive_production_runtime_and_guest_exec(
    tmp_path, monkeypatch,
):
    _install_fake_harbor(monkeypatch)
    module_name = "rootless_vm.harbor_tofu_runtime_agent"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KIMI_API_KEY", "host-secret-never-artifact")
    captured: dict = {}

    class Execution:
        task_id = "task-fixture"

        async def events_async(self, **_kwargs):
            yield {
                "type": "custom_tool_call",
                "seq": 1,
                "callId": "ctool_1",
                "toolName": "custom__run_command",
                "arguments": {"command": "printf ready"},
            }
            yield {"type": "done", "seq": 2, "finishReason": "stop"}

        def resolve_custom_tool_call(self, call_id, content, *, is_error=False):
            captured["resolution"] = {
                "call_id": call_id, "content": content,
                "is_error": is_error,
            }
            return True

        async def result_async(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                content="fixture complete",
                status="done",
                finish_reason="stop",
                usage={"prompt_tokens": 19, "completion_tokens": 4},
            )

        def evidence_snapshot(self):
            return {
                "contractVersion": "tofu.agent-runtime-evidence/v1",
                "taskId": self.task_id,
                "model": "kimi-k3",
                "status": "done",
                "finishReason": "stop",
                "usage": {"prompt_tokens": 19, "completion_tokens": 4},
                "apiRounds": [{
                    "round": 1,
                    "usage": {"prompt_tokens": 19, "completion_tokens": 4},
                }],
                "contextTelemetryRounds": [{"round": 1}],
                "contextCompactionEvents": [],
                "toolSchemas": [],
                "customToolsMode": "exclusive",
                "orchestrationDecisions": [],
                "output": {"content": "fixture complete"},
            }

    class Runtime:
        def start(self, messages, **kwargs):
            captured["messages"] = messages
            captured["start_kwargs"] = kwargs
            return Execution()

        def close(self, **kwargs):
            captured["closed"] = kwargs

    def fake_local(**kwargs):
        captured["runtime_kwargs"] = kwargs
        return Runtime()

    monkeypatch.setattr(module.AgentRuntime, "local", fake_local)

    class Environment:
        async def exec(self, *, command, timeout_sec):
            captured["guest_exec"] = {
                "command": command, "timeout_sec": timeout_sec,
            }
            return SimpleNamespace(
                return_code=0, stdout="ready", stderr="")

    runtime_config = {
        "responses": {"promptProfile": "lean"},
        "tools": {"resultEnvelope": "v2"},
    }
    config_digest = module._canonical_sha256(runtime_config)
    logs = tmp_path / "logs"
    agent = module.TofuKimiRuntimeAgent(
        logs_dir=logs,
        model_name="kimi-k3",
        upstream_base_url_env="KIMI_CHAT_BASE_URL",
        upstream_api_key_env="KIMI_API_KEY",
        provider_face="meituan-chat",
        provider_slot_id="slot-a",
        experiment_arm="prompt_lean_kimi",
        runtime_config=runtime_config,
        runtime_config_sha256=config_digest,
        reasoning_effort="high",
        timeout_sec=30,
        command_timeout_sec=17,
    )
    context = sys.modules[
        "harbor.models.agent.context"
    ].AgentContext()

    asyncio.run(agent.run("Solve the fixture.", Environment(), context))

    assert captured["start_kwargs"]["custom_tools_mode"] == "exclusive"
    assert {
        row["function"]["name"]
        for row in captured["start_kwargs"]["custom_tools"]
    } == {"custom__run_command", "custom__submit_result"}
    assert captured["start_kwargs"]["config"]["disableModelFallback"] is True
    access = captured["runtime_kwargs"]["model_routing"]
    assert access.model == {"creator_id": "moonshot", "model_id": "kimi-k3"}
    assert access.routing == {"preferred_provider_id": "harbor-kimi"}
    assert captured["runtime_kwargs"]["model_routing_source"] == \
        "harbor-formal-kimi"
    assert captured["guest_exec"] == {
        "command": "printf ready", "timeout_sec": 17,
    }
    assert captured["resolution"]["call_id"] == "ctool_1"
    assert "exit_code=0" in captured["resolution"]["content"]
    assert context.n_input_tokens == 19
    assert context.n_output_tokens == 4
    metadata = context.metadata["tofuKimiEvidence"]
    assert metadata["credentialBoundary"] == "harbor-host-only"
    assert metadata["status"] == "done"
    event_rows = [
        json.loads(line)
        for line in (logs / "tofu-kimi-evidence/events.jsonl")
        .read_text().splitlines()
    ]
    assert [row["event"]["type"] for row in event_rows] == [
        "custom_tool_call", "done",
    ]
    evidence = json.loads(
        (logs / "tofu-kimi-evidence/runtime-evidence.json").read_text())
    assert evidence["contractVersion"] == "tofu.agent-runtime-evidence/v1"
    tool_audit = json.loads(
        (logs / "tofu-kimi-evidence/tool-audit.json").read_text())
    assert tool_audit["contractVersion"] == \
        "tofu.harbor-custom-tool-audit/v1"
    assert tool_audit["calls"][0]["callId"] == "ctool_1"
    assert tool_audit["calls"][0]["rawBytes"] \
        == tool_audit["calls"][0]["visibleBytes"]
    assert tool_audit["calls"][0]["truncated"] is False
    assert tool_audit["calls"][0]["rawResultSha256"] \
        == tool_audit["calls"][0]["visibleResultSha256"]
    assert metadata["toolAuditSha256"] == module.hashlib.sha256(
        (logs / "tofu-kimi-evidence/tool-audit.json").read_bytes()
    ).hexdigest()
    assert metadata["toolSchemaSha256"] == \
        module.tofu_kimi_tool_schema_sha256()
    assert metadata["promptContractSha256"] == \
        module.tofu_kimi_prompt_contract_sha256(runtime_config)
    trajectory = json.loads((logs / "trajectory.json").read_text())
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert trajectory["agent"]["name"] == "tofu-kimi-runtime"
    artifacts = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in logs.rglob("*") if path.is_file()
    )
    assert "host-secret-never-artifact" not in artifacts
    assert "https://models.example/v1" not in artifacts


def test_adapter_output_budget_includes_validation_suffix(
    monkeypatch,
):
    _install_fake_harbor(monkeypatch)
    module_name = "rootless_vm.harbor_tofu_runtime_agent"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    stdout = "汉" * 20_000
    suffix = "\nValidation passed. Return the final answer now."

    content, audit = module._bounded_output(
        stdout, "", 0, suffix=suffix)
    raw = f"exit_code=0\nstdout:\n{stdout}{suffix}".encode("utf-8")

    assert audit["truncated"] is True
    assert audit["rawBytes"] == len(raw)
    assert audit["visibleBytes"] <= 24 * 1024
    assert audit["rawResultSha256"] == module.hashlib.sha256(raw).hexdigest()
    assert audit["visibleResultSha256"] == module.hashlib.sha256(
        content.encode("utf-8")).hexdigest()
    assert content.endswith(suffix)


@pytest.mark.parametrize(
    ("failure_kind", "expected_error"),
    [("timeout", TimeoutError), ("cancel", asyncio.CancelledError)],
)
def test_adapter_persists_partial_runtime_evidence_on_timeout_or_cancel(
    tmp_path, monkeypatch, failure_kind, expected_error,
):
    _install_fake_harbor(monkeypatch)
    module_name = "rootless_vm.harbor_tofu_runtime_agent"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KIMI_API_KEY", "host-secret-never-artifact")
    captured: dict = {}
    usage = {
        "prompt_tokens": 17,
        "completion_tokens": 3,
        "cache_read_tokens": 5,
    }

    class Execution:
        async def events_async(self, **_kwargs):
            yield {
                "type": "round_usage", "seq": 1, "roundNum": 1,
                "model": "kimi-k3", "usage": usage,
            }
            if failure_kind == "timeout":
                raise TimeoutError("injected partial stream timeout")
            raise asyncio.CancelledError("injected adapter cancellation")

        def evidence_snapshot(self):
            return {
                "contractVersion": "tofu.agent-runtime-evidence/v1",
                "requestId": "failure-fixture",
                "taskId": "task-failure-fixture",
                "model": "kimi-k3",
                "providerId": "inline",
                "status": "aborted" if failure_kind == "cancel" else "error",
                "finishReason": failure_kind,
                "usage": usage,
                "apiRounds": [{"round": 1, "usage": usage}],
                "contextTelemetryRounds": [],
                "contextCompactionEvents": [],
                "compactionUsage": {},
                "toolSchemas": [],
                "customToolsMode": "exclusive",
                "orchestrationDecisions": [],
                "output": {"content": "", "charCount": 0},
            }

    class Runtime:
        def start(self, *_args, **_kwargs):
            return Execution()

        def close(self, **kwargs):
            captured["closed"] = kwargs

    monkeypatch.setattr(
        module.AgentRuntime, "local", lambda **_kwargs: Runtime())
    runtime_config = {
        "responses": {"promptProfile": "lean"},
        "tools": {"resultEnvelope": "v2"},
    }
    logs = tmp_path / failure_kind
    agent = module.TofuKimiRuntimeAgent(
        logs_dir=logs,
        model_name="kimi-k3",
        upstream_base_url_env="KIMI_CHAT_BASE_URL",
        upstream_api_key_env="KIMI_API_KEY",
        provider_face="meituan-chat",
        provider_slot_id="slot-a",
        experiment_arm="prompt_lean_kimi",
        runtime_config=runtime_config,
        runtime_config_sha256=module._canonical_sha256(runtime_config),
        reasoning_effort="high",
        timeout_sec=30,
        command_timeout_sec=17,
    )
    context = sys.modules[
        "harbor.models.agent.context"
    ].AgentContext()

    with pytest.raises(expected_error):
        asyncio.run(agent.run("Solve the fixture.", object(), context))

    evidence_dir = logs / "tofu-kimi-evidence"
    evidence = json.loads(
        (evidence_dir / "runtime-evidence.json").read_text())
    audit = json.loads((evidence_dir / "tool-audit.json").read_text())
    events = (evidence_dir / "events.jsonl").read_text()
    assert evidence["apiRounds"][0]["usage"] == usage
    assert audit["calls"] == []
    assert '\"type\":\"round_usage\"' in events
    assert context.n_input_tokens == 17
    assert context.n_output_tokens == 3
    assert captured["closed"] == {"abort": True}
    assert "host-secret-never-artifact" not in (
        events + json.dumps(evidence) + json.dumps(audit))
