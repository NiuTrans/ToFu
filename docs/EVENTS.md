# Emitting streaming events — contributor guide

> **Audience: anyone adding or changing backend code that emits an SSE / push
> event.** If you are *consuming* the event stream (building a frontend / SDK),
> read [`HEADLESS_API.md` §3.6.1](HEADLESS_API.md#361-streaming-event-contract--the-frontendbackend-sync-interface)
> instead — that documents the wire vocabulary a client sees.

The event vocabulary is a **single, declared, versioned contract**. There is
ONE source of truth — [`lib/agent_core/events.py`](../lib/agent_core/events.py) —
and ONE way to construct an event. This guide is the rule set that keeps it that
way.

---

## 1. The rule

> **Never write a raw `{'type': '...'}` dict for a streaming event. Always
> construct it with `build_event(EventType.X, ...)`.**

```python
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event

# ✅ CORRECT
append_event(task, build_event(EventType.PHASE,
                               phase='llm_thinking', detail='Working…', round=1))

# ❌ FORBIDDEN — a raw literal reintroduces the implicit contract we removed
append_event(task, {'type': 'phase', 'phase': 'llm_thinking',
                    'detail': 'Working…', 'round': 1})
```

`build_event(type_, **fields)` returns `{'type': type_, **fields}` and is
**byte-for-byte identical** to the literal — Python preserves keyword-argument
insertion order. The conversion changes the *construction site*, never the
wire output. So there is no behavioural cost to following the rule, only the
benefit that every emission references the declared vocabulary.

### Scope: what this rule covers

This rule governs the **chat / agent task event stream** — the ~41 registered
types flowing over `/api/chat/stream`, `/api/v1/tasks/{id}/stream`, and the
`chat` push channel (the ones a frontend renders). It does **not** govern
unrelated `TaskRuntime` channels that define their own small private vocabulary
for a non-chat feature (e.g. the paper/translate runtimes emitting
`{'type': 'chunk'}` on their own channel — see CLAUDE.md §14). Those are
self-contained producer↔consumer pairs, not part of the shared frontend
contract, so they are not registered here. If in doubt: if the built-in chat
frontend (`sse_pipeline.js`) needs to understand it, it belongs in the registry.

### Delivery vs construction

`build_event` only *builds* the dict; you still deliver it through the existing
chokepoint:

- From the orchestrator / managers: `append_event(task, build_event(...))`.
- Convenience one-liner (build + deliver): `emit(task, EventType.X, **fields)`
  — wraps `build_event` + `append_event`. Use it for new call sites; the
  explicit two-step is fine where the surrounding code already holds
  `append_event`.

### Events built up conditionally

When fields are added based on runtime conditions, construct the typed base and
mutate exactly as before:

```python
done = build_event(EventType.DONE)
if task.get('preset'):
    done['preset'] = task['preset']
if usage:
    done['usage'] = usage
append_event(task, done)
```

---

## 2. Adding a NEW event type

Editing one file — `lib/agent_core/events.py` — covers it:

1. **Add the constant** to the `EventType` class, under the right category
   block.
2. **Add an `EventSpec`** to the `_SPECS` tuple: its `category`, a one-line
   `purpose`, `terminal` / `requires_response` flags if applicable, and a
   `fields` map documenting the payload (these become the
   `/api/v1/capabilities` `events` block automatically).
3. **Handle it in the frontend** — add the `ev.type === "..."` branch in
   `static/js/ui/sse_pipeline.js` (and `branch.js` if relevant), OR add it to
   `TRANSPORT_TYPES` if it is a stream-internal signal a frontend should ignore.
4. **Emit it** via `build_event(EventType.NEW_THING, ...)`.

That's it — no second registration list, no capabilities-endpoint edit. The
drift guard (below) confirms you didn't miss a step.

### Versioning

`EVENT_CONTRACT_VERSION` bumps **only on a breaking change to an existing
event's shape** (a field removed / renamed / retyped). A new event type or a
new *optional* field is additive — do **not** bump. Clients are told to ignore
unknown event types and unknown fields, so additive changes are always safe.

---

## 3. The drift guards (what CI enforces)

| Test | Enforces |
|------|----------|
| `tests/test_event_registry.py` | Every event the backend emits — whether written as a `'type': 'x'` literal **or** `EventType.X` — is registered. Every `ev.type === "..."` the frontend handles is registered. No orphan specs. |
| `tests/test_event_emit.py` | `build_event` is byte-identical to the literal (incl. key order); `emit` delivers through `append_event`; a real converted orchestrator helper still emits the exact pre-conversion dict; the external-backend `NormalizedEvent` path maps onto registered `EventType`s. |

If you add an emitter in a NEW file, add its path to `_BACKEND_FILES` in
`tests/test_event_registry.py` so the new call sites are scanned.

---

## 4. ⚠️ Gotcha — grep tests for literal type strings BEFORE converting

Converting `{'type': 'foo'}` → `build_event(EventType.FOO)` **removes the
literal string `'type': 'foo'` from the source file.** Any test that does a
*static source scan* for that literal will break — not because behaviour
changed, but because the substring it greps for is gone.

**Before converting an emitter, grep `tests/` for the event's literal type
string:**

```bash
grep -rn "'type': 'compaction'" tests/      # the substring the audit matches
grep -rn '"type": "compaction"' tests/
```

If a test matches, update its assertion to also accept the typed form
(`EventType.COMPACTION`) — the test's *intent* (e.g. "compaction is only
emitted from `_archive`") is still valid; only its string-matching needs to
recognize the new construction.

**Precedent:** `tests/test_compaction_invariants.py::test_compaction_event_emit_sites_are_audited`
hard-matched `'type': 'compaction'`; the 2026-06 conversion broke it until the
assertion was widened to `... or 'EventType.COMPACTION' in all_src`.

---

## 5. External CLI backends (Claude Code / Codex)

The external-backend ingress path is converged onto the same vocabulary, so
you don't get a second event model:

- `lib/agent_backends/protocol.py` declares `KIND_TO_EVENT_TYPE`, the explicit
  map from each `NormalizedEventKind` → wire `EventType`.
- `lib/agent_backends/sse_bridge.py` (`SSEBridgeState.translate` and the
  stateless `normalized_to_sse`) emit through `build_event(EventType.X, ...)`.

If you add a `NormalizedEventKind`, add its `KIND_TO_EVENT_TYPE` entry — the
convergence test (`TestNormalizedEventConvergence`) fails otherwise.

---

## 6. Why this exists (one paragraph)

Before the registry, ~40 event `type` strings were an *implicit* contract,
defined only by scattered `append_event(task, {'type': ...})` calls and the
`ev.type === "..."` ladders in the JS. A third party building a frontend had to
reverse-engineer the stream by reading our source. The registry makes the
contract explicit, versioned, machine-discoverable (`/api/v1/capabilities`),
and drift-guarded — and `build_event`/`EventType` is the discipline that keeps
every emission pinned to it.
