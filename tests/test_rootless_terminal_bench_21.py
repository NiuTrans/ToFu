import json
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts import rootless_terminal_bench_21 as bench


MODEL = "deepseek-v4-flash-yourprovider"
pytestmark = pytest.mark.unit


def _trial_dir(tmp_path: Path) -> Path:
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    return trial


def test_classifier_excludes_verifier_dependency_download_failure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "Failed to download numpy after 30 retries: tunnel error",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__one",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert result["reward"] == 0.0


def test_classifier_does_not_excuse_reported_semantic_download_failure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_solution.py::test_download - failed to download required output",
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__reported",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "model_semantic"


def test_classifier_excludes_proven_legacy_git_proxy_failure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "Repository tests failed with return code 4\n"
        "ERROR: file or directory not found: /tmp/example/tests\n",
        encoding="utf-8",
    )
    padding = "x" * (300 * 1024)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "assistant": {"content": "git clone https://example.invalid/repo"},
                    "result": "CONNECT tunnel failed, response 407",
                    "usage": {"_dispatch": {"model": MODEL}},
                },
                {"result": padding},
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/build-cython-ext",
        "trial_name": "build-cython-ext__proxy",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert "Git/libcurl" in result["reason"]


def test_classifier_excludes_legacy_missing_localhost_mapping(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_background_commands\n"
        "NameResolutionError: HTTPConnection(host='localhost', port=8000): "
        "Failed to resolve 'localhost' ([Errno -3] Temporary failure in name resolution)",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/headless-terminal",
        "trial_name": "headless-terminal__hosts",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert "/etc/hosts" in result["reason"]


def test_classifier_excludes_proven_fixed_inner_timing_deadline(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_apply_macros_runs - subprocess.TimeoutExpired: "
        "Command vim timed out after 600 seconds\n",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": "vim exit=0 elapsed=503s\nBYTE-FOR-BYTE MATCH",
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/large-scale-text-editing",
        "trial_name": "large-scale-text-editing__tcg",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_timing_sensitive"
    assert "600 second" in result["reason"]


def test_classifier_excludes_legacy_posix_shell_verifier_mismatch(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "/tests/test.sh: 12: source: not found\n"
        "/tests/test.sh: 20: uvx: not found\n",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/llm-inference-batching-scheduler",
        "trial_name": "llm-inference-batching-scheduler__shell",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_shell_mismatch"
    assert "Bash" in result["reason"]


def test_classifier_keeps_numeric_post_timeout_reward_as_model_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_outputs.py - artifact does not exist", encoding="utf-8"
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__timeout",
        "exception_info": {
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 3600 seconds",
        },
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "model_timeout"
    assert result["reward"] == 0.0


def test_unscored_agent_timeout_does_not_hide_verifier_network_failure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "Failed to fetch package: Connection failed [IP: 10.0.2.100 3128]",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__timeout-then-network",
        "exception_info": {
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 3600 seconds",
        },
        "verifier_result": None,
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert result["reward"] is None


def test_agent_timeout_with_unscored_verifier_is_infrastructure_invalid(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": "VALIDATION PASSED: gradients match reference",
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__agent-and-verifier-timeout",
        "exception_info": {"exception_type": "AgentTimeoutError"},
        "verifier_result": None,
        "verifier": {
            "started_at": "2026-08-14T00:00:00Z",
            "finished_at": "2026-08-14T01:00:00Z",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_timeout"
    assert result["reward"] is None
    assert "unscored" in result["reason"]


def test_hf_timeout_is_invalid_when_required_route_and_verifier_network_both_fail(
    tmp_path,
):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "pip: ProxyError Connection reset by peer; no matching distribution",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": (
                        "https://huggingface.co/api/models/example DOWN: "
                        "Connection reset by peer"
                    ),
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/hf-model-inference",
        "trial_name": "hf-model-inference__network",
        "exception_info": {"exception_type": "AgentTimeoutError"},
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"


def test_transcript_persistence_redacts_ephemeral_proxy_credentials(tmp_path):
    try:
        from rootless_vm.harbor_tofu_agent import _persist_transcript
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    token = "a" * 48
    encoded = "c2VjcmV0c2VjcmV0"
    _persist_transcript(
        tmp_path,
        [
            {
                "command": (
                    f"AUTH={encoded}; git -c http.proxy=http://rootless:{token}"
                    "@10.0.2.100:3128 clone https://example.invalid/repo"
                ),
                "result": (
                    f"Proxy-Authorization: Basic {encoded}\n"
                    f"credential rootless:{token}"
                ),
            }
        ],
    )

    rendered = (tmp_path / "tofu-host-transcript.json").read_text(encoding="utf-8")
    assert token not in rendered
    assert encoded not in rendered
    assert "<redacted>" in rendered


def test_dispatch_gate_caps_concurrency_across_workers(tmp_path):
    try:
        from rootless_vm.harbor_tofu_agent import _dispatch_slot
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    gate = tmp_path / "gate"
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def worker():
        with _dispatch_slot(gate, 2):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: worker(), range(6)))

    assert state["peak"] == 2
    assert gate.stat().st_mode & 0o077 == 0


def test_long_trajectory_checkpoint_preserves_instruction_and_recent_evidence():
    try:
        from rootless_vm.harbor_tofu_agent import (
            _context_checkpoint_messages,
            _usage_prompt_tokens,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "harbor":
            pytest.skip("Harbor is installed only in the evaluation environment")
        raise

    messages = _context_checkpoint_messages(
        "build the MIPS interpreter",
        [{"result": "old"}, {"result": "new failing test"}],
    )

    assert messages[0]["role"] == "system"
    assert "build the MIPS interpreter" in messages[1]["content"]
    assert "new failing test" in messages[1]["content"]
    assert "files persist" in messages[1]["content"]
    assert _usage_prompt_tokens({"input_tokens": 0, "prompt_tokens": 349584}) == 349584
    assert _usage_prompt_tokens({"input_tokens": "300001"}) == 300001


def test_classifier_excludes_proven_legacy_loopback_proxy_failure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_outputs.py - Web server returned HTTP 403",
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {"usage": {"_dispatch": {"model": MODEL}}},
                {
                    "result": (
                        "curl --unix-socket /run/web.sock: hello world\n"
                        "LISTEN 0 511 0.0.0.0:8080"
                    )
                },
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/configure-git-webserver",
        "trial_name": "configure-git-webserver__proxy",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert "localhost" in result["reason"]


def test_classifier_counts_agent_damaged_package_state_as_model_failure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "E: dpkg was interrupted; curl: command not found; uvx: command not found",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__damaged",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "model_environment_damage"
    assert result["reward"] == 0.0


def test_classifier_excludes_legacy_scaled_command_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "E: dpkg was interrupted; curl: command not found; uvx: command not found",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "usage": {"_dispatch": {"model": MODEL}},
                    "assistant": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "run_command",
                                    "arguments": json.dumps(
                                        {
                                            "command": "apt-get install -y python3-biopython",
                                            "timeout_sec": 300,
                                        }
                                    ),
                                }
                            }
                        ]
                    },
                },
                {"tool": "run_command", "result": "exit_code=124"},
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/dna-insert",
        "trial_name": "dna-insert__legacy-timeout",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_timing_sensitive"
    assert "watchdog" in result["reason"]

    trial["agent_result"] = {
        "metadata": {"command_timeout_multiplier": 4.0}
    }
    result = bench._classify_trial(trial, trial_dir, MODEL)
    assert result["classification"] == "model_environment_damage"


