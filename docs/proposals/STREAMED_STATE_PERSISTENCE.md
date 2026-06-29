# Proposal — Durable Streamed-State Persistence (the `persist_streamed_field` primitive)

> **Status:** proposal (Phase A scoped; Phase B noted as future direction)
> **Author:** design doc, 2026-06-28
> **Scope:** `lib/swarm/snapshot.py`, `lib/swarm/master.py`,
> `lib/tasks_pkg/manager.py`, `lib/tasks_pkg/endpoint.py`,
> `lib/orchestration_endpoint_adapter.py`, and the frontend recovery readers.
> **Non-goal:** building a new "engine." This formalizes an *existing,
> proven* pattern into one reusable primitive — it is consolidation, not a
> green-field rewrite.

---

## 1. Problem statement

A class of bug has recurred four times in the last week's JOURNAL, each time
fixed in isolation, each time with the *same* shape:

| Incident (JOURNAL 2026-06-28) | Field that vanished | Why |
|---|---|---|
| Swarm "Parallel Execution" panel fails to expand on reload | `_swarmAgents` per-agent state | synthesized live on FE from `swarm_*` SSE; never persisted |
| Endpoint critic/worker thinking refreshes then vanishes at finalize | `thinking` on planner/worker/critic turns | streamed live via `delta{thinking}`; dropped at the finalize boundary |
| `event_id` collision storm (4000 hits) | timer-poll tool events | unregistered task proxy → legacy `len()` seq fallback |
| Orphaned `task_events` rows | per-poll event rows | id never reaches `task_results`, JOIN-based prune can't see them |

The first two are the core of this proposal. The root cause both share is named
explicitly in `lib/swarm/snapshot.py`'s own module docstring and in the JOURNAL:

> **Streaming state is RECONSTRUCTED at render from live SSE events rather than
> PERSISTED as authoritative state.**

When a field only ever lives in the live SSE stream and the frontend's in-memory
synthesis, it is *ephemeral by construction*: correct during the turn, gone on
reload / finalize / re-render. Each fix has been a frontend band-aid
(empty-body recovery, stale-pill guard, reconcile sweep, then finally the
backend snapshot). The repeated band-aids ARE the signal — per JOURNAL:

> *"repeated frontend recovery band-aids … are the tell that the authoritative
> state was never written down."*

The swarm-snapshot fix (2026-06-28) finally wrote the truth down, and it
**worked**. This proposal generalizes that one-off into a primitive so the
*next* streamed-but-reconstructed field (and there will be one) is durable by
default instead of becoming band-aid #4.

---

## 2. What already exists (the proven pattern to formalize)

The swarm-snapshot fix established a **dual-write, CAS-guarded** persistence
pattern. It is currently hand-implemented per field. The two write legs:

### Leg 1 — stamp the LIVE task's tool round (in-turn)
When the spawning chat task is still live in `manager.tasks`, the snapshot is
stamped onto the in-memory `round_entry`; the regular per-round sync
(`manager._sync_partial_to_conversation`, `lib/tasks_pkg/manager.py:1278`)
then persists it to `conversations.messages` and — crucially — does not clobber
it because the field already rides in the round dict it serializes.

### Leg 2 — direct CAS into `conversations.messages` (detached / fire-and-forget)
When the spawning turn has already ended (the common fire-and-forget swarm
case), there is no live task to stamp. `persist_snapshot_to_conversation`
(`lib/swarm/snapshot.py:113`) does an optimistic-locked direct write:

```
SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1
  → find target round  (find_spawn_round: intersect handle agent-ids)
  → stamp_round(target, snapshot)   (idempotent; returns changed?)
UPDATE conversations SET messages=?, updated_at=?
  WHERE id=? AND user_id=1 AND updated_at=?   ← CAS guard
  → rowcount==0 → re-read & retry, up to _MAX_CAS (4)
```

Key properties already encoded in `snapshot.py` (these become the primitive's
contract):

- **Best-effort, never raises** into the producer thread (broad `except` →
  `logger.warning` + return `False`).
- **CAS via `updated_at`** — never clobbers a concurrent frontend sync write.
- **Idempotent stamp** — `stamp_round` returns `False` when the snapshot is
  byte-identical, so no needless DB write / re-render.
