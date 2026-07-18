# Epic B — Externalize PushHub Fan-Out via Pub/Sub for Horizontal Scale-Out

> **Status: DESIGN-FIRST / §10-GATED. No code until owner sign-off.**
> Board epic `pt_823ff5a3bf004c40`. Read alongside
> [`EPIC_C_RUNTIME_STATE_DESIGN.md`](EPIC_C_RUNTIME_STATE_DESIGN.md) — this
> doc owns the **shared-datastore decision (§4)** and the **lease-TTL
> primitive (§5)** that BOTH B and C commit to. Neither is designed in
> isolation.

---

## 0. Build Order — RATIFIED by owner 2026-07-02 (implement in THIS sequence)

The dependencies force one order. Each step is its own §10-gated change with
NC-biting tests + byte-revert evidence; **no step starts before the owner
confirms the finalized docs, and no later step starts before the earlier one
is green.**

1. **Shared lease-store primitive FIRST.** The Redis-backed
   key+TTL+heartbeat layer (§5) behind a `TOFU_RUNTIME_STATE_BACKEND` seam
   with an `inproc` default and fail-open — mirroring `lib/rate_limit_store.py`
   exactly (pluggable backend, memoized `get_store()`, `reset_for_test()`).
   B's subscription registry (§5.1 `sub:*`/`alive:*`) AND C's admission/SSE
   counters (§5.1 `admit:*`/`sse:*`) both sit on this. It ships with ZERO
   behaviour change under the default `inproc` backend.
2. **Re-key the two Epic A caps onto the lease-store** (admission controller +
   per-principal SSE semaphore). THIS is the actual closure of the Epic A
   follow-up: under `redis` the caps become `N`-invariant; under `inproc` they
   stay byte-identical to today. Reuses the exact pattern already proven for
   the open-mode throttle (Step 2b).
3. **Epic B fan-out** (§3): `push_event` publishes to the shared bus; each
   replica's subscriber-loop re-delivers to its local WS clients; webhook
   once-delivery via the publishing replica (§6.3).
4. **Epic C sticky-affinity + the `interrupted` fix** (C §4.1): LB
   consistent-hash on `taskId`, and report `running`+reconnect (no liveness
   probe). Depends on the lease-store (step 1) for the supersede index (C §4.3).

Rationale for "primitive first": steps 2–4 are all consumers of the same
lease/counter/heartbeat layer; building that layer once, with its own tests,
means the three consumers each become a thin re-key rather than three
independent reimplementations of TTL/heartbeat logic (the exact duplication
mistake Step 2b corrected for rate-limiting).

---

## 1. Problem statement

The push path is a single-process, single-event-loop fan-out:

- `/api/push` is one WebSocket per browser tab → a `PushClient`
  (`lib/agent_core/push.py:PushClient`) registered in the module-global
  `hub` singleton (`push.py` bottom).
- The client subscribes with `{action:'subscribe', channel, taskId}`
  (`routes/push.py:139`); the hub stores that in an in-process
  `_subscriptions[channel][task_id] = {clients}` dict.
- A backend worker (any thread) calls `push_event(channel, task_id, payload)`
  which looks up local subscribers and schedules delivery onto the **one**
  event loop via `hub.set_loop(loop)` + `loop.call_soon_threadsafe`
  (`push.py:135`). It also invokes in-process `_listeners` (the webhooks
  delivery worker).

**The break under N replicas:** the publishing worker for task `T` runs on
the replica that owns `T`'s task object (replica B). The client's `/api/push`
WebSocket is a long-lived connection pinned to whichever replica the LB gave
it (replica A). `push_event` on B finds **no local subscriber** and
**silently drops the frame** (`push.py:126` — the `if not targets` debug
breadcrumb). The webhook `_listeners` fan-out is likewise process-local. So
live progress, report/translate events, the "N running" badge, and webhook
delivery all silently stop working the moment there is more than one replica.

Note: chat text itself streams over SSE (`/api/chat/stream/<id>`, Epic C's
sticky-session affinity), NOT `/api/push`. `/api/push` carries the
*cross-cutting* events (paper/translate/notify/webhook + the future chat
lifecycle hook). So B and C are complementary: C pins the *task's own*
stream; B broadcasts *ambient* events to whichever replica holds each
subscriber's WS.

## 2. Design goal

Any backend worker on any replica can `push_event(...)` and have it reach the
subscribed client's WebSocket **regardless of which replica that WS lives on**,
without changing the public `push_event` / `PushClient` / frontend
`pushSubscribe` contract.

## 3. Design — a pub/sub relay behind the existing `PushHub` interface

Keep `hub`, `PushClient`, `push_event()`, `add_listener()` exactly as the
public surface. Change only the *transport between replicas*:

