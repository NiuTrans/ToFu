"""Fair-comparison model relay for native sandbox Terminal-Bench runs.

Both harness arms call this one relay; it forwards every chat completion to the
same FRIDAY deployment with the same AppId pool, so model identity, rate limits
and keys are identical across arms. ``--temperature/--top-p/--max-tokens``
override every request identically, which is how a protocol's sampling is
enforced. Credentials are read from the live Tofu model-routing authority
inside this process only; sandboxes see a key-less URL.

The gateway's Mt-Session-Id carrier is forwarded unchanged for every arm and
retry when the client sends it. No identity is synthesized or put in the
prompt. On the chat face other client headers, including authorization, are
not forwarded; the Anthropic face's allow-list is below.

The Anthropic Messages face (``--anthropic-model``) forwards each harness's own
request to FRIDAY's Anthropic face with only ``model`` pinned: sampling,
thinking, effort and cache_control stay the harness's, because those are what
a cache comparison measures. Besides Mt-Session-Id it forwards the request
semantics headers ``anthropic-version`` and ``anthropic-beta`` and the gateway
trace id ``M-TraceId``. One normalization is documented in
:func:`compact_pi_effort_markers`: Pi's empty mid-conversation effort markers,
which FRIDAY rejects, become the top-level effort their last marker asks for.
``--preserve-pi-effort-markers`` disables it. Each record carries the request's cache-relevant facts
(breakpoints and TTLs, digests of tools, system and every message without
``cache_control``, joined tool-name bytes, a digest of the affinity value) and
the provider-reported usage, never prompt text or the affinity value itself.

Entry point: ``python -m evaluations.terminal_bench.relay --log FILE ...``.
Dependencies: aiohttp (server/upstream), httpx (credential reveal).

Routes (``<arm>`` is a free label used only for accounting; ``<task>`` names
the trial's task so every request is attributed to its trial):
  POST /arm/<arm>/v1/chat/completions  (also without /v1, and without /arm/<arm>)
  POST /arm/<arm>/task/<task>/anthropic/v1/messages[/count_tokens]
       (also without /task/<task>, and without /arm/<arm>)
  GET  /arm/<arm>/v1/models
  GET  /probe, /arm/<arm>/probe        reachability check from inside a sandbox
  GET  /files/<name>                   static artifacts (e.g. the tofu wheel)
"""
import argparse
import asyncio
import hashlib
import itertools
import json
import random
import re
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

UPSTREAM = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_UPSTREAM = "https://api.openai.com/v1/anthropic/v1/messages"
LONG_DIGITS = re.compile(r"\d{12,}")
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
# Anthropic's documented overload status joins the gateway's retryable set.
ANTHROPIC_RETRY_STATUSES = RETRY_STATUSES | {529}
# Client headers the Anthropic face forwards: request semantics (API version,
# beta flags), the gateway's cache affinity carrier and its trace id.
ANTHROPIC_FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta", "mt-session-id", "m-traceid")
# SSE ``data:`` lines worth parsing; content deltas are passed through unread.
_ANTHROPIC_EVENT_MARKERS = (b'"message_start"', b'"message_delta"', b'"content_block_start"', b'"error"')


def load_keys(origin, provider_id):
    """Owner credentials of one provider from the live Tofu model-routing authority.

    Plaintext is resolved through the audited reveal endpoint and kept in memory only.
    """
    import httpx

    with httpx.Client(base_url=origin, timeout=30, trust_env=False) as client:
        document = client.get("/api/v4/model-routing").json()["data"]["model_routing"]
        accesses = {a["provider_access_id"] for a in document["provider_accesses"]
                    if a["provider_id"] == provider_id}
        keys = []
        for credential in document["credentials"]:
            if credential["provider_access_id"] in accesses and credential.get("enabled") \
                    and credential.get("kind") == "api_key":
                reply = client.post(f"/api/v4/model-routing/credentials/{credential['credential_id']}/secret/reveal")
                secret = (reply.json().get("data") or {}).get("secret") or ""
                try:
                    envelope = json.loads(secret)
                except ValueError:
                    envelope = None
                if isinstance(envelope, dict):
                    secret = envelope.get("api_key") or ""
                if secret.strip():
                    keys.append((credential["key_hint"][-4:], secret.strip()))
    if not keys:
        raise SystemExit(f"no api_key credentials for provider {provider_id!r}")
    return keys