- **Round-matching by intersection**, not position — `find_spawn_round`
  matches the spawn round by intersecting the launch handle's agent ids,
  disambiguating multiple waves in one conversation. (Generalizes to "a
  predicate that locates the target round.")

### The materialize-from-authoritative-source rule
`master._build_agent_snapshot` (`lib/swarm/master.py:283`) builds the snapshot
from the authoritative `_results_by_id` (+ scheduler running/pending), NOT from
the live SSE-synthesized array. JOURNAL guardrail:

> *"Source streamed-only fields from the authoritative `_results_by_id`, never
> the live SSE-synthesized array."*

This is a contract requirement of the primitive: the *producer* supplies the
authoritative value; the primitive only persists it.

### A SECOND, genuinely different shape (endpoint turns) — NOT the same primitive
The endpoint loop persists per-turn via `_sync_endpoint_turns_to_conversation`
(`lib/tasks_pkg/endpoint.py:343`, called at planner/worker/synth/critic/replan
boundaries). It is tempting to call this a third caller of the swarm pattern —
but it is a **different persistence shape**, and conflating them would mislead
the implementer:

| | Swarm path | Endpoint path |
|---|---|---|
| Unit written | ONE located tool round's field (`_swarmSnapshot`) | the WHOLE `endpoint_turns` array, rewritten by index |
| How the round is found | agent-id intersection predicate (`find_spawn_round`) | positional — sync rewrites all turns, returns an absolute message index |
| Concurrency control | `updated_at` CAS, retry loop | the per-turn sync owns the row write |
| `thinking` durability today | n/a | ALREADY durable — `thinking` rides inside the turn dicts the array-sync persists (the critic fix made this true) |

So the endpoint case is **not** a hand-rolled copy of the swarm CAS that needs
de-duplicating. Its streamed field (`thinking`) is *already* persisted durably
by its own, appropriate mechanism. Forcing it through a single-round-field CAS
primitive would be a worse fit, not a consolidation.

**Conclusion:** there are TWO persistence shapes, and they should stay two. The
swarm/single-round-field-CAS shape is currently hand-rolled in one place
(`snapshot.py`) and is what *future* streamed-onto-a-tool-round fields will
need — that is what the primitive generalizes. The endpoint turn-array sync
keeps its own path. What binds the two is NOT a shared function but a shared
**contract + ratchet** (§4): "every FE-reconstructed streamed field has a
durable persisted counterpart," whichever mechanism provides it.

---

## 3. Proposed primitive

A single module — `lib/tasks_pkg/streamed_state.py` (new) — exposing one
function for the **single-round-field-CAS shape only** (the swarm case and
future fields that attach to ONE located tool round). The endpoint turn-array
sync is explicitly NOT a caller — it has its own shape (§2) and already
persists `thinking` durably.

```python
def persist_streamed_field(
    conv_id: str,
    *,
    locate_round,            # Callable[[dict], bool]  — predicate: is THIS the target round?
    field: str,              # the round-attached key, e.g. '_swarmSnapshot' (NOT endpoint 'thinking' — see §2)
    value,                   # the authoritative value (producer-supplied, JSON-serializable)
    live_task=None,          # optional: the live task dict, for Leg-1 stamping
    assert_flags=None,       # optional: {'_swarm': True} flags the round must carry for FE to render
    max_cas: int = 4,
) -> bool:
    """Durably persist `value` under `field` on the matching tool round.

    Dual-write, CAS-guarded, best-effort, never raises. Returns True iff the
    value was newly written (False on no-op / unchanged / CAS-exhausted).

    Leg 1: if `live_task` is supplied and live in manager.tasks, stamp the
           in-memory round so the regular partial-sync persists it.
    Leg 2: CAS direct write into conversations.messages for the detached case.
    Both legs are attempted; Leg 2 is the durable backstop.
    """
```

This is a faithful extraction of `snapshot.py` with two generalizations — both
staying WITHIN the single-round-field shape:

1. `find_spawn_round` → caller-supplied `locate_round` predicate. Today the
   sole caller is swarm (agent-id-intersection); the predicate exists so the
   *next* tool-round-attached field (not endpoint turns) can reuse it.
2. The hardcoded `_swarmSnapshot` key + `_swarm:true` assertion → `field` +
   `assert_flags`.

`snapshot.py` becomes a thin wrapper (`persist_snapshot_to_conversation` keeps
its signature, delegates to the primitive with the swarm predicate) so nothing
downstream changes and the existing tests stay green. This is a pure refactor
with exactly one caller on day one — the generalization is justified by the
contract (§4), not by a second caller existing today.

### 3.1 Producer-side contract (the rule that prevents regressions)

A field is "durable" iff its producer, at the authoritative settle point,
calls `persist_streamed_field` with the value sourced from the authoritative
store (not the SSE stream). Producers:

- **Swarm (uses the primitive):** `master._persist_agent_snapshot` (already
  calls the snapshot helper incrementally + at the `finally` settle) → routes
  through `persist_streamed_field`.
- **Endpoint (contract-bound, keeps its own mechanism):**
  `_sync_endpoint_turns_to_conversation` already persists the turn dicts; it
  does NOT call the primitive. The contract it must satisfy is "every streamed
  field on a turn (currently `thinking`) is in the turn dict before sync" —
  which the critic fix made true. The ratchet (§4) verifies this independently
  of which mechanism provides the durability.

### 3.2 Frontend-side contract

The renderer **prefers the persisted field** and treats a snapshot-only round
as renderable, with the live SSE synthesis kept ONLY as the in-turn fast path /
offline fallback. This is exactly what the swarm fix did:

- `_recoverSwarmAgents` (`streaming_swarm_panel.js`) prefers
  `round._swarmSnapshot.agents` before the handle+sibling fallback.
- `_isRoundSwarm` (`tool_rounds.js`) treats a snapshot-only round as renderable.
- `sse_pipeline.js` critic handler applies `ev.thinking` (with live-delta
  fallback).

The contract: **FE renders FROM the persisted field; live synthesis is an
optimization, never the source of truth.** No new FE machinery — this codifies
the pattern the last two fixes already adopted.

---

## 4. Migration order (low-risk, incremental)

1. **Extract** `streamed_state.persist_streamed_field` verbatim from
   `snapshot.py` (pure refactor; `snapshot.py` delegates). Existing tests
   (`test_swarm_snapshot_persist.py`) must stay green unchanged — that is the
   regression gate for the extraction.
2. **Leave the endpoint path as-is** — `thinking` is already durable via the
   turn-array sync (verified by
   `test_orchestration_endpoint_adapter.py::ThinkingPropagationTest`). Do NOT
   re-route it through the primitive; that would be a worse fit (§2). This step
   is a no-op on code — it exists in the plan to make "endpoint stays separate"
   an explicit decision, not an omission.
3. **Document the contract** in `CLAUDE.md` (§ near the streaming-event
   contract), mechanism-agnostic so it binds BOTH shapes:
   "Any field synthesized live on the FE from SSE that must survive reload MUST
   have a durable persisted counterpart written at its authoritative settle
   point, and the renderer MUST prefer the persisted value over live synthesis.
   For a field attached to a single tool round, persist it via
   `persist_streamed_field`; for endpoint turns, ensure it rides the turn dict
   that `_sync_endpoint_turns_to_conversation` persists."
4. **Add the ratchet test — this is the real cross-cutting unifier.** Enumerate
   the streamed fields the FE reconstructs (today: swarm `_swarmSnapshot`,
   endpoint turn `thinking`) and assert each has a durable persisted
   counterpart — *regardless of which mechanism provides it* (the primitive for
   swarm, the turn-dict for endpoint). This is the band-aid-prevention
   mechanism: a new reconstruct-at-render field fails CI until it is made
   durable by EITHER shape. The binding force across the two shapes is this
   contract+ratchet, not a shared function.

Each step is independently shippable and revert-proof; none touches the DB
schema or the `event_log`/`append_event` sequencing seam.

---

## 5. Explicitly out of scope (Phase B — future direction, NOT now)

**Making the persisted `task_events` log the single render source** — i.e. the
frontend stops synthesizing ANY state from live SSE and renders purely from the
durable event log on every path (live + reload). This is the more
*fundamental* fix and would retire the dual-write entirely.

It is deliberately deferred because its blast radius is large and crosses the
exact seam the JOURNAL flags as the most fragile in the backend:

- rewrites `sse_handlers_swarm.js`, `_recoverSwarmAgents`, `_isRoundSwarm`,
  the endpoint adapter, and the hot/cold replay split in `event_log.py`
  simultaneously;
- depends on the `manager.append_event ↔ event_log` sequencing being airtight
  (the very seam that produced the `event_id` collision + orphan-row
  incidents this week);
- would need a render-from-event-log path proven out under reload + reconnect
  + fire-and-forget before it could replace the working dual-write.

**Entry criteria for Phase B** (revisit only when all hold): (a) Phase A
shipped and the band-aid-class incidents have stopped; (b) the
`append_event`/`event_log` seam has gone N weeks with zero collision-canary
hits; (c) a spike proves render-from-event-log handles the fire-and-forget
swarm case at parity with the persisted snapshot.

---

## 6. Why this is the right amount of engineering

- It builds **nothing new** — it extracts one proven function (one caller on
  day one: swarm) and leaves the endpoint path, which is already durable,
  alone.
- It directly kills the **recurring** incident class (4 JOURNAL entries / 1
  week) at its **named root cause**, not its symptoms.
- The **ratchet+contract — not a shared function — is what binds the two
  persistence shapes**, so the fix can't silently rot: the next
  reconstruct-at-render field fails CI until it is made durable by whichever
  shape fits it.
- It respects the constraints: no new frontend framework (CLAUDE.md §3.2), no
  schema change, no touching the fragile sequencing seam.
- The bigger, "more correct" rewrite (Phase B) is named and gated, not
  half-started — avoiding the speculative-abstraction trap.
```

