"""Loopback-only passthrough chat proxy for fair OpenCode benchmark trials.

This module registers no Tofu production route, reads no user configuration,
and logs only credential-free timing/usage metadata.  The upstream credential
never leaves this process; guests see only the predeclared loopback endpoint
and a per-trial correlation header.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import httpx

from evaluations.codex_kimi_proxy.codex_contract import (
    TRIAL_HEADER,
    CodexContractError,
    validate_trial_token,
)
from evaluations.codex_kimi_proxy.server import MetricsSink, _loopback


_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_TERMINAL_STATUSES = {"completed", "upstream_error", "transport_error",
                      "cancelled"}


def _chat_endpoint(base_url: str) -> str:
    value = str(base_url or "").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("upstream must use HTTP(S)")
    if parsed.scheme == "http" and not _loopback(parsed.hostname or ""):
        raise ValueError("plaintext upstream is allowed only on loopback")
    if parsed.username is not None or parsed.password is not None \
            or parsed.query or parsed.fragment:
        raise ValueError("upstream base URL must not carry credentials or query")
    if value.endswith("/chat/completions"):
        return value
    return value + "/chat/completions"


@dataclass(frozen=True)
class ProxyConfig:
    upstream_base_url: str
    upstream_api_key: str = field(repr=False)
    model: str = ""
    metrics_jsonl: str = ""
    trial_metrics_dir: str = ""
    timeout_seconds: float = 300.0
    require_trial_header: bool = True

    def __post_init__(self) -> None:
        _chat_endpoint(self.upstream_base_url)
        if not self.upstream_api_key:
            raise ValueError("upstream API key is required")
        if not str(self.model).strip():
            raise ValueError("a pinned benchmark model is required")


class OpenCodeBenchmarkProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: ProxyConfig):
        host = address[0]
        if not _loopback(host):
            raise ValueError("benchmark proxy must bind to loopback")
        super().__init__(address, ProxyHandler)
        self.config = config
        self.metrics = MetricsSink(
            config.metrics_jsonl, config.trial_metrics_dir)
        self.client = httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds),
            limits=httpx.Limits(max_connections=32,
                                max_keepalive_connections=16),
            # HTTP/1.1: an idle-reaped h2 connection races the next reuse
            # into "SEND_SETTINGS in state CLOSED" (h2 ProtocolError), which
            # the supervisor surfaces as launcher-fatal. Benchmark evidence
            # must not die on a transport dialect race; the upstream speaks
            # HTTP/1.1 fine.
            http2=False,
            trust_env=False,
        )

    def server_close(self) -> None:
        self.client.close()
        super().server_close()


class ProxyHandler(BaseHTTPRequestHandler):
    server: OpenCodeBenchmarkProxy
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Never log request bodies, headers, or upstream errors that may echo
        # credentials. Per-trial metadata goes to the explicit metrics sink.
        return

    def _json(self, status: int, value: dict[str, Any], **headers: str) -> None:
        payload = json.dumps(value, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, child in headers.items():
            self.send_header(name, child)
        self.end_headers()
        self.wfile.write(payload)

    def _trial_token(self, trace_id: str) -> str | None:
        raw = str(self.headers.get(TRIAL_HEADER) or "").strip()
        if not raw and not self.server.config.require_trial_header:
            return ""
        try:
            return validate_trial_token(raw)
        except CodexContractError:
            self.server.metrics.append({
                "traceId": trace_id,
                "event": "invalidTrialHeader",
                "invalidTrial": True,
                "upstreamCalls": 0,
                "atMs": int(time.time() * 1000),
            })
            self._json(403, {"error": {
                "type": "invalid_trial_header",
                "message": "A valid benchmark trial correlation header is required.",
            }})
            return None

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        """Serve a credential-free model probe without upstream I/O."""
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in {"/models", "/v1/models"}:
            self._json(404, {"error": {"type": "not_found",
                                        "message": "route not found"}})
            return
        self._json(200, {
            "object": "list",
            "data": [{
                "id": self.server.config.model,
                "object": "model",
                "created": 0,
                "owned_by": "eval",
            }],
        })

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        trace_id = uuid.uuid4().hex
        started_wall = time.perf_counter_ns()
        started_unix = time.time_ns()
        started_cpu = time.thread_time_ns()
        trial_token = self._trial_token(trace_id)
        if trial_token is None:
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in {"/chat/completions", "/v1/chat/completions"}:
            self._json(404, {"error": {"type": "not_found",
                                        "message": "route not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            self._json(413, {"error": {"type": "invalid_request_size",
                                        "message": "request size is invalid"}})
            return
        try:
            raw = self.rfile.read(length)
            request = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": {"type": "invalid_json",
                                        "message": "request is not valid JSON"}})
            return
        if not isinstance(request, dict):
            self._json(400, {"error": {"type": "invalid_request",
                                        "message": "request body must be an object"}})
            return
        pinned = self.server.config.model
        requested_model = str(request.get("model") or "")
        if requested_model != pinned:
            self.server.metrics.append({
                "traceId": trace_id,
                "event": "modelPinViolation",
                "requestedModel": requested_model[:128],
                "trialToken": trial_token,
                "invalidTrial": True,
                "upstreamCalls": 0,
                "atMs": int(time.time() * 1000),
            })
            self._json(403, {"error": {
                "type": "model_pin_violation",
                "message": "This benchmark endpoint serves exactly one pinned model.",
            }})
            return
        stream = bool(request.get("stream"))
        injected_usage_option = False
        if stream:
            options = request.get("stream_options")
            if not isinstance(options, dict) \
                    or options.get("include_usage") is not True:
                request["stream_options"] = {
                    **(options if isinstance(options, dict) else {}),
                    "include_usage": True,
                }
                raw = json.dumps(
                    request, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                injected_usage_option = True
        request_digest = hashlib.sha256(raw).hexdigest()[:24]
        upstream_url = _chat_endpoint(self.server.config.upstream_base_url)
        headers = {
            "Authorization": f"Bearer {self.server.config.upstream_api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        upstream_calls = 0
        upstream_started = time.perf_counter_ns()
        disconnected = False
        status = "failed"
        stream_started = False
        response_usage: dict[str, Any] = {}
        finish_reasons: list[str] = []
        first_upstream_byte_unix = 0
        try:
            # This is the only upstream call site in the proxy.
            upstream_calls += 1
            with self.server.client.stream(
                    "POST", upstream_url, headers=headers,
                    content=raw) as upstream:
                if upstream.status_code >= 400:
                    # Bounded error forwarding; never include upstream headers.
                    detail = upstream.read()[:16_384].decode(
                        "utf-8", errors="replace")
                    detail = detail.replace(
                        self.server.config.upstream_api_key,
                        "[redacted-benchmark-credential]",
                    )
                    self._json(upstream.status_code, {"error": {
                        "type": "upstream_error", "message": detail}})
                    status = "upstream_error"
                    return
                if stream:
                    stream_started = True
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Tofu-Proxy-Trace", trace_id)
                    # No Content-Length/TE on SSE: connection close is the
                    # only body delimiter, otherwise HTTP/1.1 clients block
                    # waiting for EOF after [DONE].
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    saw_done = False
                    for line in upstream.iter_lines():
                        if not first_upstream_byte_unix:
                            first_upstream_byte_unix = time.time_ns()
                        self.wfile.write(line.encode("utf-8") + b"\n")
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            saw_done = True
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            chunk = None
                        if isinstance(chunk, dict):
                            usage = chunk.get("usage")
                            if isinstance(usage, dict) and usage:
                                response_usage = dict(usage)
                            for choice in (chunk.get("choices") or []):
                                if not isinstance(choice, dict):
                                    continue
                                reason = choice.get("finish_reason")
                                if isinstance(reason, str) and reason:
                                    finish_reasons.append(reason)
                    self.wfile.flush()
                    status = "completed" if saw_done else "truncated"
                else:
                    body = upstream.read()
                    first_upstream_byte_unix = time.time_ns()
                    try:
                        response = json.loads(body)
                    except json.JSONDecodeError:
                        response = None
                    if isinstance(response, dict):
                        usage = response.get("usage")
                        if isinstance(usage, dict) and usage:
                            response_usage = dict(usage)
                        for choice in (response.get("choices") or []):
                            if not isinstance(choice, dict):
                                continue
                            reason = choice.get("finish_reason")
                            if isinstance(reason, str) and reason:
                                finish_reasons.append(reason)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Tofu-Proxy-Trace", trace_id)
                    self.end_headers()
                    self.wfile.write(body)
                    status = "completed"
        except (BrokenPipeError, ConnectionResetError):
            disconnected = True
            status = "cancelled"
        except (httpx.HTTPError, ValueError) as exc:
            if not self.wfile.closed:
                try:
                    if stream_started:
                        self.wfile.write(
                            b"data: [DONE]\n\n")
                        self.wfile.flush()
                        self.close_connection = True
                    else:
                        self._json(502, {"error": {
                            "type": "upstream_transport_error",
                            "message": type(exc).__name__}})
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True
            status = "transport_error"
        finally:
            ended = time.perf_counter_ns()
            proxy_cpu = max(0, time.thread_time_ns() - started_cpu)
            from evaluations.model_request import request_parameter_receipt
            self.server.metrics.append({
                "traceId": trace_id,
                "event": "chatCompletion",
                "requestDigest": request_digest,
                "requestBytes": len(raw),
                "requestParameters": request_parameter_receipt(request),
                "trialToken": trial_token,
                "model": pinned,
                "stream": stream,
                "injectedUsageOption": injected_usage_option,
                "status": status,
                "finishReasons": finish_reasons,
                "clientDisconnected": disconnected,
                "upstreamCalls": upstream_calls,
                "proxyCpuNs": proxy_cpu,
                "upstreamWallNs": max(0, ended - upstream_started),
                "rawWallNs": max(0, ended - started_wall),
                "startedAtUnixNs": started_unix,
                "firstUpstreamByteAtUnixNs": first_upstream_byte_unix,
                "usage": response_usage,
                "invalidTrial": upstream_calls != 1,
            })


def serve(config: ProxyConfig, *, host: str = "127.0.0.1",
          port: int = 0) -> None:
    server = OpenCodeBenchmarkProxy((host, int(port)), config)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


__all__ = ["OpenCodeBenchmarkProxy", "ProxyConfig", "serve"]