def _digest(value) -> str:
    """Short content digest; ASCII JSON so lone surrogates in tool output still hash."""
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _without_cache_control(value):
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    return value


def _breakpoint(where: str, block) -> str | None:
    marker = block.get("cache_control") if isinstance(block, dict) else None
    if not isinstance(marker, dict):
        return None
    return f"{where}:{marker.get('ttl') or '5m'}"


def compact_pi_effort_markers(body: dict) -> dict:
    """Fold Pi's mid-conversation effort markers into ``output_config.effort``.

    Pi 1.4.0's built-in Opus 5.5 definition declares ``supportsMidConvoEffort``:
    it asks for a constant top-level ``output_config.effort`` of ``high`` and
    puts the requested effort in empty ``system`` messages before every
    assistant turn (``insertThinkingLevelMessages``). FRIDAY's Anthropic face
    rejects those markers on every request, which makes the model unusable
    through Pi's own defaults:

    - ``content: []``          -> 400 ``system content must contain at least one block``
    - ``content: [{text: ""}]`` -> 400 ``system text blocks must contain non-whitespace text``
    - no ``content`` key       -> 400 ``...content: Input should be a valid array``

    This fold drops exactly those markers, keeps the LAST one as the effort in
    force for the response, and leaves ``anthropic-beta`` and ``thinking`` (Pi
    declares its own ``block_binding``) untouched. No text is added: a non-empty
    system marker would be prompt content and would change the cache prefix.
    The record keeps the marker count and the folded value in ``pi_effort_fold``.
    """
    markers = [message.get("output_config") or {} for message in body.get("messages") or []
               if isinstance(message, dict) and message.get("role") == "system" and "output_config" in message]
    if not markers:
        return {"markers": 0}
    before = (body.get("output_config") or {}).get("effort")
    effort = markers[-1].get("effort")
    body["messages"] = [message for message in body["messages"]
                        if not (isinstance(message, dict) and message.get("role") == "system"
                                and "output_config" in message)]
    if effort:
        body["output_config"] = {**(body.get("output_config") or {}), "effort": effort}
    return {"markers": len(markers), "effort": effort, "top_level_before": before}


def anthropic_settings(body: dict) -> dict:
    """The harness's own model, sampling, thinking and effort settings, as sent.

    ``effort_markers`` counts mid-conversation ``system`` messages that carry
    ``output_config`` (Pi's managed effort); the last one is the effort in
    force for the response being requested.
    """
    markers = [message.get("output_config") or {} for message in body.get("messages") or []
               if isinstance(message, dict) and message.get("role") == "system" and "output_config" in message]
    metadata = body.get("metadata")
    return {"model": body.get("model"), "max_tokens": body.get("max_tokens"), "stream": body.get("stream"),
            "thinking": body.get("thinking"), "output_config": body.get("output_config"),
            "temperature": body.get("temperature"), "top_p": body.get("top_p"), "top_k": body.get("top_k"),
            "tool_choice": body.get("tool_choice"), "effort_markers": len(markers),
            "last_effort_marker": markers[-1].get("effort") if markers else None,
            "metadata_keys": sorted(metadata) if isinstance(metadata, dict) else None,
            "keys": sorted(body)}