1. **Local delivery stays local.** Each replica keeps its own `_subscriptions`
   for the WebSockets IT terminates. Delivering to a locally-connected client
   is unchanged (`loop.call_soon_threadsafe(self._deliver, ...)`).
2. **Publish becomes a broadcast to a shared channel.** `push_event` no longer
   only looks at local subscribers — it PUBLISHES the frame
   `{channel, taskId, ...payload}` to a shared pub/sub topic. Every replica
   is SUBSCRIBED to that topic.
3. **Each replica's subscriber-loop receives every published frame and
   fans it out to its OWN local subscribers** (the existing
   `_deliver → client.enqueue` path). A replica with no matching local
   subscriber drops the frame locally (cheap, correct).
4. **Webhook `_listeners`** run on exactly ONE replica (a leader, or the
   publishing replica) to avoid N-fold duplicate webhook deliveries — see
   §6.3.

This is the standard "each app instance subscribes to a fan-out bus and
re-delivers to its local WS connections" pattern (Socket.IO Redis adapter,
Django Channels layer, Phoenix PubSub all do exactly this). The `PushHub`
becomes a thin local-delivery cache in front of the bus.

### 3.1 De-dup / self-echo
The publishing replica also receives its own published frame back from the
bus. Tag each frame with an origin-replica id and skip re-delivering a frame
the local replica already delivered inline — OR (simpler) do NOT deliver
inline at publish time and rely solely on the bus round-trip for ALL delivery
(uniform path, one code branch, ~sub-ms Redis latency). Lean the latter for
correctness; measure the added latency before committing.

### 3.2 Ordering / loss
`/api/push` events are already best-effort (the `PushClient` queue is bounded
`maxsize=1000` with drop-oldest, `push.py:PushClient.enqueue`). The bus does
not need stronger delivery than that. Last-Event-ID durability is an SSE-only
feature (chat), NOT a `/api/push` guarantee — so no per-event persistence is
required on the bus. This keeps B simple.

## 4. SHARED DATASTORE DECISION (B **and** C commit to this)

> This section is binding for both epics. C's counters/leases (§4.2 of the
> Epic C doc) and B's pub/sub bus MUST use the same substrate unless a
> justified exception is recorded here.

**Recommendation: Redis (single substrate for B's pub/sub AND C's
counters/leases).** Justification:

| Need | Redis | Postgres |
|---|---|---|
| **B: fan-out pub/sub** | Native `PUBLISH`/`SUBSCRIBE`, sub-ms, purpose-built | `LISTEN`/`NOTIFY` exists but payload ≤ 8 KB, no pattern fan-out at scale, ties every replica to a dedicated PG connection held open forever |
| **C: atomic counter (admission/SSE)** | `INCR`/`DECR` atomic, O(1) | `UPDATE … RETURNING` under contention → row-lock hotspot |
| **C: lease-TTL reclaim (§5)** | Native `EXPIRE` / `SET … EX` — the killer feature | Manual `last_seen` column + a sweep job; no native expiry |
| **Ops burden** | One more system to run | Reuse existing PG |

The decisive factor is **§5 (lease-TTL)**: Redis has native key expiry, which
is *exactly* the primitive both "dead replica's subscriptions must age out"
(B) and "dead replica's admission/SSE slots must reclaim" (C) need. Emulating
TTL expiry in Postgres means a sweep job + `last_seen` bookkeeping on every
heartbeat — more code, more failure surface, and a sweep-interval-sized window
where a dead replica still holds capacity.

**The one honest cost:** Redis is a new operational dependency (the project
ships zero today; PG is already required in prod per Epic D). We accept it
because running TWO externalization mechanisms (PG for counters + `LISTEN/
NOTIFY` for pub/sub) is strictly worse operationally than one Redis, and
because `LISTEN/NOTIFY` genuinely does not scale to a high-fan-out push bus.

**Rollout shape (mirrors `TOFU_RATE_LIMIT_BACKEND`):** a `TOFU_PUBSUB_BACKEND`
/ `TOFU_RUNTIME_STATE_BACKEND` env with `inproc` (default — today's
single-process behaviour, byte-equivalent) and `redis`. A single-box install
never needs Redis; a scaled deployment sets `=redis`. **Fail-open / degrade:**
if Redis is unreachable, a replica falls back to LOCAL-only delivery + logs
loudly (a single-replica deployment is then fully functional; a multi-replica
one degrades to "events only reach same-replica clients" — the today-behaviour,
not a crash). This is the same fail-open discipline as `rate_limit_store`.

## 5. LEASE-TTL PRIMITIVE (resolves the capacity-leak failure mode for B **and** C)

