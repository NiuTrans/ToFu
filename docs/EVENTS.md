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
types flowing over `/api/v1/tasks/{id}/stream` and the `chat` push channel.
It does **not** govern
unrelated `TaskRuntime` channels that define their own small private vocabulary
for a non-chat feature (e.g. the paper/translate runtimes emitting
`{'type': 'chunk'}` on their own channel — see CLAUDE.md §14). Those are
self-contained producer↔consumer pairs, not part of the shared task-event
contract, so they are not registered here. The conversation UI consumes the
v3 turn projection/event protocol described below; do not add a second task-SSE
consumer to make a new chat field visible.

Turn-native conversation synchronization is a separate declared protocol. Its
`turn.upsert`, `turn.patch`, `turn.deleted`, `attempt.event`,
`conversation.activity`, heartbeat, and reset envelopes are owned by
`contracts/conversation_sync_v3.yaml` and
[`CONVERSATION_SYNC_V3.md`](CONVERSATION_SYNC_V3.md), not by `EventType`.
Those events are appended transactionally by the Storage Sidecar and consumed
by the one conversation coordinator; do not emit them with `build_event` or
add a second frontend stream.

`turn.compact` is deliberately represented by one small transactional
`conversation.activity` change whose payload is
`{requiresSnapshot: true, conversationRevision}`. Compaction can delete a turn
graph closure, insert a summary, rewrite ancestry, and patch retained
projections atomically; no bounded delta can faithfully describe that mutation.
The coordinator therefore performs one authoritative snapshot re-anchor, and
the replay log never copies the folded transcript or large retained turns.

Provider microchunks are not individually observable contract events. The
stream manager emits the first text delta immediately, then losslessly merges
later content/reasoning for at most 100 ms or 256 characters. It flushes before
every structural boundary and assigns a sequence only to the merged event, so
live delivery and durable replay remain byte-identical without paying one
Sidecar transaction per 4-character provider chunk.

### Activity timeline projection

Execution diagnostics use the registered task-event vocabulary as facts and
one cumulative Turn sidecar as presentation. Tool lifecycle,
`tool_schema_rejected`, retry `phase` cycles (and any phase carrying an HTTP
error status), the `compaction` → `compaction_done` archive/receipt pair,
`model_fallback`, failed/aborted
`model_request_complete`, and terminal errors are folded by
`lib/turn_activity_timeline.py` into `projection.activityTimeline`. The fold is
durable-before-visible through `record_task_event`, so live delivery, reconnect,
and cold snapshot render the same order without a second task-SSE consumer.

The compaction pair becomes one inspectable Turn row keyed by `archiveId`; its
settled form carries the estimated token/message counts before and after plus
the reduction percentage. The transient `phase=compacting` beat remains
progress text and is replaced by that receipt row rather than rendered twice.

Each entry carries a 0-based `llmRound` display anchor — its own 1-based
`R{n}` request-tag round minus one when present, else the last model-request
round tracked as events flow through the fold — so the browser splices every
warning/error row inline under its tool block or model round instead of
rendering one consolidated timeline block.

The sidecar deliberately excludes the two channels that already have a home:
routine `phase` status text (working/thinking/waiting/startup beats — the
live-status surface owns them, persisting them would render the same fact
twice) and per-round `model_request_start` / successful request completion
bookkeeping (the turn trace owns timing). A model request earns a row only
when it fails or is aborted.
Model completion diagnostics additionally carry credential-free `routeId`,
`routeMode`, `routeDecision`, and `failureStage` fields. Repeated recovery
incidents with an explicit `backoff_s` coalesce as counted occurrences whose
`durationMs` is the sum of actual backoffs; their first-to-last wall envelope
must never be presented as one continuous wait.
 The wire boundary re-isolates the same malformed
schema on every model dispatch of an attempt; repeated `tool_schema_rejected`
facts with identical tool, reason, and detail coalesce into one counted row
per attempt rather than one row per request.

`tool_wire_projection` is a separate bounded Request Inspector diagnostic. It
records the ordered final provider tool names, schema-token estimate, resolved
discovery backend, explicit budget, budget compaction/omission names, and an
opaque fingerprint of the exact cache-relevant tools bytes after provider
projection. It is deliberately not rendered into the Turn activity
timeline, does not duplicate full schemas, never enters model context, and
grants no execution authority.

The sidecar is not a second event authority and its rows are not synthetic tool
calls. It is capped at 128 entries and 96 KiB of serialized JSON; repeated
wait/retry/schema rows coalesce by span, tool progress updates the matching
tool row, and old low-priority status rows are reclaimed before failures and
model switches. It never enters the LLM transcript or grants tool authority.
The legacy `error` event remains a terminal compatibility frame; current fatal
paths normally settle with `done.error`.

### Delivery vs construction

`build_event` only *builds* the dict; you still deliver it through the existing
chokepoint:

- From the orchestrator / managers: `append_event(task, build_event(...))`.
- Convenience one-liner (build + deliver): `emit(task, EventType.X, **fields)`
  — wraps `build_event` + `append_event`. Use it for new call sites; the
  explicit two-step is fine where the surrounding code already holds
  `append_event`.

### The PHASE event has its own typed pair — use it

The `phase` event is the stream's **status-text channel** ("Retrying…",
"Sent to kimi-k3…", "Compressing context…") — the pushes a user actually
reads while a turn runs. Its `phase` field is a declared sub-vocabulary with
its own registry (the `Phase` constants + `PhaseSpec` catalogue in
`lib/agent_core/events.py`) and its own constructors:

