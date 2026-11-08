"""Offline contract tests for the Claude Code control arm and its Anthropic bridge."""
from __future__ import annotations

import json
import shlex
import subprocess
from evaluations.terminal_bench.harness import CLAUDE_CODE_EFFORT
from pathlib import Path

import pytest

from evaluations import claude_code_contract as contract
from evaluations.terminal_bench import anthropic_bridge as bridge
from evaluations.terminal_bench import harness, runner, source_guard as guard

pytestmark = pytest.mark.unit


def make_task(root: Path, name: str = "demo-task") -> Path:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "environment").mkdir()
    (task / "task.toml").write_text('[verifier]\ntimeout_sec = 900.0\n[agent]\ntimeout_sec = 1200.0\n'
                                    '[environment]\ndocker_image = "example/demo:1"\n', encoding="utf-8")
    (task / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    (task / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n", encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text("FROM debian\n", encoding="utf-8")
    return task


def agent(**overrides):
    return harness.claude_code_agent(
        artifact=harness.Artifact(name="claude-code", filename="claude-code.tar.gz", sha256="a" * 64,
                                  version=contract.CLAUDE_CODE_VERSION),
        files_url="http://relay:1/files", anthropic_base_url="http://bridge:2/arm/claude-code-x",
        model="glm-5.3-flash", instruction="Do the thing.\n", output_tokens=65536,
        source_guard=overrides.pop("source_guard", True))


def test_command_assembly_matches_the_upstream_adapter_and_runs_once():
    spec = agent()
    program = spec["program"]
    subprocess.run(["bash", "-n"], input=program.encode(), check=True)
    assert f"{harness.CLAUDE_CODE_HOME}/{contract.CLAUDE_BIN}" in program
    assert "--verbose --output-format=stream-json" in program
    assert "--permission-mode=bypassPermissions" in program
    assert shlex.join(["--effort", CLAUDE_CODE_EFFORT]) in program
    assert program.count("--print") == 1
    # Instruction on stdin, stream and stderr in files (no tee pipe a daemon could hold open).
    assert "< /logs/agent/instruction.md" in program
    assert f"> /logs/agent/{contract.GUEST_STREAM_FILE}" in program
    assert f"2> /logs/agent/{contract.GUEST_STDERR_FILE}" in program
    assert "tee" not in program and "--continue" not in program and "--resume" not in program
    # Every other flag of Harbor's adapter is absent unless the run needs it.
    assert spec["completion_marker"] == {"file": contract.GUEST_STREAM_FILE,
                                         "line_prefix": contract.RESULT_LINE_PREFIX}
    assert spec["artifacts"][0]["container_path"] == harness.CLAUDE_CODE_HOME
    assert spec["install_check"].endswith(f"{harness.CLAUDE_CODE_HOME}/{contract.CLAUDE_BIN} --version")
    with pytest.raises(ValueError):
        contract.guest_shell_command(home=harness.CLAUDE_CODE_HOME, effort="extreme")


def test_settings_env_and_guard_project_the_protocol():
    spec = agent()
    env = spec["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://bridge:2/arm/claude-code-x"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "relay-held"
    assert env["ANTHROPIC_MODEL"] == "glm-5.3-flash"
    assert all(env[name] == "glm-5.3-flash" for name in contract.MODEL_ALIAS_ENV)
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "65536"
    assert env["IS_SANDBOX"] == "1" and env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == contract.GUEST_CONFIG_DIR
    assert env["no_proxy"].startswith("bridge,")
    assert "ANTHROPIC_API_KEY" not in env and "real-key" not in json.dumps(spec)
    settings = json.loads(spec["log_files"][contract.GUEST_SETTINGS_FILE])
    assert settings["skipWebFetchPreflight"] is True
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash|WebFetch|WebSearch"
    assert guard.CLAUDE_CODE_HOOK_FILE in hook["hooks"][0]["command"]
    assert guard.CLAUDE_CODE_HOOK_FILE in spec["log_files"] and "guard_check" in spec
    # Unguarded runs carry no hook and no canary.
    unguarded = agent(source_guard=False)
    assert "guard_check" not in unguarded and guard.CLAUDE_CODE_HOOK_FILE not in unguarded["log_files"]
    assert json.loads(unguarded["log_files"][contract.GUEST_SETTINGS_FILE])["hooks"] == {}


def test_guard_is_opt_in_for_the_other_arms():
    for arm, builder, kwargs in (("tofu", harness.tofu_agent, {"timeout_sec": 60}),
                                 ("opencode", harness.opencode_agent, {}), ("pi", harness.pi_agent, {})):
        plain = builder(artifact=harness.Artifact(name=arm, filename="a", sha256="a" * 64, version="1"),
                        files_url="http://r/files", model_base_url="http://r/arm/x/v1", model="glm-5.3-flash",
                        instruction="x", context_tokens=262144, output_tokens=65536, **kwargs)
        assert "guard_check" not in plain
        assert not {"tofu-server-config.json", guard.PI_EXTENSION_FILE} & set(plain["log_files"])
        guarded = builder(artifact=harness.Artifact(name=arm, filename="a", sha256="a" * 64, version="1"),
                          files_url="http://r/files", model_base_url="http://r/arm/x/v1", model="glm-5.3-flash",
                          instruction="x", context_tokens=262144, output_tokens=65536, source_guard=True, **kwargs)
        assert "guard_check" in guarded
        if arm == "opencode":
            assert "permission" not in json.loads(plain["log_files"]["opencode.json"])
            assert "bash" in json.loads(guarded["log_files"]["opencode.json"])["permission"]
        if arm == "pi":
            assert "--extension" not in plain["program"]
            assert f"--extension /logs/agent/{guard.PI_EXTENSION_FILE}" in guarded["program"]
            subprocess.run(["bash", "-n"], input=guarded["program"].encode(), check=True)
        if arm == "tofu":
            assert guarded["program"].endswith(plain["program"])
            assert guard.TOFU_SERVER_CONFIG_FILE in guarded["log_files"]


def test_runner_routes_claude_code_to_the_bridge_and_others_to_the_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_857_TOKEN", "test-token")
    for name in ("tofu", "opencode", "pi", "claude-code"):
        make_task(tmp_path / "tasks", name)
    manifest = tmp_path / "artifacts.json"
    manifest.write_text(json.dumps({"artifacts": {name: {"filename": f"{name}.tar.gz", "sha256": "a" * 64,
                                                         "version": "1"}
                                                  for name in ("tofu", "opencode", "pi", "claude-code")}}))
    config = runner.RunConfig(work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["tofu"],
                              arms=["claude-code", "pi"], protocol="vendor-6h", model="glm-5.3-flash",
                              relay_url="http://relay:1", files_url="http://relay:1/files",
                              artifacts_manifest=manifest, endpoint="https://x/system/857", image="img",
                              branch="b", proxy="http://p", max_alive_min=540, max_attempts=3,
                              context_tokens=262144, poll_interval_sec=30, run_label="fullcc",
                              anthropic_url="http://bridge:2", source_guard=True)
    live = runner.Runner(config)
    task = live.tasks["tofu"]
    assert live.agent_for(task, "claude-code")["env"]["ANTHROPIC_BASE_URL"] == "http://bridge:2/arm/claude-code-fullcc"
    assert live.agent_for(task, "pi")["env"]["no_proxy"].startswith("relay,")
    spec = live.spec_for(task, "claude-code", 1)
    assert spec["egress"]["deny_hosts"] == list(guard.ANSWER_HOSTS)
    assert set(guard.SOURCE_REPOS) <= set(spec["egress"]["deny_url_substrings"])
    without_bridge = runner.RunConfig(**{**config.__dict__, "anthropic_url": None})
    with pytest.raises(ValueError, match="anthropic_url"):
        runner.Runner(without_bridge).agent_for(task, "claude-code")


def test_frozen_config_requires_the_bridge_and_defaults_old_runs_unguarded(tmp_path):
    from evaluations.terminal_bench.__main__ import build_parser, frozen_config

    parser = build_parser()
    args = parser.parse_args(["run", "--work-dir", str(tmp_path / "new"), "--concurrency", "1",
                              "--arms", "claude-code", "--tasks-dir", str(tmp_path), "--relay-url", "http://r",
                              "--artifacts-manifest", str(tmp_path / "m"), "--endpoint", "http://e/system/857",
                              "--image", "i", "--proxy", "p"])
    with pytest.raises(SystemExit, match="anthropic-url"):
        frozen_config(args)
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "run.json").write_text(json.dumps({"tasks": ["x"], "arms": ["tofu"]}))
    resumed = parser.parse_args(["run", "--work-dir", str(tmp_path / "old"), "--concurrency", "2"])
    assert frozen_config(resumed).get("source_guard") is None


# ---------------------------------------------------------------- request translation

REQUEST = {
    "model": "glm-5.3-flash", "max_tokens": 65536, "stream": True, "output_config": {"effort": "max"},
    "thinking": {"type": "adaptive"},
    "system": [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}],
    "tools": [{"name": "Bash", "description": "Run a command",
               "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}],
    "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
    "messages": [
        {"role": "user", "content": "Run echo hi."},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "I should use Bash.", "signature": "abc"},
            {"type": "text", "text": "Using Bash."},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo hi"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]},
            {"type": "text", "text": "Now report it."}]},
    ],
}