> **The failure mode that defeats the 100k objective:** a replica crashes
> holding global capacity — B's subscription registry keeps a dead WS's
> subscriptions forever (frames fan out to nowhere, and any "who's watching
> task T" accounting is wrong); C's admission/SSE slots never release (the
> `on_terminal` decrement never fires because the worker died) → global
> capacity monotonically shrinks until the fleet wedges. This section
> specifies the ONE primitive that prevents it.

**Primitive: Redis key with a TTL, refreshed by a per-replica heartbeat;
reclaim by natural expiry.**

### 5.1 Registration keys carry a TTL
Every piece of replica-owned shared state is a Redis key with an expiry:

- **C — admission slot:** `admit:{task_id}` = `{replica_id, acquired_at}`,
  `SET … EX <lease_ttl>`. The global in-flight count is the cardinality of
  live `admit:*` keys (or a counter kept consistent with them).
- **C — SSE slot:** `sse:{principal}:{replica}:{stream_id}` `EX <lease_ttl>`.
  Per-principal active = count of that principal's live `sse:*` keys.
- **B — subscription:** `sub:{channel}:{task_id}` is a Redis SET of
  `{replica_id}` (which replicas have a subscriber), each membership backed by
  a per-replica presence key `alive:{replica_id}` with `EX <lease_ttl>`.

### 5.2 Heartbeat refresh
Each replica runs a lightweight loop (every `lease_ttl / 3`) that refreshes
`alive:{replica_id}` and re-`EXPIRE`s its currently-held slot keys. While the
replica is alive, its leases never expire. This is the standard
lease/heartbeat pattern (etcd lease, Consul session, k8s node heartbeat).

### 5.3 Reclaim trigger — natural expiry, no sweep
When a replica **crashes**, its heartbeat stops. Within `lease_ttl` seconds:
- `alive:{replica}` expires → the replica is considered dead.
- Its `admit:*` / `sse:*` slot keys expire → the slots are automatically
  reclaimed into global capacity (no sweep job, no explicit decrement).
- Its membership in every `sub:*` SET is treated as stale (readers filter SET
  members against live `alive:*` keys, and a periodic cheap `SREM` of dead
  members keeps the SETs bounded).

The **normal (non-crash) release** paths still fire eagerly: C's `on_terminal`
`DEL admit:{task_id}`; the SSE `finally` `DEL sse:...`; the WS disconnect
`SREM sub:...`. TTL expiry is the *backstop* for the crash case only — so a
healthy fleet reclaims instantly and a crashed replica reclaims within one
lease window.

### 5.4 Choosing `lease_ttl`
Trade-off: too short → a slow-but-alive replica's slots wrongly expire mid-task
(heartbeat must comfortably beat it, hence refresh at `ttl/3`); too long → a
crashed replica holds capacity for that long. Proposal: `lease_ttl = 90s`,
heartbeat every 30s. Rationale: 30s heartbeat is negligible load; 90s worst-
case capacity hold after a crash is tolerable against a 64-slot (per current
default) global budget. **RATIFIED (§7.2): these values are the commitment,
but MUST be confirmed by a benchmark before scale rollout (living-task lease
never expires under 30s heartbeat; post-crash reclaim ≤ ~lease_ttl) rather than
asserted.**
For SSE/admission whose legit lifetime can exceed 90s (a long agent task),
the eager heartbeat refresh keeps the lease alive for the whole task — the TTL
only bites when heartbeats STOP (crash), so a long *living* task is safe.

### 5.5 Postgres fallback (if §4 is overruled to PG)
If review rejects Redis, the equivalent is a `runtime_leases(kind, key,
replica_id, expires_at_ms)` table + a sweep coroutine that `DELETE … WHERE
expires_at_ms < now` every `ttl/3`, and every read filters on `expires_at_ms
> now`. This works but is strictly more code and has a sweep-interval capacity-
hold window on top of the TTL. Documented for completeness; not recommended.

## 6. Secondary design points

- **6.1 Frontend contract unchanged.** `pushSubscribe(channel, taskId, fn)`
  and the `{channel, taskId, ...}` frame shape are untouched. Pure backend
  transport swap.
- **6.2 `set_loop` stays** — each replica still binds its own loop for LOCAL
  delivery; the bus subscriber-loop hands frames to that same
  `call_soon_threadsafe` path.
- **6.3 Webhook `_listeners` de-dup.** Webhooks must deliver ONCE per event
  fleet-wide, not once per replica. Option: only the publishing replica runs
  `_listeners` (it already has the event inline before publishing) → natural
  once-delivery, no leader election needed. Confirm at review.
- **6.4 Metrics.** `hub.client_count` becomes per-replica; add a fleet-wide
  gauge from the bus (`PUBSUB NUMSUB` / live `alive:*` count).

