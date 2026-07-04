# Epic C — Externalize Task/Session Runtime State for Horizontal Scale-Out

> **Status: DESIGN-FIRST / §10-GATED. No code until owner sign-off.**
> Board epic `pt_96b80d88c8d54b71`. This document exists to be reviewed
> *before* a line of runtime-state code is written. It also discharges the
> owner requirement (2026-07-02) that Epic A's remaining per-process caps —
> the SSE per-principal semaphore and the admission controller — have a
> stated, justified path to replica-correctness rather than silently
> multiplying by replica count.

---

## 1. Problem statement

Tofu today is one Hypercorn process. Every piece of *request-spanning live
runtime state* lives in that process's heap:

| State | Location | Consumer |
|---|---|---|
| Task registry | `TaskRuntime._tasks` dict (`lib/agent_core/task_runtime.py:95`), aliased by `manager.tasks` | chat poll/abort/stream, paper/translate poll |
| Conv freshness / supersede index | `_conv_latest_task` (`lib/tasks_pkg/manager.py:69`) | "new task aborts stale task for this conv" |
| Push fan-out subscriptions | `PushHub` singleton (`lib/agent_core/push.py`) | `/api/push` WebSocket (Epic B owns this) |
| Admission ceiling | `AdmissionController` semaphore (`lib/agent_core/admission.py`) | headless paths + UI `/chat/start` (Epic A) |
| **Per-principal SSE count** | `SSELimiter._counts` (`lib/agent_core/sse_limit.py`, Epic A) | `chat_stream` |
| Interactive per-task dicts | `_write_approvals`, `_human_guidance_requests`, `_stdin_requests` | approval / ask_user / stdin routes |

Behind an N-replica load balancer with round-robin (or even least-conn)
routing, **all of the above break or loosen**:

- A poll for task `T` that lands on a replica which didn't create `T` gets
  `not_found` (paper/translate) or a **false `interrupted`** verdict
  (chat — `routes/chat.py:~1810` reads the DB checkpoint and, seeing
  `status='running'` with no in-memory task, assumes a crash).
- An abort for `T` on the wrong replica is a silent no-op.
- The admission ceiling becomes `64 × N` (each replica counts only its own
  in-flight tasks).
- The SSE per-principal cap becomes `12 × N`.
- The supersede invariant ("starting a new task aborts the stale one for
  this conv") only sees the local replica's tasks, so a conv can have one
  live task per replica.

This is the core reason the project is **not** horizontally scalable today,
and hundreds-of-thousands-concurrent *requires* horizontal scale-out.

## 2. Precedent already in the tree

Epic A Step 2b (2026-07-02) proved the pattern for the *stateless-counter*
sub-problem: the open-mode per-IP throttle was re-keyed off a duplicate
in-process dict onto the **shared, pluggable** `lib/rate_limit_store.py`
seam (`record_and_check`, memory | db backends via
`TOFU_RATE_LIMIT_BACKEND`, fail-open, schema v19 `rate_limit_events`). Under
`=db` that counter is authoritative across replicas. That is the template
for the *counter-shaped* pieces of runtime state (admission, SSE cap). The
*object-shaped* pieces (the task registry itself) need a different answer
(§4).

## 3. The two viable strategies

### (a) Shared-store counters / state
Move state into a store every replica reads/writes (Postgres, or Redis if we
add it). Pros: true elasticity — any replica can serve any request; a replica
dying loses nothing. Cons: every state touch is now a network round-trip;
needs careful TTL/lease semantics so a crashed replica's tasks don't wedge
forever; the task *object* (with its live event buffer, abort event,
threading locks) cannot be trivially serialized.

### (b) Sticky sessions
Pin a principal (or a conversation, or a task) to one replica via LB
affinity, so the per-process state IS authoritative for every request that
principal makes. Pros: zero code change to the in-process structures; cheapest
path; keeps the live task object (event buffer, locks, abort) exactly where
it runs. Cons: loses replica-failure resilience for in-flight work (a replica
dying strands its live tasks — but the DB checkpoint + restart-recovery
already handles the reconnect case for chat); uneven load if affinity is
coarse.

## 4. Recommended design — a hybrid, split by state shape

**Do NOT pick one globally. Split by what the state actually is.**

### 4.1 The live task OBJECT → sticky sessions (affinity by task/conv)
The `TaskRuntime._tasks` entry is not a value; it's a live object graph:
an event buffer being appended by a worker thread, an `events_lock`, an
abort `threading.Event`, an SSE `_sse_gen_id`. Serializing that into a shared
store per-append is both expensive and semantically wrong (the worker thread
lives on ONE replica). So:

- **LB affinity keyed by `taskId`** for `/api/chat/stream/<task_id>`,
  `/poll/<task_id>`, `/abort/<task_id>`, and the paper/translate poll/abort
  routes. The task's origin replica is the only one with the live object; all
  its follow-up requests must return there. Implementation: a cookie or a
  consistent-hash on the path's `taskId` at the LB (both are standard;
  consistent-hash avoids a cookie round-trip and survives replica add/remove
  with minimal reshuffle).
- **Fallback stays correct:** if affinity misroutes (replica died, hash
  reshuffled), the existing DB-checkpoint path already serves a terminal
  snapshot for *finished* chat tasks. The design MUST additionally fix the
  wrong-replica **`interrupted`** false-positive (`routes/chat.py:~1810`):
  a `running` checkpoint whose task is absent locally is reported as
  `running` + "reconnect via affinity", not `interrupted`. Per the ratified
  ruling (§6.4) there is NO cross-replica liveness probe — the client simply
  re-routes to the origin replica via `taskId` affinity.

### 4.2 The COUNTERS (admission ceiling, SSE per-principal) → shared store
These are integers, not object graphs — exactly the `rate_limit_store`
shape. Two options, decide at review:

