"""Anthropic Messages -> OpenAI chat-completions bridge for the Claude Code control arm.

Responsibility: Claude Code speaks only the Anthropic Messages API and the
shared model relay (``relay.py``) only OpenAI chat completions. This process
translates each ``POST /arm/<arm>/v1/messages`` into one relay request under
the same arm label and translates the reply, streamed or not, back. Claude
Code thereby reaches the same FRIDAY deployment, keys and relay-enforced
sampling (temperature, top_p, max_tokens) as every other arm. The bridge holds
no credentials and enforces no sampling.

Wire mapping for GLM-5.3 on FRIDAY (Z.ai thinking format, as Pi sends it):

- ``thinking`` enabled/adaptive -> ``thinking: {type: enabled, clear_thinking:
  false}`` (thinking preserved across turns); disabled -> omitted, because the
  deployment always thinks and rejects a disabled request (error 1210);
- ``output_config.effort`` -> ``reasoning_effort`` on the deployment's levels
  (low, high, max; Pi 1.4.0's catalog): medium -> high, xhigh -> max;
- assistant ``thinking`` blocks -> ``reasoning_content``, sent back every turn;
- ``tool_use``/``tool_result`` -> ``tool_calls``/``tool`` messages; images ->
  ``image_url`` parts (tool-result images follow the tool messages);
- server tools (no ``input_schema``, e.g. web search) are refused with 400:
  nothing on the OpenAI side executes them.

Each request is logged as one JSONL record with the client's settings
(``client``), the translated settings sent to the relay (``sent``) and
history facts (assistant turns carrying reasoning), so the settings that
reach the model can be audited per request.

Entry point: ``python -m evaluations.terminal_bench.anthropic_bridge
--relay-url URL --log FILE``. Dependencies: aiohttp.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

# Deployment effort levels (Pi 1.4.0 zai.json thinkingLevelMap for glm-5.3-flash:
# low, high, max); Claude Code levels without a twin round up.
EFFORT_LEVELS = {"low": "low", "medium": "high", "high": "high", "xhigh": "max", "max": "max"}
STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                "function_call": "tool_use", "content_filter": "refusal", "sensitive": "refusal"}
# Opaque, non-empty: Claude Code stores and returns it; the bridge ignores it on input.
THINKING_SIGNATURE = "relay-bridge"


class BridgeError(Exception):
    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind = status, kind


def _image_part(block: dict) -> dict:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        url = f"data:{source.get('media_type') or 'image/png'};base64,{source.get('data') or ''}"
    else:
        url = source.get("url") or ""
    return {"type": "image_url", "image_url": {"url": url}}


def _file_part(block: dict) -> dict:
    """Anthropic ``document`` block -> OpenAI ``file`` part (this deployment accepts PDFs this way)."""
    source = block.get("source") or {}
    media_type = source.get("media_type") or "application/pdf"
    if source.get("type") == "base64":
        data = f"data:{media_type};base64,{source.get('data') or ''}"
    else:
        data = source.get("url") or ""
    return {"type": "file", "file": {"file_data": data, "filename": "document.pdf"}}


def _user_messages(content, facts: dict) -> list[dict]:
    """One Anthropic user turn -> tool messages first (OpenAI order), then the user's own parts."""
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    tools, parts = [], []
    for block in content or []:
        kind = block.get("type")
        if kind == "tool_result":
            inner = block.get("content")
            blocks = [{"type": "text", "text": inner}] if isinstance(inner, str) else (inner or [])
            text = "".join(item.get("text") or "" for item in blocks if item.get("type") == "text")
            for item in blocks:
                if item.get("type") == "image":
                    parts.append(_image_part(item))
                    facts["images"] += 1
                elif item.get("type") == "document":
                    parts.append(_file_part(item))
                    facts["documents"] += 1
                elif item.get("type") != "text":
                    facts["dropped_blocks"].append(item.get("type"))
            tools.append({"role": "tool", "tool_call_id": block.get("tool_use_id"), "content": text})
        elif kind == "text":
            parts.append({"type": "text", "text": block.get("text") or ""})
        elif kind == "image":
            parts.append(_image_part(block))
            facts["images"] += 1
        elif kind == "document":
            parts.append(_file_part(block))
            facts["documents"] += 1
        else:
            facts["dropped_blocks"].append(kind)
    messages = list(tools)
    if parts:
        if all(part["type"] == "text" for part in parts):
            messages.append({"role": "user", "content": "\n\n".join(part["text"] for part in parts)})
        else:
            messages.append({"role": "user", "content": parts})
    return messages


