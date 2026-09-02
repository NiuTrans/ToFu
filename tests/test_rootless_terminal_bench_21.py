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


def test_run_series_parser_preserves_order():
    args = bench.build_parser().parse_args(
        [
            "run-series",
            "--harbor",
            "/bin/harbor",
            "--config",
            "one.json",
            "--config",
            "two.json",
        ]
    )

    assert args.config == ["one.json", "two.json"]


def test_registry_candidates_prefer_explicit_docker_hub_mirrors():
    assert bench._registry_candidates(
        "alexgshaw/example:20260403",
        ("docker.m.daocloud.io", "docker.1ms.run/"),
    ) == (
        "docker.m.daocloud.io/alexgshaw/example:20260403",
        "docker.1ms.run/alexgshaw/example:20260403",
        "alexgshaw/example:20260403",
    )


def test_registry_candidates_do_not_rewrite_non_docker_hub_registry():
    assert bench._registry_candidates(
        "ghcr.io/example/image:tag", ("docker.m.daocloud.io",)
    ) == ("ghcr.io/example/image:tag",)


def test_registry_candidates_reject_url_schemes():
    with pytest.raises(ValueError, match="invalid registry mirror"):
        bench._registry_candidates("example/image:tag", ("https://mirror.invalid",))


def test_write_retry_configs_caps_standard_profile_to_template(
    monkeypatch, tmp_path, capsys
):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(mode=0o700)
    output_root = tmp_path / "control"
    output_root.mkdir(mode=0o700)
    source_job = jobs_dir / "source"
    source_job.mkdir()
    task_path = tmp_path / "tasks" / "alpha"
    task_path.mkdir(parents=True)
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "job_name": "template",
                "jobs_dir": str(jobs_dir),
                "n_attempts": 1,
                "n_concurrent_trials": 16,
                "agent_timeout_multiplier": 4,
                "verifier_timeout_multiplier": 4,
                "retry": {"max_retries": 2},
                "agents": [
                    {
                        "model_name": MODEL,
                        "n_concurrent": 4,
                        "kwargs": {},
                    }
                ],
                "environment": {"kwargs": {}},
                "tasks": [{"path": str(task_path)}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bench,
        "_load_tasks",
        lambda _root: [bench.Task("alpha", task_path, "image", 1, 1, 10, 120)],
    )
    monkeypatch.setattr(
        bench,
        "_build_retry_plan",
        lambda _args: {
            "expected_attempts": 20,
            "valid_trials": 0,
            "missing_valid_trials": 20,
            "retry_profiles": [
                {
                    "profile": "standard",
                    "attempts": 20,
                    "tasks": ["terminal-bench/alpha"],
                    "agent_timeout_multiplier": 4,
                    "verifier_timeout_multiplier": 4,
                    "max_concurrent_trials": 32,
                    "agent_concurrency": 16,
                }
            ],
            "surplus_valid_trials": {},
            "unexpected_tasks": [],
            "invalid_trials": {},
            "provenance_violations": {},
            "compatible_legacy_agent_trials": 7,
        },
    )
    args = Namespace(
        jobs=[str(source_job)],
        tasks_root=str(task_path.parent),
        template=str(template),
        output_root=str(output_root),
        job_prefix="resume",
        expected_model=MODEL,
    )

    assert bench.write_retry_configs(args) == 0

    output = json.loads((output_root / "resume-01-standard-a20.json").read_text())
    assert output["n_attempts"] == 20
    assert output["n_concurrent_trials"] == 16
    assert output["agents"][0]["n_concurrent"] == 4
    assert output["agents"][0]["kwargs"]["command_timeout_multiplier"] == 4
    assert output["environment"]["kwargs"]["default_exec_timeout_sec"] == 900
    assert output["environment"]["kwargs"]["virtual_time_shift"] is None
    assert output["tasks"] == [{"path": str(task_path)}]
    manifest = json.loads((output_root / "resume-manifest.json").read_text())
    assert manifest["missing_valid_trials"] == 20
    assert manifest["compatible_legacy_agent_trials"] == 7
    assert manifest["job_names"] == ["resume-01-standard-a20"]
    assert str(output_root / "resume-manifest.json") in capsys.readouterr().out