def anthropic_cache_facts(body: dict, affinity_id: str | None) -> dict:
    """Everything in one Messages request that decides prompt-cache reuse, without its text.

    Digests exclude ``cache_control`` so consecutive requests of one trial can be
    compared block by block; the breakpoints are listed separately with their TTL.
    """
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    system = body.get("system")
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    breakpoints = [mark for index, tool in enumerate(tools) if (mark := _breakpoint(f"tools[{index}]", tool))]
    if isinstance(system, list):
        breakpoints += [mark for index, block in enumerate(system)
                        if (mark := _breakpoint(f"system[{index}]", block))]
    for index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            breakpoints += [mark for position, block in enumerate(content)
                            if (mark := _breakpoint(f"messages[{index}][{position}]", block))]
    names = [tool.get("name") for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
    return {
        "n_messages": len(messages), "n_tools": len(tools),
        "tool_name_bytes": sum(len(name.encode("utf-8")) + 1 for name in names),
        "deferred_tools": sum(1 for tool in tools if isinstance(tool, dict) and tool.get("defer_loading")),
        "breakpoints": breakpoints,
        "h_tools": _digest(_without_cache_control(tools)),
        "h_system": _digest(_without_cache_control(system)),
        "h_params": _digest({key: body.get(key) for key in ("thinking", "output_config", "tool_choice")}),
        "h_messages": [_digest(_without_cache_control(message)) for message in messages],
        "affinity_digest": hashlib.sha256(affinity_id.encode("utf-8")).hexdigest()[:12] if affinity_id else "",
    }


class AnthropicStreamFacts:
    """Usage and response identity read from an Anthropic SSE stream as it passes through."""

    def __init__(self):
        self.pending = b""
        self.facts = {}

    def feed(self, chunk: bytes) -> None:
        self.pending += chunk
        *lines, self.pending = self.pending.split(b"\n")
        for line in lines:
            self.line(line)

    def close(self) -> None:
        if self.pending:
            self.line(self.pending)
            self.pending = b""

    def line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        if b'"estimated_tokens"' in line:
            self.facts["estimated_tokens"] = True
        if not any(marker in line for marker in _ANTHROPIC_EVENT_MARKERS):
            return
        try:
            event = json.loads(line[5:])
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start" and isinstance(event.get("message"), dict):
            message = event["message"]
            self.facts.update(usage_start=message.get("usage"), response_model=message.get("model"),
                              message_id=str(message.get("id") or "")[:48],
                              context_management="context_management" in message)
        elif kind == "message_delta":
            self.facts["usage_delta"] = event.get("usage")
            self.facts["stop_reason"] = (event.get("delta") or {}).get("stop_reason")
        elif kind == "content_block_start":
            block_type = str((event.get("content_block") or {}).get("type"))
            blocks = self.facts.setdefault("blocks", {})
            blocks[block_type] = blocks.get(block_type, 0) + 1
        elif kind == "error":
            self.facts["stream_error"] = str(event.get("error"))[:300]


def anthropic_error(status: int, message: str) -> web.Response:
    return web.json_response({"type": "error", "error": {"type": "api_error", "message": message}}, status=status)


class Relay:
    def __init__(self, args):
        self.args = args
        pairs = load_keys(args.origin, args.provider)
        if args.key_suffixes:
            wanted = args.key_suffixes.split(",")
            pairs = [pair for pair in pairs if pair[0] in wanted]
        self.key_names = [name for name, _ in pairs]
        self.keys = [secret for _, secret in pairs]
        self.key_cycle = itertools.cycle(range(len(self.keys)))
        self.cool_until = [0.0] * len(self.keys)
        self.dead = set()
        self.log_path = Path(args.log)
        self.files = Path(args.files_dir) if args.files_dir else None
        self.inflight = {}
        self.session = None

    async def start(self, app):
        self.session = ClientSession(
            connector=TCPConnector(limit=0, ttl_dns_cache=300),
            timeout=ClientTimeout(total=None, sock_connect=30, sock_read=self.args.read_timeout),
            trust_env=False)

    async def stop(self, app):
        await self.session.close()

    def scrub(self, text):
        text = str(text)
        for key in self.keys:
            text = text.replace(key, "<key>")
        return LONG_DIGITS.sub("<digits>", text)

    def log(self, record):
        record["ts"] = round(time.time(), 3)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def probe(self, request):
        arm = request.match_info.get("arm", "-")
        self.log({"kind": "probe", "arm": arm, "peer": request.remote, "query": dict(request.query)})
        return web.json_response({"ok": True, "peer": request.remote, "arm": arm})

    async def models(self, request):
        return web.json_response({"object": "list", "data": [
            {"id": self.args.model, "object": "model", "owned_by": "relay"}]})

    async def file(self, request):
        if self.files is None:
            raise web.HTTPNotFound()
        name = request.match_info["name"]
        path = (self.files / name).resolve()
        if path.parent != self.files.resolve() or not path.is_file():
            raise web.HTTPNotFound()
        self.log({"kind": "file", "name": name, "peer": request.remote})
        return web.FileResponse(path)

    def shape(self, body, arm):
        """Same model, sampling and pinned effort for every arm; everything else is the harness's own request."""
        original = {k: body.get(k) for k in ("model", "temperature", "top_p", "max_tokens",
                                              "max_completion_tokens", "stream", "reasoning_effort",
                                              "thinking", "tool_stream")}
        body["model"] = self.args.model
        if self.args.max_tokens is not None:
            body.pop("max_completion_tokens", None)
            body["max_tokens"] = self.args.max_tokens
        if self.args.temperature is not None:
            body["temperature"] = self.args.temperature
        if self.args.top_p is not None:
            body["top_p"] = self.args.top_p
        if self.args.reasoning_effort is not None:
            body["reasoning_effort"] = self.args.reasoning_effort
        return original

    async def pick_key(self, deadline):
        """Next live key not cooling down after a 429; waits for the earliest one otherwise."""
        while True:
            live = [i for i in range(len(self.keys)) if i not in self.dead]
            if not live:
                return None
            now = time.monotonic()
            for _ in range(len(self.keys)):
                index = next(self.key_cycle)
                if index in live and self.cool_until[index] <= now:
                    return index
            wake = min(self.cool_until[i] for i in live)
            if wake > deadline:
                return None
            await asyncio.sleep(max(0.05, wake - now) + random.random() * 0.5)

    async def chat(self, request):
        arm = request.match_info.get("arm", "-")
        started = time.monotonic()
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "invalid JSON body"}}, status=400)
        original = self.shape(body, arm)
        stream = bool(body.get("stream"))
        affinity_id = request.headers.get("Mt-Session-Id")
        record = {"kind": "chat", "arm": arm, "peer": request.remote, "orig": original,
                  "sent": {k: body.get(k) for k in ("temperature", "top_p", "max_tokens", "reasoning_effort")},
                  "n_messages": len(body.get("messages") or []), "n_tools": len(body.get("tools") or [])}
        record["cache_affinity"] = "Mt-Session-Id" if affinity_id else ""

        def headers_for(key):
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                       "Accept-Encoding": "identity"}
            if affinity_id:
                headers["Mt-Session-Id"] = affinity_id
            return headers

        def failure(status, error):
            return web.json_response({"error": {"message": f"relay upstream failure: {error}",
                                                "type": "upstream_error"}}, status=status)

        return await self.dispatch(request, arm=arm, url=UPSTREAM, body=body, headers_for=headers_for,
                                   record=record, started=started, retry_statuses=RETRY_STATUSES,
                                   forward=lambda upstream: self.forward(request, upstream, stream, record, started),
                                   failure=failure)

    def archive_anthropic_body(self, record, body, forwarded):
        """Write one request body for later replay, when --anthropic-body-dir is set.

        Off by default: bodies are prompt text, so this is a debugging facility
        for a bounded probe, not a run-time log. The affinity value is replaced
        by a marker so a replay can reproduce or remove the header.
        """
        directory = Path(self.args.anthropic_body_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = '{}__{}__{:04d}__{:d}.json'.format(
            str(record.get('arm')).replace('/', '_'), str(record.get('task')).replace('/', '_'),
            record.get('n_messages') or 0, round(time.time() * 1000))
        payload = {'body': body, 'headers': forwarded, 'arm': record.get('arm'), 'task': record.get('task')}
        (directory / name).write_text(json.dumps(payload, ensure_ascii=False))

    async def messages(self, request):
        """Anthropic Messages passthrough: the harness's request with only ``model`` pinned."""
        return await self.anthropic(request, ANTHROPIC_UPSTREAM, kind="messages")

    async def count_tokens(self, request):
        return await self.anthropic(request, ANTHROPIC_UPSTREAM + "/count_tokens", kind="count_tokens")

    async def anthropic(self, request, url, *, kind):
        arm = request.match_info.get("arm", "-")
        started = time.monotonic()
        if not self.args.anthropic_model:
            return anthropic_error(404, "this relay has no --anthropic-model")
        try:
            body = await request.json()
        except Exception:
            return anthropic_error(400, "invalid JSON body")
        if not isinstance(body, dict):
            return anthropic_error(400, "the request body must be a JSON object")
        forwarded = {name.lower(): value for name, value in request.headers.items()
                     if name.lower() in ANTHROPIC_FORWARDED_HEADERS}
        affinity_id = forwarded.get("mt-session-id")
        stream = kind == "messages" and bool(body.get("stream"))
        record = {"kind": kind, "arm": arm, "task": request.match_info.get("task", ""), "peer": request.remote,
                  "orig": anthropic_settings(body), "sent_model": self.args.anthropic_model,
                  "headers_in": sorted({name.lower() for name in request.headers}),
                  "anthropic_beta": forwarded.get("anthropic-beta", ""),
                  "anthropic_version": forwarded.get("anthropic-version", ""),
                  "trace_id": forwarded.get("m-traceid", ""),
                  "cache_affinity": "Mt-Session-Id" if affinity_id else "",
                  "body_bytes": request.content_length,
                  **anthropic_cache_facts(body, affinity_id)}
        body["model"] = self.args.anthropic_model
        if kind == "messages" and self.args.anthropic_body_dir:
            self.archive_anthropic_body(record, body, forwarded)
        if kind == "messages" and not self.args.preserve_pi_effort_markers:
            record["pi_effort_fold"] = compact_pi_effort_markers(body)
        record["sent_effort"] = (body.get("output_config") or {}).get("effort")
        record["sent_thinking"] = body.get("thinking")

        def headers_for(key):
            headers = {"x-api-key": key, "Authorization": f"Bearer {key}",
                       "Content-Type": "application/json", "Accept-Encoding": "identity"}
            headers.update(forwarded)
            headers.setdefault("anthropic-version", "2023-06-01")
            return headers

        def failure(status, error):
            return anthropic_error(status, f"relay upstream failure: {error}")

        return await self.dispatch(request, arm=arm, url=url, body=body, headers_for=headers_for,
                                   record=record, started=started, retry_statuses=ANTHROPIC_RETRY_STATUSES,
                                   forward=lambda upstream: self.forward_anthropic(
                                       request, upstream, stream, record, started),
                                   failure=failure)

    async def dispatch(self, request, *, arm, url, body, headers_for, record, started, retry_statuses,
                       forward, failure):
        """One upstream call under the shared key pool, 429 cool-down and retry budget."""
        self.inflight[arm] = self.inflight.get(arm, 0) + 1
        attempt, delay, last = 0, 2.0, {"status": None, "error": "no live key"}
        deadline = started + self.args.retry_budget
        try:
            while True:
                key_index = await self.pick_key(deadline)
                if key_index is None:
                    break
                attempt += 1
                try:
                    upstream = await self.session.post(url, json=body, headers=headers_for(self.keys[key_index]))
                except (asyncio.TimeoutError, OSError, Exception) as error:  # transport failure
                    last = {"status": None, "error": self.scrub(f"{type(error).__name__}: {str(error)[:300]}")}
                    upstream = None
                if upstream is not None and upstream.status == 200:
                    record.update(status=200, attempts=attempt, key=self.key_names[key_index],
                                  ttfb_s=round(time.monotonic() - started, 3))
                    return await forward(upstream)
                if upstream is not None:
                    text = self.scrub((await upstream.text())[:600])
                    upstream.release()
                    last = {"status": upstream.status, "error": text}
                    if upstream.status in (401, 402, 403):
                        self.dead.add(key_index)
                        self.log({"kind": "key_retired", "key": self.key_names[key_index],
                                  "status": upstream.status, "error": text[:160]})
                        continue
                    if upstream.status not in retry_statuses:
                        break
                record.setdefault("retries", []).append({"status": last["status"], "key": self.key_names[key_index],
                                                         "error": (last["error"] or "")[:160]})
                if last["status"] == 429:
                    # Per-minute RPM window: rest this key, try another one right away.
                    self.cool_until[key_index] = time.monotonic() + 5 + random.random() * 5
                    continue
                if time.monotonic() + delay > deadline:
                    break
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30.0)
            record.update(status=last["status"] or 502, attempts=attempt, error=last["error"],
                          latency_s=round(time.monotonic() - started, 3))
            self.log(record)
            return failure(last["status"] if last["status"] else 502, last["error"])
        finally:
            self.inflight[arm] -= 1

    async def forward(self, request, upstream, stream, record, started):
        usage = None
        try:
            if not stream:
                data = await upstream.read()
                try:
                    usage = json.loads(data).get("usage")
                except Exception:
                    pass
                response = web.Response(body=data, status=200, content_type="application/json")
                return response
            response = web.StreamResponse(status=200, headers={
                "Content-Type": upstream.headers.get("Content-Type", "text/event-stream"),
                "Cache-Control": "no-cache"})
            await response.prepare(request)
            tail = b""
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
                tail = (tail + chunk)[-8192:]
            await response.write_eof()
            for line in reversed(tail.split(b"\n")):
                line = line.strip()
                if line.startswith(b"data:") and b'"usage"' in line:
                    try:
                        usage = json.loads(line[5:].strip()).get("usage")
                    except Exception:
                        pass
                    if usage:
                        break
            return response
        except (ConnectionResetError, asyncio.CancelledError) as error:
            record["client_gone"] = type(error).__name__
            raise
        finally:
            upstream.release()
            record.update(usage=usage, latency_s=round(time.monotonic() - started, 3))
            self.log(record)

    async def forward_anthropic(self, request, upstream, stream, record, started):
        facts = AnthropicStreamFacts()
        try:
            if not stream:
                data = await upstream.read()
                try:
                    reply = json.loads(data)
                except ValueError:
                    reply = {}
                if isinstance(reply, dict):
                    facts.facts.update(usage=reply.get("usage"), input_tokens=reply.get("input_tokens"),
                                       response_model=reply.get("model"), stop_reason=reply.get("stop_reason"),
                                       message_id=str(reply.get("id") or "")[:48])
                return web.Response(body=data, status=200, content_type="application/json")
            response = web.StreamResponse(status=200, headers={
                "Content-Type": upstream.headers.get("Content-Type", "text/event-stream"),
                "Cache-Control": "no-cache"})
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
                facts.feed(chunk)
            facts.close()
            await response.write_eof()
            return response
        except (ConnectionResetError, asyncio.CancelledError) as error:
            record["client_gone"] = type(error).__name__
            raise
        finally:
            upstream.release()
            record.update(facts.facts)
            record["latency_s"] = round(time.monotonic() - started, 3)
            self.log(record)

    async def stats(self, request):
        now = time.monotonic()
        return web.json_response({"inflight": self.inflight, "keys": self.key_names,
                                  "dead": [self.key_names[i] for i in self.dead],
                                  "cooling": [self.key_names[i] for i, t in enumerate(self.cool_until) if t > now]})