## 7. Decisions — RATIFIED by owner 2026-07-02 (were open questions)

These are commitments; implementation follows them.

1. **Redis is accepted as the single B+C substrate** (§4). The §5.5 Postgres
   lease + `LISTEN/NOTIFY` path is retained ONLY as a documented fallback if
   Redis proves unavailable in a given deployment — not the primary design.
2. **`lease_ttl = 90s`, heartbeat every 30s** (§5.4). **This pair is validated
   by a test/benchmark before the scale rollout, not asserted** — the
   implementation MUST include a benchmark that (a) confirms a living task's
   lease never expires under the 30s heartbeat, and (b) measures worst-case
   post-crash reclaim ≤ ~lease_ttl. The numbers may be tuned by that
   benchmark; the heartbeat-at-`ttl/3` relationship is fixed.
3. **Uniform bus-only delivery** (§3.1): ALL delivery goes through the bus
   round-trip — ONE code path, no inline+dedup branch. The implementation
   MUST measure and log the added Redis round-trip latency (so a regression is
   visible), but does NOT keep a dual path.
4. **Webhook once-delivery: publishing-replica-only** (§6.3). The replica that
   calls `push_event` runs `_listeners` inline before publishing → natural
   fleet-wide once-delivery, NO leader election.
5. **`interrupted` false-positive fix (C §4.1): report `running` +
   reconnect-via-affinity, NO cross-replica liveness probe** (mirrors Epic C
   §6.4). Recorded here because it is part of the same ratified decision set.

Still to confirm operationally (does NOT block the design or step 1–2 code,
only the scale rollout): **managed Redis vs self-run** in the target
(Meituan/internal) environment — its availability/failover story tunes the
fail-open posture (§4), but the fail-open behaviour itself is already
specified and is correct regardless. **→ RESOLVED in §7a below.**

## 7a. Managed-vs-self-run Redis — RESOLVED by owner 2026-07-04 ("most robust / long-term; ignore migration cost")

**Decision: MANAGED Redis with HA / automatic failover — NOT a self-run single
instance.** The owner delegated this with an explicit rule: pick the most
long-term, robust option and do not weigh migration cost.

Rationale (robust-first):
- Redis is the SINGLE substrate for BOTH B (pub/sub fan-out) and C
  (counters + lease-TTL, §4/§5). At the 100k-concurrent target it sits on the
  critical path of every replica's ambient event delivery AND every
  admission/SSE capacity decision. A self-run single Redis is therefore a
  fleet-wide SPOF whose failure silently collapses cross-replica push and
  wedges capacity accounting — precisely the failure class this epic exists to
  remove. A managed HA tier (primary + replica + automatic failover) removes
  that SPOF.
- The fail-open seam (§4) is retained UNCHANGED and is now a *degradation
  backstop during a failover blip*, not the steady-state posture: if the
  managed endpoint is briefly unreachable a replica degrades to local-only
  delivery + loud logs, then rejoins the bus when failover completes. This is
  strictly safer than leaning on fail-open to paper over a self-run instance
  that has no failover at all.
- Persistence posture: the bus itself needs no durability (§3.2 — `/api/push`
  is best-effort, Last-Event-ID durability is SSE-only). The lease/counter
  keys (§5) are reconstructible from live heartbeats within one `lease_ttl`
  window, so a failover that loses volatile keys self-heals — an AOF/RDB
  durability requirement is NOT imposed on the managed tier (keeps the managed
  offering cheap and the design tolerant of a cold failover).

If a managed Redis is genuinely unavailable in a specific deployment, the
ordered fallback is: (1) a self-run Redis Sentinel/Cluster HA pair (still HA,
just self-operated), and only then (2) the §5.5 Postgres-lease +
`LISTEN/NOTIFY` path — which remains a documented last resort, never the
target. The design target is managed HA.

## 8. Scope boundary

- **Epic C** (task/session state + sticky sessions) — its own doc; shares §4
  and §5 with this doc.
- **Epic D** (PG-guarantee, PgBouncer, read replicas, sidebar cache) — separate.
- Any schema change (the §5.5 PG-fallback table) is §10.3-gated. Adding a
  Redis dependency is itself a §10 infra decision requiring sign-off.

---

*Prepared 2026-07-02 as the design-first deliverable for board epic
`pt_823ff5a3bf004c40`. §4 (shared datastore) and §5 (lease-TTL) are binding
on Epic C as well. §7 decisions RATIFIED by owner 2026-07-02; §0 Build Order
is the ratified implementation sequence. Implementation begins with §0 step 1
(the shared lease-store primitive, a §10 infra change) ONLY after the owner
confirms upon seeing these finalized docs — no B/C/D code is written before
that confirmation.*
