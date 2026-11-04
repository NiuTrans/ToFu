"""Offline contract tests for the native Terminal-Bench sandbox runner."""
from __future__ import annotations

import http.server
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from evaluations.terminal_bench import audit, harness, runner, vm_trial

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test.sh'}


def make_task(root: Path, name: str = "demo-task") -> Path:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "environment").mkdir()
    (task / "task.toml").write_text(
        '[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 1200.0\n'
        '[environment]\ndocker_image = "example/demo:1"\nbuild_timeout_sec = 300.0\n', encoding="utf-8")
    (task / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    (task / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n", encoding="utf-8")
    (task / "solution" / "solve.sh").write_text("echo secret-solution\n", encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text("FROM debian\n", encoding="utf-8")
    return task


def artifact(name: str) -> harness.Artifact:
    return harness.Artifact(name=name, filename=f"{name}.tar.gz", sha256="a" * 64, version="1")


def test_bundle_never_ships_the_reference_solution(tmp_path):
    task = harness.load_task(make_task(tmp_path))
    with tarfile.open(fileobj=io.BytesIO(harness.bundle_task(task))) as bundle:
        names = bundle.getnames()
    assert "tests/test.sh" in names and "environment/Dockerfile" in names
    assert not [name for name in names if name.startswith("solution")]


def test_trial_spec_uses_protocol_budget_and_egress_policy(tmp_path):
    task = harness.load_task(make_task(tmp_path))
    protocol = harness.PROTOCOLS["vendor-6h"]
    timeout = harness.agent_timeout_for(task, protocol)
    agent = harness.tofu_agent(artifact=artifact("tofu"), files_url="http://relay:1/files",
                               model_base_url="http://relay:1/arm/tofu-x/v1", model="glm-5.3-flash",
                               timeout_sec=timeout, instruction=task.instruction, context_tokens=262144,
                               output_tokens=65536)
    spec = harness.build_trial_spec(task=task, arm="tofu", attempt=2, agent=agent, protocol=protocol,
                                    agent_timeout_sec=timeout, egress_upstream="http://proxy:8080")
    assert timeout == 21600 and spec["agent_timeout_sec"] == 21600
    assert harness.agent_timeout_for(task, harness.PROTOCOLS["task-default"]) == 1200
    assert spec["trial_key"] == "demo-task-tofu-a2"
    dotted = harness.Task(**{**task.__dict__, "name": "install-windows-3.11"})
    assert harness.trial_key(dotted, "opencode", 1) == "install-windows-3.11-opencode-a1"
    with pytest.raises(ValueError):
        harness.trial_key(harness.Task(**{**task.__dict__, "name": "../escape"}), "tofu", 1)
    assert spec["verifier_timeout_sec"] == 900.0
    assert "marginlab.ai" in spec["egress"]["deny_hosts"]
    assert spec["egress"]["upstream"] == "http://proxy:8080"
    assert "--timeout-s 21600" in agent["program"] and " -I -m tofu_agent " in agent["program"]
    assert agent["artifacts"][0]["url"] == "http://relay:1/files/tofu.tar.gz"


def test_agent_specs_carry_no_credentials_and_same_endpoint_shape(tmp_path):
    task = harness.load_task(make_task(tmp_path))
    tofu = harness.tofu_agent(artifact=artifact("tofu"), files_url="http://r/files",
                              model_base_url="http://r/arm/tofu/v1", model="glm-5.3-flash", timeout_sec=60,
                              instruction=task.instruction, context_tokens=262144, output_tokens=65536)
    opencode = harness.opencode_agent(artifact=artifact("opencode"), files_url="http://r/files",
                                      model_base_url="http://r/arm/opencode/v1", model="glm-5.3-flash",
                                      instruction=task.instruction, context_tokens=262144,
                                      output_tokens=65536)
    pi = harness.pi_agent(artifact=artifact("pi"), files_url="http://r/files",
                          model_base_url="http://r/arm/pi/v1", model="glm-5.3-flash",
                          instruction=task.instruction, context_tokens=262144, output_tokens=65536)
    envelope = json.loads(tofu["env"]["TOFU_AGENT_MODEL_ROUTING"])
    routing = envelope["model_routing"]
    assert routing["contract_version"] == "tofu.model-routing/v2"
    assert routing["connections"][0]["base_url"] == "http://r/arm/tofu/v1"
    assert routing["offerings"][0]["context_window"] == 262144
    # Every arm is told the same output limit the relay enforces.
    assert routing["deployments"][0]["max_output_tokens"] == 65536
    # Tofu runs with its product defaults: no evaluation-only fault budgets.
    assert not {"TOFU_GATEWAY_OUTAGE_BUDGET_S", "TOFU_429_SATURATION_SECS"} & set(tofu["env"])
    assert envelope["model"] == {"creator_id": "zhipu", "model_id": "glm-5.3-flash"}
    assert list(envelope["credential_secrets"].values()) == ["relay-held"]
    assert all(agent["env"]["no_proxy"].startswith("r,") for agent in (tofu, opencode, pi))
    config = json.loads(opencode["log_files"]["opencode.json"])
    provider = config["provider"]["benchmark"]
    assert provider["options"] == {"baseURL": "http://r/arm/opencode/v1"}
    assert provider["models"]["glm-5.3-flash"]["limit"] == {"context": 262144, "output": 65536}
    assert opencode["env"]["OPENCODE_CONFIG"] == "/logs/agent/opencode.json"
    # Each baseline runs once, exactly as its upstream adapter runs it.
    subprocess.run(["bash", "-n", "-c", opencode["program"]], check=True)
    assert opencode["program"].count(" run ") == 1 and "--continue" not in opencode["program"]
    pi_provider = json.loads(pi["log_files"]["pi-models.json"])["providers"]["zai"]
    assert pi_provider["baseUrl"] == "http://r/arm/pi/v1" and pi_provider["apiKey"] == "relay-held"
    assert pi_provider["modelOverrides"]["glm-5.3-flash"] == {"contextWindow": 262144, "maxTokens": 65536}
    assert "--continue" not in pi["program"]


class _Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"origin:" + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def origin():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def proxy_get(proxy: vm_trial.EgressProxy, url: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": "http://" + proxy.address}))
    with opener.open(url, timeout=10) as response:
        return response.read()


def wait_for_records(path: Path, count: int) -> list[dict]:
    for _ in range(100):
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        if len(rows) >= count:
            return rows
        threading.Event().wait(0.05)
    return rows


def test_egress_proxy_allows_logs_and_denies(tmp_path, origin, monkeypatch):
    # urllib bypasses an explicit proxy for hosts listed in the inherited no_proxy.
    for key in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(key, raising=False)
    log_path = tmp_path / "egress.jsonl"
    proxy = vm_trial.EgressProxy("127.0.0.1", None, ["marginlab.ai"], ["terminal-bench"], log_path)
    proxy.phase = "agent"
    assert proxy_get(proxy, f"http://127.0.0.1:{origin}/ok?q=1") == b"origin:/ok?q=1"
    with pytest.raises(urllib.error.HTTPError) as denied_host:
        proxy_get(proxy, "http://docs.marginlab.ai/explorers/x")
    assert denied_host.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as denied_url:
        proxy_get(proxy, f"http://127.0.0.1:{origin}/terminal-bench/solve.sh")
    assert denied_url.value.code == 403
    rows = wait_for_records(log_path, 3)
    decisions = sorted((row["decision"], row["host"]) for row in rows)
    assert decisions == [("allow", "127.0.0.1"), ("deny", "127.0.0.1"), ("deny", "docs.marginlab.ai")]
    assert all(row["phase"] == "agent" for row in rows)
    assert proxy.counts["denied"] == 2


def test_egress_proxy_connect_tunnel_and_denial(tmp_path, origin):
    proxy = vm_trial.EgressProxy("127.0.0.1", None, ["tbench.ai"], [], tmp_path / "egress.jsonl")
    host, port = proxy.address.split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(b"CONNECT www.tbench.ai:443 HTTP/1.1\r\nHost: www.tbench.ai:443\r\n\r\n")
        assert sock.recv(100).startswith(b"HTTP/1.1 403")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(f"CONNECT 127.0.0.1:{origin} HTTP/1.1\r\n\r\n".encode())
        assert sock.recv(100).startswith(b"HTTP/1.1 200")
        sock.sendall(b"GET /tunnel HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        reply = b""
        while chunk := sock.recv(4096):
            reply += chunk
    assert reply.endswith(b"origin:/tunnel")


def test_reward_files_and_trial_paths(tmp_path):
    trial = vm_trial.Trial({"out_dir": str(tmp_path), "task_dir": str(tmp_path / "task"), "trial_key": "k",
                            "trial_id": "t", "task_name": "x", "arm": "tofu", "agent": {"kind": "tofu"}})
    assert trial.read_reward() == (None, None)
    (tmp_path / "verifier").mkdir()
    (tmp_path / "verifier" / "reward.json").write_text('{"reward": 0.5}')
    assert trial.read_reward() == (0.5, "reward.json")
    (tmp_path / "verifier" / "reward.txt").write_text("1\n")
    assert trial.read_reward() == (1.0, "reward.txt")


def test_bundle_extraction_skips_unsafe_members(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, data in (("trial/result.json", b"{}"), ("../escape.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("trial/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        bundle.addfile(link)
    skipped = runner.extract_regular_files(archive, tmp_path / "out")
    assert skipped == 2
    assert (tmp_path / "out" / "trial" / "result.json").read_bytes() == b"{}"
    assert not (tmp_path / "escape.txt").exists()


def test_summary_counts_only_measured_trials(tmp_path):
    runner.write_json(tmp_path / "run.json", {"tasks": ["a", "b", "c"], "arms": ["tofu"], "max_attempts": 2,
                                              "protocol": "vendor-6h"})
    states = {
        "a/attempt-1": {"phase": "scored", "reward": 1.0, "finished": True, "session_id": "s", "stopped": True},
        "b/attempt-1": {"phase": "lost", "finished": True, "session_id": "s", "stopped": True},
        "b/attempt-2": {"phase": "no_reward", "reward": None, "finished": True},
        "c/attempt-1": {"phase": "infra_error", "finished": True},
        "c/attempt-2": {"phase": "start_failed", "finished": True, "session_id": "s"},
    }
    for path, state in states.items():
        runner.write_json(tmp_path / "trials" / "tofu" / path / "state.json", state)
    arm = runner.summarize(tmp_path)["arms"]["tofu"]
    assert (arm["measured"], arm["passed"], arm["no_reward"], arm["unmeasured"]) == (2, 1, 1, 1)
    assert arm["pass_rate_of_planned"] == round(1 / 3, 4) and arm["pass_rate_of_measured"] == 0.5
    assert arm["unstopped_sessions"] == 1


HIDDEN_TEST_LINE = "assert render_checksum('/app/out.png') == 'f3a9c1d0e8b7a6c5d4e3f2a1b0c9d8e7'"
VISIBLE_LINE = "this configuration line is visible inside the task environment image"


def audit_task(root: Path) -> Path:
    task = make_task(root)
    (task / "tests" / "test_outputs.py").write_text(f"{HIDDEN_TEST_LINE}\n{VISIBLE_LINE}\n", encoding="utf-8")
    (task / "environment" / "app.conf").write_text(VISIBLE_LINE + "\n", encoding="utf-8")
    return task


def write_tofu_trajectory(trial: Path, steps: list[dict]) -> None:
    (trial / "agent").mkdir(parents=True)
    (trial / "agent" / "tofu-result.json").write_text(json.dumps({"trajectory": {"steps": steps}}))


def step(call_id: str, tool: str, arguments: dict, result: str) -> dict:
    return {"tool_calls": [{"tool_call_id": call_id, "function_name": tool, "arguments": arguments}],
            "observation": {"results": [{"source_call_id": call_id, "content": result}]}}


def test_audit_flags_hidden_content_received_from_outside(tmp_path):
    fingerprints = audit.task_fingerprints(audit_task(tmp_path / "tasks"))
    assert fingerprints == {audit.normalize(HIDDEN_TEST_LINE)}
    trial = tmp_path / "trial"
    write_tofu_trajectory(trial, [step("c1", "fetch_url", {"url": "https://example.org/terminal-bench/x"},
                                       f"12: {HIDDEN_TEST_LINE}\n# terminal-bench-canary GUID {harness.TB_CANARY_GUID}")])
    report = audit.audit_trial(trial, fingerprints)
    assert report["flags"] == ["hidden_lines", "canary_results", "source_calls"]
    assert report["hidden_lines"][0]["tool"] == "fetch_url"


def test_audit_treats_hidden_lines_from_local_files_as_weak(tmp_path):
    fingerprints = audit.task_fingerprints(audit_task(tmp_path / "tasks"))
    trial = tmp_path / "trial"
    write_tofu_trajectory(trial, [step("c1", "read_files", {"path": "/app/repo/checks.py"},
                                       f"{HIDDEN_TEST_LINE}\n# terminal-bench-canary GUID {harness.TB_CANARY_GUID}")])
    report = audit.audit_trial(trial, fingerprints)
    assert report["flags"] == ["local_hidden_lines"] and report["strong"] is False


def test_audit_ignores_lines_the_agent_wrote_itself(tmp_path):
    fingerprints = audit.task_fingerprints(audit_task(tmp_path / "tasks"))
    trial = tmp_path / "trial"
    write_tofu_trajectory(trial, [
        step("c1", "write_file", {"path": "/app/t.py", "content": HIDDEN_TEST_LINE}, "written"),
        step("c2", "run_command", {"command": "cat /app/t.py"}, HIDDEN_TEST_LINE + "\n" + VISIBLE_LINE),
    ])
    assert audit.audit_trial(trial, fingerprints)["flags"] == []


def test_audit_fingerprints_the_task_readme_the_agent_never_sees(tmp_path):
    task = audit_task(tmp_path / "tasks")
    answer = "3. The line contains the correct model name: `example-org/model-7b`"
    restated = "Write the best model for Scandinavian texts to /app/result.txt"
    (task / "instruction.md").write_text(restated + "\n", encoding="utf-8")
    (task / "README.md").write_text(f"{restated}\n{answer}\n", encoding="utf-8")
    fingerprints = audit.task_fingerprints(task)
    assert audit.normalize(answer) in fingerprints and audit.normalize(restated) not in fingerprints
    trial = tmp_path / "trial"
    write_tofu_trajectory(trial, [step("c1", "fetch_url", {"url": "https://example.org/task-page"}, answer)])
    report = audit.audit_trial(trial, fingerprints)
    assert report["flags"] == ["hidden_lines"] and report["strong"] is True


def test_audit_evidence_does_not_depend_on_the_hash_seed(tmp_path):
    task = audit_task(tmp_path / "tasks")
    hidden = [f"assert frame_checksum({index}) == 'reference-value-{index:04d}-abcdef'" for index in range(30)]
    (task / "tests" / "test_frames.py").write_text("\n".join(hidden) + "\n", encoding="utf-8")
    trial = tmp_path / "trial"
    write_tofu_trajectory(trial, [step("c1", "fetch_url", {"url": "https://example.org/page"}, "\n".join(hidden))])
    program = ("import json, sys\nfrom pathlib import Path\nfrom evaluations.terminal_bench import audit\n"
               "print(json.dumps(audit.audit_trial(Path(sys.argv[2]), audit.task_fingerprints(Path(sys.argv[1])))))")
    repo = Path(audit.__file__).resolve().parents[2]
    outputs = [subprocess.run([sys.executable, "-c", program, str(task), str(trial)], cwd=repo, check=True,
                              capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(repo), "PYTHONHASHSEED": seed}).stdout
               for seed in ("1", "2")]
    report = json.loads(outputs[0])
    assert report["hidden_line_count"] == 30 and len(report["hidden_lines"]) == 20
    assert outputs[0] == outputs[1]


@pytest.fixture
def local_docker(tmp_path, monkeypatch):
    """``docker exec`` stand-in whose command, like a container's, runs outside the client's tree."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\nshift\n"
        'while [ "$1" = -e ] || [ "$1" = -w ]; do shift 2; done\nshift\n'
        'pid=$("$@" < /dev/null > /dev/null 2>&1 & echo $!)\n'
        'while kill -0 "$pid" 2> /dev/null; do sleep 0.1; done\n', encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(vm_trial, "KILL_GRACE_SEC", 1)
    return tmp_path


def live_sleeps(seconds: str) -> list[int]:
    return [pid for pid, (_, argv, _) in vm_trial._proc_table().items() if argv == ["sleep", seconds]]


def settle(predicate, seconds: float = 10.0) -> bool:
    deadline = time.time() + seconds
    while not predicate() and time.time() < deadline:
        time.sleep(0.05)
    return predicate()


@pytest.mark.parametrize("program", ["exec sleep {n}", "(trap '' TERM; exec sleep {n}) & wait"],
                         ids=["program-execs", "child-outlives-its-shell"])
def test_deadline_stops_the_whole_in_container_program(local_docker, program):
    seconds = str(100000 + uuid.uuid4().int % 800000)
    try:
        outcome = vm_trial.exec_with_deadline(
            "trial", program.format(n=seconds), {}, "tbn-test-" + uuid.uuid4().hex, 2,
            local_docker / "stdout", local_docker / "stderr")
        assert outcome["timed_out"] is True
        assert settle(lambda: not live_sleeps(seconds))
    finally:
        for pid in live_sleeps(seconds):
            os.kill(pid, signal.SIGKILL)


def test_deadline_kill_counts_signalled_and_force_killed_processes(local_docker):
    seconds, marker = str(100000 + uuid.uuid4().int % 800000), "tbn-test-" + uuid.uuid4().hex
    proc = subprocess.Popen(["bash", "-c", vm_trial.MARKED_SHELL, marker, f"(trap '' TERM; exec sleep {seconds}) & wait"])
    try:
        assert settle(lambda: live_sleeps(seconds))
        assert vm_trial.kill_marked_tree(marker) == (3, 1)
        assert proc.wait(timeout=5) != 0
    finally:
        for pid in live_sleeps(seconds):
            os.kill(pid, signal.SIGKILL)


def test_report_merges_fixups_and_applies_contamination_review(tmp_path):
    from evaluations.terminal_bench import report

    tasks_dir = tmp_path / "tasks"
    for name in ("a", "b", "c"):
        audit_task(tasks_dir / name)
    main, fixup = tmp_path / "main", tmp_path / "fixup"
    runner.write_json(main / "run.json", {"tasks": ["a-demo-task", "b-demo-task", "c-demo-task"],
                                          "arms": ["tofu"], "protocol": "vendor-6h"})
    runner.write_json(fixup / "run.json", {"tasks": ["b-demo-task"], "arms": ["tofu"], "protocol": "vendor-6h"})
    for name in ("a", "b", "c"):
        (tasks_dir / f"{name}-demo-task").mkdir(parents=True, exist_ok=True)
        for item in (tasks_dir / name / "demo-task").iterdir():
            item.rename(tasks_dir / f"{name}-demo-task" / item.name)
    states = {(main, "a-demo-task", 1): {"phase": "scored", "reward": 1.0},
              (main, "b-demo-task", 3): {"phase": "infra_error", "finished": True},
              (fixup, "b-demo-task", 1): {"phase": "scored", "reward": 1.0},
              (main, "c-demo-task", 1): {"phase": "scored", "reward": 0.0}}
    for (root, task, attempt), state in states.items():
        runner.write_json(root / "trials" / "tofu" / task / f"attempt-{attempt}" / "state.json", state)
    review = {"tofu/a-demo-task": {"contaminated": True, "note": "fetched the reference solution"}}
    arm = report.build_report([main, fixup], tasks_dir, review)["arms"]["tofu"]
    assert (arm["measured"], arm["passed_raw"], arm["contaminated_passes"], arm["passed_strict"]) == (3, 2, 1, 1)
    assert arm["unmeasured"] == [] and arm["strict_rate"] == round(1 / 3, 4)


def plan_run(root: Path, arms: list[str], tasks: list[str], **overrides) -> None:
    config = {"tasks": tasks, "arms": arms, "protocol": "vendor-6h", "model": "m",
              "task_identity": {"dataset_commit": "c1"}}
    runner.write_json(root / "run.json", {**config, **overrides})


def scored_trial(root: Path, arm: str, task: str, reward: float, agent: dict | None = None) -> None:
    attempt = root / "trials" / arm / task / "attempt-1"
    runner.write_json(attempt / "state.json", {"phase": "scored", "reward": reward})
    if agent is not None:
        runner.write_json(attempt / "result.json", {"phases": {"agent": agent}})


def test_report_counts_budget_exhaustion_reported_by_the_harness_as_a_timeout(tmp_path):
    from evaluations.terminal_bench import report

    tasks = ["self-exit", "runner-kill", "early-exit"]
    for task in tasks:
        make_task(tmp_path / "tasks", task)
    work = tmp_path / "work"
    plan_run(work, ["tofu"], tasks)
    scored_trial(work, "tofu", "self-exit", 1.0,
                 {"budget_sec": 100, "duration_s": 101.7, "exit_code": 3, "timed_out": False})
    scored_trial(work, "tofu", "runner-kill", 0.0,
                 {"budget_sec": 100, "duration_s": 100.0, "exit_code": -9, "timed_out": True})
    scored_trial(work, "tofu", "early-exit", 0.0,
                 {"budget_sec": 100, "duration_s": 40.0, "exit_code": 0, "timed_out": False})
    built = report.build_report([work], tmp_path / "tasks", {})
    assert built["arms"]["tofu"]["agent_timeouts"] == 2
    assert [built["trials"][f"tofu/{task}"]["agent_budget_exhausted"] for task in tasks] == [True, True, False]


def test_report_combines_arms_of_comparable_runs_and_refuses_the_rest(tmp_path):
    from evaluations.terminal_bench import report

    tasks = ["a-task", "b-task"]
    for task in tasks:
        make_task(tmp_path / "tasks", task)
    native, pi = tmp_path / "native", tmp_path / "pi"
    plan_run(native, ["tofu", "opencode"], tasks)
    plan_run(pi, ["pi"], tasks)
    for root, arm in ((native, "tofu"), (native, "opencode"), (pi, "pi")):
        for task in tasks:
            scored_trial(root, arm, task, 1.0)
    native_review, pi_review = tmp_path / "native-review.json", tmp_path / "pi-review.json"
    native_review.write_text(json.dumps({"tofu/a-task": {"contaminated": True, "note": "fetched tests"}}))
    pi_review.write_text(json.dumps({"pi/b-task": {"contaminated": False, "note": "public docs"}}))
    output = tmp_path / "combined" / "report.json"
    assert report.main([str(native), str(pi), "--tasks-dir", str(tmp_path / "tasks"), "--review", str(native_review),
                        "--review", str(pi_review), "--output", str(output)]) == 0
    arms = json.loads(output.read_text())["arms"]
    assert list(arms) == ["tofu", "opencode", "pi"]
    assert [arms[arm]["passed_strict"] for arm in arms] == [1, 2, 2]
    assert not (native / "report.json").exists()

    pi_review.write_text(json.dumps({"tofu/a-task": {"contaminated": False, "note": "disagrees"}}))
    with pytest.raises(ValueError, match="tofu/a-task"):
        report.load_reviews([native_review, pi_review])
    plan_run(pi, ["pi"], tasks, task_identity={"dataset_commit": "c2"})
    with pytest.raises(ValueError, match="dataset_commit"):
        report.build_report([native, pi], tmp_path / "tasks", {})
    plan_run(pi, ["pi"], tasks + ["unplanned"])
    with pytest.raises(ValueError, match="unplanned"):
        report.build_report([native, pi], tmp_path / "tasks", {})
