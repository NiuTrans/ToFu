"""Host-secret/guest-execution contract for the pinned Codex Kimi adapter."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


def _install_fake_harbor(monkeypatch) -> None:
    modules = {
        name: ModuleType(name)
        for name in (
            "harbor",
            "harbor.agents",
            "harbor.agents.base",
            "harbor.environments",
            "harbor.environments.base",
            "harbor.models",
            "harbor.models.agent",
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


def test_codex_guest_agent_keeps_credentials_host_side_and_persists_exact_evidence(
    tmp_path, monkeypatch
):
    _install_fake_harbor(monkeypatch)
    module_name = "rootless_vm.harbor_codex_kimi_agent"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)

    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture binary")
    binary.chmod(0o755)
    metrics_dir = tmp_path / "trial-metrics"
    metrics_dir.mkdir(mode=0o700)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(mode=0o700)
    binary_digest = "a" * 64
    monkeypatch.setattr(
        module,
        "verify_codex_binary",
        lambda path, *, expected_sha256: {
            "path": str(Path(path).resolve()),
            "version": "0.149.1",
            "sha256": expected_sha256,
        },
    )

    class Environment:
        def __init__(self):
            self.uploads: list[tuple[str, str]] = []
            self.commands: list[str] = []
            self.raw = ""
            self.codex_return_code = 0

        def loopback_service_url(self, name: str) -> str:
            assert name == "benchmark-proxy"
            return "http://10.0.2.101:8765"

        async def upload_file(self, source, target):
            self.uploads.append((str(source), str(target)))

        async def exec(self, command, **_kwargs):
            self.commands.append(command)
            if "codex exec" in command:
                token_match = re.search(r"[0-9a-f]{64}", command)
                assert token_match is not None
                token = token_match.group(0)
                self.raw = "".join((
                    json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n",
                    json.dumps({"type": "turn.started"}) + "\n",
                    json.dumps({"type": "item.completed", "item": {
                        "id": "m", "type": "agent_message", "text": "done",
                    }}) + "\n",
                    json.dumps({"type": "turn.completed", "usage": {
                        "input_tokens": 5, "cached_input_tokens": 2,
                        "cache_write_input_tokens": 0, "output_tokens": 2,
                        "reasoning_output_tokens": 1,
                    }}) + "\n",
                ))
                metrics = {
                    "event": "responsesTranslation",
                    "trialToken": token,
                    "status": "completed",
                    "clientDisconnected": False,
                    "upstreamCalls": 1,
                    "invalidTrial": False,
                    "translationCpuNs": 10,
                    "proxyCpuNs": 20,
                    "upstreamWallNs": 100,
                    "rawWallNs": 120,
                    "requestBytes": 200,
                    "requestDigest": "request",
                    "toolSchemaBytes": 50,
                    "toolSchemaDigest": "b" * 64,
                    "toolCount": 2,
                    "startedAtUnixNs": time.time_ns(),
                    "firstUpstreamByteAtUnixNs": time.time_ns(),
                    "usage": {
                        "input_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 2},
                        "output_tokens": 2,
                        "output_tokens_details": {"reasoning_tokens": 1},
                    },
                }
                (metrics_dir / f"{token}.jsonl").write_text(
                    json.dumps(metrics) + "\n", encoding="utf-8"
                )
            return SimpleNamespace(
                return_code=(
                    self.codex_return_code if "codex exec" in command else 0
                ),
                stdout="",
                stderr="",
            )

        async def download_file(self, source, target):
            target = Path(target)
            if source.endswith("codex-events.jsonl"):
                target.write_text(self.raw, encoding="utf-8")
            else:
                target.write_text("", encoding="utf-8")

    environment = Environment()
    agent = module.CodexKimiGuestAgent(
        logs_dir=logs_dir,
        model_name="kimi-k3",
        codex_binary=str(binary),
        codex_sha256=binary_digest,
        proxy_trial_metrics_dir=str(metrics_dir),
        reasoning_effort="high",
        timeout_sec=30,
    )
    context = sys.modules[
        "harbor.models.agent.context"
    ].AgentContext()

    asyncio.run(agent.setup(environment))
    asyncio.run(agent.run("Solve the fixture.", environment, context))

    assert environment.uploads[0][1] == "/var/tmp/tofu-codex-kimi/codex"
    codex_command = next(command for command in environment.commands
                         if "codex exec" in command)
    assert "http://10.0.2.101:8765/v1" in codex_command
    assert "--ignore-user-config" in codex_command
    assert "--ephemeral" in codex_command
    assert "features.remote_compaction_v2=false" in codex_command
    assert "KIMI_API_KEY" not in codex_command
    assert context.n_input_tokens == 5
    assert context.n_output_tokens == 2
    evidence = context.metadata["codexKimiEvidence"]
    assert evidence["responsesRequests"] == 1
    assert evidence["codexBinarySha256"] == binary_digest
    assert evidence["projectionError"] == ""
    assert "taskStartedAtUnixNs" not in evidence
    assert evidence["agentSetupStartedAtUnixNs"] > 0
    assert "outer task-start" in evidence["latencyScope"]
    assert (logs_dir / "codex-kimi-evidence/codex-events.jsonl").is_file()
    assert (logs_dir / "codex-kimi-evidence/proxy-metrics.jsonl").is_file()
    trajectory = json.loads((logs_dir / "trajectory.json").read_text())
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert trajectory["agent"]["version"] == "0.149.1"

    environment.codex_return_code = 7
    with pytest.raises(RuntimeError, match="exited nonzero"):
        asyncio.run(agent.run("Solve another fixture.", environment, context))