@pytest.mark.parametrize(
    "task_name",
    [
        "install-windows-3.11",
        "qemu-alpine-ssh",
        "qemu-startup",
    ],
)
def test_retry_plan_keeps_timeout_cleanup_nested_tasks_at_16x(
    monkeypatch, tmp_path, task_name
):
    task_path = tmp_path / "tasks" / task_name
    task_path.mkdir(parents=True)
    monkeypatch.setattr(
        bench,
        "_load_tasks",
        lambda _root: [
            bench.Task(task_name, task_path, "image", 2, 2048, 900, 900)
        ],
    )
    monkeypatch.setattr(
        bench,
        "_observations",
        lambda _args: [
            {
                "task": f"terminal-bench/{task_name}",
                "classification": "harness_timeout_process_leak",
                "underlying_classification": "passed",
            }
        ],
    )
    monkeypatch.setattr(bench, "_load_frozen_task_checksums", lambda _tasks: {})
    args = Namespace(
        tasks_root=str(task_path.parent),
        expected_attempts=1,
        harness=None,
    )

    plan = bench._build_retry_plan(args)

    assert plan["retry_profiles"][0]["profile"] == "nested_emulation"
    assert plan["retry_profiles"][0]["agent_timeout_multiplier"] == 16


def test_write_retry_configs_refuses_surplus(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bench,
        "_build_retry_plan",
        lambda _args: {
            "surplus_valid_trials": {"terminal-bench/alpha": 1},
            "unexpected_tasks": [],
        },
    )
    args = Namespace(jobs=[], tasks_root=str(tmp_path), job_prefix="resume")

    with pytest.raises(ValueError, match="surplus"):
        bench.write_retry_configs(args)


def test_write_retry_configs_rejects_path_prefix_before_planning(monkeypatch):
    monkeypatch.setattr(
        bench,
        "_build_retry_plan",
        lambda _args: pytest.fail("unsafe prefix reached ledger planning"),
    )

    with pytest.raises(ValueError, match="job prefix"):
        bench.write_retry_configs(Namespace(job_prefix="../escape"))


def test_write_retry_configs_manifest_collision_leaves_no_partial_configs(
    monkeypatch, tmp_path
):
    output_root = tmp_path / "control"
    jobs_dir = tmp_path / "jobs"
    task_path = tmp_path / "tasks" / "alpha"
    output_root.mkdir(mode=0o700)
    jobs_dir.mkdir(mode=0o700)
    task_path.mkdir(parents=True)
    (output_root / "resume-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        bench,
        "_build_retry_plan",
        lambda _args: {
            "surplus_valid_trials": {},
            "unexpected_tasks": [],
            "retry_profiles": [
                {
                    "profile": "standard",
                    "attempts": 1,
                    "tasks": ["terminal-bench/alpha"],
                    "agent_timeout_multiplier": 4,
                    "verifier_timeout_multiplier": 4,
                    "max_concurrent_trials": 16,
                    "agent_concurrency": 8,
                }
            ],
        },
    )
    monkeypatch.setattr(
        bench,
        "_retry_template",
        lambda _args: {
            "jobs_dir": str(jobs_dir),
            "agents": [{"kwargs": {}}],
            "environment": {"kwargs": {}},
        },
    )
    monkeypatch.setattr(
        bench,
        "_load_tasks",
        lambda _root: [bench.Task("alpha", task_path, "image", 1, 1, 1, 1)],
    )
    args = Namespace(
        jobs=[],
        tasks_root=str(task_path.parent),
        output_root=str(output_root),
        job_prefix="resume",
    )

    with pytest.raises(ValueError, match="manifest"):
        bench.write_retry_configs(args)

    assert not (output_root / "resume-01-standard-a1.json").exists()