def test_classifier_requires_audited_route_for_numeric_reward(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__unrouted",
        "verifier_result": {"rewards": {"reward": 1}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "routing_unverified"


def test_trial_and_agent_concurrency_are_independently_bounded():
    assert bench._resolve_concurrency(8, 4) == (8, 4)
    assert bench._resolve_concurrency(4, None) == (4, 4)
    with pytest.raises(ValueError, match="no greater"):
        bench._resolve_concurrency(4, 5)


def test_classifier_recognizes_legacy_inner_watchdog(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__two",
        "verifier": {
            "started_at": "2026-08-14T00:00:00Z",
            "finished_at": "2026-08-14T00:15:01Z",
        },
        "exception_info": {
            "exception_type": "RewardFileNotFoundError",
            "exception_message": "No reward file found",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_timeout"
    assert result["verifier_elapsed_sec"] == 901.0


def test_cancel_classifier_distinguishes_tcg_startup_from_cleanup_bug(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/cancel-async-tasks",
        "trial_name": "cancel-async-tasks__one",
        "verifier_result": {"rewards": {"reward": 0}},
    }
    stdout = trial_dir / "verifier" / "test-stdout.txt"
    stdout.write_text(
        '>       assert stdout.count("Task started.") == 2\n'
        "E       AssertionError: assert 0 == 2\n",
        encoding="utf-8",
    )

    result = bench._classify_trial(trial, trial_dir, MODEL)
    assert result["classification"] == "environment_timing_sensitive"

    stdout.write_text(
        '        assert stdout.count("Task started.") == 2\n'
        '>       assert stdout.count("Cleaned up.") == 2\n'
        "E       AssertionError: assert 0 == 2\n",
        encoding="utf-8",
    )
    result = bench._classify_trial(trial, trial_dir, MODEL)
    assert result["classification"] == "model_semantic"


def test_classifier_excludes_legacy_nonbinding_round_cap(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_outputs.py - FileNotFoundError: artifact missing",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__round-limit",
        "agent_result": {"metadata": {"exit_reason": "round_limit"}},
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "harness_round_limit"
    assert "round cap" in result["reason"]


def test_classifier_excludes_active_compcert_build_killed_by_tcg_wall_clock(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "assistant": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "arguments": json.dumps(
                                        {"command": "nohup make -j2 >/tmp/build.log &"}
                                    )
                                }
                            }
                        ],
                    },
                    "usage": {"_dispatch": {"model": MODEL}},
                },
                {
                    "result": (
                        "root 1 S make proof\nroot 2 S /bin/sh -c coqc ...\n"
                        "ps grep [c]oqc"
                    )
                },
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/compile-compcert",
        "trial_name": "compile-compcert__tcg",
        "exception_info": {"exception_type": "AgentTimeoutError"},
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_timing_sensitive"
    assert "low VM load" in result["reason"]

    trial["config"] = {"agent_timeout_multiplier": 8}
    assert bench._classify_trial(trial, trial_dir, MODEL)["classification"] == (
        "model_timeout"
    )


def test_classifier_excludes_required_stan_sampling_and_verifier_dual_timeout(
    tmp_path,
):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": (
                        "Chain 1: Iteration: 60000 / 100000 [60%] (Sampling)\n"
                        "Chain 3: Iteration: 70000 / 100000 [70%] (Sampling)"
                    ),
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/mcmc-sampling-stan",
        "trial_name": "mcmc-sampling-stan__tcg",
        "exception_info": {"exception_type": "AgentTimeoutError"},
        "agent_execution": {
            "started_at": "2026-08-14T00:00:00Z",
            "finished_at": "2026-08-14T02:00:00Z",
        },
        "verifier": {
            "started_at": "2026-08-14T02:00:00Z",
            "finished_at": "2026-08-14T04:00:00Z",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_timing_sensitive"
    assert "100,000" in result["reason"]

    trial["config"] = {
        "agent_timeout_multiplier": 8,
        "verifier_timeout_multiplier": 8,
    }
    trial["verifier_result"] = {"rewards": {"reward": 0}}
    assert bench._classify_trial(trial, trial_dir, MODEL)["classification"] == (
        "model_timeout"
    )


def test_classifier_excludes_active_inner_qemu_under_outer_tcg_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": (
                        "qemu-system-x86_64 -kernel /tmp/boot/vmlinuz-lts\n"
                        "--- port 6665 ---\n6665 open"
                    ),
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/qemu-startup",
        "trial_name": "qemu-startup__nested",
        "exception_info": {"exception_type": "AgentTimeoutError"},
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_nested_emulation"
    assert "TCG-on-TCG" in result["reason"]

    trial["config"] = {
        "agent_timeout_multiplier": 16,
        "verifier_timeout_multiplier": 16,
    }
    calibrated = bench._classify_trial(trial, trial_dir, MODEL)
    assert calibrated["classification"] == "model_timeout"
    assert calibrated["agent_timeout_multiplier"] == 16.0