def build_app(relay: Relay) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(relay.start)
    app.on_cleanup.append(relay.stop)
    for prefix in ("", "/arm/{arm}"):
        for path in ("/v1/chat/completions", "/chat/completions"):
            app.router.add_post(prefix + path, relay.chat)
        app.router.add_get(prefix + "/v1/models", relay.models)
        app.router.add_get(prefix + "/models", relay.models)
        app.router.add_get(prefix + "/probe", relay.probe)
    for prefix in ("", "/arm/{arm}", "/arm/{arm}/task/{task}"):
        app.router.add_post(prefix + "/anthropic/v1/messages", relay.messages)
        app.router.add_post(prefix + "/anthropic/v1/messages/count_tokens", relay.count_tokens)
    app.router.add_get("/files/{name}", relay.file)
    app.router.add_get("/stats", relay.stats)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18431)
    parser.add_argument("--origin", default="http://127.0.0.1:15000", help="live Tofu application")
    parser.add_argument("--provider", default="example-corp")
    parser.add_argument("--key-suffixes", help="comma list of key hint suffixes (last 4 chars) to use")
    parser.add_argument("--model", default="glm-5.3-flash-m17")
    parser.add_argument("--anthropic-model",
                        help="FRIDAY wire model for the Anthropic Messages face (enables /anthropic/v1/messages)")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--retry-budget", type=float, default=900.0)
    parser.add_argument("--read-timeout", type=float, default=900.0)
    parser.add_argument("--anthropic-body-dir",
                        help="debugging: also write each Anthropic request body to this directory "
                             "(prompt text; off by default)")
    parser.add_argument("--preserve-pi-effort-markers", action="store_true",
                        help="pass Pi's empty mid-conversation effort markers through "
                             "unchanged (FRIDAY rejects them; used only to measure that)")
    parser.add_argument("--log", required=True)
    parser.add_argument("--files-dir")
    args = parser.parse_args()
    relay = Relay(args)
    asyncio.run(serve(build_app(relay), relay, args))


async def serve(app, relay, args):
    """Bind as soon as the port is free, so a successor can take over from a draining relay."""
    import signal

    runner = web.AppRunner(app, access_log=None, shutdown_timeout=120)
    await runner.setup()
    while True:
        try:
            await web.TCPSite(runner, args.host, args.port).start()
            break
        except OSError:
            await asyncio.sleep(0.2)
    print(f"relay listening on {args.host}:{args.port} keys={relay.key_names} model={args.model} "
          f"anthropic_model={args.anthropic_model} read_timeout={args.read_timeout}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()


if __name__ == "__main__":
    main()
