"""Contract tests for the native runner's Pi arm (``evaluations.pi_contract`` + ``harness.pi_agent``).

Pure and helper tests run offline (the helper tests need ``node`` on PATH).
``test_pinned_pi_runs_against_a_relay_stub`` drives the real pinned Pi through
a local OpenAI-compatible stub when ``TB_NATIVE_PI_RUNTIME`` names an extracted
``python -m evaluations.pi_contract build-runtime`` tree.
"""
from __future__ import annotations

import http.server
import json
import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from evaluations import pi_contract
from evaluations.terminal_bench import audit, harness

pytestmark = pytest.mark.unit

MODEL = "glm-5.3-flash"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="the Pi events helper needs node on PATH")


def pi_spec(base_url: str = "http://r/arm/pi/v1", instruction: str = "Do the thing.\n") -> dict:
    artifact = harness.Artifact(name="pi", filename="pi.tar.gz", sha256="a" * 64, version="pi 1.4.0")
    return harness.pi_agent(artifact=artifact, files_url="http://r/files", model_base_url=base_url, model=MODEL,
                            instruction=instruction, context_tokens=262144, output_tokens=65536)


def write_log_files(spec: dict, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    for name, text in spec["log_files"].items():
        (log_dir / name).write_text(text, encoding="utf-8")


def test_pi_spec_routes_the_catalog_model_through_the_relay():
    spec = pi_spec(base_url="http://r/arm/pi/v1/")
    config = json.loads(spec["log_files"][pi_contract.GUEST_MODELS_FILE])
    assert config == {"providers": {"zai": {
        "baseUrl": "http://r/arm/pi/v1", "apiKey": "relay-held",
        "modelOverrides": {MODEL: {"contextWindow": 262144, "maxTokens": 65536}}}}}
    assert spec["kind"] == "pi" and spec["thinking"] == harness.PI_THINKING_LEVEL == "max"
    assert spec["artifacts"] == [{"name": "pi", "url": "http://r/files/pi.tar.gz", "sha256": "a" * 64,
                                  "container_path": harness.PI_HOME}]
    assert spec["env"]["PI_CODING_AGENT_DIR"] == "/logs/agent/pi/agent"
    assert {spec["env"][key] for key in ("PI_OFFLINE", "PI_SKIP_VERSION_CHECK")} == {"1"}
    assert spec["env"]["no_proxy"].startswith("r,") and "PATH" not in spec["env"]
    assert spec["log_files"]["instruction.md"] == "Do the thing.\n"
    assert spec["install_check"].endswith(f"{harness.PI_HOME}/{pi_contract.PI_CLI} --version")


def test_pi_rejects_what_it_cannot_run_as_specified():
    with pytest.raises(ValueError, match="'@'"):
        pi_spec(instruction="@notes.md describes the task")
    with pytest.raises(ValueError, match="catalog provider"):
        pi_contract.guest_models_config(base_url="http://r/v1", model="gpt-x", context_tokens=1, output_tokens=1)
    with pytest.raises(ValueError, match="thinking level"):
        pi_contract.guest_shell_command(home="/h", model=MODEL, thinking="extreme")


def test_harbor_path_keeps_its_explicit_thinking_contract():
    assert pi_contract.pi_agent_kwargs(version="1.4.0", thinking="high", models=("zai/glm-5.3",)) == {
        "version": "1.4.0", "thinking": "high"}
    with pytest.raises(ValueError):
        pi_contract.pi_agent_kwargs(version="1.4.0", thinking="max", models=("zai/glm-5.3",))


def test_guest_program_is_valid_bash_with_harbor_flags():
    program = pi_spec()["program"]
    subprocess.run(["bash", "-n", "-c", program], check=True)
    for flags in (["--print", "--mode", "json"], ["--session-dir", "/logs/agent/pi/sessions"],
                  ["--provider", "zai", "--model", MODEL, "--thinking", "max"]):
        assert shlex.join(flags) in program
    assert '-- "$(< /logs/agent/instruction.md)"' in program
    assert "--continue" not in program


# A ``node`` that scripts Pi's stop reason and exit code per call and runs the
# real events helper for everything else.
FAKE_NODE = r'''#!/bin/bash
if [[ "$1" == *cli.js ]]; then
  n=$(( $(cat "$FAKE_PI_DIR/count" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$FAKE_PI_DIR/count"
  printf '%s\0' "$@" > "$FAKE_PI_DIR/call-$n.args"
  printf '{"type":"message_update","message":{"role":"assistant"}}\n'
  printf '{"type":"message_end","message":{"role":"assistant","stopReason":"%s"}}\n' "$(sed -n "${n}p" "$FAKE_PI_DIR/stops")"
  exit "$(sed -n "${n}p" "$FAKE_PI_DIR/codes")"
fi
exec "$FAKE_REAL_NODE" "$@"
'''


@needs_node
@pytest.mark.parametrize(("stop", "code"), [("length", 0), ("stop", 0), ("stop", 3)])
def test_guest_program_runs_pi_once_and_keeps_its_exit_code(tmp_path, stop, code):
    home, log_dir, fake = tmp_path / "runtime", tmp_path / "logs", tmp_path / "fake"
    (home / "node" / "bin").mkdir(parents=True)
    node = home / "node" / "bin" / "node"
    node.write_text(FAKE_NODE)
    node.chmod(0o755)
    fake.mkdir()
    (fake / "stops").write_text(stop + "\n")
    (fake / "codes").write_text(f"{code}\n")
    write_log_files(pi_spec(), log_dir)
    program = pi_contract.guest_shell_command(home=str(home), model=MODEL, thinking="max",
                                              log_dir=str(log_dir))
    env = {"PATH": os.environ["PATH"], "FAKE_PI_DIR": str(fake), "FAKE_REAL_NODE": NODE}
    done = subprocess.run(["bash", "-c", program], env=env, cwd=tmp_path, capture_output=True, timeout=60)
    assert done.returncode == code, done.stderr
    # An output-limit stop ends the run exactly like any other ending.
    assert int((fake / "count").read_text()) == 1
    call = (fake / "call-1.args").read_bytes().decode().split("\0")[:-1]
    assert call[1:9] == ["--print", "--mode", "json", "--session-dir", f"{log_dir}/pi/sessions",
                         "--provider", "zai", "--model"]
    assert call[-2:] == ["--", "Do the thing."] and "--continue" not in call
    events = (log_dir / pi_contract.GUEST_EVENTS_FILE).read_text().splitlines()
    assert [json.loads(line)["type"] for line in events] == ["message_end"]
    assert (log_dir / "pi" / "agent" / "models.json").read_text() == (log_dir / "pi-models.json").read_text()


@needs_node
def test_events_helper_drops_stream_updates_across_pipe_chunks(tmp_path):
    helper = tmp_path / pi_contract.GUEST_HELPER_FILE
    helper.write_text(pi_contract.EVENTS_HELPER)
    kept = [b'{"type":"session","id":"s"}\n', b'{"type":"message_end","message":{"role":"assistant"}}\n']
    update = b'{"type":"message_update","delta":"' + b"x" * 300_000 + b'"}\n'
    stream = kept[0] + update + kept[1] + update + b'{"type":"agent_end"}'
    done = subprocess.run([NODE, str(helper), "filter"], input=stream, capture_output=True, timeout=60)
    assert done.returncode == 0 and done.stdout == kept[0] + kept[1] + b'{"type":"agent_end"}'


def session_line(message: dict) -> str:
    return json.dumps({"type": "message", "id": "m", "parentId": None, "message": message})


def test_audit_reads_pi_sessions_and_falls_back_to_the_event_stream(tmp_path):
    call = {"role": "assistant", "stopReason": "toolUse", "content": [
        {"type": "thinking", "thinking": "look it up"},
        {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "curl -s https://example.org/a"}}]}
    result = {"role": "toolResult", "toolCallId": "c1", "toolName": "bash", "isError": False,
              "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]}
    stream_dir = tmp_path / "stream"
    stream_dir.mkdir()
    (stream_dir / "pi.jsonl").write_text("\n".join(
        json.dumps({"type": "message_end", "message": message}) for message in (call, result)) + "\n")
    sessions = tmp_path / "session" / "pi" / "sessions" / "--work--"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text("\n".join([json.dumps({"type": "session"}), session_line(call),
                                                 session_line(result)]) + "\n")
    for agent_dir in (stream_dir, tmp_path / "session"):
        events = audit.tool_events(agent_dir)
        assert [event[:3] for event in events] == [("call", "c1", "bash"), ("result", "c1", "bash")]
        assert "curl -s https://example.org/a" in events[0][3] and events[1][3] == "line one\nline two"
        assert audit.is_network_call("bash", events[0][3])


def sse(step: dict) -> bytes:
    head = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0, "model": MODEL}

    def chunk(delta, finish=None):
        return {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    chunks = [chunk({"role": "assistant", "reasoning_content": "Thinking it over."})]
    if "command" in step:
        call = {"index": 0, "id": "call_1", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": step["command"]})}}
        chunks += [chunk({"tool_calls": [call]}), chunk({}, "tool_calls")]
    else:
        chunks += [chunk({"content": step["text"]}), chunk({}, step["finish"])]
    chunks.append({**head, "choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40,
                                                    "total_tokens": 940}})
    return b"".join(b"data: " + json.dumps(item).encode() + b"\n\n" for item in chunks) + b"data: [DONE]\n\n"


@pytest.mark.skipif(not os.environ.get("TB_NATIVE_PI_RUNTIME"),
                    reason="set TB_NATIVE_PI_RUNTIME to an extracted Pi runtime tree")
def test_pinned_pi_runs_against_a_relay_stub(tmp_path):
    script = [{"command": "printf hello > out.txt"},
              {"text": "The file is written, and the summary is", "finish": "length"}]
    seen: list[dict] = []

    class Relay(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "body": body})
            payload = sse(script[min(len(seen), len(script)) - 1])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        spec = pi_spec(base_url=f"http://127.0.0.1:{server.server_address[1]}/arm/pi-x/v1",
                       instruction="Write hello into out.txt.\n")
        log_dir, work, home = tmp_path / "logs", tmp_path / "work", tmp_path / "home"
        work.mkdir()
        home.mkdir()
        write_log_files(spec, log_dir)
        program = pi_contract.guest_shell_command(home=os.environ["TB_NATIVE_PI_RUNTIME"], model=MODEL,
                                                  thinking=spec["thinking"], log_dir=str(log_dir))
        env = {**spec["env"], "PI_CODING_AGENT_DIR": f"{log_dir}/pi/agent", "HOME": str(home),
               "PATH": "/usr/local/bin:/usr/bin:/bin"}
        done = subprocess.run(["bash", "-c", program], env=env, cwd=work, capture_output=True, timeout=300)
    finally:
        server.shutdown()
    stderr = (log_dir / pi_contract.GUEST_STDERR_FILE).read_text(errors="replace")
    assert done.returncode == 0, (done.stdout, done.stderr, stderr)
    assert (work / "out.txt").read_text() == "hello"
    # Pi itself ends the run at the output-limit stop; nothing resumes it.
    assert len(seen) == 2 and {item["path"] for item in seen} == {"/arm/pi-x/v1/chat/completions"}
    first = seen[0]["body"]
    assert first["model"] == MODEL and first["stream"] is True
    assert first["reasoning_effort"] == "max"
    assert first["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert first["tool_stream"] is True
    assert {"bash", "read", "edit", "write"} <= {tool["function"]["name"] for tool in first["tools"]}
    stream = (log_dir / pi_contract.GUEST_EVENTS_FILE).read_text().splitlines()
    assert stream and not [line for line in stream if '"type":"message_update"' in line]
    events = audit.tool_events(log_dir)
    assert ("call", "call_1", "bash") in [event[:3] for event in events]
    assert ("result", "call_1", "bash") in [event[:3] for event in events]
    print(json.dumps({key: value for key, value in first.items() if key not in ("messages", "tools")}, indent=1))