def test_classifier_excludes_progressing_alpine_boot_under_outer_tcg_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": (
                        "qemu-system-x86_64 -kernel /tmp/iso/boot/vmlinuz-lts\n"
                        "Alpine Init 3.9.0-r0\nOpenRC 0.52.1 is starting up"
                    ),
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/qemu-alpine-ssh",
        "trial_name": "qemu-alpine-ssh__nested",
        "exception_info": {"exception_type": "AgentTimeoutError"},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_nested_emulation"
    assert "guest boot" in result["reason"]

    trial["config"] = {"agent_timeout_multiplier": 16}
    trial["verifier_result"] = {"rewards": {"reward": 0}}
    assert bench._classify_trial(trial, trial_dir, MODEL)["classification"] == (
        "model_timeout"
    )


def test_classifier_excludes_verified_windows_guest_under_outer_tcg_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": (
                        "qemu running; monitor socket exists; ports 5901 and 6080 open; "
                        "GUI screen rendered; ALL CHECKS PASSED"
                    ),
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/install-windows-3.11",
        "trial_name": "install-windows-3.11__nested",
        "exception_info": {"exception_type": "VerifierTimeoutError"},
        "verifier": {
            "started_at": "2026-08-14T00:00:00Z",
            "finished_at": "2026-08-14T04:00:00Z",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_nested_emulation"
    assert "Windows guest" in result["reason"]

    trial["config"] = {"verifier_timeout_multiplier": 16}
    assert bench._classify_trial(trial, trial_dir, MODEL)["classification"] == (
        "infrastructure_timeout"
    )


def test_classifier_excludes_legacy_prompt_context_overflow(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "exception.txt").write_text(
        "lib.llm_errors.PromptTooLongError: maximum context length",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/make-mips-interpreter",
        "trial_name": "make-mips-interpreter__context",
        "exception_info": {"exception_type": "PromptTooLongError"},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "harness_context_limit"


def test_classifier_excludes_model_gateway_read_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/regex-chess",
        "trial_name": "regex-chess__api-timeout",
        "exception_info": {
            "exception_type": "ReadTimeout",
            "exception_message": "your-llm-gateway.example.com read timed out",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_api"


def test_route_audit_rejects_raw_reasoning_and_accepts_redaction(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    transcript = [
        {
            "usage": {"_dispatch": {"model": MODEL}},
            "assistant": {
                "reasoning_content": {
                    "redacted": True,
                    "characters": 12,
                    "sha256": "0" * 64,
                }
            },
        }
    ]
    path = trial_dir / "agent" / "tofu-host-transcript.json"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    assert bench._route_audit(trial_dir) == ([MODEL], 0)

    transcript[0]["assistant"]["reasoning_content"] = "secret chain of thought"
    path.write_text(json.dumps(transcript), encoding="utf-8")
    assert bench._route_audit(trial_dir) == ([MODEL], 1)


def test_transcript_audit_sums_provider_backpressure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "usage": {
                        "_dispatch": {
                            "model": MODEL,
                            "429_retries": 3,
                            "gate_wait_ms": 125,
                        }
                    }
                },
                {
                    "usage": {
                        "_dispatch": {
                            "model": MODEL,
                            "429_retries": 1,
                            "gate_wait_ms": 75,
                        }
                    }
                },
            ]
        ),
        encoding="utf-8",
    )

    models, raw_reasoning, metrics = bench._transcript_audit(trial_dir)

    assert models == [MODEL]
    assert raw_reasoning == 0
    assert metrics == {
        "dispatches": 2,
        "429_retries": 4,
        "max_429_retries": 3,
        "gate_wait_ms": 200,
    }


