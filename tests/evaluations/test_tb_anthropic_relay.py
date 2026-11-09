"""Claude Opus 5.5 on FRIDAY's Anthropic face: both arms, one relay, per-trial evidence.

The cache comparison needs four guarantees that the GLM chat face never asked
for: the Tofu Connection speaks Anthropic Messages to FRIDAY's own wire model
under FRIDAY's provider identity, Pi drives its built-in Opus 5.5 definition at
the requested effort, every request is attributed to its trial, and the relay
pins only the model while recording what decides prompt-cache reuse.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="TB relay runtime dependency")

from evaluations import pi_contract  # noqa: E402
from evaluations.terminal_bench import harness, relay  # noqa: E402

pytestmark = pytest.mark.unit

MODEL = "claude-opus-5.5"


def _args(tmp_path):
    return SimpleNamespace(
        origin="unused", provider="unused", key_suffixes=None, files_dir=None,
        log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30,
        model="unused", anthropic_model="kunlunbedrock.claude-opus-5.5-m17",
        temperature=None, top_p=None, max_tokens=None, reasoning_effort=None,
        preserve_pi_effort_markers=False)


def _body():
    return {
        "model": "claude-opus-5-5", "stream": True, "max_tokens": 128000,
        "system": [{"type": "text", "text": "instructions", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "first"},
                     {"role": "assistant", "content": "work"},
                     {"role": "user", "content": [{"type": "text", "text": "more",
                                                  "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}],
        "tools": [{"name": "run_command", "description": "run", "input_schema": {"type": "object"}},
                  {"name": "read_files", "description": "read", "input_schema": {"type": "object"},
                   "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "medium"},
    }


def test_opus_tofu_connection_uses_the_friday_anthropic_face_and_provider():
    doc = harness.tofu_model_routing(base_url="http://relay/anthropic", model=MODEL,
                                     context_tokens=1_000_000, output_tokens=128_000)["model_routing"]
    connection, deployment = doc["connections"][0], doc["deployments"][0]
    assert connection["protocol"] == "anthropic"
    assert connection["inference_profile"]["features"] == {"cache_affinity_mt_session_id": "supported"}
    assert deployment["wire_model_id"] == "kunlunbedrock.claude-opus-5.5-m17"
    # FRIDAY's provider id selects its Claude cache multipliers (reads 0.1x),
    # which is what the run's compaction economics must use.
    assert doc["providers"][0]["provider_id"] == "example-corp"
    assert "vision" in doc["offerings"][0]["capabilities"]
    # No hand-written reasoning profile: the catalog's official one is used.
    assert "inference_profile" not in deployment


def test_opus_tofu_and_pi_are_asked_for_the_same_effort_and_output_limit():
    protocol = harness.PROTOCOLS["claude-6h-medium"]
    assert (protocol.reasoning_effort, protocol.agent_timeout_sec, protocol.max_tokens) == ("medium", 21600, 128000)
    assert protocol.temperature is None and protocol.top_p is None
    artifact = harness.Artifact(name="x", filename="x.tar.gz", sha256="a" * 64, version="1")
    url = "http://relay/arm/a/task/t/anthropic"
    tofu = harness.tofu_agent(artifact=artifact, files_url="http://r/files", model_base_url=url, model=MODEL,
                              timeout_sec=protocol.agent_timeout_sec, instruction="do it",
                              context_tokens=1_000_000, output_tokens=protocol.max_tokens,
                              thinking=protocol.reasoning_effort)
    pi = harness.pi_agent(artifact=artifact, files_url="http://r/files", model_base_url=url, model=MODEL,
                          instruction="do it", context_tokens=1_000_000, output_tokens=protocol.max_tokens,
                          thinking=protocol.reasoning_effort)
    assert "--thinking medium" in tofu["program"] and pi["thinking"] == "medium"
    assert "--thinking medium" in pi["program"]
    assert "--provider anthropic --model claude-opus-5-5" in pi["program"]
    overrides = json.loads(pi["log_files"][pi_contract.GUEST_MODELS_FILE])["providers"]["anthropic"]
    assert overrides["baseUrl"] == url
    assert overrides["modelOverrides"] == {"claude-opus-5-5": {"contextWindow": 1_000_000, "maxTokens": 128000}}
    assert json.loads(tofu["env"]["TOFU_AGENT_MODEL_ROUTING"])["model_routing"]["connections"][0]["base_url"] == url


def test_opus_runs_use_their_production_context_window_and_per_task_urls(tmp_path, monkeypatch):
    from evaluations.terminal_bench import __main__ as cli
    from evaluations.terminal_bench import runner

    monkeypatch.setattr(cli, "verify_task_identity",
                        lambda tasks_dir, names: {"dataset_commit": "test", "checked": len(names),
                                                  "mismatched": []})

    task_dir = tmp_path / "tasks" / "a-task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        '[environment]\ndocker_image = "img"\n[agent]\ntimeout_sec = 60\n'
        '[verifier]\ntimeout_sec = 60\n')
    (task_dir / "instruction.md").write_text("do it\n")
    (task_dir / "tests" / "test.sh").write_text("true\n")
    manifest = tmp_path / "artifacts.json"
    manifest.write_text(json.dumps({"artifacts": {
        name: {"filename": f"{name}.tar.gz", "sha256": "a" * 64, "version": "1"}
        for name in ("tofu", "pi")}}))
    args = cli.build_parser().parse_args(["run", "--work-dir", str(tmp_path / "run"), "--concurrency", "1",
                                      "--tasks-dir", str(tmp_path / "tasks"), "--tasks", "a-task",
                                      "--arms", "tofu", "--protocol", "claude-6h-medium", "--model", MODEL,
                                      "--relay-url", "http://relay:1", "--artifacts-manifest", str(manifest),
                                      "--endpoint", "https://x/system/857", "--image", "img",
                                      "--proxy", "http://p"])
    config = cli.frozen_config(args)
    assert config["context_tokens"] == 1_000_000
    assert config["protocol"] == "claude-6h-medium"
    live = runner.Runner(runner.RunConfig(
        work_dir=tmp_path / "work", tasks_dir=tmp_path / "tasks", tasks=["a-task"], arms=["tofu"],
        protocol="claude-6h-medium", model=MODEL, relay_url="http://relay:1",
        files_url="http://relay:1/files", artifacts_manifest=manifest,
        endpoint="https://x/system/857", image="img", branch="b", proxy="http://p",
        max_alive_min=540, max_attempts=3, context_tokens=config["context_tokens"],
        poll_interval_sec=30, run_label="opusr1", source_guard=False))
    url = live.agent_for(live.tasks["a-task"], "tofu")["env"]["TOFU_AGENT_MODEL_ROUTING"]
    assert json.loads(url)["model_routing"]["connections"][0]["base_url"] == \
        "http://relay:1/arm/tofu-opusr1/task/a-task/anthropic"
    pi_url = live.agent_for(live.tasks["a-task"], "pi")["log_files"][pi_contract.GUEST_MODELS_FILE]
    assert json.loads(pi_url)["providers"]["anthropic"]["baseUrl"] == \
        "http://relay:1/arm/pi-opusr1/task/a-task/anthropic"


def test_relay_anthropic_pins_only_model_and_records_cache_evidence(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    captured = []
    usage = {"input_tokens": 5, "output_tokens": 0,
             "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 50},
             "cache_creation_input_tokens": 150, "cache_read_input_tokens": 1000}
    stream = (
        "event: message_start\ndata: " + json.dumps({
            "type": "message_start", "message": {"id": "msg_1", "model": "served", "usage": usage}}) + "\n\n"
        "event: content_block_start\ndata: " + json.dumps({
            "type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}) + "\n\n"
        "event: message_delta\ndata: " + json.dumps({
            "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 7}}) + "\n\n")

    async def upstream(request):
        captured.append({"headers": dict(request.headers), "body": await request.json()})
        return web.Response(text=stream, content_type="text/event-stream")

    monkeypatch.setattr(relay, "load_keys", lambda *_: [("first", "upstream-first")])
    live = relay.Relay(_args(tmp_path))

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/messages", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "ANTHROPIC_UPSTREAM", str(server.make_url("/messages")))
            async with TestClient(TestServer(relay.build_app(live))) as client:
                headers = {"anthropic-version": "2023-06-01", "anthropic-beta": "flag-a,flag-b",
                           "Mt-Session-Id": "trial-session", "M-TraceId": "trace-1",
                           "x-api-key": "sandbox-placeholder", "Authorization": "Bearer placeholder",
                           "X-Unrelated": "omit"}
                async with client.post("/arm/pi-r1/task/a-task/anthropic/v1/messages",
                                       json=_body(), headers=headers) as response:
                    assert response.status == 200
                    assert await response.text() == stream

    asyncio.run(exercise())
    assert len(captured) == 1
    sent, headers = captured[0]["body"], {k.lower(): v for k, v in captured[0]["headers"].items()}
    expected = _body()
    expected["model"] = "kunlunbedrock.claude-opus-5.5-m17"
    assert sent == expected  # no effort markers: nothing but the model changed
    assert headers["x-api-key"] == "upstream-first"
    assert headers["authorization"] == "Bearer upstream-first"
    assert headers["mt-session-id"] == "trial-session"
    assert headers["anthropic-beta"] == "flag-a,flag-b"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["m-traceid"] == "trace-1"
    assert "x-unrelated" not in headers
    record = json.loads((tmp_path / "requests.jsonl").read_text())
    assert record["kind"] == "messages" and record["arm"] == "pi-r1" and record["task"] == "a-task"
    assert record["breakpoints"] == ["tools[1]:5m", "system[0]:5m", "messages[2][0]:1h"]
    assert record["tool_name_bytes"] == len("run_command") + 1 + len("read_files") + 1
    assert record["usage_start"] == usage and record["usage_delta"] == {"output_tokens": 7}
    assert record["stop_reason"] == "end_turn" and record["response_model"] == "served"
    assert record["blocks"] == {"thinking": 1}
    assert record["cache_affinity"] == "Mt-Session-Id"
    assert record["orig"]["output_config"] == {"effort": "medium"}
    text = (tmp_path / "requests.jsonl").read_text()
    # No prompt text, credential or affinity value reaches the log: every
    # top-level string field is a label, a count, a status or a digest.
    assert "upstream-first" not in text and "placeholder" not in text
    assert "trial-session" not in text
    assert all(len(digest) == 16 for digest in record["h_messages"]) and len(record["h_messages"]) == 3
    leaked = [key for key, value in record.items()
              if isinstance(value, str) and any(marker in value for marker in ("instructions", "more", "first"))]
    assert leaked == ["key"]  # the upstream key's tail is named "first" by the fake pool


def test_relay_folds_pis_rejected_effort_markers_into_the_requested_effort():
    body = _body()
    body["output_config"] = {"effort": "high"}  # Pi's constant top-level value
    body["messages"] = [{"role": "user", "content": "x"},
                        {"role": "system", "content": [], "output_config": {"effort": "high"}},
                        {"role": "assistant", "content": "y"},
                        {"role": "system", "content": [], "output_config": {"effort": "medium"}}]
    original = json.loads(json.dumps(body))
    folded = relay.compact_pi_effort_markers(body)
    assert folded == {"markers": 2, "effort": "medium", "top_level_before": "high"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["messages"] == [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    # Everything else Pi sends (thinking binding, tools, system, cache marks)
    # is untouched.
    assert body["thinking"] == original["thinking"] and body["tools"] == original["tools"]
    assert body["system"] == original["system"]
    # A request without markers is left alone, which is what Tofu sends.
    plain = _body()
    assert relay.compact_pi_effort_markers(plain) == {"markers": 0}
    assert plain["messages"] == _body()["messages"]
    assert plain["output_config"] == {"effort": "medium"}


def test_relay_anthropic_digests_ignore_cache_control_but_see_history_rewrites(tmp_path):
    body = _body()
    facts = relay.anthropic_cache_facts(body, None)
    edited = copy.deepcopy(body)
    edited["system"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    changed = relay.anthropic_cache_facts(edited, None)
    assert facts["h_system"] == changed["h_system"]
    assert "system[0]:1h" in changed["breakpoints"]
    edited["messages"][1]["content"] = "rewritten history"
    rewritten = relay.anthropic_cache_facts(edited, None)
    assert rewritten["h_messages"][:1] == facts["h_messages"][:1]
    assert rewritten["h_messages"][1] != facts["h_messages"][1]
    # Pi's managed effort rides mid-conversation system messages; the last one
    # is the effort the response was requested with.
    body["messages"] = [{"role": "user", "content": "x"},
                        {"role": "system", "content": [], "output_config": {"effort": "high"}},
                        {"role": "assistant", "content": "y"},
                        {"role": "system", "content": [], "output_config": {"effort": "medium"}}]
    settings = relay.anthropic_settings(body)
    assert settings["effort_markers"] == 2 and settings["last_effort_marker"] == "medium"


def test_relay_anthropic_stream_facts_survive_split_frames():
    events = [{"type": "message_start", "message": {"id": "m", "model": "x", "usage": {"input_tokens": 1}}},
              {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}]
    blob = b"".join(("event: e\ndata: " + json.dumps(event) + "\n\n").encode() for event in events)
    facts = relay.AnthropicStreamFacts()
    for index in range(len(blob)):
        facts.feed(blob[index:index + 1])
    facts.close()
    assert facts.facts["usage_start"] == {"input_tokens": 1}
    assert facts.facts["usage_delta"] == {"output_tokens": 3}
    assert facts.facts["stop_reason"] == "tool_use"
