"""Harness selection, ATIF collection, and attribution contracts."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest

from rootless_vm.harness_profiles import harness_profile, harness_profile_ids
from rootless_vm.harbor_runner import HarborRunSpec, harbor_argv
from rootless_vm.trajectory import (
    host_audit_to_atif,
    sanitize_collected_trajectory,
    validate_atif,
)
from scripts import rootless_terminal_bench_21 as bench


pytestmark = pytest.mark.unit
MODEL = "deepseek-v4-flash-meituan"


def _config_args(
    tmp_path: Path, harness: str, *, allow_guest: bool = False
) -> Namespace:
    roots = {}
    for name in ("tasks", "assets", "control", "state", "cache", "jobs"):
        roots[name] = tmp_path / name
        roots[name].mkdir(mode=0o700, exist_ok=True)
    (roots["assets"] / "index.json").write_text("{}", encoding="utf-8")
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"base")
    return Namespace(
        tasks_root=str(roots["tasks"]),
        assets_root=str(roots["assets"]),
        control_root=str(roots["control"]),
        state_root=str(roots["state"]),
        cache_root=str(roots["cache"]),
        jobs_dir=str(roots["jobs"]),
        base_disk=str(base),
        qemu="/bin/true",
        qemu_img="/bin/true",
        job_name="test-job",
        attempts=1,
        concurrency=2,
        agent_concurrency=1,
        max_retries=0,
        egress_max_gib=1,
        egress_global_concurrency=2,
        model=MODEL,
        harness=harness,
        allow_guest_credentials=allow_guest,
        reasoning_effort="max",
        temperature=1.0,
        top_p=0.95,
        max_rounds=None,
        max_output_tokens=None,
        context_checkpoint_tokens=None,
        dispatch_max_retries=8,
        global_dispatch_concurrency=2,
        runtime_timeout_multiplier=4.0,
        verifier_timeout_multiplier=None,
        virtual_time_shift=None,
        task=None,
    )


def _patch_one_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task_path = tmp_path / "tasks" / "example"
    task_path.mkdir(parents=True)
    monkeypatch.setattr(
        bench,
        "_load_tasks",
        lambda _root: [bench.Task("example", task_path, "image", 1, 512, 1, 10)],
    )


def test_harness_registry_exposes_security_boundary_and_stable_choices():
    assert harness_profile_ids() == (
        "claude-code",
        "codex",
        "codex-kimi",
        "deepseek-minimal",
        "tofu",
        "tofu-kimi",
    )
    assert harness_profile("deepseek-minimal").credential_boundary == "host-only"
    assert harness_profile("deepseek-minimal").agent_version == "1.0.2"
    assert harness_profile("deepseek-minimal").default_max_output_tokens == 256_000
    assert harness_profile("deepseek-minimal").default_context_window_tokens == 393_216
    assert harness_profile("codex").requires_guest_credentials is True
    assert harness_profile("codex-kimi").requires_guest_credentials is False
    assert harness_profile("codex-kimi").agent_version == "0.149.1"
    assert harness_profile("codex-kimi").host_dispatch is False
    assert harness_profile("tofu-kimi").requires_guest_credentials is False
    assert harness_profile("tofu-kimi").agent_name == "tofu-kimi-runtime"
    assert harness_profile("tofu-kimi").host_dispatch is True
    assert harness_profile("claude-code").requires_guest_credentials is True


def test_single_trial_runner_applies_the_same_guest_credential_gate(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    base = tmp_path / "base.qcow2"
    payload = tmp_path / "payload.iso"
    base.write_bytes(b"base")
    payload.write_bytes(b"payload")
    spec = HarborRunSpec(
        harbor="/bin/true",
        task_path=task,
        base_disk=base,
        base_disk_sha256="a" * 64,
        image_iso=payload,
        image_iso_sha256="b" * 64,
        image_reference="example/image@sha256:" + "c" * 64,
        state_root=tmp_path / "state",
        prepared_cache_root=tmp_path / "cache",
        jobs_dir=tmp_path / "jobs",
        harness="codex",
    )

    with pytest.raises(PermissionError, match="short-lived"):
        harbor_argv(spec)


def test_single_trial_runner_routes_codex_kimi_to_formal_launcher(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    base = tmp_path / "base.qcow2"
    payload = tmp_path / "payload.iso"
    base.write_bytes(b"base")
    payload.write_bytes(b"payload")
    spec = HarborRunSpec(
        harbor="/bin/true",
        task_path=task,
        base_disk=base,
        base_disk_sha256="a" * 64,
        image_iso=payload,
        image_iso_sha256="b" * 64,
        image_reference="example/image@sha256:" + "c" * 64,
        state_root=tmp_path / "state",
        prepared_cache_root=tmp_path / "cache",
        jobs_dir=tmp_path / "jobs",
        model="kimi-k3",
        harness="codex-kimi",
    )

    with pytest.raises(ValueError, match="formal evaluations.swebench"):
        harbor_argv(spec)


def test_terminal_config_routes_codex_kimi_to_formal_launcher(monkeypatch, tmp_path):
    _patch_one_task(monkeypatch, tmp_path)
    args = _config_args(tmp_path, "codex-kimi")

    with pytest.raises(ValueError, match="evaluations.swebench run"):
        bench.write_config(args)


def test_deepseek_minimal_config_uses_profile_defaults(monkeypatch, tmp_path):
    _patch_one_task(monkeypatch, tmp_path)
    args = _config_args(tmp_path, "deepseek-minimal")

    assert bench.write_config(args) == 0

    payload = json.loads(
        (tmp_path / "control" / "test-job.json").read_text(encoding="utf-8")
    )
    agent = payload["agents"][0]
    assert agent["name"].endswith(":DeepSeekMinimalHostAgent")
    assert agent["kwargs"]["max_output_tokens"] == 256_000
    assert agent["kwargs"]["context_window_tokens"] == 393_216
    assert agent["kwargs"]["max_rounds"] == 4_096
    assert agent["kwargs"]["bash_timeout_sec"] == 1_200
    assert "context_checkpoint_tokens" not in agent["kwargs"]


def test_adaptive_config_records_private_resource_probe_evidence(
    monkeypatch, tmp_path
):
    _patch_one_task(monkeypatch, tmp_path)
    args = _config_args(tmp_path, "deepseek-minimal")
    args.concurrency = None
    args.agent_concurrency = None
    probe_values = {
        "effective_cpu_count": 1,
        "effective_memory_capacity_mb": 8 * 1024,
        "effective_memory_available_mb": 6 * 1024,
        "disk_total_mb": 500 * 1024,
        "disk_free_mb": 300 * 1024,
    }
    monkeypatch.setattr(
        bench,
        "probe_system_resources",
        lambda _environment: Namespace(
            **probe_values, as_dict=lambda: probe_values
        ),
    )

    assert bench.write_config(args) == 0

    config = json.loads(
        (tmp_path / "control" / "test-job.json").read_text(encoding="utf-8")
    )
    evidence_path = tmp_path / "control" / "test-job.resources.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert config["n_concurrent_trials"] == 1
    assert config["agents"][0]["n_concurrent"] == 1
    assert evidence["resolved_trial_concurrency"] == 1
    assert evidence["resolved_agent_concurrency"] == 1
    assert evidence_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_guest_credential_harnesses_are_default_deny(monkeypatch, tmp_path, harness):
    _patch_one_task(monkeypatch, tmp_path)
    args = _config_args(tmp_path, harness)

    with pytest.raises(PermissionError, match="short-lived"):
        bench.write_config(args)


def test_explicit_codex_config_does_not_receive_tofu_dispatch_kwargs(
    monkeypatch, tmp_path
):
    _patch_one_task(monkeypatch, tmp_path)
    args = _config_args(tmp_path, "codex", allow_guest=True)

    assert bench.write_config(args) == 0

    payload = json.loads(
        (tmp_path / "control" / "test-job.json").read_text(encoding="utf-8")
    )
    agent = payload["agents"][0]
    assert agent["name"] == "codex"
    assert agent["kwargs"] == {"reasoning_effort": "max"}


def _host_trajectory() -> dict:
    return host_audit_to_atif(
        [
            {
                "round": 0,
                "assistant": {
                    "content": "",
                    "reasoning_content": {
                        "redacted": True,
                        "characters": 12,
                        "sha256": "a" * 64,
                    },
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command":"pwd"}',
                            },
                        }
                    ],
                },
                "usage": {
                    "input_tokens": 0,
                    "prompt_tokens": 10,
                    "output_tokens": 0,
                    "completion_tokens": 5,
                    "_dispatch": {"model": MODEL, "latency_ms": 100},
                    "_harness_request": {
                        "context_window_tokens": 393216,
                        "estimated_input_tokens": 20,
                        "max_output_tokens": 256000,
                    },
                },
            },
            {
                "round": 0,
                "tool_call_id": "call-1",
                "tool": "bash",
                "result": "/root",
            },
        ],
        instruction="solve it",
        system_prompt="system",
        agent_name="deepseek-minimal-host",
        agent_version="1.0.1",
        model_name=MODEL,
        tool_definitions=[],
        session_id="trial-1",
        harness_profile="deepseek-minimal",
    )


def test_host_audit_projects_to_valid_reasoning_redacted_atif():
    payload = _host_trajectory()

    validate_atif(payload)
    assert payload["schema_version"] == "ATIF-v1.7"
    assert payload["steps"][2]["tool_calls"][0]["function_name"] == "bash"
    assert payload["steps"][2]["observation"]["results"][0]["content"] == "/root"
    assert "reasoning_content" not in payload["steps"][2]
    assert (
        payload["steps"][2]["metrics"]["extra"]["harness_request"]["max_output_tokens"]
        == 256000
    )
    assert payload["final_metrics"]["total_prompt_tokens"] == 10


def test_collected_trajectory_removes_reasoning_and_credentials():
    payload = _host_trajectory()
    payload["steps"][2]["reasoning_content"] = "private reasoning"
    payload["steps"][2]["message"] = (
        "API_KEY=abcdefghijklmnop Authorization: Bearer bearer-secret-value "
        "sk-example0123456789 http://rootless:"
        + "b" * 48
        + "@10.0.2.100:3128"
    )

    sanitized = sanitize_collected_trajectory(payload)

    assert "reasoning_content" not in sanitized["steps"][2]
    assert "private reasoning" not in json.dumps(sanitized)
    assert "abcdefghijklmnop" not in json.dumps(sanitized)
    assert "bearer-secret-value" not in json.dumps(sanitized)
    assert "sk-example0123456789" not in json.dumps(sanitized)
    assert "b" * 48 not in json.dumps(sanitized)


def test_root_cause_attribution_separates_provider_from_model():
    provider = bench._root_cause_attribution(
        {"classification": "infrastructure_api", "reason": "429"}
    )
    semantic = bench._root_cause_attribution(
        {"classification": "model_semantic", "reward": 0, "reason": "failed"}
    )

    assert provider["layer"] == "provider"
    assert provider["retry_scope"] == "retry_infrastructure"
    assert semantic["layer"] == "model"
    assert semantic["retry_scope"] == "new_model_attempt"


def test_collect_trajectories_writes_sanitized_atif_and_attribution(tmp_path, capsys):
    job = tmp_path / "job"
    trial_dir = job / "trial-one"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True)
    trajectory = _host_trajectory()
    trajectory["steps"][2]["reasoning_content"] = "do not retain this reasoning"
    trajectory["steps"][2]["message"] = "TOKEN=abcdefghijklmnop"
    trajectory["steps"][2]["metrics"]["prompt_tokens"] = 0
    trajectory["steps"][2]["metrics"]["completion_tokens"] = 0
    trajectory["final_metrics"]["total_prompt_tokens"] = 0
    trajectory["final_metrics"]["total_completion_tokens"] = 0
    (agent_dir / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
    (agent_dir / "host-dispatch-transcript.json").write_text(
        json.dumps(
            [
                {
                    "round": 0,
                    "assistant": {"content": "", "tool_calls": []},
                    "usage": {
                        "input_tokens": 0,
                        "prompt_tokens": 10,
                        "output_tokens": 0,
                        "completion_tokens": 5,
                        "_dispatch": {"model": MODEL},
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "task_name": "terminal-bench/example",
                "trial_name": "example__one",
                "agent_info": {
                    "name": "deepseek-minimal-host",
                    "version": "1.0.1",
                },
                "config": {
                    "agent": {
                        "name": (
                            "rootless_vm.harbor_deepseek_minimal_agent:"
                            "DeepSeekMinimalHostAgent"
                        ),
                        "model_name": MODEL,
                        "kwargs": {
                            "reasoning_effort": "max",
                            "temperature": 1.0,
                            "top_p": 0.95,
                            "max_rounds": 4096,
                            "max_output_tokens": 256000,
                            "context_window_tokens": 393216,
                        },
                    },
                    "environment": {
                        "import_path": (
                            "rootless_vm.harbor_environment:RootlessQemuEnvironment"
                        ),
                        "kwargs": {},
                    },
                },
                "verifier_result": {"rewards": {"reward": 1}},
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "bundle"
    args = Namespace(
        jobs=[str(job)], output_root=str(output_root), expected_model=MODEL
    )

    assert bench.collect_trajectories(args) == 0

    summary = json.loads((output_root / "summary.json").read_text(encoding="utf-8"))
    assert summary["trials"] == 1
    assert summary["trajectory_status"] == {"native_atif": 1}
    assert summary["root_cause_layers"] == {"none": 1}
    manifest = json.loads((output_root / "manifest.jsonl").read_text(encoding="utf-8"))
    collected_path = output_root / manifest["trajectory_path"]
    collected = json.loads(collected_path.read_text(encoding="utf-8"))
    rendered = json.dumps(collected)
    assert "do not retain this reasoning" not in rendered
    assert "abcdefghijklmnop" not in rendered
    assert collected["steps"][2]["metrics"]["prompt_tokens"] == 10
    assert collected["steps"][2]["metrics"]["completion_tokens"] == 5
    assert collected["final_metrics"]["total_prompt_tokens"] == 10
    assert collected["final_metrics"]["total_completion_tokens"] == 5
    assert collected["agent"]["extra"]["usage_normalized_from_host_audit"] is True
    attribution = json.loads(
        (output_root / manifest["attribution_path"]).read_text(encoding="utf-8")
    )
    assert attribution["attribution"]["layer"] == "none"
    for directory in (output_root, output_root / "trials", collected_path.parent):
        assert directory.stat().st_mode & 0o077 == 0
    for private_file in output_root.rglob("*"):
        if private_file.is_file():
            assert private_file.stat().st_mode & 0o077 == 0
    assert str(output_root / "summary.json") in capsys.readouterr().out


def test_fallback_trajectory_preserves_recorded_historical_agent_version(tmp_path):
    trial_dir = tmp_path / "historical-trial"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "host-dispatch-transcript.json").write_text(
        "[]", encoding="utf-8"
    )
    trial = {
        "task_name": "terminal-bench/example",
        "instruction": "solve it",
        "config": {
            "agent": {
                "name": (
                    "rootless_vm.harbor_deepseek_minimal_agent:"
                    "DeepSeekMinimalHostAgent"
                )
            }
        },
    }
    observation = {
        "harness_profile": "deepseek-minimal",
        "agent_name": "deepseek-minimal-host",
        "agent_version": "0.9.9",
        "configured_model": MODEL,
    }

    trajectory = bench._fallback_host_trajectory(trial_dir, trial, observation)

    assert trajectory is not None
    assert trajectory["agent"]["name"] == "deepseek-minimal-host"
    assert trajectory["agent"]["version"] == "0.9.9"


def test_minimal_tool_surface_and_context_budget_match_official_profile(tmp_path):
    try:
        from rootless_vm.deepseek_minimal_tools import MINIMAL_TOOLS
        from rootless_vm.harbor_deepseek_minimal_agent import (
            SYSTEM_PROMPT,
            DeepSeekMinimalHostAgent,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    assert SYSTEM_PROMPT == "You are a helpful software engineer assistant."
    assert [tool["function"]["name"] for tool in MINIMAL_TOOLS] == [
        "bash",
        "str_replace_editor",
    ]
    assert MINIMAL_TOOLS[0]["function"]["parameters"]["required"] == ["command"]
    agent = DeepSeekMinimalHostAgent(logs_dir=tmp_path, model_name=MODEL)
    assert agent.version() == "1.0.2"
    budget, estimated = agent._request_output_budget(
        [{"role": "user", "content": "small prompt"}]
    )
    assert estimated > 0
    assert budget == 256_000


def test_minimal_timeout_reset_terminates_complete_guest_process_groups():
    try:
        from rootless_vm.deepseek_minimal_tools import PersistentBash
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    class RecordingEnvironment:
        def __init__(self):
            self.commands = []

        async def exec(self, command, *, timeout_sec):
            self.commands.append((command, timeout_sec))
            return Namespace(return_code=0, stdout="", stderr="")

    environment = RecordingEnvironment()
    bash = PersistentBash(environment)
    bash._started = True

    asyncio.run(bash._reset())

    command, timeout_sec = environment.commands[0]
    assert 'kill -TERM -- "-$pid"' in command
    assert 'kill -KILL -- "-$pid"' in command
    assert timeout_sec == 10


def test_minimal_context_budget_covers_observed_tokenizer_underestimate(
    tmp_path, monkeypatch
):
    try:
        from lib.token_counter import heuristic
        from rootless_vm.harbor_deepseek_minimal_agent import (
            DeepSeekMinimalHostAgent,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    monkeypatch.setattr(heuristic, "cheap_estimate", lambda *args, **kwargs: 100_000)
    agent = DeepSeekMinimalHostAgent(logs_dir=tmp_path, model_name=MODEL)

    budget, estimated = agent._request_output_budget(
        [{"role": "user", "content": "large prompt"}],
        observed_input_ratio=1.36,
    )

    assert estimated == 100_000
    assert budget == 241_168
    assert budget + 150_000 + 2_048 == 393_216