```python
from lib.agent_core.events import Phase, emit_phase

# ✅ CORRECT — one interface, registered value
emit_phase(task, Phase.RETRYING, detail='…', detailKey='stream.phase.retryGeneric',
           detailArgs={'model': label, 'attempt': n}, attempt=n)

# ❌ FORBIDDEN — a raw literal bypasses the registry (the drift test fails)
append_event(task, {'type': 'phase', 'phase': 'retrying', 'detail': '…'})
```

* `emit_phase(task, Phase.X, **fields)` = `build_phase(Phase.X, **fields)` +
  `append_event` — byte-identical to the old literal, same as `build_event`.
* Delivering through a NON-manager append seam (a production channel's
  `_append_*_event`, the endpoint adapter's `_stream`, an `inner=` envelope)?
  Construct with `build_phase(Phase.X, ...)` and pass the dict to your seam.
* **Adding a NEW phase** = add the `Phase` constant + its `PhaseSpec`
  (domain, one-line purpose, payload-field docs) — it becomes
  machine-discoverable via `/api/v1/capabilities` (`phases` block, grouped
  by domain: `chat` = the shared status row; production channels are
  catalogued for perceivability, their private event *types* stay
  unregistered per the §1 scope ruling). Then emit it via `emit_phase`.
* Frontend-local phase states the client derives itself (e.g.
  `thinking_active`) are NOT pushes — they are deliberately unregistered.

For model streaming, the three progress phases have non-overlapping meaning:

- `waiting_model` is the current attempt waiting for its first semantic
  provider progress; headers, keep-alives and bytes do not turn it into a
  retry.
- `stream_stalled` is the current attempt after reasoning, assistant text or
  valid tool progress has paused.
- `retrying` is emitted only after an actual failed or rate-limited provider
  attempt is being retried. Its `attempt` field is a real retry count, never a
  heartbeat sequence.

Every phase snapshot carries the assigned event `seq`; repeated wait/stall
heartbeats repaint through that sequence. Wait/stall phases remain transient
and do not create activity-timeline retry rows.

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

Model-backed `done` events also carry `streamState`. This is the authoritative
closed provider-stream verdict (`provider_finished`, `premature_close`,
`malformed_stream`, and so on; `semantic_progress_timeout` is retained only for
historical attempt compatibility). Consumers must not
infer success from non-empty content or from a compatibility `finishReason=stop` value. Only
`streamState=provider_finished` is positive stream-completion evidence.

---

## 2. Adding a NEW event type

Editing one file — `lib/agent_core/events.py` — covers it:

1. **Add the constant** to the `EventType` class, under the right category
   block.
2. **Add an `EventSpec`** to the `_SPECS` tuple: its `category`, a one-line
   `purpose`, `terminal` / `requires_response` flags if applicable, and a
   `fields` map documenting the payload (these become the
   `/api/v1/capabilities` `events` block automatically).
3. **Project it deliberately** — if it changes conversation UI, update the
   cumulative turn projection and its typed renderer. If it belongs only to
   generic task clients, update that declared client contract. Never recreate
   the retired chat task-SSE reducer.
4. **Emit it** via `build_event(EventType.NEW_THING, ...)`.

That's it — no second registration list, no capabilities-endpoint edit. The
drift guard (below) confirms you didn't miss a step.

### Versioning

`EVENT_CONTRACT_VERSION` bumps **only on a breaking change to an existing
event's shape** (a field removed / renamed / retyped). A new event type or a
new *optional* field is additive — do **not** bump. Clients are told to ignore
unknown event types and unknown fields, so additive changes are always safe.

### Program lifecycle projection

Native OpenAI PTC and local ToolScript share `program_start` and
`program_output`. Both events carry `programCallId`, model/display round,
source/backend, status, and child call identity; start also carries authored
code and enforced limits, while output carries the aggregate result. Emitters
must update the cumulative `programRuns`/parent `toolRounds` projection before
delivery. Real child calls keep their ordinary tool lifecycle, so clients show
one program parent plus inspectable children and never invent native adoption
from an `execute_tools` gateway call.

---

## 3. The drift guards (what CI enforces)

| Test | Enforces |
|------|----------|
| `tests/test_event_registry.py` | Every event the backend emits — whether written as a `'type': 'x'` literal **or** `EventType.X` — is registered. Every `ev.type === "..."` the frontend handles is registered. No orphan specs. |
| `tests/test_event_emit.py` | `build_event` is byte-identical to the literal (incl. key order); `emit` delivers through `append_event`; a real converted orchestrator helper still emits the exact pre-conversion dict. |
| `tests/test_phase_registry.py` | The PHASE sub-vocabulary: every `phase='x'` in `lib/` is registered (or a documented out-of-channel carve-out), every `.phase === "x"` the frontend branches on is registered, **zero raw `{'type': 'phase'` literals** outside the registry module (the unified-interface ratchet), no dead phase vocabulary, and `build_phase` byte-identity. |

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
rg -n "'type': 'compaction'|\"type\": \"compaction\"" tests/
```

If a test matches, update its assertion to also accept the typed form
(`EventType.COMPACTION`) — the test's *intent* (e.g. "compaction is only
emitted from `_archive`") is still valid; only its string-matching needs to
recognize the new construction.

**Precedent:** `tests/test_compaction_invariants.py::test_compaction_event_emit_sites_are_audited`
hard-matched `'type': 'compaction'`; the 2026-06 conversion broke it until the
assertion was widened to `... or 'EventType.COMPACTION' in all_src`.

---

## 5. Why this exists (one paragraph)

Before the registry, ~40 event `type` strings were an *implicit* contract,
defined only by scattered `append_event(task, {'type': ...})` calls and the
`ev.type === "..."` ladders in the JS. A third party building a frontend had to
reverse-engineer the stream by reading our source. The registry makes the
contract explicit, versioned, machine-discoverable (`/api/v1/capabilities`),
and drift-guarded — and `build_event`/`EventType` is the discipline that keeps
every emission pinned to it.