- **Option A (preferred): reuse the `rate_limit_store` seam.** Admission and
  SSE caps become sliding-window / current-count queries against a shared
  table (or Redis counter). The admission "release on terminal" becomes a
  decrement (or a lease TTL that auto-reclaims a crashed replica's slots —
  important, else a dead replica permanently consumes global capacity). The
  SSE cap becomes a per-principal current-count with a lease keyed by
  `(principal, replica, stream_id)` so a dead replica's streams age out.
- **Option B: keep counters per-replica BUT divide the global budget by N.**
  If the deployment is fixed-size (known replica count), set each replica's
  cap to `global / N`. Cheap, no shared store, but brittle (wrong under
  autoscaling; a skewed LB overshoots). Acceptable ONLY as an interim for a
  fixed 2–3 replica deployment; NOT the 100k-concurrent answer.

**Recommendation: Option A**, because it composes with the sticky-session
choice (the counter is authoritative regardless of which replica the request
lands on) and because the precedent + fail-open machinery already exist. The
lease-TTL reclaim is the one genuinely new primitive and must be designed
carefully (a slot held by a dead replica must free itself).

### 4.3 The supersede index (`_conv_latest_task`) → shared store
"Newest task for conv C" is a single value per conv → a tiny shared KV
(`conv_id → (task_id, replica, started_at)`), read on task start to abort the
prior one cross-replica (the abort itself routes to the prior task's replica
via the §4.1 affinity). Small, correctness-critical.

### 4.4 Interactive dicts (approval / guidance / stdin) → sticky (piggyback)
These are inherently tied to the live task object (a human is answering a
prompt THIS task raised), so they ride the §4.1 task affinity for free — no
separate externalization. Document the dependency so a future change doesn't
break it.

## 5. Why this satisfies the Epic A follow-up explicitly

- **SSE per-principal semaphore** → §4.2 Option A: a shared per-principal
  current-count with lease-TTL reclaim. The cap is `N`-invariant. Until Epic C
  ships, the interim posture is honest: the Epic A cap is **per-replica**, so
  behind an LB it is `cap × replicas` — acceptable only as a per-replica blast
  radius bound, and JOURNAL/this doc say so.
- **Admission controller** → §4.2 Option A: a shared in-flight counter with
  lease-TTL. Same `N`-invariance; the `on_terminal` release becomes a
  decrement, and a dead replica's slots reclaim by TTL.

Neither cap is "done for 100k-concurrent" until this design lands. That is
stated, not hidden.

## 6. Decisions — RATIFIED by owner 2026-07-02 (were open questions)

These are now commitments, not questions. The B-shared ones (§4 substrate,
lease-TTL) are ratified in [`EPIC_B_PUSH_FANOUT_DESIGN.md`](EPIC_B_PUSH_FANOUT_DESIGN.md)
§7 and restated here for a single reading of Epic C.

1. **Substrate: Redis**, the single shared substrate for B's pub/sub AND C's
   counters/leases (B §4). Decided jointly because C's lease-TTL reclaim
   (§4.2 here / B §5) needs Redis's native key expiry. Postgres is the
   documented fallback only (B §5.5).
2. **Affinity transport: consistent-hash on `taskId`** (stateless, survives
   replica add/remove with minimal reshuffle; no per-task cookie).
3. **Lease TTL: `lease_ttl = 90s`, heartbeat every 30s** — the SINGLE
   fleet-wide value defined in B §5.4. NOT `max_task_duration + margin`: the
   heartbeat-refresh model (B §5.2) keeps a *living* long task's lease alive
   regardless of TTL, so the small-TTL-plus-heartbeat value is correct and the
   large-TTL idea is retired. The 90s/30s pair is **validated by a
   test/benchmark before the scale rollout, not asserted** (B §7).
4. **The `interrupted` false-positive fix (§4.1): report `running` +
   reconnect-via-affinity, NO cross-replica liveness probe.** A `running`
   checkpoint whose task is absent locally is reported `running` and the client
   re-routes via affinity to the origin replica; we do NOT probe peer replicas
   for liveness (simpler, and affinity should not misroute a live replica).
   The old "unless a cross-replica liveness check confirms the origin dead"
   caveat in §4.1 is superseded by this ruling.
5. **Migration/rollout: memory/`inproc` backend stays default** (single-process
   byte-equivalent); `redis` opt-in per deployment via
   `TOFU_RUNTIME_STATE_BACKEND`, exactly like `TOFU_RATE_LIMIT_BACKEND`;
   fail-open when the substrate is unreachable.

## 7. Scope boundary (what this doc does NOT cover)

- **Epic B (PushHub pub-sub)** — its own board epic `pt_823ff5a3bf004c40`.
  C and B share a likely datastore decision (§6.1) but are separate changes.
- **Epic D (data tier: PG-guarantee, PgBouncer, read replicas, user-keyed
  sidebar cache)** — board epic `pt_6879b628b896430d`.
- Any schema change here is §10.3-gated (bump `_SCHEMA_VERSION` in both
  `_schema_pg.py` and `_schema_sqlite.py`, mirror DDL, `audit_log('config_
  change', approved_by='user')`).

---

*Prepared 2026-07-02 as the design-first deliverable for board epic
`pt_96b80d88c8d54b71`. §6 decisions RATIFIED by owner 2026-07-02. Implementation
follows the Build Order in [`EPIC_B_PUSH_FANOUT_DESIGN.md`](EPIC_B_PUSH_FANOUT_DESIGN.md)
§0 — the shared lease-store primitive lands FIRST, then the Epic A caps re-key
onto it, then B fan-out, then this doc's sticky-affinity + `interrupted` fix.
No implementation code until the owner confirms after seeing these finalized docs.*
