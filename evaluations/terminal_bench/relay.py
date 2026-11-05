"""Fair-comparison model relay for native sandbox Terminal-Bench runs.

Both harness arms call this one relay; it forwards every chat completion to the
same FRIDAY deployment with the same AppId pool, so model identity, rate limits
and keys are identical across arms. ``--temperature/--top-p/--max-tokens``
override every request identically, which is how a protocol's sampling is
enforced. Credentials are read from the live Tofu model-routing authority
inside this process only; sandboxes see a key-less URL.

Entry point: ``python -m evaluations.terminal_bench.relay --log FILE ...``.
Dependencies: aiohttp (server/upstream), httpx (credential reveal).

Routes (``<arm>`` is a free label used only for accounting):
  POST /arm/<arm>/v1/chat/completions  (also without /v1, and without /arm/<arm>)
  GET  /arm/<arm>/v1/models
  GET  /probe, /arm/<arm>/probe        reachability check from inside a sandbox
  GET  /files/<name>                   static artifacts (e.g. the tofu wheel)
"""
import argparse
import asyncio
import itertools
import json
import random
import re
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

UPSTREAM = "https://api.openai.com/v1/chat/completions"
LONG_DIGITS = re.compile(r"\d{12,}")
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


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
        """Same model and sampling for every arm; everything else is the harness's own request."""
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
        record = {"kind": "chat", "arm": arm, "peer": request.remote, "orig": original,
                  "n_messages": len(body.get("messages") or []), "n_tools": len(body.get("tools") or [])}
        self.inflight[arm] = self.inflight.get(arm, 0) + 1
        attempt, delay, last = 0, 2.0, {"status": None, "error": "no live key"}
        deadline = started + self.args.retry_budget
        try:
            while True:
                key_index = await self.pick_key(deadline)
                if key_index is None:
                    break
                attempt += 1
                headers = {"Authorization": f"Bearer {self.keys[key_index]}",
                           "Content-Type": "application/json", "Accept-Encoding": "identity"}
                try:
                    upstream = await self.session.post(UPSTREAM, json=body, headers=headers)
                except (asyncio.TimeoutError, OSError, Exception) as error:  # transport failure
                    last = {"status": None, "error": self.scrub(f"{type(error).__name__}: {str(error)[:300]}")}
                    upstream = None
                if upstream is not None and upstream.status == 200:
                    record.update(status=200, attempts=attempt, key=self.key_names[key_index],
                                  ttfb_s=round(time.monotonic() - started, 3))
                    return await self.forward(request, upstream, stream, record, started)
                if upstream is not None:
                    text = self.scrub((await upstream.text())[:600])
                    upstream.release()
                    last = {"status": upstream.status, "error": text}
                    if upstream.status in (401, 402, 403):
                        self.dead.add(key_index)
                        self.log({"kind": "key_retired", "key": self.key_names[key_index],
                                  "status": upstream.status, "error": text[:160]})
                        continue
                    if upstream.status not in RETRY_STATUSES:
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
            status = last["status"] if last["status"] else 502
            return web.json_response({"error": {"message": f"relay upstream failure: {last['error']}",
                                                "type": "upstream_error"}}, status=status)
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

    async def stats(self, request):
        now = time.monotonic()
        return web.json_response({"inflight": self.inflight, "keys": self.key_names,
                                  "dead": [self.key_names[i] for i in self.dead],
                                  "cooling": [self.key_names[i] for i, t in enumerate(self.cool_until) if t > now]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18431)
    parser.add_argument("--origin", default="http://127.0.0.1:15000", help="live Tofu application")
    parser.add_argument("--provider", default="example-corp")
    parser.add_argument("--key-suffixes", help="comma list of key hint suffixes (last 4 chars) to use")
    parser.add_argument("--model", default="glm-5.3-flash-m17")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--retry-budget", type=float, default=900.0)
    parser.add_argument("--read-timeout", type=float, default=900.0)
    parser.add_argument("--log", required=True)
    parser.add_argument("--files-dir")
    args = parser.parse_args()
    relay = Relay(args)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(relay.start)
    app.on_cleanup.append(relay.stop)
    for prefix in ("", "/arm/{arm}"):
        for path in ("/v1/chat/completions", "/chat/completions"):
            app.router.add_post(prefix + path, relay.chat)
        app.router.add_get(prefix + "/v1/models", relay.models)
        app.router.add_get(prefix + "/models", relay.models)
        app.router.add_get(prefix + "/probe", relay.probe)
    app.router.add_get("/files/{name}", relay.file)
    app.router.add_get("/stats", relay.stats)
    asyncio.run(serve(app, relay, args))


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
          f"read_timeout={args.read_timeout}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()


if __name__ == "__main__":
    main()
