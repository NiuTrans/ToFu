"""Loopback-only standalone proxy for fair Codex↔Kimi evaluation.

This module registers no Tofu production route, reads no user configuration,
and logs only credential-free timing/call-count metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .codex_contract import TRIAL_HEADER, CodexContractError, validate_trial_token
from .translation import (
    ChatSSETranslator,
    TranslationError,
    chat_response_to_responses,
    responses_request_to_chat,
    sse_line,
    suppressed_native_tool_types,
)


_MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _loopback(host: str) -> bool:
    try:
        return all(address[4][0].startswith("127.") or address[4][0] == "::1"
                   for address in socket.getaddrinfo(host, None))
    except socket.gaierror:
        return False


def _chat_endpoint(base_url: str) -> str:
    value = str(base_url or "").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("Kimi upstream must use HTTP(S)")
    if parsed.scheme == "http" and not _loopback(parsed.hostname or ""):
        raise ValueError("plaintext upstream is allowed only on loopback")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


@dataclass(frozen=True)
class ProxyConfig:
    upstream_base_url: str
    upstream_api_key: str = field(repr=False)
    metrics_jsonl: str = ""
    trial_metrics_dir: str = ""
    timeout_seconds: float = 300.0
    require_trial_header: bool = False

    def __post_init__(self) -> None:
        _chat_endpoint(self.upstream_base_url)
        if not self.upstream_api_key:
            raise ValueError("Kimi API key is required")


class MetricsSink:
    def __init__(self, path: str, trial_directory: str = ""):
        self._path = Path(path) if path else None
        self._trial_directory = Path(trial_directory) if trial_directory else None
        self._lock = threading.Lock()
        if self._trial_directory is not None:
            directory = self._trial_directory.expanduser()
            if directory.is_symlink():
                raise ValueError("trial metrics directory must not be a symlink")
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory = directory.resolve(strict=True)
            info = directory.stat()
            if not directory.is_dir() or info.st_uid != os.getuid() \
                    or info.st_mode & 0o077:
                raise PermissionError(
                    "trial metrics directory must be private and owner-scoped"
                )
            self._trial_directory = directory

    @staticmethod
    def _append_file(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = (
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("metrics target must be a regular file")
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("metrics append made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def append(self, value: dict[str, Any]) -> None:
        if self._path is None and self._trial_directory is None:
            return
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self._lock:
            if self._path is not None:
                self._append_file(self._path, payload)
            token = str(value.get("trialToken") or "")
            if self._trial_directory is not None and token:
                try:
                    token = validate_trial_token(token)
                except CodexContractError:
                    return
                self._append_file(
                    self._trial_directory / f"{token}.jsonl", payload
                )


class CodexKimiProxy(ThreadingHTTPServer):
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
            limits=httpx.Limits(max_connections=16,
                                max_keepalive_connections=8),
            http2=True,
            trust_env=False,
        )

    def server_close(self) -> None:
        self.client.close()
        super().server_close()


class ProxyHandler(BaseHTTPRequestHandler):
    server: CodexKimiProxy
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log request bodies, headers, or upstream errors that may echo
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
        """Serve Codex's credential-free model probe without upstream I/O."""
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in {"/models", "/v1/models"}:
            self._json(404, {"error": {"type": "not_found",
                                        "message": "route not found"}})
            return
        self._json(200, {
            "object": "list",
            "data": [{
                "id": "kimi-k3", "object": "model", "created": 0,
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
        if path.endswith("/responses/compact"):
            self.server.metrics.append({
                "traceId": trace_id, "event": "invalidCompactRequest",
                "invalidTrial": True, "upstreamCalls": 0,
                "trialToken": trial_token,
                "atMs": int(time.time() * 1000),
            })
            self._json(409, {"error": {
                "type": "remote_compaction_forbidden",
                "message": "Codex local compaction is required for this benchmark."}})
            return
        if path not in {"/responses", "/v1/responses"}:
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
            raw_tools = request.get("tools") if isinstance(request, dict) else None
            tool_schema_payload = json.dumps(
                raw_tools if isinstance(raw_tools, list) else [],
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            suppressed_tool_types = suppressed_native_tool_types(
                raw_tools)
            chat_body = responses_request_to_chat(request)
        except json.JSONDecodeError:
            self._json(400, {"error": {"type": "invalid_json",
                                        "message": "request is not valid JSON"}})
            return
        except TranslationError as exc:
            self._json(exc.status, exc.to_response())
            return
        translate_request_cpu = time.thread_time_ns() - started_cpu
        request_digest = hashlib.sha256(raw).hexdigest()[:24]
        upstream_url = _chat_endpoint(self.server.config.upstream_base_url)
        headers = {
            "Authorization": f"Bearer {self.server.config.upstream_api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if chat_body.get("stream") else "application/json",
        }
        upstream_calls = 0
        upstream_started = time.perf_counter_ns()
        translation_cpu = translate_request_cpu
        disconnected = False
        status = "failed"
        stream_started = False
        translator: ChatSSETranslator | None = None
        response_usage: dict[str, Any] = {}
        first_upstream_byte_unix = 0
        try:
            # This is the only upstream call site in the proxy.
            upstream_calls += 1
            with self.server.client.stream(
                    "POST", upstream_url, headers=headers,
                    json=chat_body) as upstream:
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
                if chat_body.get("stream"):
                    stream_started = True
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.send_header("X-Tofu-Proxy-Trace", trace_id)
                    self.send_header(
                        "X-Tofu-Proxy-Request-Translate-Ns",
                        str(translate_request_cpu))
                    self.end_headers()
                    translator = ChatSSETranslator(model=chat_body["model"])
                    saw_done = False
                    translation_failure: tuple[str, str] | None = None
                    for line in upstream.iter_lines():
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        if not first_upstream_byte_unix:
                            first_upstream_byte_unix = time.time_ns()
                        data = line[5:].strip()
                        if data == "[DONE]":
                            saw_done = True
                            break
                        cpu_start = time.thread_time_ns()
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError:
                            translation_cpu += time.thread_time_ns() - cpu_start
                            translation_failure = (
                                "invalid_upstream_sse_json",
                                "Kimi emitted a non-JSON SSE data frame.")
                            break
                        try:
                            events = translator.feed(payload)
                        except (TranslationError, TypeError, ValueError) as exc:
                            translation_failure = (
                                "invalid_upstream_sse_chunk", type(exc).__name__)
                            events = []
                        translation_cpu += time.thread_time_ns() - cpu_start
                        for event in events:
                            self.wfile.write(sse_line(event))
                        self.wfile.flush()
                        if translation_failure is not None:
                            break
                    cpu_start = time.thread_time_ns()
                    for event in translator.finish(
                            completed=saw_done and translation_failure is None,
                            error_code=(translation_failure or ("", ""))[0],
                            error_message=(translation_failure or ("", ""))[1]):
                        self.wfile.write(sse_line(event))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    translation_cpu += time.thread_time_ns() - cpu_start
                    status = (
                        "translation_error" if translation_failure is not None
                        else "completed" if saw_done else "truncated")
                    response_usage = dict(translator.usage or {})
                else:
                    chat_response = json.loads(upstream.read())
                    first_upstream_byte_unix = time.time_ns()
                    cpu_start = time.thread_time_ns()
                    response = chat_response_to_responses(chat_response)
                    translation_cpu += time.thread_time_ns() - cpu_start
                    self._json(
                        200, response, **{"X-Tofu-Proxy-Trace": trace_id,
                                         "X-Tofu-Proxy-Request-Translate-Ns": str(
                                             translate_request_cpu)})
                    status = str(response.get("status") or "completed")
                    response_usage = dict(response.get("usage") or {})
        except (BrokenPipeError, ConnectionResetError):
            disconnected = True
            status = "cancelled"
        except TranslationError as exc:
            if stream_started and translator is not None:
                try:
                    for event in translator.finish(
                            completed=False, error_code=exc.code,
                            error_message=str(exc)):
                        self.wfile.write(sse_line(event))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True
            elif not self.wfile.closed:
                try:
                    self._json(exc.status, exc.to_response())
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True
            status = "translation_error"
        except (httpx.HTTPError, ValueError) as exc:
            if stream_started and translator is not None:
                try:
                    for event in translator.finish(
                            completed=False,
                            error_code="upstream_transport_error",
                            error_message=type(exc).__name__):
                        self.wfile.write(sse_line(event))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True
            elif not self.wfile.closed:
                try:
                    self._json(502, {"error": {
                        "type": "upstream_transport_error",
                        "message": type(exc).__name__}})
                except (BrokenPipeError, ConnectionResetError):
                    disconnected = True
            status = "transport_error"
        finally:
            ended = time.perf_counter_ns()
            proxy_cpu = max(0, time.thread_time_ns() - started_cpu)
            self.server.metrics.append({
                "traceId": trace_id,
                "event": "responsesTranslation",
                "requestDigest": request_digest,
                "requestBytes": len(raw),
                "trialToken": trial_token,
                "toolSchemaDigest": hashlib.sha256(
                    tool_schema_payload).hexdigest(),
                "toolSchemaBytes": len(tool_schema_payload),
                "toolCount": len(raw_tools) if isinstance(raw_tools, list) else 0,
                "suppressedNativeToolTypes": list(suppressed_tool_types),
                "status": status,
                "clientDisconnected": disconnected,
                "upstreamCalls": upstream_calls,
                "translationCpuNs": max(0, translation_cpu),
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
    server = CodexKimiProxy((host, int(port)), config)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


__all__ = ["CodexKimiProxy", "ProxyConfig", "serve"]
