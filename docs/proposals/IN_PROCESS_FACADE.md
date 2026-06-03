# Proposal: A supported in-process façade for embedders

> **Status:** IMPLEMENTED (v1). Shipped as the top-level `tofu` package over a
> shared kernel `lib/tasks_pkg/entry.py`. This note is kept as the design
> rationale + the record of decisions taken. See `docs/HEADLESS_API.md` §4.5
> for user-facing docs.
>
> **Decisions taken** (the §5 open questions, now resolved):
> 1. **Location** — top-level `tofu/` package (the in-process façade imports
>    `lib/` directly; `clients/python/` is the *HTTP* SDK, a different layer).
>    The shared kernel lives in `lib/tasks_pkg/entry.py`.
> 2. **Sync + streaming generator** for v1. No async surface yet (the first
>    consumer, keyan, is sync Flask).
> 3. **v1 scope** — `chat` / `stream` / `capabilities`. **Billing and BYO stay
>    HTTP-only** — they are key-scoped concerns, deliberately excluded from the
>    in-process kernel.
> 4. **Ownership** — Tofu-repo deliverable.

## 1. Problem

Tofu today exposes exactly two integration surfaces:

1. **The HTTP API** (`/api/v1/*`, `/v1/*`) + the `tofu-sdk` HTTP client.
2. **The raw `lib/` internals** (~100 modules), with no stability contract.

There is no supported way to call Tofu's orchestrator **in-process** —
same Python interpreter, no HTTP hop. An embedder that wants this (lower
latency, shared process, no separate deployment) is forced to reach into
`lib/` directly.

### Evidence this gap is real

The `keyan` project — a downstream consumer used to dogfood Tofu —
did exactly this. It:

- imported `lib._chatui.llm_dispatch.dispatch_chat` (the lowest-level
  primitive) directly, then
- **vendored ~15,000 lines** of `lib/` into its own tree (`keyan/lib/_chatui/`)
  to pin the internals it depends on, and
- re-implemented JSON mode, streaming callbacks, and an error classifier
  on top of the primitive — features the HTTP layer already provides.

Every one of those workarounds traces back to the same root cause: the only
in-process entrypoint available was the raw dispatcher, far below the
orchestrator that actually carries the features (tool loop, fallback chain,
compaction, typed errors, MCP).

## 2. Goal / non-goals

**Goal:** a small, versioned, in-process API that wraps the *same*
`create_task` / `spawn_task` orchestrator path the HTTP routes use — so an
embedder gets tool use, thinking, fallback, compaction, MCP, and typed errors
without HTTP and without importing `lib/` internals.

**Non-goals:**
- Not a new orchestrator. It calls the existing one.
- Not a replacement for the HTTP API or `tofu-sdk`. Those remain the default
  for out-of-process callers.
- Not a stability promise over `lib/`. The façade is the *only* in-process
  contract; `lib/` stays private.

## 3. Proposed shape

A new top-level package `tofu/` (importable as `import tofu`) that mirrors the
HTTP surface method-for-method, so the mental model transfers 1:1:

```python
import tofu

# Mirrors POST /api/v1/chat/completions, but in-process.
result = tofu.chat(
    messages=[{"role": "user", "content": "Hi"}],
    model="claude-opus-4-7",
    response_format={"type": "json_object"},   # same knobs as the HTTP body
    config={"thinkingDepth": "high", "tools": ["search"]},
)
print(result.content, result.usage, result.error)   # typed result object

# Streaming yields the SAME event dicts as the SSE/WS contract (§3.6.1).
for ev in tofu.stream(messages=[...], model="..."):
    if ev["type"] == "delta":
        ...
```

Key properties:

- **Thin.** Each function builds a `cfg` + messages and calls
  `create_task` / `spawn_task` — the identical path `routes/api_v1/chat.py`
  takes. No business logic is duplicated; the route handler and the façade
  converge on one core.
- **Same vocabulary.** Request knobs == HTTP body fields; streamed events ==
  the declared event contract; errors == the typed envelope (§3.8). An
  embedder that knows the HTTP API already knows this.
- **Versioned.** `tofu.__api_version__`; additive changes only, same policy as
  `/api/v1`.

### Refactor implied

`routes/api_v1/chat.py::chat_completions` currently mixes HTTP concerns
(parsing, billing, SSE framing) with the core "run a chat task" call. The
façade should extract that core into a shared helper (e.g.
`lib/tasks_pkg/entry.py::run_chat`) that **both** the route and `tofu.chat`
call. This is the bulk of the work and the main risk — it touches the hot
path. It must be behavior-preserving for the HTTP route (covered by the
existing route tests + a new parity test).

## 4. Alternatives considered

- **Do nothing / "just use the HTTP API."** Rejected for the in-process use
  case: the HTTP hop, separate deployment, and SSE re-parsing are exactly what
  an embedder in the same process wants to avoid — and when we offer no
  supported alternative, they vendor `lib/` instead (the keyan outcome).
- **Bless a subset of `lib/` as public.** Rejected: `lib/` is 100 modules with
  cross-imports; drawing a stable line through it is more surface than a thin
  façade, and freezes internals we want to keep refactoring.
- **Generate an in-process client from the OpenAPI spec.** Rejected: that
  reproduces the HTTP semantics (serialization, status codes) without removing
  the thing we're trying to remove (the transport).

## 5. Open questions (need your call)

1. **Package name & location** — `tofu/` top-level in this repo, or shipped
   under `clients/python/` alongside the HTTP SDK?
2. **Sync only, or async too?** The orchestrator spawns a task thread; a sync
   `wait`-style call is straightforward, an async generator for streaming is
   more work.
3. **Scope of v1** — `chat` + `stream` + `capabilities` only to start, or also
   the agent/task endpoints (`agent_run`, `tasks.*`)?
4. **Who owns it** — is this a Tofu-repo deliverable, or does keyan drive it as
   the first consumer and upstream it?

## 6. Recommendation

Proceed, but in two gated steps:

1. **Step A (low risk):** extract the shared `run_chat` core out of
   `routes/api_v1/chat.py` with a parity test proving the HTTP route is
   unchanged. This is a net cleanup regardless of the façade.
2. **Step B (new surface):** add `tofu.chat` / `tofu.stream` /
   `tofu.capabilities` on top of that core, plus docs in `HEADLESS_API.md`
   §4.4 (Native SDKs) and a migration note for keyan.

Do **not** start Step B until the open questions in §5 are answered — a public
in-process API is a long-lived commitment.
