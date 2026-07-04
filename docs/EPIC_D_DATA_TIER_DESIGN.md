# Epic D — Data-Tier Scale-Out for Horizontal Scale

> **Status: DESIGN-FIRST / §10-GATED. No code until owner sign-off.**
> Board epic `pt_6879b628b896430d`. Companion to
> [`EPIC_B_PUSH_FANOUT_DESIGN.md`](EPIC_B_PUSH_FANOUT_DESIGN.md) and
> [`EPIC_C_RUNTIME_STATE_DESIGN.md`](EPIC_C_RUNTIME_STATE_DESIGN.md). This
> document is the autonomous design deliverable for Epic D; the
> implementation is deliberately human-gated (schema + infra changes fall
> under CLAUDE.md §10 and require explicit sign-off + an
> `audit_log('config_change', approved_by='user')`).

---

## 1. Problem statement

The runtime-state epics (B/C) make the *application layer* horizontally
scalable. Epic D addresses the layer underneath: the database and its
connection topology, which today has four scale ceilings for a
hundreds-of-thousands-concurrent deployment.

| # | Ceiling | Evidence | Severity |
|---|---|---|---|
| D1 | **SQLite fallback serializes ALL writes** under one file lock even in WAL mode (WAL = concurrent readers, ONE writer) | `lib/database/_core.py` SQLite factory (`journal_mode=WAL`, `busy_timeout=30000ms`); backend selection falls to SQLite when `TOFU_DB_BACKEND=sqlite` OR PG bootstrap/psycopg2 import fails | **Critical** — SQLite is a dev/zero-config fallback, structurally unusable at 100k write concurrency |
| D2 | **Hard PG connection ceiling, no pooler tier** | app-side `BoundedSemaphore` (`CHATUI_DB_MAX_CONNS`, ~1000) + pool cap `_CONN_POOL_MAX=100` (`_core.py:954`); PG server `max_connections` provisioned above the semaphore. **N replicas × ~1100 conns each exceeds one PG server** | **High** — a single PG instance cannot hold N replicas' worth of direct connections |
| D3 | **All reads hit the primary** | no read-replica routing anywhere in `lib/database/` | **High** — the read-heavy sidebar / conversation-list / poll traffic has nowhere to spread |
| D4 | **Sidebar `_meta_cache` is single-tenant + unbounded + process-local** | `DEFAULT_USER_ID=1` single global cache blob; the conversation-list `SELECT … ORDER BY updated_at DESC` has **no LIMIT**; the cache is an in-process dict with TTL-only, no cross-replica invalidation (per the 2026-07-02 DB audit) | **Med→High** — wrong once multi-user; unbounded scan grows with history; stale sidebars across replicas |

## 2. Design — four independent, sequenceable changes

Each is its own §10-gated change with NC-biting tests + byte-revert evidence.
They are ordered by "correctness gate first, elasticity second".

### 2.1 D1 — Guarantee PostgreSQL in production (fail-CLOSED at boot, not fall-back)
The SQLite fallback is the right zero-config behaviour for a single-box /
desktop install, and MUST stay the default there. The fix is a **production
assertion**, not removing SQLite:

- A new env `TOFU_REQUIRE_PG=1` (set in the scaled deployment) makes the DB
  bootstrap **fail-closed**: if PG can't be reached, the server refuses to
  start with a clear error, INSTEAD of silently degrading to the
  write-serializing SQLite file. A single-box install leaves it unset →
  today's graceful SQLite fallback is byte-identical.
- Rationale: silent degradation to SQLite under load is the worst failure —
  it "works" in a smoke test then serializes every write in production. A
  loud boot-time refusal is the honest behaviour for a deployment that has
  declared itself at-scale.
- **NC:** with `TOFU_REQUIRE_PG=1` and PG unreachable, boot MUST raise (not
  fall through to SQLite); with it unset, the SQLite fallback still engages.

### 2.2 D2 — PgBouncer pooler tier (transaction pooling)
Put **PgBouncer** (transaction-pooling mode) between the app replicas and PG
so N replicas share a bounded set of real PG backend connections:

- Each replica's pool points at PgBouncer, not PG directly. PgBouncer
  multiplexes many short app transactions onto few PG backends, so
  `N × pool_max` app connections collapse to a small, fixed PG backend count
  well under `max_connections`.
- App-side change is minimal: the connection string / host targets the
  bouncer; the existing `_conn_pool` + semaphore stay (they now bound
  per-replica in-flight, PgBouncer bounds the PG-side total). A config knob
  `TOFU_PG_VIA_POOLER` documents the topology; default off (direct PG) so a
  single-box install is unchanged.
- **Transaction-pooling caveat (must be honoured in code):** session-level
  state does not survive across pooled transactions — no session-scoped
  `SET`, no server-side prepared statements pinned to a session, no
  advisory-lock-held-across-statements. Audit `_post_connect_setup` and any
  `SET`/advisory-lock usage; anything session-scoped must move to
  transaction scope or a `SET LOCAL`. **NC:** a test that asserts no
  session-pinned state is relied on across two transactions.

