"""Gateway affinity crosses the TB relay without changing the model prompt."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from evaluations.terminal_bench import harness

pytestmark = pytest.mark.unit


def test_tofu_evaluation_connection_declares_gateway_affinity():
    from lib.llm.cache_affinity import cache_affinity_headers, cache_affinity_key

    envelope = harness.tofu_model_routing(
        base_url="http://relay/arm/tofu/v1", model="glm-5.3-flash",
        context_tokens=262144, output_tokens=65536)
    connection = envelope["model_routing"]["connections"][0]
    key = cache_affinity_key({"_conv_id": "trial-conversation"})
    assert cache_affinity_headers(
        connection.get("inference_profile"), protocol="openai", oauth="", key=key,
    ) == {"Mt-Session-Id": key}


def test_gateway_affinity_changes_only_headers_on_glm_chat_requests():
    from lib.llm._sse_core import prepare_request

    body = {"model": "glm-5.3-flash", "stream": True, "max_tokens": 16,
            "_conv_id": "trial-conversation",
            "messages": [{"role": "system", "content": "Stable instructions."},
                         {"role": "user", "content": "Solve the task."}]}
    plain = prepare_request(copy.deepcopy(body), api_protocol="openai", api_key="test-key",
                            base_url="https://api.openai.com/v1")
    body["_route_inference_profile"] = {
        "features": {"cache_affinity_mt_session_id": "supported"}}
    bound = prepare_request(body, api_protocol="openai", api_key="test-key",
                            base_url="https://api.openai.com/v1")
    assert "Mt-Session-Id" not in plain.hdrs
    assert bound.hdrs["Mt-Session-Id"]
    assert bound.body.wire_body() == plain.body.wire_body()


@pytest.mark.parametrize("arm", ["tofu", "opencode", "pi"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("session_id", [None, "trial-session"])
def test_relay_preserves_affinity_across_key_retry_without_editing_prompt(
        tmp_path, monkeypatch, arm, stream, session_id):
    pytest.importorskip("aiohttp", reason="TB relay runtime dependency")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from evaluations.terminal_bench import relay

    captured = []
    usage = {"prompt_tokens": 1024, "completion_tokens": 1,
             "prompt_tokens_details": {"cached_tokens": 960}}

    async def upstream(request):
        captured.append({"headers": dict(request.headers), "body": await request.json()})
        if len(captured) == 1:
            return web.json_response({"error": {"message": "retry another key"}}, status=429)
        if stream:
            return web.Response(
                text="data: " + json.dumps({"choices": [], "usage": usage}) + "\n\ndata: [DONE]\n\n",
                content_type="text/event-stream")
        return web.json_response({"choices": [], "usage": usage})

    monkeypatch.setattr(relay, "load_keys", lambda *_: [
        ("first", "upstream-first"), ("second", "upstream-second")])
    args = SimpleNamespace(
        origin="unused", provider="unused", key_suffixes=None, files_dir=None,
        log=str(tmp_path / "requests.jsonl"), read_timeout=30, retry_budget=30,
        model="glm-5.3-flash", temperature=None, top_p=None, max_tokens=None,
        reasoning_effort=None)
    live = relay.Relay(args)
    payload = {
        "model": args.model, "stream": stream,
        "messages": [{"role": "system", "content": "Stable instructions."},
                     {"role": "user", "content": "Solve the task."}],
        "tools": [{"type": "function", "function": {
            "name": "inspect", "parameters": {"type": "object", "properties": {}}}}],
    }

    async def exercise():
        upstream_app = web.Application()
        upstream_app.router.add_post("/chat/completions", upstream)
        async with TestServer(upstream_app) as server:
            monkeypatch.setattr(relay, "UPSTREAM", str(server.make_url("/chat/completions")))
            app = web.Application()
            app.on_startup.append(live.start)
            app.on_cleanup.append(live.stop)
            app.router.add_post("/arm/{arm}/v1/chat/completions", live.chat)
            async with TestClient(TestServer(app)) as client:
                headers = {"Authorization": "Bearer sandbox-placeholder", "X-Unrelated": "omit"}
                if session_id:
                    headers["mT-sEsSiOn-Id"] = session_id
                async with client.post(f"/arm/{arm}/v1/chat/completions",
                                       json=payload, headers=headers) as response:
                    assert response.status == 200
                    await response.read()

    asyncio.run(exercise())
    assert len(captured) == 2
    for sent, secret in zip(captured, ("upstream-first", "upstream-second")):
        headers = {name.lower(): value for name, value in sent["headers"].items()}
        assert headers.get("mt-session-id") == session_id
        assert headers["authorization"] == "Bearer " + secret
        assert "x-unrelated" not in headers
        assert sent["body"] == payload
    record = json.loads((tmp_path / "requests.jsonl").read_text())
    assert record["attempts"] == 2 and record["usage"] == usage
    assert record["cache_affinity"] == ("Mt-Session-Id" if session_id else "")
    assert "trial-session" not in (tmp_path / "requests.jsonl").read_text()
