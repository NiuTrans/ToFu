"""Pinned Codex CLI smoke contract for the isolated Kimi benchmark proxy."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


pytestmark = pytest.mark.unit


def test_generated_command_drives_pinned_codex_through_one_kimi_call(
        tmp_path, monkeypatch):
    """Catch CLI-flag and SSE drift that pure translation tests cannot see."""
    from evaluations.codex_kimi_proxy.codex_contract import (
        CODEX_VERSION,
        benchmark_trial_token,
        build_codex_command,
        validate_proxy_metrics,
    )
    from evaluations.codex_kimi_proxy import server as proxy_server
    from evaluations.codex_kimi_proxy.server import CodexKimiProxy, ProxyConfig

    captured_responses_requests: list[dict] = []
    translate_request = proxy_server.responses_request_to_chat

    def capture_request(request):
        captured_responses_requests.append(request)
        return translate_request(request)

    monkeypatch.setattr(proxy_server, "responses_request_to_chat",
                        capture_request)

    binary = shutil.which("codex")
    if not binary:
        pytest.skip("pinned Codex CLI is not installed")
    observed_version = subprocess.check_output(
        [binary, "--version"], text=True, timeout=10)
    if CODEX_VERSION not in observed_version:
        pytest.skip(f"requires Codex {CODEX_VERSION}")

    class KimiUpstream(BaseHTTPRequestHandler):
        requests: list[dict] = []

        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length))
            type(self).requests.append(request)
            assert request["model"] == "kimi-k3"
            assert request["stream"] is True
            chunks = [
                {
                    "id": "chat-cli-smoke",
                    "object": "chat.completion.chunk",
                    "model": "kimi-k3",
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant",
                                           "content": "OK"},
                                 "finish_reason": None}],
                },
                {
                    "id": "chat-cli-smoke",
                    "object": "chat.completion.chunk",
                    "model": "kimi-k3",
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1,
                              "total_tokens": 3},
                },
            ]
            payload = b"".join(
                b"data: " + json.dumps(chunk, separators=(",", ":")).encode()
                + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), KimiUpstream)
    upstream_thread = threading.Thread(
        target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    metrics = tmp_path / "proxy-metrics.jsonl"
    trial_token = benchmark_trial_token("cli-smoke", "one")
    proxy = CodexKimiProxy(("127.0.0.1", 0), ProxyConfig(
        upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
        upstream_api_key="test-key",
        metrics_jsonl=str(metrics),
        require_trial_header=True,
    ))
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = build_codex_command(
        binary=binary,
        proxy_base_url=f"http://127.0.0.1:{proxy.server_port}",
        prompt="Reply with exactly OK and do not call tools.",
        reasoning_effort="low",
        trial_token=trial_token,
        sandbox="read-only",
    )
    command[-1:-1] = ["--skip-git-repo-check", "-C", str(workspace)]
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=30,
            env=environment)
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)

    captured_tools = ((captured_responses_requests[0].get("tools") or [])
                      if captured_responses_requests else [])
    representative_tools = list(captured_tools[:2])
    representative_tools.extend(
        tool for tool in captured_tools
        if isinstance(tool, dict) and tool.get("type") != "function")
    tool_shapes = [
        {key: value for key, value in tool.items()
         if key in {"type", "name", "description", "tools", "parameters",
                    "namespaces"}}
        for tool in representative_tools[:4]
        if isinstance(tool, dict)
    ]
    assert result.returncode == 0, (
        result.stderr + "\n" + result.stdout + "\n"
        + json.dumps(tool_shapes, ensure_ascii=False)[:10_000])
    assert '"text":"OK"' in result.stdout
    assert len(KimiUpstream.requests) == 1
    report = validate_proxy_metrics(
        str(metrics), expected_request_count=1,
        expected_trial_token=trial_token, require_trial_token=True)
    assert report["valid"] is True
    assert report["upstreamCalls"] == 1
    assert report["trialTagged"] is True
    assert report["suppressedNativeToolTypes"] == ["web_search"]