def _assistant_message(content, facts: dict) -> dict:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    reasoning, texts, calls = [], [], []
    for block in content or []:
        kind = block.get("type")
        if kind == "thinking":
            reasoning.append(block.get("thinking") or "")
        elif kind == "text":
            texts.append(block.get("text") or "")
        elif kind == "tool_use":
            calls.append({"id": block.get("id"), "type": "function",
                          "function": {"name": block.get("name"),
                                       "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
        elif kind != "redacted_thinking":
            facts["dropped_blocks"].append(kind)
    message: dict = {"role": "assistant", "content": "".join(texts)}
    if reasoning:
        message["reasoning_content"] = "\n\n".join(reasoning)
        facts["assistant_with_reasoning"] += 1
        facts["reasoning_chars"] += len(message["reasoning_content"])
    if calls:
        message["tool_calls"] = calls
    facts["assistant_turns"] += 1
    return message


def _tool_choice(choice: dict | None):
    kind = (choice or {}).get("type")
    if kind == "any":
        return "required"
    if kind == "tool":
        return {"type": "function", "function": {"name": choice.get("name")}}
    if kind in ("auto", "none"):
        return kind
    return None


def translate_request(body: dict) -> tuple[dict, dict]:
    """Anthropic request -> (OpenAI request, log record of client/sent settings and history facts)."""
    facts = {"assistant_turns": 0, "assistant_with_reasoning": 0, "reasoning_chars": 0, "images": 0,
             "documents": 0, "dropped_blocks": []}
    # Anthropic's server-side tools (web_search_*) have no OpenAI counterpart: drop them so the
    # request still runs, and tell the model they are unavailable instead of failing the turn.
    server_tools = [tool.get("type") or tool.get("name") for tool in body.get("tools") or []
                    if "input_schema" not in tool]
    if server_tools:
        facts["server_tools_dropped"] = server_tools
        facts.setdefault("dropped_blocks", []).extend(f"server_tool:{name}" for name in server_tools)
    messages = []
    system = body.get("system") or ""
    system_text = system if isinstance(system, str) else "\n\n".join(
        block.get("text") or "" for block in system if block.get("type") == "text")
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for message in body.get("messages") or []:
        if message.get("role") == "assistant":
            messages.append(_assistant_message(message.get("content"), facts))
        else:
            messages.extend(_user_messages(message.get("content"), facts))
    out: dict = {"model": body.get("model"), "messages": messages, "max_tokens": body.get("max_tokens"),
                 "stream": bool(body.get("stream"))}
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    thinking = body.get("thinking") or {}
    if thinking.get("type") in ("enabled", "adaptive"):
        out["thinking"] = {"type": "enabled", "clear_thinking": False}
    effort = (body.get("output_config") or {}).get("effort")
    if effort is not None:
        if effort not in EFFORT_LEVELS:
            raise BridgeError(400, "invalid_request_error", f"unknown effort level {effort!r}")
        out["reasoning_effort"] = EFFORT_LEVELS[effort]
    output_format = (body.get("output_config") or {}).get("format")
    if output_format and output_format.get("type") == "json_schema":
        out["response_format"] = {"type": "json_schema",
                                  "json_schema": {"name": "output", "schema": output_format.get("schema") or {}}}
    if body.get("tools"):
        out["tools"] = [{"type": "function", "function": {"name": tool["name"],
                                                          "description": tool.get("description") or "",
                                                          "parameters": tool["input_schema"]}}
                        for tool in body["tools"] if "input_schema" in tool]
        choice = _tool_choice(body.get("tool_choice"))
        if choice is not None:
            out["tool_choice"] = choice
        if (body.get("tool_choice") or {}).get("disable_parallel_tool_use"):
            out["parallel_tool_calls"] = False
    if server_tools:
        messages.append({"role": "system", "content":
                         "Anthropic server-side tools " + ", ".join(server_tools)
                         + " are not available through this endpoint. Do not attempt to use them; "
                           "use your own tools (Bash, Read, WebFetch) to fetch information."})
    record = {
        "client": {"model": body.get("model"), "max_tokens": body.get("max_tokens"),
                   "temperature": body.get("temperature"), "top_p": body.get("top_p"),
                   "top_k": body.get("top_k"), "stream": body.get("stream"), "thinking": body.get("thinking"),
                   "effort": effort, "n_messages": len(body.get("messages") or []),
                   "n_tools": len(body.get("tools") or []),
                   "ignored": sorted(key for key in body if key in ("top_k", "metadata", "context_management",
                                                                    "speed", "diagnostics", "container"))},
        "sent": {key: out.get(key) for key in ("model", "max_tokens", "temperature", "top_p", "stream",
                                               "reasoning_effort", "thinking")},
        "history": facts,
    }
    return out, record


def anthropic_usage(usage: dict | None) -> dict:
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {"input_tokens": max(prompt - cached, 0), "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0, "output_tokens": int(usage.get("completion_tokens") or 0)}


def stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    if has_tool_calls and finish_reason in (None, "stop", "tool_calls", "function_call"):
        return "tool_use"
    return STOP_REASONS.get(finish_reason or "stop", "end_turn")


def translate_response(data: dict, model: str) -> dict:
    """Non-streaming OpenAI completion -> Anthropic message."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    if message.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": message["reasoning_content"],
                        "signature": THINKING_SIGNATURE})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        content.append({"type": "tool_use", "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                        "name": function.get("name"), "input": arguments})
    return {"id": f"msg_{data.get('id') or uuid.uuid4().hex}", "type": "message", "role": "assistant",
            "model": model, "content": content,
            "stop_reason": stop_reason(choice.get("finish_reason"), bool(message.get("tool_calls"))),
            "stop_sequence": None, "usage": anthropic_usage(data.get("usage"))}


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class StreamTranslator:
    """OpenAI chat-completion chunks -> Anthropic message stream events.

    Content arrives as reasoning, then text, then tool calls; each kind opens
    its own content block, closed when the next kind starts. A tool call's
    arguments are forwarded verbatim as ``input_json_delta`` fragments, so
    Claude Code parses exactly what the model produced.
    """

    def __init__(self, model: str):
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.index = -1
        self.open_kind: str | None = None
        self.tool_blocks: dict[int, int] = {}
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        # Tool calls are buffered and emitted at the end of the message: Anthropic has
        # no per-call fragment index, so interleaved fragments could not be represented.
        self.pending: dict[int, dict] = {}

    def start(self) -> bytes:
        return sse("message_start", {"type": "message_start", "message": {
            "id": self.message_id, "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close(self) -> bytes:
        if self.open_kind is None:
            return b""
        events = b""
        if self.open_kind == "thinking":
            events += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                  "delta": {"type": "signature_delta",
                                                            "signature": THINKING_SIGNATURE}})
        self.open_kind = None
        return events + sse("content_block_stop", {"type": "content_block_stop", "index": self.index})

    def _open(self, kind: str, block: dict) -> bytes:
        events = self._close()
        self.index += 1
        self.open_kind = kind
        return events + sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                    "content_block": block})

    def _tool_events(self, calls) -> bytes:
        """One ``tool_use`` block per call, arguments forwarded verbatim as JSON deltas."""
        events = b""
        for position in sorted(calls):
            call = calls[position]
            function = call["function"]
            events += self._open("tool", {"type": "tool_use",
                                          "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                          "name": function["name"], "input": {}})
            self.tool_blocks[position] = self.index
            for fragment in function["arguments"]:
                events += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                      "delta": {"type": "input_json_delta",
                                                                "partial_json": fragment}})
        return events

    def feed(self, chunk: dict) -> bytes:
        events = b""
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                if self.open_kind != "thinking":
                    events += self._open("thinking", {"type": "thinking", "thinking": "", "signature": ""})
                events += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                      "delta": {"type": "thinking_delta",
                                                                "thinking": delta["reasoning_content"]}})
            if delta.get("content"):
                if self.open_kind != "text":
                    events += self._open("text", {"type": "text", "text": ""})
                events += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                      "delta": {"type": "text_delta", "text": delta["content"]}})
            for call in delta.get("tool_calls") or []:
                position = call.get("index", 0)
                function = call.get("function") or {}
                entry = self.pending.setdefault(position, {"id": call.get("id"),
                                                          "function": {"name": function.get("name") or "",
                                                                       "arguments": []}})
                entry["id"] = call.get("id") or entry["id"]
                if function.get("name"):
                    entry["function"]["name"] = function["name"]
                if function.get("arguments"):
                    entry["function"]["arguments"].append(function["arguments"])
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def finish(self) -> bytes:
        events = self._tool_events(self.pending)
        reason = stop_reason(self.finish_reason, bool(self.tool_blocks))
        return (events + self._close()
                + sse("message_delta", {"type": "message_delta",
                                        "delta": {"stop_reason": reason, "stop_sequence": None},
                                        "usage": anthropic_usage(self.usage)})
                + sse("message_stop", {"type": "message_stop"}))


def _error_kind(status: int) -> str:
    return {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error"}.get(
        status, "api_error" if status < 529 else "overloaded_error")


class Bridge:
    def __init__(self, args):
        self.args = args
        self.relay_url = args.relay_url.rstrip("/")
        self.log_path = Path(args.log)
        self.session = None

    async def start(self, app):
        from aiohttp import ClientSession, ClientTimeout, TCPConnector

        self.session = ClientSession(
            connector=TCPConnector(limit=0),
            timeout=ClientTimeout(total=None, sock_connect=30, sock_read=self.args.read_timeout),
            trust_env=False)

    async def stop(self, app):
        await self.session.close()

    def log(self, record: dict) -> None:
        record["ts"] = round(time.time(), 3)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def error(status: int, message: str, kind: str | None = None):
        from aiohttp import web

        return web.json_response({"type": "error", "error": {"type": kind or _error_kind(status),
                                                              "message": message}}, status=status)

    async def models(self, request):
        from aiohttp import web

        model = self.args.model
        return web.json_response({"data": [{"type": "model", "id": model, "display_name": model,
                                            "created_at": "2026-01-01T00:00:00Z"}],
                                  "has_more": False, "first_id": model, "last_id": model})

    async def unsupported(self, request):
        arm = request.match_info.get("arm", "-")
        self.log({"kind": "unsupported", "arm": arm, "method": request.method, "path": request.path})
        return self.error(404, f"{request.method} {request.path} is not supported by the bridge")

    async def messages(self, request):
        from aiohttp import web

        arm = request.match_info.get("arm", "-")
        started = time.monotonic()
        record: dict = {"kind": "messages", "arm": arm, "peer": request.remote}
        try:
            body = await request.json()
            outgoing, facts = translate_request(body)
        except BridgeError as failure:
            record.update(status=failure.status, error=str(failure)[:300])
            self.log(record)
            return self.error(failure.status, str(failure), failure.kind)
        except ValueError:
            record.update(status=400, error="invalid JSON body")
            self.log(record)
            return self.error(400, "invalid JSON body")
        record.update(facts)
        model = body.get("model") or self.args.model
        url = f"{self.relay_url}/arm/{arm}/v1/chat/completions"
        try:
            upstream = await self.session.post(url, json=outgoing, headers={"Accept-Encoding": "identity"})
        except (asyncio.TimeoutError, OSError) as failure:
            record.update(status=502, error=f"{type(failure).__name__}: {str(failure)[:200]}")
            self.log(record)
            return self.error(502, f"relay unreachable: {type(failure).__name__}")
        record["ttfb_s"] = round(time.monotonic() - started, 3)
        try:
            if upstream.status != 200:
                text = (await upstream.text())[:600]
                record.update(status=upstream.status, error=text[:300])
                return self.error(upstream.status, f"relay error {upstream.status}: {text}")
            if not outgoing["stream"]:
                data = await upstream.json(content_type=None)
                message = translate_response(data, model)
                record.update(status=200, stop_reason=message["stop_reason"], usage=message["usage"])
                return web.json_response(message)
            return await self.stream(request, upstream, model, record)
        except BridgeError as failure:
            record.update(status=failure.status, error=str(failure)[:300])
            return self.error(failure.status, str(failure), failure.kind)
        finally:
            upstream.release()
            record["latency_s"] = round(time.monotonic() - started, 3)
            self.log(record)

    async def stream(self, request, upstream, model: str, record: dict):
        from aiohttp import ClientError, web

        translator = StreamTranslator(model)
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream",
                                                           "Cache-Control": "no-cache"})
        await response.prepare(request)
        record["status"] = 200
        try:
            await response.write(translator.start())
            try:
                await self.relay_stream(upstream, translator, response)
            except (BridgeError, ValueError, ClientError, asyncio.TimeoutError) as failure:
                # Headers are sent: an upstream failure reaches Claude Code as a stream error event.
                kind = failure.kind if isinstance(failure, BridgeError) else "api_error"
                record["stream_error"] = f"{type(failure).__name__}: {str(failure)[:300]}"
                await response.write(sse("error", {"type": "error", "error": {
                    "type": kind, "message": record["stream_error"]}}))
            await response.write_eof()
        except ConnectionResetError:
            record["client_gone"] = True
        record.update(stop_reason=stop_reason(translator.finish_reason, bool(translator.tool_blocks)),
                      finish_reason=translator.finish_reason, usage=anthropic_usage(translator.usage),
                      blocks=translator.index + 1)
        return response

    @staticmethod
    async def relay_stream(upstream, translator: StreamTranslator, response) -> None:
        buffer = b""
        async for piece in upstream.content.iter_any():
            buffer += piece
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                line = line.strip()
                if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                    continue
                chunk = json.loads(line[5:].strip())
                if chunk.get("error"):
                    raise BridgeError(502, "api_error", json.dumps(chunk["error"], ensure_ascii=False)[:300])
                events = translator.feed(chunk)
                if events:
                    await response.write(events)
        await response.write(translator.finish())


def build_app(bridge: Bridge):
    from aiohttp import web

    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.stop)
    for prefix in ("", "/arm/{arm}"):
        app.router.add_post(prefix + "/v1/messages", bridge.messages)
        app.router.add_get(prefix + "/v1/models", bridge.models)
        app.router.add_post(prefix + "/v1/messages/count_tokens", bridge.unsupported)
    app.router.add_route("*", "/{tail:.*}", bridge.unsupported)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluations.terminal_bench.anthropic_bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18484)
    parser.add_argument("--relay-url", required=True, help="model relay base URL, e.g. http://127.0.0.1:18483")
    parser.add_argument("--model", default="glm-5.3-flash", help="model id listed by GET /v1/models")
    parser.add_argument("--read-timeout", type=float, default=3600.0,
                        help="seconds without relay bytes before a request fails (relay retry budget + read timeout)")
    parser.add_argument("--log", required=True)
    args = parser.parse_args(argv)
    from aiohttp import web

    bridge = Bridge(args)
    print(f"anthropic bridge on {args.host}:{args.port} -> {args.relay_url}", flush=True)
    web.run_app(build_app(bridge), host=args.host, port=args.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