def test_translate_request_preserves_reasoning_tools_images_and_settings():
    outgoing, record = bridge.translate_request(REQUEST)
    assert outgoing["max_tokens"] == 65536 and outgoing["stream"] is True
    assert outgoing["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert outgoing["reasoning_effort"] == "max"
    assert outgoing["stream_options"] == {"include_usage": True}
    assert outgoing["tools"][0]["function"]["parameters"]["properties"]["command"]["type"] == "string"
    assert outgoing["parallel_tool_calls"] is False
    assert outgoing["messages"][0] == {"role": "system", "content": "You are Claude Code."}
    assert outgoing["messages"][1] == {"role": "user", "content": "Run echo hi."}
    assistant = outgoing["messages"][2]
    assert assistant["role"] == "assistant" and assistant["content"] == "Using Bash."
    assert assistant["reasoning_content"] == "I should use Bash."
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"command": "echo hi"}
    assert outgoing["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "hi"}
    trailing = outgoing["messages"][4]
    # The tool result's image and the user's own text form one user message after the tool message.
    assert trailing["role"] == "user"
    assert trailing["content"][0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    assert trailing["content"][1] == {"type": "text", "text": "Now report it."}
    assert outgoing["messages"][3]["content"] == "hi"
    assert len(outgoing["messages"]) == 5
    assert record["history"] == {"assistant_turns": 1, "assistant_with_reasoning": 1,
                                 "reasoning_chars": len("I should use Bash."),
                                 "images": 1, "documents": 0, "dropped_blocks": []}
    assert record["client"]["effort"] == "max" and record["sent"]["reasoning_effort"] == "max"
    # The deployment always thinks; a disabled request must not be forwarded as one.
    outgoing, _ = bridge.translate_request({**REQUEST, "thinking": {"type": "disabled"}})
    assert "thinking" not in outgoing


@pytest.mark.parametrize("level,expected", [("low", "low"), ("medium", "high"), ("high", "high"),
                                            ("xhigh", "max"), ("max", "max")])
def test_effort_maps_to_the_deployments_levels(level, expected):
    outgoing, _ = bridge.translate_request({**REQUEST, "output_config": {"effort": level}})
    assert outgoing["reasoning_effort"] == expected


def test_server_tools_are_dropped_with_a_note_not_a_failed_turn():
    outgoing, record = bridge.translate_request({**REQUEST, "tools": [
        {"type": "web_search_20250305", "name": "web_search"},
        REQUEST["tools"][0]]})
    assert record["history"]["server_tools_dropped"] == ["web_search_20250305"]
    assert [tool["function"]["name"] for tool in outgoing["tools"]] == ["Bash"]
    assert any("web_search_20250305" in message["content"] and "not available" in message["content"]
               for message in outgoing["messages"] if message["role"] == "system")


def test_documents_become_file_parts():
    document = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0"}}
    outgoing, record = bridge.translate_request({**REQUEST, "messages": [
        {"role": "user", "content": [document, {"type": "text", "text": "Summarize it."}]}]})
    parts = outgoing["messages"][-1]["content"]
    assert parts[0] == {"type": "file", "file": {"file_data": "data:application/pdf;base64,JVBERi0",
                                                 "filename": "document.pdf"}}
    assert parts[1] == {"type": "text", "text": "Summarize it."}
    assert record["history"]["documents"] == 1


# ---------------------------------------------------------------- response translation

def _events(translator, chunks):
    raw = translator.start()
    for chunk in chunks:
        raw += translator.feed(chunk)
    return (raw + translator.finish()).decode()


def _parse(raw):
    events = []
    for part in raw.split("\n\n"):
        if not part.strip():
            continue
        event, _, data = part.partition("\ndata: ")
        events.append((event[len("event: "):], json.loads(data)))
    return events


def _chunk(delta=None, finish=None, usage=None):
    chunk = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    return chunk


def test_stream_translation_produces_anthropic_blocks_and_usage():
    translator = bridge.StreamTranslator("glm-5.3-flash")
    # The deployment sends reasoning, then text, then each tool call whole, and usage on the finish chunk.
    chunks = [
        _chunk({"role": "assistant", "reasoning_content": "Think "}),
        _chunk({"reasoning_content": "more."}),
        _chunk({"content": "Running."}),
        _chunk({"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "Bash", "arguments": '{"command"'}},
            {"index": 1, "id": "call_2", "type": "function", "function": {"name": "Bash", "arguments": '{"command"'}},
        ]}),  # both tool calls arrive whole in one chunk
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": ': "echo a"}'}}]}, finish="tool_calls",
               usage={"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 80}}),
    ]
    raw = _events(translator, chunks)
    events = _parse(raw)
    names = [name for name, _ in events]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    # thinking block (deltas + signature), text block, then two tool_use blocks in index order.
    assert [name for name in names if name == "content_block_start"].count("content_block_start") == 4
    assert names.index("content_block_stop") < names.index("message_delta")
    assert names.count("content_block_stop") == 4
    starts = [data for name, data in events if name == "content_block_start"]
    assert [block["content_block"]["type"] for block in starts] == ["thinking", "text", "tool_use", "tool_use"]
    assert starts[2]["content_block"] == {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}
    arguments = [data for name, data in events if name == "content_block_delta"
                 and data["delta"]["type"] == "input_json_delta"]
    assert [data["index"] for data in arguments] == [2, 2, 3]
    assert "".join(data["delta"]["partial_json"] for data in arguments if data["index"] == 2) == '{"command": "echo a"}'
    thinking = [data for name, data in events if name == "content_block_delta"
                and data["delta"]["type"] == "thinking_delta"]
    assert "".join(data["delta"]["thinking"] for data in thinking) == "Think more."
    assert any(data["delta"]["type"] == "signature_delta" and data["delta"]["signature"]
               for name, data in events if name == "content_block_delta")
    final = events[-2][1]
    assert final["delta"] == {"stop_reason": "tool_use", "stop_sequence": None}
    assert final["usage"] == {"input_tokens": 20, "cache_read_input_tokens": 80,
                              "cache_creation_input_tokens": 0, "output_tokens": 20}


def test_stream_translation_reports_max_tokens_and_usage_on_the_finish_chunk():
    translator = bridge.StreamTranslator("glm-5.3-flash")
    raw = _events(translator, [
        {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 65536}},
    ])
    assert _parse(raw)[-2][1]["delta"]["stop_reason"] == "max_tokens"
    assert _parse(raw)[-2][1]["usage"]["output_tokens"] == 65536


def test_non_streaming_response_translation():
    message = bridge.translate_response({"id": "abc", "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                                         "choices": [{"finish_reason": "stop", "message": {
                                             "role": "assistant", "content": "done",
                                             "reasoning_content": "thought"}}]}, "glm-5.3-flash")
    assert message["stop_reason"] == "end_turn" and message["role"] == "assistant"
    assert [block["type"] for block in message["content"]] == ["thinking", "text"]
    assert message["content"][0]["thinking"] == "thought"
    assert message["usage"]["output_tokens"] == 3


