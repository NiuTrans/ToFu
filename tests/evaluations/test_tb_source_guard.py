"""Offline contract tests for the benchmark-source blocklist shared by the native arms."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evaluations.terminal_bench import harness, source_guard as guard

pytestmark = pytest.mark.unit

BLOCKED = [
    "https://www.tbench.ai/registry/terminal-bench/2.0",
    "https://marginlab.ai/explorers/terminal-bench/count-dataset-tokens",
    "https://raw.githubusercontent.com/harbor-framework/terminal-bench-2/main/tasks/x/tests/test_outputs.py",
    "https://raw.githubusercontent.com/Bruce-lys/LLM-benchmark/main/terminal-bench-2-1/tasks/x/tests/test_outputs.py",
    "https://api.github.com/repos/Eyan-L/Agent_Bench-NSCC/contents/agent_bench",
    "https://raw.githubusercontent.com/lynl7130/terminal-bench/refs/heads/main/tasks/x/solution.sh",
    "git clone github.com/laude-institute/terminal-bench",
    "hf_hub_download(repo_id='laude-institute/terminal-bench-2', filename='x')",
    "curl -s https://hub.harborframework.com/v2/terminal-bench/manifests/latest",
]
ALLOWED = [
    "https://raw.githubusercontent.com/python/cpython/main/README.rst",
    "https://huggingface.co/datasets/ryanmarten/OpenThoughts-1k-sample",
    "https://github.com/povray/povray/archive/refs/tags/v3.7.tar.gz",
    "cd /app/terminal-bench-notes && ls",
    # A lookalike host ("notmarginlab.ai") is denied by OpenCode's substring glob but
    # not by the other arms' boundary-aware rule; documented divergence, kept out of this list.
    "git clone https://gitlab.com/gitlab-org/gitlab.git",
]


@pytest.mark.parametrize("text", BLOCKED)
def test_blocked_finds_answer_sources(text):
    assert guard.blocked(text)


@pytest.mark.parametrize("text", ALLOWED)
def test_blocked_leaves_legitimate_internet_alone(text):
    assert guard.blocked(text) is None


@pytest.mark.parametrize("text", BLOCKED + ALLOWED)
@pytest.mark.parametrize("tool,key", [("Bash", "command"), ("WebFetch", "url")])
def test_claude_code_hook_refuses_what_the_rule_refuses(tmp_path, tool, key, text):
    hook = tmp_path / "hook.sh"
    hook.write_text(guard.claude_code_hook_script(), encoding="utf-8")
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: text}})
    probe = subprocess.run(["bash", str(hook)], input=payload.encode(), capture_output=True, check=False)
    if guard.blocked(text):
        assert probe.returncode == 2
        assert b"answer source" in probe.stderr
    else:
        assert probe.returncode == 0 and not probe.stderr


def test_pi_extension_refuses_the_same_inputs(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node on PATH for the Pi extension dialect")
    module = tmp_path / "guard.mjs"
    module.write_text(guard.pi_extension(), encoding="utf-8")
    cases = [[text, bool(guard.blocked(text))] for text in BLOCKED + ALLOWED]
    program = (
        f"const m = await import({json.dumps('file://' + str(module))}); let handler; "
        "m.default({on: (name, fn) => { handler = fn; }}); "
        f"let ok = true; for (const [text, want] of {json.dumps(cases)}) {{ "
        "const reply = await handler({toolName: 'bash', input: {command: text}}); "
        "if (Boolean(reply && reply.block) !== want) { ok = false; console.log('MISMATCH', text); } } "
        "process.exit(ok ? 0 : 1);")
    assert subprocess.run([node, "--input-type=module", "-e", program], capture_output=True, text=True,
                          check=False).returncode == 0


def test_opencode_patterns_match_how_opencode_evaluates_them():
    """Same matching as OpenCode 1.19.0 Wildcard.match: case-sensitive glob, longest pattern last."""
    rules = guard.opencode_permission()["bash"]
    assert "webfetch" not in guard.opencode_permission()

    def evaluate(command):
        matches = [(len(pattern), pattern, action) for pattern, action in sorted(rules.items())
                   if pattern == command or _glob(pattern).fullmatch(command)]
        return max(matches)[2]

    for text in BLOCKED:
        if guard.blocked(text) and any(marker in text.lower() for marker in guard.ANSWER_HOSTS + guard.SOURCE_REPOS):
            assert evaluate(text) == "deny", text
        else:  # mirrors spelled with capitals fall outside the case-sensitive patterns
            assert evaluate(text) in ("deny", "allow")
    for text in ALLOWED:
        assert evaluate(text) == "allow", text


def _glob(pattern):
    import re

    escaped = re.sub(r"[.+^${}()|[\]\\]", lambda m: "\\" + m.group(), pattern)
    return re.compile("^" + escaped.replace("*", ".*").replace("?", ".") + "$")


def guard_url_blocked(url, domains):
    from lib.search.url_blocklist import url_is_blocked
    return url_is_blocked(url, domains)


def test_tofu_config_keeps_the_product_defaults_and_only_adds_answer_sources():
    from lib import _DEFAULT_SKIP_DOMAINS

    domains = set(guard.tofu_server_config()["search"]["skip_domains"])
    assert set(_DEFAULT_SKIP_DOMAINS) <= domains
    assert set(guard.ANSWER_HOSTS) <= domains
    # Shared hosts stay open as hosts: only their answer-source paths are refused.
    assert not {"github.com", "raw.githubusercontent.com", "huggingface.co"} & domains
    for text in BLOCKED:
        if text.startswith("https://"):
            assert guard_url_blocked(text, domains), text
    for text in ALLOWED:
        if text.startswith("https://"):
            assert not guard_url_blocked(text, domains), text


def test_tofu_canary_blocks_repository_paths_on_shared_hosts_only():
    domains = set(guard.tofu_server_config()["search"]["skip_domains"])
    for url in guard.CANARY_BLOCKED_URLS:
        assert guard_url_blocked(url, domains), url
    assert not guard_url_blocked(guard.CANARY_ALLOWED_URL, domains)


@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/harbor-framework/terminal-bench-2-1/main/tasks/x/tests/test_outputs.py",
    "https://github.com/harbor-framework/terminal-bench/blob/main/archive/x/README.md",
    "https://api.github.com/repos/harbor-framework/terminal-bench/contents/archive/x",
    "https://codeload.github.com/harbor-framework/terminal-bench/tar.gz/refs/heads/main",
    "https://github.com/lynl7130/terminal-bench/blob/main/tasks/x/solution.sh",
    "https://huggingface.co/datasets/laude-institute/terminal-bench-2",
    # 2026-11-06 Tofu A/B (torch-pipeline-parallelism): a mirror nesting the
    # benchmark deep in another repository, and an HF dataset copy.
    "https://github.com/eigent-ai/agenticrl-bench/tree/main/tasks/capability-x/tests/__image__/tb21/"
    "terminal-bench-2-1/torch-pipeline-parallelism/tests",
    "https://raw.githubusercontent.com/eigent-ai/agenticrl-bench/main/tasks/capability-x/tests/__image__/tb21/"
    "terminal-bench-2-1/torch-pipeline-parallelism/tests/test_outputs.py",
    "https://api.github.com/repos/eigent-ai/agenticrl-bench/contents/tasks/capability-x/tests/__image__/tb21/"
    "terminal-bench-2-1/torch-pipeline-parallelism",
    "https://huggingface.co/datasets/harborframework/terminal-bench-2.0/raw/main/torch-pipeline-parallelism/"
    "tests/test_outputs.py",
])
def test_tofu_guard_covers_every_observed_leak_shape(url):
    domains = set(guard.tofu_server_config()["search"]["skip_domains"])
    assert guard_url_blocked(url, domains), url


PARITY_URLS = [
    *(text for text in BLOCKED + ALLOWED if text.startswith("https://")),
    "https://gitlab.com/someone/terminal-bench-mirror/-/raw/main/tests/test_outputs.py",
    "https://r.jina.ai/https://github.com/harbor-framework/terminal-bench",
    "https://ghproxy.example.net/https://raw.githubusercontent.com/laude-institute/x/main/y",
    "https://github.com/someone/notes/blob/main/bruce-lys/llm-benchmark-copy.md",
    "https://pypi.org/project/terminal-notes/",
    "https://github.com/pytorch/pytorch/blob/main/torch/distributed/pipelining/README.md",
    "https://huggingface.co/datasets/stanfordnlp/imdb",
]


@pytest.mark.parametrize("url", PARITY_URLS)
def test_tofu_refuses_exactly_the_urls_the_shared_rule_refuses(url):
    """Tofu's projection matches the other arms' rule on URLs (any host, any depth).

    Known divergence, not listed: a marker inside a host name (the shared rule
    sees the whole URL text; Tofu's host rules match hosts, not substrings).
    """
    domains = set(guard.tofu_server_config()["search"]["skip_domains"])
    assert guard_url_blocked(url, domains) == bool(guard.blocked(url)), url


def test_tofu_projection_is_deterministic():
    code = ("import json; from evaluations.terminal_bench import source_guard as g; "
            "print(json.dumps(g.tofu_server_config()))")
    outputs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                              env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2", "3")}
    assert len(outputs) == 1


def test_tofu_fetch_gate_refuses_an_answer_host_from_the_seeded_config(tmp_path):
    """Tofu's own skip-domains setting (search_bridge -> tofu_search gate) refuses the canary host."""
    base = tmp_path / "Tofu"
    (base / "data" / "config").mkdir(parents=True)
    (base / "data" / "config" / "server_config.json").write_text(json.dumps(guard.tofu_server_config()))
    repo = Path(harness.__file__).resolve().parents[2]
    # The checkout's venv resolves tofu_search through the LIVE tree's editable
    # install; put this tree's package first so the spec tests THIS source.
    program = (
        "import sys; sys.path.insert(0, %r); " % str(repo / "packages" / "tofu-search") +
        "sys.path.insert(0, %r); " % str(repo) +
        "from lib.config_dir import CONFIG_DIR; import lib; "
        "from lib.search.search_bridge import sync_search_config; from tofu_search.fetch.utils import _should_fetch; "
        f"sync_search_config(); print('CONFIG_DIR', CONFIG_DIR); print('skip', sorted(lib.SKIP_DOMAINS)); "
        f"refused = {{u: _should_fetch(u) for u in {list(guard.CANARY_BLOCKED_URLS)!r}}}; "
        f"allowed = _should_fetch({guard.CANARY_ALLOWED_URL!r}); "
        "print('refused', refused, 'allowed', allowed); "
        "raise SystemExit(0 if not any(refused.values()) and allowed else 1)")
    environment = {"TOFU_DATA_DIR": str(base), "HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    probe = subprocess.run([sys.executable, "-I", "-c", program], env=environment, cwd=tmp_path,
                           capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stdout[-2000:] + probe.stderr[-3000:]


def test_guard_check_program_refuses_and_allows_in_a_real_shell(tmp_path):
    log = tmp_path / "agent"
    log.mkdir()
    (log / guard.CLAUDE_CODE_HOOK_FILE).write_text(guard.claude_code_hook_script(), encoding="utf-8")
    program = guard.guard_check_program("claude-code", log_dir=str(log))
    subprocess.run(["bash", "-n"], input=program.encode(), check=True)
    probe = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "-> exit 2 (want 2)" in probe.stdout and "-> exit 0 (want 0)" in probe.stdout
    assert "proxy-canary" in probe.stdout


def test_guard_check_program_fails_when_the_guard_is_broken(tmp_path):
    log = tmp_path / "agent"
    log.mkdir()
    (log / guard.CLAUDE_CODE_HOOK_FILE).write_text("exit 0\n", encoding="utf-8")
    probe = subprocess.run(["bash", "-c", guard.guard_check_program("claude-code", log_dir=str(log))],
                           capture_output=True, text=True, check=False)
    assert probe.returncode == 1


def test_audit_ignores_the_guard_canary_denial_but_counts_agent_phase_ones(tmp_path):
    from evaluations.terminal_bench import audit

    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "egress.jsonl").write_text("\n".join([
        json.dumps({"phase": "setup", "decision": "deny", "method": "CONNECT", "host": "www.tbench.ai"}),
        json.dumps({"phase": "agent", "decision": "deny", "method": "CONNECT", "host": "marginlab.ai"}),
        json.dumps({"phase": "agent", "decision": "allow", "method": "CONNECT", "host": "github.com"})]))
    report = audit.audit_trial(trial, set())
    assert report["denied_egress"] == [{"phase": "agent", "method": "CONNECT", "host": "marginlab.ai", "url": None}]


def test_audit_reads_claude_code_transcripts_and_flags_answer_sources(tmp_path):
    from evaluations.terminal_bench import audit

    agent = tmp_path / "trial" / "agent"
    projects = agent / "sessions" / "projects" / "-app"
    projects.mkdir(parents=True)
    rows = [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "WebFetch",
         "input": {"url": guard.CANARY_BLOCKED_URLS[1], "prompt": "tests"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
             "content": "PreToolUse:WebFetch hook error: Blocked by the evaluation harness"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Bash",
             "input": {"command": "curl -s https://example.org/x"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_2",
             "content": [{"type": "text", "text": f"canary {harness.TB_CANARY_GUID}"}]}]}}]
    (projects / "session.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    report = audit.audit_trial(agent.parent, set())
    assert report["source_calls"][0]["tool"] == "WebFetch"
    assert report["canary_results"] == [{"call_id": "toolu_2", "tool": "Bash"}]


def test_vm_trial_runs_the_canary_before_the_agent_and_fails_closed(tmp_path, monkeypatch):
    from evaluations.terminal_bench import vm_trial

    spec = {"out_dir": str(tmp_path / "out"), "task_dir": str(tmp_path / "task"), "trial_key": "k", "trial_id": "t",
            "task_name": "x", "arm": "claude-code",
            "agent": {"kind": "claude-code", "artifacts": [], "log_files": {"claude-settings.json": "{}"},
                      "install_check": "true", "guard_check": "exit 1", "env": {"ANTHROPIC_MODEL": "m"}}}
    trial = vm_trial.Trial(spec)
    (tmp_path / "out" / "agent").mkdir(parents=True)
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        returncode = 1 if "guard_check" in " ".join(str(a) for a in args) or "exit 1" in " ".join(str(a) for a in args) else 0
        return type("P", (), {"returncode": returncode, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(vm_trial, "docker", fake_docker)
    with pytest.raises(vm_trial.InfraError) as failure:
        trial.install_agent()
    assert "guard check failed" in str(failure.value)
    assert trial.result["phases"]["install"]["guard_check_ok"] is False