def test_score_rejects_surplus_valid_attempts(tmp_path, capsys):
    job = tmp_path / "job"
    for index in range(2):
        trial_dir = job / f"trial-{index}"
        (trial_dir / "agent").mkdir(parents=True)
        (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
            json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
            encoding="utf-8",
        )
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "terminal-bench/example",
                    "trial_name": f"example__{index}",
                    "verifier_result": {"rewards": {"reward": 1}},
                }
            ),
            encoding="utf-8",
        )
    args = Namespace(
        jobs=[str(job)],
        expected_model=MODEL,
        expected_tasks=1,
        expected_attempts=1,
        tasks_root=None,
    )

    assert bench.score(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage_complete"] is False
    assert payload["score_percent"] is None
    assert payload["valid_trials"] == 2
    assert payload["surplus_valid_trials"] == {"terminal-bench/example": 1}


def test_observations_reject_duplicate_job_directories(tmp_path):
    args = Namespace(jobs=[str(tmp_path), str(tmp_path)], expected_model=MODEL)
    with pytest.raises(ValueError, match="duplicate job"):
        bench._observations(args)


def test_score_validates_full_task_identity_from_pinned_checkout(
    tmp_path, monkeypatch, capsys
):
    job = tmp_path / "job"
    trial_dir = job / "trial"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "task_name": "terminal-bench/example",
                "trial_name": "example__one",
                "verifier_result": {"rewards": {"reward": 1}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bench,
        "_load_tasks",
        lambda _root: [bench.Task("example", tmp_path, "image", 1, 1, 1, 1)],
    )
    args = Namespace(
        jobs=[str(job)],
        expected_model=MODEL,
        expected_tasks=1,
        expected_attempts=1,
        tasks_root=str(tmp_path),
    )

    assert bench.score(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage_complete"] is True
    assert payload["score_percent"] == 100.0
    assert payload["unexpected_tasks"] == []


def test_retry_plan_groups_exact_missing_attempts(monkeypatch, tmp_path, capsys):
    tasks = [
        bench.Task(name, tmp_path / name, "image", 1, 1, 1, 1)
        for name in ("alpha", "beta")
    ]
    monkeypatch.setattr(bench, "_load_tasks", lambda _root: tasks)
    monkeypatch.setattr(
        bench,
        "_observations",
        lambda _args: [
            {
                "task": "terminal-bench/alpha",
                "classification": "passed",
            },
            {
                "task": "terminal-bench/beta",
                "classification": "infrastructure_network",
            },
        ],
    )
    args = Namespace(
        jobs=[str(tmp_path)],
        tasks_root=str(tmp_path),
        expected_model=MODEL,
        expected_attempts=2,
    )

    assert bench.plan_retries(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["missing_valid_trials"] == 3
    assert payload["retry_groups"] == [
        {"attempts": 1, "tasks": ["terminal-bench/alpha"]},
        {"attempts": 2, "tasks": ["terminal-bench/beta"]},
    ]
    assert payload["invalid_trials"] == {"infrastructure_network": 1}
    assert payload["retry_profiles"] == [
        {
            "profile": "standard",
            "attempts": 1,
            "tasks": ["terminal-bench/alpha"],
            "agent_timeout_multiplier": 4,
            "verifier_timeout_multiplier": 4,
            "max_concurrent_trials": 16,
            "agent_concurrency": 4,
        },
        {
            "profile": "standard",
            "attempts": 2,
            "tasks": ["terminal-bench/beta"],
            "agent_timeout_multiplier": 4,
            "verifier_timeout_multiplier": 4,
            "max_concurrent_trials": 16,
            "agent_concurrency": 4,
        },
    ]


def test_retry_plan_routes_tcg_failures_to_bounded_profiles(
    monkeypatch, tmp_path, capsys
):
    tasks = [
        bench.Task(name, tmp_path / name, "image", 1, 1, 1, 1)
        for name in ("dual", "nested", "slow", "verify")
    ]
    monkeypatch.setattr(bench, "_load_tasks", lambda _root: tasks)
    monkeypatch.setattr(
        bench,
        "_observations",
        lambda _args: [
            {
                "task": "terminal-bench/dual",
                "classification": "infrastructure_timeout",
                "exception_type": "AgentTimeoutError",
            },
            {
                "task": "terminal-bench/nested",
                "classification": "environment_nested_emulation",
            },
            {
                "task": "terminal-bench/slow",
                "classification": "environment_timing_sensitive",
            },
            {
                "task": "terminal-bench/verify",
                "classification": "infrastructure_timeout",
            },
        ],
    )
    args = Namespace(
        jobs=[str(tmp_path)],
        tasks_root=str(tmp_path),
        expected_model=MODEL,
        expected_attempts=1,
    )

    assert bench.plan_retries(args) == 0
    profiles = {
        row["profile"]: row for row in json.loads(capsys.readouterr().out)["retry_profiles"]
    }
    assert profiles["nested_emulation"]["agent_timeout_multiplier"] == 16
    assert profiles["nested_emulation"]["max_concurrent_trials"] == 1
    assert profiles["tcg_low_load"]["agent_timeout_multiplier"] == 8
    assert profiles["tcg_low_load"]["max_concurrent_trials"] == 2
    assert profiles["tcg_low_load"]["tasks"] == [
        "terminal-bench/dual",
        "terminal-bench/slow",
    ]
    assert profiles["verifier_heavy"]["agent_timeout_multiplier"] == 4
    assert profiles["verifier_heavy"]["verifier_timeout_multiplier"] == 8


def test_analyze_does_not_call_an_unverified_route_pure(monkeypatch, capsys):
    monkeypatch.setattr(
        bench,
        "_observations",
        lambda _args: [
            {
                "classification": "routing_unverified",
                "served_models": [],
                "raw_reasoning_records": 0,
            }
        ],
    )
    args = Namespace(jobs=[], expected_model=MODEL)

    assert bench.analyze(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["audited_route_trials"] == 0
    assert payload["route_pure"] is False


def test_run_config_revalidates_unique_tasks_from_one_checkout(tmp_path, monkeypatch):
    tasks_root = tmp_path / "tasks"
    first = tasks_root / "first"
    second = tasks_root / "second"
    first.mkdir(parents=True)
    second.mkdir()
    available = [
        bench.Task("first", first, "one", 1, 1, 1, 1),
        bench.Task("second", second, "two", 1, 1, 1, 1),
    ]
    monkeypatch.setattr(bench, "_load_tasks", lambda root: available)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"tasks": [{"path": str(first)}, {"path": str(second)}]}),
        encoding="utf-8",
    )

    assert bench._validate_config_tasks(config) is None

    config.write_text(
        json.dumps({"tasks": [{"path": str(first)}, {"path": str(first)}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        bench._validate_config_tasks(config)