# ---------------------------------------------------------------- end to end against a fake relay

def test_bridge_end_to_end_with_a_fake_relay(tmp_path):
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp import web
    import asyncio
    import socket

    calls = []
    finish = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    async def chat(request):
        body = await request.json()
        calls.append(body)
        if body.get("model") == "rate-limited":
            return web.json_response({"error": {"message": "no live key", "type": "upstream_error"}}, status=429)
        if not body["stream"]:
            return web.json_response({"id": "x", "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "done", "reasoning_content": "thought"}}],
                                      "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        lines = []
        for delta in ({"role": "assistant", "reasoning_content": "think"}, {"content": "done"}):
            lines.append("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}))
        lines.append("data: " + json.dumps(finish))
        lines.append("data: [DONE]")
        return web.Response(status=200, content_type="text/event-stream",
                            body=("\n\n".join(lines) + "\n\n").encode())

    async def exercise():
        app = web.Application()
        app.router.add_post("/arm/{arm}/v1/chat/completions", chat)
        relay = web.AppRunner(app, access_log=None)
        await relay.setup()
        port = socket.socket()
        port.bind(("127.0.0.1", 0))
        relay_port = port.getsockname()[1]
        port.close()
        await web.TCPSite(relay, "127.0.0.1", relay_port).start()
        args = type("Args", (), {"relay_url": f"http://127.0.0.1:{relay_port}", "log": str(tmp_path / "bridge.jsonl"),
                                 "read_timeout": 30.0, "model": "glm-5.3-flash"})()
        gateway = web.AppRunner(bridge.build_app(bridge.Bridge(args)), access_log=None)
        await gateway.setup()
        bridge_socket = socket.socket()
        bridge_socket.bind(("127.0.0.1", 0))
        bridge_port = bridge_socket.getsockname()[1]
        bridge_socket.close()
        await web.TCPSite(gateway, "127.0.0.1", bridge_port).start()
        url = f"http://127.0.0.1:{bridge_port}/arm/claude-code-test/v1/messages"
        try:
            async with aiohttp.ClientSession() as client:
                # Streaming, with the arm label propagated to the relay.
                async with client.post(url, json={**REQUEST, "messages": [{"role": "user", "content": "hi"}]}) as reply:
                    assert reply.status == 200
                    raw = (await reply.text()).split("\n\n")
                events = [(part.split("\ndata: ", 1)[0], part.split("\ndata: ", 1)[1]) for part in raw if part]
                assert events[0][0] == "event: message_start" and events[-1][0] == "event: message_stop"
                assert any("thinking_delta" in data and "think" in data for _, data in events)
                assert any("stop_reason" in data and "end_turn" in data for _, data in events)
                # Non-streaming.
                async with client.post(url, json={**REQUEST, "stream": False,
                                                  "messages": [{"role": "user", "content": "hi"}]}) as reply:
                    message = await reply.json()
                assert message["stop_reason"] == "end_turn"
                assert [block["type"] for block in message["content"]] == ["thinking", "text"]
                # Upstream rate limits pass through with an Anthropic error body.
                async with client.post(url, json={**REQUEST, "stream": False, "model": "rate-limited",
                                                  "messages": [{"role": "user", "content": "hi"}]}) as reply:
                    assert reply.status == 429
                    assert (await reply.json())["error"]["type"] == "rate_limit_error"
                # count_tokens has no counterpart; it must not become a fabricated number.
                async with client.post(url + "/count_tokens", json={"model": "glm-5.3-flash"}) as reply:
                    assert reply.status == 404 and (await reply.json())["error"]["type"] == "not_found_error"
        finally:
            await gateway.cleanup()
            await relay.cleanup()

    asyncio.run(exercise())
    assert [call["max_tokens"] for call in calls] == [65536, 65536, 65536]
    assert all(call["reasoning_effort"] == "max" for call in calls)
    records = [json.loads(line) for line in (tmp_path / "bridge.jsonl").read_text().splitlines()]
    assert [record["arm"] for record in records if record["kind"] == "messages"] == ["claude-code-test"] * 3
    assert any(record["kind"] == "unsupported" and record["path"].endswith("count_tokens") for record in records)
    assert all("usage" in record for record in records if record["kind"] == "messages" and record["status"] == 200)


def test_registry_pins_match_the_downloaded_runtime_shape():
    assert contract.CLAUDE_CODE_VERSION == "2.1.207"
    for package, integrity in contract.CLAUDE_CODE_TARBALL_INTEGRITY.items():
        assert integrity.startswith("sha512-")
        assert contract.tarball_url(package).endswith(f"-{contract.CLAUDE_CODE_VERSION}.tgz")
    with pytest.raises(SystemExit, match="differs from the registry pin"):
        contract.check_integrity(b"not the real tarball", list(contract.CLAUDE_CODE_TARBALL_INTEGRITY.values())[0])
