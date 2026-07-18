# Epic D — Scaled-Deployment Rollout Runbook

> **Audience:** the operator standing up Tofu as a **multi-replica, horizontally
> scaled** deployment (many app processes behind a load balancer, targeting
> hundreds-of-thousands concurrent). It is the *operational* companion to the
> *design* doc [`EPIC_D_DATA_TIER_DESIGN.md`](EPIC_D_DATA_TIER_DESIGN.md).
>
> **NOT for a single-box / desktop install.** Every flag below defaults OFF so
> a single box is byte-identical to today. If you run one server on one machine,
> you do NOT need any of this — the only relevant flag for you is
> `TOFU_REQUIRE_PG=1` (see §2, D1), and even that is optional.
>
> **Scope boundary — code vs infra.** The application code for D1/D2/D3 is
> already merged and green; this runbook does not add code. It provisions the
> **infrastructure** those flags talk to, then flips the flags. Standing up
> managed PgBouncer HA, read replicas, and managed Redis is a deployment/ops
> project with real cost — it is `[human-gated]` (CLAUDE.md §10) and cannot be
> done from an agent shell. Each activation step ends with an
> `audit_log('config_change', approved_by='user')` obligation.

---

## 0. Preconditions

| Requirement | Why |
|---|---|
| Multiple app replicas actually running behind a load balancer | The pooler / replicas / Redis do nothing with one replica |
| A real (not app-owned-local) managed PostgreSQL primary | The `install.sh` local-userspace PG is a dev convenience, not a scale primary |
| `psycopg2` importable in every replica's env | PG backend selection requires it |
| Decision recorded that you are moving to scale | Per charter: managed HA only, never self-run single instances |

**Charter constraints you MUST honour (already ratified — do not re-litigate):**
- Redis and PgBouncer are **MANAGED, HA** (primary + replica + automatic
  failover). A self-run single instance is a fleet-wide SPOF and is explicitly
  rejected. Self-run HA (Sentinel/Cluster; PgBouncer HA pair) is the *fallback*,
  never the target.
- Every mechanism stays **env-gated OFF by default** so the single-box install
  is byte-identical. The scaled deployment opts in.
- Build/rollout order is fixed: **lease-store → Epic A caps re-key → Epic B
  fan-out → Epic C affinity → D1 → D4 → D2 → D3.** Do not turn on a later stage
  before the earlier one is green.

---

## 1. The env-flag surface (all confirmed in code)

| Flag | Stage | Default (OFF) behaviour | Code anchor |
|---|---|---|---|
| `TOFU_REQUIRE_PG=1` | D1 | unset → graceful SQLite fallback | `_core.py:_require_pg` (219) |
| `TOFU_PG_REQUIRE_WAIT_S` | D1 | `60` when require-PG set; `0` otherwise | `_core.py:_pg_require_wait_s` (226) |
| `TOFU_PG_VIA_POOLER=1` | D2 | unset → direct PG, `SET SESSION` timeouts | `_core.py:_pg_via_pooler` (357) |
| `TOFU_PG_DIRECT_DSN=...` | D2 | unset → admin lane uses `PG_DSN` | `_core.py:_pg_admin_dsn` (388) |
| `TOFU_PG_READ_REPLICAS=...` | D3 | unset → `get_read_db()` aliases `get_db()` | `_core.py:get_read_db` (1350) |
| `TOFU_RUNTIME_STATE_BACKEND=redis` | B/C (+D4) | `inproc` → per-process state | `lib/runtime_state_store.py:468` |
| `TOFU_REDIS_URL=redis://...` | B/C (+D4) | `redis://127.0.0.1:6379/0` | `lib/runtime_state_store.py:246` |
| `TOFU_RATE_LIMIT_BACKEND=db` | Epic A | `memory` → per-process cap | `lib/rate_limit_store.py:221` |