### 2.3 D3 — Read-replica routing for read-only hot paths
Add an OPTIONAL read-replica lane:

- A `get_read_db()` companion to `get_db()` that, when `TOFU_PG_READ_REPLICAS`
  is set, routes read-only queries (conversation list, sidebar, poll
  snapshots, search) to a replica connection pool; writes and
  read-your-write-sensitive paths stay on the primary. Default unset →
  `get_read_db()` is an alias of `get_db()` (byte-identical single-box).
- **Replication-lag discipline:** a path that must read its own just-written
  row (e.g. immediately after `chat_send` persists) MUST use the primary.
  The design enumerates which hot paths are lag-tolerant (sidebar list, old
  conversation load) vs lag-sensitive (just-sent message, task poll of a
  freshly-persisted result) — only the former route to replicas.
- **NC:** a lag-sensitive path pinned to the primary (assert it does NOT use
  `get_read_db`); a lag-tolerant path proven to use the replica lane when
  configured.

### 2.4 D4 — User-key + paginate + cross-replica-invalidate the sidebar cache
Three sub-fixes to `_meta_cache` (routes/common + meta_cache):

- **User-key it:** the cache key becomes `(user_id, …)` not a single global
  blob — a prerequisite for multi-user correctness (the `DEFAULT_USER_ID=1`
  assumption is retired here, coordinated with the multi-user auth model).
- **Bound the query:** add `LIMIT` + keyset pagination to the conversation-
  list `SELECT` so a user with tens of thousands of conversations doesn't
  full-scan on every sidebar fetch.
- **Cross-replica invalidation:** the in-process TTL cache goes stale across
  replicas (a write on replica A leaves B's sidebar stale up to the TTL).
  Fix via the **same shared substrate B/C use** — a Redis pub/sub
  invalidation channel (or a shared cache) so a mutation on any replica
  invalidates the key fleet-wide. This REUSES the Epic B Redis bus
  (§4 of the B doc), NOT a new mechanism.
- **NC:** stale-across-replicas reproduced with two in-proc caches + a
  mutation → assert the invalidation channel clears the peer; unbounded-scan
  NC (no LIMIT → scan grows with history).

## 3. Shared decisions inherited (NOT re-litigated here)

- **Redis** is the shared substrate (Epic B §4, ratified). D4's cross-replica
  cache invalidation rides the SAME Redis pub/sub as Epic B — no second
  system.
- **Rollout shape** mirrors `TOFU_RATE_LIMIT_BACKEND` / `TOFU_RUNTIME_STATE_BACKEND`:
  every D change is env-gated OFF by default so a single-box / desktop install
  is byte-identical to today; the scaled deployment opts in
  (`TOFU_REQUIRE_PG`, `TOFU_PG_VIA_POOLER`, `TOFU_PG_READ_REPLICAS`).
- **§10.3 schema discipline:** any DDL change (unlikely for D — it's mostly
  topology/config — but D4's keyset pagination may want an index) bumps
  `_SCHEMA_VERSION` in BOTH `_schema_pg.py` and `_schema_sqlite.py` with
  mirrored DDL.

## 4. Sequencing (within Epic D; Epic D itself follows B/C)

1. **D1 (correctness gate)** — fail-closed PG guarantee. Cheapest, highest
   safety value; no dependency on B/C.
2. **D4 cache user-key + LIMIT** — the non-Redis parts (user-key, pagination)
   are independent; the cross-replica invalidation part depends on the Epic B
   Redis bus, so it lands after B.
3. **D2 PgBouncer** — infra + the session-state audit.
4. **D3 read replicas** — last, because it depends on a healthy pooler tier
   (D2) and the lag-sensitivity classification.

## 5. Open questions for review (answer before coding)

1. **D1 default:** confirm fail-closed is opt-IN via `TOFU_REQUIRE_PG=1`
   (single-box keeps graceful SQLite fallback). Yes/no.
2. **D2 pooler:** PgBouncer (transaction pooling) accepted, and is a
   managed pooler available in the target (Meituan/internal) environment, or
   self-run? The session-state audit scope depends on transaction-pooling
   mode being confirmed.
3. **D3:** are read replicas actually available in the target deployment, or
   is D3 deferred until they exist? (D1/D2/D4 stand alone without it.)
4. **D4 multi-user coupling:** user-keying the cache retires
   `DEFAULT_USER_ID=1` — confirm this is done in lockstep with the multi-user
   auth rollout, not before (else single-user installs regress).
5. **Metrics/observability** for the pooler + replica lag — what does the
   target environment already provide vs. what must be added.

## 6. Scope boundary

- **Epics B / C** — their own docs; D reuses B's Redis substrate for D4's
  cache invalidation and otherwise stands alone.
- This doc is DESIGN ONLY. No `lib/database/` code, no schema change, no
  PgBouncer deployment. Every item above is §10-gated.

---

*Prepared 2026-07-02 as the autonomous design-first deliverable for board epic
`pt_6879b628b896430d`. The implementation is human-gated (schema + infra =
CLAUDE.md §10). Awaiting owner review of §2 (the four changes) and §5 (open
questions) before any code — and Epic D follows Epics B/C in the overall
build order.*