def test_run_until_complete_replans_after_each_wave(monkeypatch, tmp_path):
    jobs_dir = tmp_path / "jobs"
    output_root = tmp_path / "control"
    source_job = jobs_dir / "source"
    jobs_dir.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    source_job.mkdir()
    monkeypatch.setattr(
        bench,
        "_retry_template",
        lambda _args: {"jobs_dir": str(jobs_dir)},
    )

    def fake_plan(args):
        complete = len(args.jobs) == 2
        return {
            "complete": complete,
            "valid_trials": 2 if complete else 1,
            "missing_valid_trials": 0 if complete else 1,
            "surplus_valid_trials": {},
            "unexpected_tasks": [],
        }

    monkeypatch.setattr(bench, "_build_retry_plan", fake_plan)

    def fake_writer(args):
        name = f"{args.job_prefix}-01-standard-a1"
        config = output_root / f"{name}.json"
        config.write_text("{}", encoding="utf-8")
        (output_root / f"{args.job_prefix}-manifest.json").write_text(
            json.dumps({"configs": [str(config)], "job_names": [name]}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(bench, "write_retry_configs", fake_writer)

    def fake_run_series(args):
        assert len(args.config) == 1
        (jobs_dir / "autofill-w01-01-standard-a1").mkdir()
        return 0

    monkeypatch.setattr(bench, "run_series", fake_run_series)
    args = Namespace(
        jobs=[str(source_job)],
        harbor="/bin/harbor",
        template="template.json",
        output_root=str(output_root),
        job_prefix="autofill",
        max_waves=2,
    )

    assert bench.run_until_complete(args) == 0
    assert len(args.jobs) == 2


def _trial_dir(tmp_path: Path) -> Path:
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    return trial


def test_verifier_failure_signatures_ignore_zero_failure_summary():
    signatures = bench._verifier_failure_signatures(
        "PASSED (5/5): alpha, beta\n"
        "FAILED (0/5): None\n"
        "FAILED test_outputs.py::test_real_failure - AssertionError\n"
    )

    assert signatures == [
        "FAILED test_outputs.py::test_real_failure - AssertionError"
    ]


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


def test_classifier_excludes_apt_proxy_connection_failure_without_reward(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "Err:2 http://security.ubuntu.com/ubuntu noble-security InRelease\n"
        "  Connection failed [IP: 10.0.2.100 3128]\n",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/polyglot-rust-c",
        "trial_name": "polyglot-rust-c__apt-network",
        "exception_info": {
            "exception_type": "RewardFileNotFoundError",
            "exception_message": "No reward file found",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert result["reward"] is None


def test_classifier_excludes_reshard_c4_fixture_download_cache_fallback(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "Using the latest cached version of the dataset since allenai/c4 "
        "couldn't be found on the Hugging Face Hub\n"
        "ValueError: Couldn't find cache for allenai/c4 for config "
        "'default-c33333bd6ac7ada4'\n"
        "Available configs in the cache: ['default-b04fc8a0b8562884']\n",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/reshard-c4-data",
        "trial_name": "reshard-c4-data__fixture-network",
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert "external C4 fixture" in result["reason"]
    assert result["reward"] == 0.0


def test_classifier_excludes_verifier_stalled_mid_dependency_download(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "uv installed successfully\nDownloading pygments (1.2MiB)\n",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/code-from-image",
        "trial_name": "code-from-image__network-stall",
        "exception_info": {
            "exception_type": "RewardFileNotFoundError",
            "exception_message": "No reward file found",
        },
        "verifier": {
            "started_at": "2026-08-15T06:24:14Z",
            "finished_at": "2026-08-15T07:43:46Z",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert result["reward"] is None


def test_classifier_distinguishes_started_pytest_from_mid_download(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "Downloading cpython (32.0MiB)\n"
        "Installed 6 packages\n"
        "================ test session starts ================\n"
        "collected 6 items\n\n"
        "../tests/test_outputs.py ",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/query-optimize",
        "trial_name": "query-optimize__tcg-stall",
        "exception_info": {
            "exception_type": "RewardFileNotFoundError",
            "exception_message": "No reward file found",
        },
        "verifier": {
            "started_at": "2026-08-16T07:35:32Z",
            "finished_at": "2026-08-16T09:35:04Z",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_verifier_stall"
    assert "pytest started" in result["reason"]
    assert result["reward"] is None


def test_classifier_does_not_infer_network_from_completed_uv_install(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "downloading uv 0.9.5 x86_64-unknown-linux-gnu\n"
        "installing to /root/.local/bin\n"
        "everything's installed!\n"
        "source /root/.local/bin/env\n",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/pytorch-model-cli",
        "trial_name": "pytorch-model-cli__silent-bootstrap",
        "exception_info": {
            "exception_type": "RewardFileNotFoundError",
            "exception_message": "No reward file found",
        },
        "verifier": {
            "started_at": "2026-08-16T07:00:00Z",
            "finished_at": "2026-08-16T07:59:31Z",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_verifier_stall"
    assert "without a proven network error" in result["reason"]
    assert result["reward"] is None


def test_classifier_does_not_score_agent_timeout_when_verifier_bootstrap_failed(
    tmp_path,
):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "Failed to fetch package: Connection failed [IP: 10.0.2.100 3128]\n"
        "/tests/test.sh: uvx: command not found\n",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/gpt2-codegolf",
        "trial_name": "gpt2-codegolf__bootstrap",
        "exception_info": {
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 3600 seconds",
        },
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_network"
    assert "numeric zero" in result["reason"]


def test_classifier_prioritizes_external_cancellation_over_secondary_errors(tmp_path):
    trial_dir = tmp_path / "cancelled"
    (trial_dir / "verifier").mkdir(parents=True)
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "temporary failure resolving package index\n"
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__cancelled",
        "exception_info": {
            "exception_type": "CancelledError",
            "exception_message": "",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_cancelled"
    assert result["reward"] is None


def test_classifier_excludes_audited_operator_terminated_vm(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "infrastructure-control.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "trial": "example__controlled",
                "action": "qmp_quit",
                "cause": "verifier_network_stall",
            }
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__controlled",
        "exception_info": {
            "exception_type": "GuestAgentError",
            "exception_message": "guest-agent channel closed",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_operator_terminated"
    assert result["reward"] is None
    assert result["operator_control"] == {
        "action": "qmp_quit",
        "cause": "verifier_network_stall",
    }


def test_classifier_marks_verifier_timeout_without_stdout_as_stall(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__stalled",
        "exception_info": {
            "exception_type": "VerifierTimeoutError",
            "exception_message": "Verifier execution timed out after 3600 seconds",
        },
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_verifier_stall"
    assert result["reward"] is None


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


def test_classifier_excludes_timeout_consumed_by_dispatch_gate_wait(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_outputs.py - artifact does not exist", encoding="utf-8"
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "usage": {
                        "_dispatch": {
                            "model": MODEL,
                            "gate_wait_ms": 240_000,
                        }
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__dispatch-contention",
        "exception_info": {
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 3600 seconds",
        },
        "agent_execution": {
            "started_at": "2026-08-24T00:00:00Z",
            "finished_at": "2026-08-24T01:00:00Z",
        },
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_dispatch_contention"
    assert "240.000 seconds" in result["reason"]
    assert result["reward"] == 0.0


def test_classifier_keeps_negligible_dispatch_wait_as_model_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_outputs.py - artifact does not exist", encoding="utf-8"
    )
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "usage": {
                        "_dispatch": {
                            "model": MODEL,
                            "gate_wait_ms": 10_620,
                        }
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__negligible-dispatch-wait",
        "exception_info": {
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 3600 seconds",
        },
        "agent_execution": {
            "started_at": "2026-08-24T00:00:00Z",
            "finished_at": "2026-08-24T01:00:00Z",
        },
        "verifier_result": {"rewards": {"reward": 0}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "model_timeout"
    assert result["agent_elapsed_sec"] == 3600.0


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


def _resource_snapshot(
    *,
    cpus: int,
    memory_capacity_mib: int | None,
    memory_available_mib: int | None,
    disk_total_mib: int | None,
    disk_free_mib: int | None,
) -> Namespace:
    values = {
        "effective_cpu_count": cpus,
        "effective_memory_capacity_mb": memory_capacity_mib,
        "effective_memory_available_mb": memory_available_mib,
        "disk_total_mb": disk_total_mib,
        "disk_free_mb": disk_free_mib,
    }
    return Namespace(**values, as_dict=lambda: values)


def test_adaptive_trial_concurrency_preserves_headroom_on_8_gib_host(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        bench,
        "probe_system_resources",
        lambda _environment: _resource_snapshot(
            cpus=8,
            memory_capacity_mib=8 * 1024,
            memory_available_mib=6 * 1024,
            disk_total_mib=500 * 1024,
            disk_free_mib=300 * 1024,
        ),
    )
    task = bench.Task("heavy", tmp_path, "image", 2, 4096, 1, 1, 10 * 1024)

    concurrency, evidence = bench._adaptive_trial_concurrency([task], tmp_path)

    assert concurrency == 1
    assert evidence["slot_limits"]["memory"] == 1
    assert evidence["reservations"]["memory_mib"] == 2 * 1024


def test_adaptive_trial_concurrency_caps_large_host_at_four(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bench,
        "probe_system_resources",
        lambda _environment: _resource_snapshot(
            cpus=32,
            memory_capacity_mib=64 * 1024,
            memory_available_mib=48 * 1024,
            disk_total_mib=2 * 1024 * 1024,
            disk_free_mib=1024 * 1024,
        ),
    )
    task = bench.Task("light", tmp_path, "image", 1, 512, 1, 1, 2 * 1024)

    concurrency, evidence = bench._adaptive_trial_concurrency([task], tmp_path)

    assert concurrency == 4
    assert evidence["hard_ceiling"] == 4


def test_adaptive_trial_concurrency_falls_back_lean_when_probe_fails(
    monkeypatch, tmp_path
):
    def fail_probe(_environment):
        raise OSError("unavailable")

    monkeypatch.setattr(bench, "probe_system_resources", fail_probe)
    task = bench.Task("example", tmp_path, "image", 1, 512, 1, 1)

    concurrency, evidence = bench._adaptive_trial_concurrency([task], tmp_path)

    assert concurrency == 1
    assert evidence["fallback"] == "resource_probe_failed"
    assert evidence["probe_error_type"] == "OSError"


def test_write_config_parser_uses_adaptive_concurrency_by_default():
    args = bench.build_parser().parse_args(
        [
            "write-config",
            "--tasks-root",
            "/tasks",
            "--assets-root",
            "/assets",
            "--control-root",
            "/control",
            "--state-root",
            "/state",
            "--cache-root",
            "/cache",
            "--jobs-dir",
            "/jobs",
            "--base-disk",
            "/base.qcow2",
            "--qemu",
            "/bin/qemu",
            "--qemu-img",
            "/bin/qemu-img",
            "--job-name",
            "adaptive",
        ]
    )

    assert args.concurrency is None


def test_preparation_parsers_use_adaptive_workers_by_default():
    parser = bench.build_parser()
    assets = parser.parse_args(
        [
            "prepare-assets",
            "--tasks-root",
            "/tasks",
            "--assets-root",
            "/assets",
            "--crane",
            "/bin/crane",
            "--archive-tool",
            "/bin/archive",
        ]
    )
    cache = parser.parse_args(
        [
            "prepare-cache",
            "--tasks-root",
            "/tasks",
            "--assets-root",
            "/assets",
            "--cache-root",
            "/cache",
            "--base-disk",
            "/base.qcow2",
            "--qemu",
            "/bin/qemu",
            "--qemu-img",
            "/bin/qemu-img",
        ]
    )

    assert assets.workers is None
    assert cache.workers is None


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


def test_classifier_excludes_progressing_caffe_training_under_tcg_timeout(tmp_path):
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
                                        {
                                            "command": (
                                                "./build/tools/caffe train "
                                                "--solver=examples/cifar10/solver.prototxt"
                                            )
                                        }
                                    )
                                }
                            }
                        ],
                    },
                    "usage": {"_dispatch": {"model": MODEL}},
                },
                {
                    "result": (
                        "Iteration 300, loss = 1.93603\n"
                        "cifar10_quick_iter_500.caffemodel"
                    )
                },
            ]
        ),
        encoding="utf-8",
    )
    (trial_dir / "verifier" / "ctrf.json").write_text("{}", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        "FAILED test_training_completed_500_iterations - "
        "AssertionError: Training did not complete 500 iterations\n",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/caffe-cifar-10",
        "trial_name": "caffe-cifar-10__tcg",
        "exception_info": {"exception_type": "AgentTimeoutError"},
        "verifier_result": {"rewards": {"reward": 0}},
        "config": {"agent_timeout_multiplier": 4},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "environment_timing_sensitive"
    assert "iteration 300" in result["reason"]

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
        "infrastructure_verifier_stall"
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


def test_classifier_excludes_qcow_limit_emergency_read_only_failure(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps(
            [
                {
                    "result": "/dev/vda / ext4 rw,noatime,emergency_ro 0 0",
                    "usage": {"_dispatch": {"model": MODEL}},
                }
            ]
        ),
        encoding="utf-8",
    )
    (trial_dir / "exception.txt").write_text(
        "AddTestsDirError: guest-file-open failed: Read-only file system",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/sam-cell-seg",
        "trial_name": "sam-cell-seg__storage",
        "exception_info": {"exception_type": "AddTestsDirError"},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_storage"
    assert "backing size" in result["reason"]


def test_classifier_excludes_verifier_bundle_transfer_timeout(tmp_path):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
        json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
        encoding="utf-8",
    )
    (trial_dir / "exception.txt").write_text(
        "AddTestsDirError: guest-file-write failed\nTimeoutError: timed out",
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/build-pov-ray",
        "trial_name": "build-pov-ray__transfer",
        "exception_info": {"exception_type": "AddTestsDirError"},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == "infrastructure_transfer"
    assert "before any independent tests" in result["reason"]


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
                            "slot_wait_cycles": 2,
                            "upstream_429_retries": 1,
                            "gate_wait_ms": 125,
                        }
                    },
                    "assistant": {"tool_calls": [{"id": "a"}, {"id": "b"}]},
                },
                {
                    "usage": {
                        "_dispatch": {
                            "model": MODEL,
                            "429_retries": 1,
                            "slot_wait_cycles": 0,
                            "upstream_429_retries": 1,
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
        "slot_wait_cycles": 2,
        "upstream_429_retries": 2,
        "gate_wait_ms": 200,
        "persistent_bash_timeouts": 0,
        "maximum_assistant_tool_calls": 2,
    }


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.0.1", "harness_timeout_process_leak"),
        ("1.0.2", "passed"),
    ],
)
def test_classifier_invalidates_only_legacy_minimal_timeout_cleanup(
    tmp_path, version, expected
):
    trial_dir = _trial_dir(tmp_path)
    (trial_dir / "agent" / "host-dispatch-transcript.json").write_text(
        json.dumps(
            [
                {"usage": {"_dispatch": {"model": MODEL}}},
                {
                    "tool": "bash",
                    "result": (
                        "[command timed out after 1200 seconds]\n"
                        "The persistent bash shell was reset"
                    ),
                },
            ]
        ),
        encoding="utf-8",
    )
    trial = {
        "task_name": "terminal-bench/example",
        "trial_name": "example__timeout",
        "agent_info": {
            "name": "deepseek-minimal-host",
            "version": version,
        },
        "config": {
            "agent": {
                "name": (
                    "rootless_vm.harbor_deepseek_minimal_agent:"
                    "DeepSeekMinimalHostAgent"
                ),
                "model_name": MODEL,
            }
        },
        "verifier_result": {"rewards": {"reward": 1}},
    }

    result = bench._classify_trial(trial, trial_dir, MODEL)

    assert result["classification"] == expected
    assert result["persistent_bash_timeouts"] == 1
    if version == "1.0.1":
        assert result["underlying_classification"] == "passed"


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
    assert payload["evaluation_mode"] == "smoke"
    assert payload["leaderboard_minimum_attempts"] == 5
    assert payload["leaderboard_attempt_contract_met"] is False
    assert payload["valid_trials"] == 2
    assert payload["surplus_valid_trials"] == {"terminal-bench/example": 1}


def test_score_optionally_writes_private_machine_readable_artifact(
    monkeypatch, tmp_path, capsys
):
    observation = {
        "task": "terminal-bench/example",
        "trial": "example__one",
        "source": str(tmp_path / "trial"),
        "reward": 1.0,
        "classification": "passed",
        "attribution": {"layer": "none"},
    }
    monkeypatch.setattr(bench, "_observations", lambda _args: [observation])
    output = tmp_path / "private" / "score.json"
    args = Namespace(
        expected_tasks=1,
        expected_attempts=1,
        expected_model=None,
        tasks_root=None,
        output=str(output),
    )

    assert bench.score(args) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert f"SCORE ARTIFACT {output}" in captured.err
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert output.stat().st_mode & 0o777 == 0o600


def test_minimal_101_is_compatible_only_when_changed_paths_were_not_exercised():
    args = Namespace(expected_agent_version="1.0.2")
    clean = {
        "agent_version": "1.0.1",
        "harness_profile": "deepseek-minimal",
        "dispatches": 1,
        "persistent_bash_timeouts": 0,
        "maximum_assistant_tool_calls": 4,
    }
    affected = {**clean, "persistent_bash_timeouts": 1}
    truncated = {**clean, "maximum_assistant_tool_calls": 16}
    unaudited = {**clean, "dispatches": 0}

    assert bench._provenance_mismatches(clean, args) == []
    for incompatible in (affected, truncated, unaudited):
        assert bench._provenance_mismatches(incompatible, args) == [
            "agent_version: expected '1.0.2', found '1.0.1'"
        ]


def test_score_excludes_valid_rewards_from_a_legacy_harness_version(tmp_path, capsys):
    job = tmp_path / "job"
    for version in ("0.7.0", "0.8.4"):
        trial_dir = job / f"trial-{version}"
        (trial_dir / "agent").mkdir(parents=True)
        (trial_dir / "agent" / "tofu-host-transcript.json").write_text(
            json.dumps([{"usage": {"_dispatch": {"model": MODEL}}}]),
            encoding="utf-8",
        )
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "terminal-bench/example",
                    "trial_name": f"example__{version}",
                    "agent_info": {"name": "tofu-host", "version": version},
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
        expected_agent_version="0.8.4",
    )

    assert bench.score(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage_complete"] is True
    assert payload["valid_trials"] == 1
    assert payload["invalid_trials"] == {"harness_provenance_violation": 1}
    assert payload["provenance_violations"] == {
        "agent_version: expected '0.8.4', found '0.7.0'": 1
    }


def test_deepseek_minimal_provenance_pins_physical_context_window():
    args = Namespace(harness="deepseek-minimal")
    defaults = bench._profile_provenance_defaults(
        bench.harness_profile("deepseek-minimal")
    )

    assert defaults["expected_context_window_tokens"] == 393_216
    assert bench._provenance_mismatches(
        {
            "agent_name": "deepseek-minimal-host",
            "agent_version": "1.0.2",
            "reasoning_effort": "max",
            "temperature": 1.0,
            "top_p": 0.95,
            "max_rounds": 4_096,
            "max_output_tokens": 256_000,
            "context_window_tokens": 393_215,
            "environment_import_path": (
                "rootless_vm.harbor_environment:RootlessQemuEnvironment"
            ),
        },
        args,
    ) == ["context_window_tokens: expected 393216, found 393215"]


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
            "max_concurrent_trials": 32,
            "agent_concurrency": 16,
        },
        {
            "profile": "standard",
            "attempts": 2,
            "tasks": ["terminal-bench/beta"],
            "agent_timeout_multiplier": 4,
            "verifier_timeout_multiplier": 4,
            "max_concurrent_trials": 32,
            "agent_concurrency": 16,
        },
    ]


def test_retry_plan_routes_tcg_failures_to_bounded_profiles(
    monkeypatch, tmp_path, capsys
):
    tasks = [
        bench.Task(
            name,
            tmp_path / name,
            "image",
            1,
            1,
            1,
            14_400 if name == "verify" else 1,
        )
        for name in (
            "cancel-async-tasks",
            "dispatch",
            "dual",
            "nested",
            "slow",
            "verify",
        )
    ]
    monkeypatch.setattr(bench, "_load_tasks", lambda _root: tasks)
    monkeypatch.setattr(
        bench,
        "_observations",
        lambda _args: [
            {
                "task": "terminal-bench/dispatch",
                "classification": "environment_dispatch_contention",
            },
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
            {
                "task": "terminal-bench/cancel-async-tasks",
                "classification": "environment_timing_sensitive",
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
        "terminal-bench/dispatch",
        "terminal-bench/dual",
        "terminal-bench/slow",
    ]
    assert profiles["tcg_clock_calibrated"]["tasks"] == [
        "terminal-bench/cancel-async-tasks"
    ]
    assert profiles["tcg_clock_calibrated"]["virtual_time_shift"] == 0
    assert profiles["tcg_clock_calibrated"]["max_concurrent_trials"] == 1
    assert profiles["verifier_heavy"]["agent_timeout_multiplier"] == 4
    assert profiles["verifier_heavy"]["agent_concurrency"] == 8
    assert profiles["verifier_heavy"]["verifier_timeout_multiplier"] == 6


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
    assert payload["exact_route_trials"] == 0
    assert payload["unaudited_route_trials"] == 1
    assert payload["mismatched_route_trials"] == 0
    assert payload["audited_routes_pure"] is False
    assert payload["route_pure"] is False


def test_analyze_separates_route_coverage_from_route_purity(monkeypatch, capsys):
    monkeypatch.setattr(
        bench,
        "_observations",
        lambda _args: [
            {
                "classification": "passed",
                "served_models": [MODEL],
                "raw_reasoning_records": 0,
            },
            {
                "classification": "routing_unverified",
                "served_models": [],
                "raw_reasoning_records": 0,
            },
        ],
    )
    args = Namespace(jobs=[], expected_model=MODEL)

    assert bench.analyze(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["audited_route_trials"] == 1
    assert payload["exact_route_trials"] == 1
    assert payload["unaudited_route_trials"] == 1
    assert payload["mismatched_route_trials"] == 0
    assert payload["audited_routes_pure"] is True
    assert payload["route_pure"] is False


def test_analyze_optionally_writes_private_machine_readable_artifact(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        bench,
        "_observations",
        lambda _args: [
            {
                "classification": "passed",
                "served_models": [MODEL],
                "raw_reasoning_records": 0,
            }
        ],
    )
    output = tmp_path / "private" / "analysis.json"
    args = Namespace(jobs=[], expected_model=MODEL, output=str(output))

    assert bench.analyze(args) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert f"ANALYSIS ARTIFACT {output}" in captured.err
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert output.stat().st_mode & 0o777 == 0o600


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