> A flag being ON without its infra present is a trap, not a no-op, for D2
> (the app will try to reach a PgBouncer that isn't there). D3 and Redis
> fail-open to the primary/inproc, so those two are safe-but-pointless if you
> set them early. Follow the order below.

---

## 2. Rollout sequence

Do these **in order**. After each, confirm green (see §3) before the next.

### D1 — Guarantee PostgreSQL (correctness gate; cheapest, highest safety)
1. Point the replicas at your managed PG primary via `TOFU_PG_HOST` /
   `TOFU_PG_PORT` / `TOFU_PG_DBNAME` / `TOFU_PG_USER` / `TOFU_PG_PASSWORD`
   (these build `PG_DSN`, `_core.py:311`).
2. Set `TOFU_REQUIRE_PG=1` on every replica. Now a PG-unreachable boot
   **refuses to start** (`_assert_pg_available_or_raise`, 279) instead of
   silently degrading to write-serializing SQLite.
3. `audit_log('config_change', param='TOFU_REQUIRE_PG', approved_by='user')`.

### Redis (Epic B/C substrate — prerequisite for D4's cross-replica invalidation)
1. Provision **managed Redis HA** (primary + replica + auto-failover). No
   AOF/RDB durability requirement — the bus is best-effort; lease/counter keys
   self-heal from heartbeats within one lease TTL.
2. Set `TOFU_REDIS_URL=redis://<managed-endpoint>:6379/0` and
   `TOFU_RUNTIME_STATE_BACKEND=redis` on every replica.
3. This makes the Epic A caps, the push fan-out (B), and runtime state (C)
   `N`-invariant across replicas. Confirm B/C are green here — D4's invalidation
   rides this same bus, no second system.
4. `audit_log('config_change', param='TOFU_RUNTIME_STATE_BACKEND', approved_by='user')`.

### D2 — Managed PgBouncer HA pooler (transaction pooling)
1. Provision a **managed PgBouncer HA tier in transaction-pooling mode** in
   front of the PG primary.
2. Repoint the runtime DSN (`TOFU_PG_HOST`/`TOFU_PG_PORT`) at the **bouncer**.
3. Set `TOFU_PG_DIRECT_DSN` to the **real backend** (bypasses the pooler for
   DDL migrations + `VACUUM FULL`/`REINDEX`, which are illegal through a
   transaction pooler — `_pg_admin_dsn`, 388).
4. Set `TOFU_PG_VIA_POOLER=1`. Connection-scoped timeouts now ship as libpq
   startup options instead of `SET SESSION` (pooler-safe — no GUC leak across
   pooled borrowers; `_pg_session_setup_plan`, 366).
5. The D2 session-state audit is already done in code (zero advisory locks,
   zero server-side prepared statements; the one runtime hazard is fixed and
   gated). Nothing to change in app code.
6. `audit_log('config_change', param='TOFU_PG_VIA_POOLER', approved_by='user')`.

### D3 — Read replicas (last; depends on a healthy D2 tier)
1. Provision one or more **read replicas** of the PG primary.
2. Set `TOFU_PG_READ_REPLICAS=<replica DSNs>`.
3. **Caveat — seam only today.** `get_read_db()` currently logs and still
   returns the primary even when this is set (`_core.py:1367-1374`); the actual
   replica-pool routing is the remaining §10 code follow-up. Setting this flag
   is safe (fail-open to primary) but delivers no read-spread until that
   routing lands. Track it as the open D3 code task.
4. Lag discipline: only lag-tolerant reads (sidebar list, old-conversation
   load, search) may adopt `get_read_db`; read-your-write-sensitive paths
   (just-sent message, poll of a fresh result) stay on `get_db` (the primary).
5. `audit_log('config_change', param='TOFU_PG_READ_REPLICAS', approved_by='user')`.

### D4 — Sidebar cache user-key + LIMIT + cross-replica invalidation
- **Coupled to multi-user auth** — do not retire `DEFAULT_USER_ID=1` before the
  auth model ships, or single-user installs regress. The user-key + `LIMIT`
  parts land with auth; the cross-replica invalidation rides the Redis bus
  (already provisioned above). This is a code increment, not a flag flip.

---

## 3. Verification after each stage

| Stage | Check |
|---|---|
| D1 | Kill PG, boot a replica → it must REFUSE (log `TOFU_REQUIRE_PG=1 but PostgreSQL is unavailable`), not serve SQLite. Restore PG → boots. |
| Redis | `audit.log` shows `db_backend_selected`; two replicas see each other's push events / share the Epic A cap (not a per-process count). |
| D2 | `SELECT count(*) FROM pg_stat_activity` on the real backend stays a small fixed number under N replicas' load (multiplexing works). DDL migration + a manual `VACUUM` still succeed (admin lane bypasses the pooler). |
| D3 | Replication-lag metric exported; a lag-tolerant read served by a replica, a lag-sensitive read pinned to primary. |

**Metrics are REQUIRED per §5a.5**, not assumed: pooler PG-backend saturation +
PgBouncer wait time; replica replication-lag. Reuse whatever the managed tier
exports; fill gaps with app-side gauges.

---

## 4. Rollback

Every stage is a flag. To roll back a stage, **unset its flag and restart** —
the code falls back to the byte-identical single-box path (SQLite-capable D1,
direct-PG D2, primary-only D3, inproc B/C). No schema or data migration is
involved in any D flag, so rollback is a restart, not a repair.

---

## 5. Why this is NOT baked into `install.sh`

`install.sh` provisions a **single-box, zero-config** install. Making it lay
down PgBouncer / Redis / replicas by default would:
- violate the ratified byte-identical-single-box invariant;
- create self-run single instances (SPOFs) — exactly what the charter's
  managed-HA decision rejects;
- add three failure-prone daemons to serve traffic one process already handles.

If you want a **local dev harness** to exercise the D2/D3 code paths before a
real rollout, that belongs behind an explicit opt-in `install.sh
--with-scale-deps` (default off), clearly labelled non-production — a separate,
smaller task from this production runbook.

---

*Prepared as the operational companion to `EPIC_D_DATA_TIER_DESIGN.md`. The
infra provisioning + flag activation is human-gated (CLAUDE.md §10); each
activation step records an `audit_log('config_change', approved_by='user')`.*
