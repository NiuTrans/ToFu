"""Harbor adapter must execute the real runtime contract, not a private loop."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
import threading
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


@pytest.mark.parametrize('cancel', [False, True])
def test_slow_evidence_fsync_does_not_block_other_trials_or_lose_cancelled_write(
    tmp_path, monkeypatch, cancel,
):
    _install_fake_harbor(monkeypatch)
    name = 'rootless_vm.harbor_tofu_runtime_agent'
    monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module(name)
    entered, release = threading.Event(), threading.Event()
    original_fsync = module.os.fsync

    def slow_fsync(fd):
        entered.set()
        assert release.wait(2), 'Harbor loop froze inside evidence fsync'
        original_fsync(fd)

    writer = module._PrivateEventWriter(tmp_path / 'events.jsonl')
    monkeypatch.setattr(module.os, 'fsync', slow_fsync)

    async def exercise():
        pending = asyncio.create_task(writer.append_async({'type': 'custom_tool_call', 'seq': 1}))
        try:
            for _ in range(250):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set() and not pending.done()
            if cancel:
                pending.cancel()
                await asyncio.sleep(0.01)
                assert not pending.done(), 'cancellation must drain the owned write before close'
            # A second trial can run and release the slow disk from this loop.
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                assert await pending > 0
        finally:
            release.set()
            if not pending.done():
                await asyncio.gather(pending, return_exceptions=True)
            writer.close()

    asyncio.run(exercise())
    assert json.loads(writer.path.read_text())['event']['seq'] == 1
    assert writer.metrics()['eventWriteCount'] == 1
    assert writer.metrics()['eventWriteTotalMs'] > 0


@pytest.mark.parametrize('resolution_accepted', [True, False])
def test_adapter_uses_exclusive_production_runtime_and_guest_exec(
    tmp_path, monkeypatch, resolution_accepted,
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
            return resolution_accepted

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

    if not resolution_accepted:
        with pytest.raises(RuntimeError, match='rejected its owned custom tool result'):
            asyncio.run(agent.run("Solve the fixture.", Environment(), context))
        audit = json.loads((logs / 'tofu-kimi-evidence/tool-audit.json').read_text())
        assert len(audit['calls']) == 1
        assert audit['calls'][0]['resolutionAccepted'] is False
        assert 'exit_code=0' in audit['calls'][0]['result']
        assert (logs / 'tofu-kimi-evidence/runtime-evidence.json').is_file()
        assert not (logs / 'trajectory.json').exists()
        return
    asyncio.run(agent.run("Solve the fixture.", Environment(), context))

    assert captured["start_kwargs"]["custom_tools_mode"] == "exclusive"
    for tool in captured['start_kwargs']['custom_tools']:
        assert tool['execution']['timeout_s'] > tool['function']['parameters']['properties']['timeout_sec']['maximum']
    assert {
        row["function"]["name"]
        for row in captured["start_kwargs"]["custom_tools"]
    } == {"custom__run_command", "custom__submit_result"}
    assert captured["start_kwargs"]["config"]["disableModelFallback"] is True
    access = captured["runtime_kwargs"]["model_routing"]
    assert access.model == {"creator_id": "moonshot", "model_id": "kimi-k3"}
    assert access.routing == {"preferred_provider_id": "harbor-benchmark"}
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


# ── tool_surface='production' ────────────────────────────────────────────────


def _install_stub_harbor_environment(monkeypatch) -> None:
    """Satisfy the adapter's lazy rootfs-mapping import without the QEMU stack."""
    from types import ModuleType as _ModuleType

    stub = _ModuleType("rootless_vm.harbor_environment")

    def _container_rootfs_path(rootfs: str, container_path: str) -> str:
        from pathlib import PurePosixPath
        path = PurePosixPath(container_path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("bad container path")
        return str(PurePosixPath(rootfs) / path.relative_to(PurePosixPath("/")))

    stub._container_rootfs_path = _container_rootfs_path
    monkeypatch.setitem(
        sys.modules, "rootless_vm.harbor_environment", stub)


def test_custom_tool_channel_cannot_shadow_builtin_run_command(monkeypatch):
    """Mechanism evidence: same-name override is rejected at mint time.

    The production run_command can therefore never be overridden by a
    client-executed custom tool — the adapter's production surface must
    withhold the host-executing shell names instead.
    """
    from lib.tools.tool_env import (
        CustomToolError, dispose_tool_env, mint_tool_env,
    )

    def _tool(name):
        return {
            "type": "function",
            "function": {
                "name": name, "description": "fixture",
                "parameters": {"type": "object", "properties": {}},
            },
            "execution": {"mode": "client"},
        }

    with pytest.raises(CustomToolError):
        mint_tool_env(tools=[_tool("run_command")], owner="fixture")
    env = mint_tool_env(
        tools=[_tool("custom__run_command")], owner="fixture")
    try:
        assert env.resolve("custom__run_command") is not None
        assert env.resolve("run_command") is None
    finally:
        dispose_tool_env(env)


def test_production_catalog_contents_and_digests():
    from evaluations.swebench import tofu_kimi_runtime as contract

    catalog = contract.tofu_kimi_production_tool_catalog()
    names = {tool["function"]["name"] for tool in catalog}
    # Production registry names, verbatim.
    for expected in (
        "read_files", "inspect_image", "grep_search", "find_files",
        "repo_map", "write_file", "edit_file", "fetch_url", "todo_write",
        "custom__run_command", "custom__submit_result",
    ):
        assert expected in names
    # Host-executing shell tools are withheld — they would run on the
    # trusted Harbor host, outside the guest trust boundary.
    assert "run_command" not in names
    assert "check_background_command" not in names
    assert "kill_background_command" not in names
    # The guest shell tool mirrors the production run_command parameter shape.
    custom_run = next(
        tool for tool in catalog
        if tool["function"]["name"] == "custom__run_command")
    properties = custom_run["function"]["parameters"]["properties"]
    assert {
        "command", "description", "timeout_s", "working_dir",
    } <= set(properties)
    with_search = contract.tofu_kimi_production_tool_catalog(
        search_enabled=True)
    assert "web_search" in {
        tool["function"]["name"] for tool in with_search
    }
    # Digests are stable and distinct from the exclusive arm's.
    assert contract.tofu_kimi_production_tool_schema_sha256() == \
        hashlib.sha256(json.dumps(
            catalog, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
    assert contract.tofu_kimi_production_tool_schema_sha256() != \
        contract.tofu_kimi_tool_schema_sha256()
    assert contract.tofu_kimi_production_tool_execution_sha256() != \
        contract.tofu_kimi_tool_execution_sha256()
    contract_sha = contract.tofu_kimi_prompt_contract_sha256(
        {"responses": {"promptProfile": "auto"}},
        tool_surface="production",
        tool_catalog_sha256=contract.tofu_kimi_production_tool_schema_sha256(),
    )
    assert contract_sha != contract.tofu_kimi_prompt_contract_sha256(
        {"responses": {"promptProfile": "auto"}})
    # Manifest round-trips for both surfaces.
    settings = contract.TofuKimiCandidateSettings(
        provider_face="face", provider_slot_id="slot",
        agent_version="v1", experiment_arm="arm",
        runtime_config={"responses": {"promptProfile": "auto"}},
        tool_surface="production")
    record = settings.manifest_record()
    assert record["toolSurface"] == "production"
    parsed = contract.TofuKimiCandidateSettings.from_manifest_record(record)
    assert parsed.tool_surface == "production"
    exclusive = contract.TofuKimiCandidateSettings(
        provider_face="face", provider_slot_id="slot",
        agent_version="v1", experiment_arm="arm",
        runtime_config={"responses": {"promptProfile": "auto"}})
    assert contract.TofuKimiCandidateSettings.from_manifest_record(
        exclusive.manifest_record()).tool_surface == "exclusive"
    assert "tool_surface" in settings.agent_kwargs()
    assert settings.agent_kwargs()["tool_surface"] == "production"


def test_guest_cwd_mapping():
    from rootless_vm.harbor_tofu_runtime_agent import _guest_cwd

    rootfs = "/tmp/rootfs-instance"
    assert _guest_cwd(None, guest_rootfs=rootfs) is None
    assert _guest_cwd("", guest_rootfs=rootfs) is None
    assert _guest_cwd(rootfs, guest_rootfs=rootfs) == "/"
    assert _guest_cwd(f"{rootfs}/app/src", guest_rootfs=rootfs) == "/app/src"
    assert _guest_cwd("/app", guest_rootfs=rootfs) == "/app"
    with pytest.raises(ValueError):
        _guest_cwd("app/relative", guest_rootfs=rootfs)
    with pytest.raises(ValueError):
        _guest_cwd("/app/../etc", guest_rootfs=rootfs)


def test_guest_environment_replica_mirrors_production_format(monkeypatch):
    """The replica must drift-track production section_environment output."""
    _install_fake_harbor(monkeypatch)
    monkeypatch.delitem(
        sys.modules, "rootless_vm.harbor_tofu_runtime_agent", raising=False)
    module = importlib.import_module("rootless_vm.harbor_tofu_runtime_agent")
    monkeypatch.setenv("SHELL", "/bin/bash")
    from lib.tasks_pkg.system_prompt_base._environment import (
        section_environment,
    )

    production = section_environment(
        cwd="/tmp/rootfs-x/app", is_git=False, model="kimi-k3")
    guest_version = "Linux 6.1.0"
    replica = module._guest_environment_message(
        project_path="/tmp/rootfs-x/app", is_git=False,
        guest_os_version=guest_version, guest_workspace="/app")
    substituted = "\n".join(
        f" - OS Version: {guest_version}" if line.startswith(" - OS Version: ")
        else " - Shell: bash" if line.startswith(" - Shell: ") else line
        for line in production.split("\n")
        if not line.startswith(" - Installed CLI tools")
    )
    expected = substituted + (
        "\n - Sandbox shell: custom__run_command executes inside the "
        "isolated task guest whose workspace is /app — the same files as "
        "the primary working directory above, reached through the "
        "host-mounted guest rootfs. Use guest-absolute (/app/...) or "
        "relative paths in shell commands; host-absolute paths exist only "
        "for the file tools."
    )
    assert replica == expected
    assert module._guest_os_version("Linux 6.1.0-25-generic\n") == "Linux 6.1.0"
    assert module._guest_os_version("") == ""


@pytest.mark.parametrize("probe_ok", [True, False])
def test_production_surface_run_wires_catalog_and_guest_exec(
    tmp_path, monkeypatch, probe_ok,
):
    _install_fake_harbor(monkeypatch)
    _install_stub_harbor_environment(monkeypatch)
    module_name = "rootless_vm.harbor_tofu_runtime_agent"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    monkeypatch.setenv("KIMI_CHAT_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KIMI_API_KEY", "host-secret-never-artifact")
    captured: dict = {"exec_calls": []}
    guest_rootfs = str(tmp_path / "guest-rootfs")
    (tmp_path / "guest-rootfs" / "app").mkdir(parents=True)

    class Execution:
        task_id = "task-production"

        async def events_async(self, **_kwargs):
            yield {
                "type": "custom_tool_call",
                "seq": 1,
                "callId": "ctool_1",
                "toolName": "custom__run_command",
                "arguments": {
                    "command": "make test",
                    "working_dir": f"{guest_rootfs}/app/src",
                    "timeout_s": 120,
                },
            }
            yield {"type": "done", "seq": 2, "finishReason": "stop"}

        def resolve_custom_tool_call(self, call_id, content, *, is_error=False):
            captured["resolution"] = {
                "call_id": call_id, "content": content, "is_error": is_error,
            }
            return True

        async def result_async(self, **_kwargs):
            return SimpleNamespace(
                ok=True, content="fixture complete", status="done",
                finish_reason="stop",
                usage={"prompt_tokens": 11, "completion_tokens": 3},
            )

        def evidence_snapshot(self):
            return {
                "contractVersion": "tofu.agent-runtime-evidence/v1",
                "taskId": self.task_id, "model": "kimi-k3",
                "status": "done", "finishReason": "stop",
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                "apiRounds": [],
                "contextTelemetryRounds": [],
                "contextCompactionEvents": [],
                "toolSchemas": [],
                "customToolsMode": "augment",
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
        _guest_rootfs = guest_rootfs

        async def exec(self, **kwargs):
            captured["exec_calls"].append(kwargs)
            if kwargs.get("command") == "uname -sr":
                if not probe_ok:
                    return SimpleNamespace(
                        return_code=1, stdout="", stderr="boom")
                return SimpleNamespace(
                    return_code=0, stdout="Linux 6.1.0-25-generic\n",
                    stderr="")
            return SimpleNamespace(
                return_code=0, stdout="tests passed", stderr="")

    runtime_config = {"responses": {"promptProfile": "auto"}}
    agent = module.TofuKimiRuntimeAgent(
        logs_dir=tmp_path / "logs",
        model_name="kimi-k3",
        upstream_base_url_env="KIMI_CHAT_BASE_URL",
        upstream_api_key_env="KIMI_API_KEY",
        provider_face="meituan-chat",
        provider_slot_id="slot-a",
        experiment_arm="production_surface",
        runtime_config=runtime_config,
        runtime_config_sha256=module._canonical_sha256(runtime_config),
        tool_surface="production",
        timeout_sec=30,
        command_timeout_sec=17,
    )
    context = sys.modules["harbor.models.agent.context"].AgentContext()
    asyncio.run(agent.run("Solve the fixture.", Environment(), context))

    start_kwargs = captured["start_kwargs"]
    # Augment mode: the production registry surface stays intact; the custom
    # env only adds the client-executed guest tools.
    assert start_kwargs["custom_tools_mode"] == "augment"
    assert {
        row["function"]["name"] for row in start_kwargs["custom_tools"]
    } == {"custom__run_command", "custom__submit_result"}
    config = start_kwargs["config"]
    catalog_names = {
        tool["function"]["name"]
        for tool in config["_explicitToolSchemas"]
    }
    assert {
        "read_files", "write_file", "edit_file", "grep_search",
        "find_files", "repo_map", "fetch_url",
        "custom__run_command", "custom__submit_result",
    } <= catalog_names
    assert "run_command" not in catalog_names
    # The project root binds the host-visible guest rootfs workspace.
    assert config["projectPath"] == f"{guest_rootfs}/app"
    # No adapter-made system prompt: the production composer runs the show.
    assert [message["role"] for message in captured["messages"]][0] == "user"
    assert captured["messages"][0]["content"] == "Solve the fixture."
    # The guest probe ran through the environment exec boundary.
    assert captured["exec_calls"][0] == {
        "command": "uname -sr", "timeout_sec": 30,
    }
    metadata = context.metadata["tofuKimiEvidence"]
    assert metadata["toolSurface"] == "production"
    assert metadata["harborPromptSha256"] is None
    surface = metadata["productionSurface"]
    assert surface["projectPath"] == f"{guest_rootfs}/app"
    assert surface["guestWorkspace"] == "/app"
    if probe_ok:
        # Environment override: guest-measured OS version replaces the host
        # value, and the built-in block is disabled via the declared channel.
        assert surface["guestOsVersion"] == "Linux 6.1.0"
        assert surface["environmentBlock"] == "guest-override"
        assert "environment" in config["systemPromptBlocks"]["disabled"]
        assert len(captured["messages"]) == 2
        replica = captured["messages"][1]["content"]
        assert " - OS Version: Linux 6.1.0" in replica
        assert f" - Primary working directory: {guest_rootfs}/app" in replica
        assert " - Sandbox shell: custom__run_command" in replica
    else:
        assert surface["guestOsVersion"] is None
        assert surface["environmentBlock"] == "host-fallback"
        assert "systemPromptBlocks" not in config
        assert len(captured["messages"]) == 1
    # Guest exec routing: host-prefixed working_dir mapped to the container
    # namespace; production timeout_s accepted.
    assert captured["exec_calls"][1] == {
        "command": "make test", "cwd": "/app/src", "timeout_sec": 120,
    }
    assert "exit_code=0" in captured["resolution"]["content"]
    # Evidence digests are the production-surface ones.
    from evaluations.swebench import tofu_kimi_runtime as contract
    assert metadata["toolSchemaSha256"] == \
        contract.tofu_kimi_production_tool_schema_sha256()
    assert metadata["promptContractSha256"] == \
        contract.tofu_kimi_prompt_contract_sha256(
            runtime_config, tool_surface="production",
            tool_catalog_sha256=contract
            .tofu_kimi_production_tool_schema_sha256())
    trajectory = json.loads(
        (tmp_path / "logs" / "trajectory.json").read_text())
    assert trajectory["agent"]["extra"]["tool_surface"] == "production"
    assert trajectory["agent"]["extra"]["tool_schema_sha256"] == \
        metadata["toolSchemaSha256"]
    step_one = trajectory["steps"][0]
    assert step_one["source"] == "system"
    # ATIF step 1 carries the production composition, not the harbor prompt.
    assert step_one["message"] != module._SYSTEM_PROMPT
    if surface["systemPromptSha256"]:
        assert module.hashlib.sha256(
            step_one["message"].encode("utf-8")).hexdigest() == \
            surface["systemPromptSha256"]


def test_agent_init_rejects_unknown_tool_surface(tmp_path, monkeypatch):
    _install_fake_harbor(monkeypatch)
    module_name = "rootless_vm.harbor_tofu_runtime_agent"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    runtime_config = {"responses": {"promptProfile": "lean"}}
    with pytest.raises(ValueError, match="tool_surface"):
        module.TofuKimiRuntimeAgent(
            logs_dir=tmp_path / "logs",
            model_name="kimi-k3",
            upstream_base_url_env="KIMI_CHAT_BASE_URL",
            upstream_api_key_env="KIMI_API_KEY",
            provider_face="meituan-chat",
            provider_slot_id="slot-a",
            experiment_arm="arm",
            runtime_config=runtime_config,
            runtime_config_sha256=module._canonical_sha256(runtime_config),
            tool_surface="sideways",
        )
