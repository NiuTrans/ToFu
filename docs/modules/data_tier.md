# Module Design Doc — Unit 7: Data Tier (`lib/database/`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> is the dual-backend persistence layer: PostgreSQL primary, SQLite fallback,
> one query surface for the whole app.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.
>
> **A correction to the CLAUDE.md map up front:** §1 lists 6 database modules
> (`_core`, `_bootstrap`, `_core_schema`, `_schema_pg`, `_schema_sqlite`,
> `_sql_translate`, `_wrappers`). The package actually has **18 modules**
> (11,014 LOC). CLAUDE.md omits `_pg_ownership` (1156), `_pg_backup` (571),
> `_pg_seed` (408), `aio` (264), `messages_rows` (250), `db_paths` (213),
> `wal_archive` (181), `schema_registry` (162), `pg_admin` (158), `_orphan_heal`
> (93). Several are recent (the JOURNAL shows `_pg_ownership` was extracted from
> `_bootstrap` TODAY, 2026-07-11, "Decoupling D"). The map is stale.

---

## 1. The analytical payload: the PG/SQLite parity seam

The load-bearing question (the highest-consequence one in the tree — a
backend-divergence here corrupts persisted state, not just a prompt): is
`_core_schema.py` GENUINELY the single source of every table, or do
`_schema_pg.py` / `_schema_sqlite.py` carry divergent table definitions that can
silently drift?

**Verdict: `_core_schema.py` IS the single table source — the parity seam is
sound, and it is guarded by a parity test AND a cross-backend equality test.**
This is the strongest single-source discipline in the entire survey. Evidence,
verified on disk:

### 1a. There is exactly ONE `CREATE TABLE` author — `_core_schema.py`

Grepping for table definitions across the package:
- `_core_schema.py` — **46 `Table()`/`define_table` calls.** Its docstring:
  "migration COMPLETE (2026-06). Every table … is now defined ONCE here as a
  SQLAlchemy Core `Table` object and compiled to correct DDL … for BOTH backends.
  `_schema_pg.py`/`_schema_sqlite.py` no longer hand-author any `CREATE TABLE`."
- `_schema_pg.py` — the 3 grep hits for "create table" are ALL in a docstring
  (line 3) and a comment (lines 222, 225). **Zero real `CREATE TABLE` literals.**
- `_schema_sqlite.py` — the 2 hits are a docstring (line 3) and a comment
  (line 501). **Zero real `CREATE TABLE` literals.**

So neither backend module hand-authors a table. Both call
`create_if_absent(conn, <core_table>)` — compiling the SAME Core `Table` to the
active dialect. A table definition CANNOT drift because there is only one.

### 1b. The two backend modules are byte-for-byte MIRRORS — verified equal where it matters

`_schema_pg.py` (906) and `_schema_sqlite.py` (799) are deliberate mirror images:
same function names (`_column_exists`, `_table_exists`, `_count_rows`,
`_missing_critical_columns`, `_read_meta`, `init_db`), differing ONLY in the
backend-specific implementation (PG `information_schema` vs SQLite
`PRAGMA table_info`/`sqlite_master`). This mirror structure is the KNOWN
drift-risk — the JOURNAL records the exact bug class TODAY (`21290a2`): the
`_CRITICAL_COLUMNS` self-heal guard was added to the PG fast-path (`a6029f5`) but
NOT the SQLite mirror, leaving the twin latently buggy for a while.

**I verified the fix is complete and pinned:**
- Both `_CRITICAL_COLUMNS` dicts are BYTE-IDENTICAL on disk:
  ```
  _CRITICAL_COLUMNS = {'project_tasks': ('blocked_until','block_count',
                        'block_reason','wait_paths','dispatch_target')}
  ```
  (identical in `_schema_pg.py:57` and `_schema_sqlite.py:52`).
- `_missing_critical_columns` is present in BOTH, with identical logic (read-only,
  best-effort, missing-table-skipped), called BEFORE the version fast-path return
  in both.
- Both carry `_SCHEMA_VERSION = 39` — identical.
- **`tests/test_sqlite_critical_column_selfheal.py:86` `test_pg_and_sqlite_critical_sets_match`
  asserts `ss._CRITICAL_COLUMNS == sp._CRITICAL_COLUMNS`** — so the two dicts
  cannot drift again without a RED test. This is exactly the "pin the invariant
  that made them diverge" guardrail. Confirmed it exists and asserts equality.

### 1c. The parity is GATED by a compile-only DDL test — 34 assertions

`tests/test_core_schema_parity.py` compiles each Core `Table` to BOTH dialects
(`both_ddl(table)` → `{pg, sqlite}`) and compares against the reference DDL — **34
`both_ddl` call-sites** (one+ per table). This proves the Core-generated DDL is
byte-equivalent to the legacy hand-DDL on both backends, which is why the
migration needed no `_SCHEMA_VERSION` bump. So the single-source claim isn't just
structural — it's continuously verified that the ONE definition emits correct DDL
for BOTH backends.

### 1d. Where a schema decision IS still made twice (the honest exceptions)

The single-source discipline covers TABLE DEFINITIONS. Three things are still
authored per-backend, by necessity — and each is correctly isolated, not drift:
1. **Backend-specific EXTRAS** Core can't express: PG-only `tsvector`/GIN/`pg_trgm`
   full-text infra + triggers (`_schema_pg`), SQLite FTS5 (`_schema_sqlite`).
   These are genuinely different features, not the same table twice.
2. **`ALTER TABLE` upgrade migrations** — each backend applies its own
   column-add loop (guarded by `_column_exists`). This is the ONE place a NEW
   column must be added twice (once per backend's ALTER loop) — the documented
   drift-risk, now mitigated by `_CRITICAL_COLUMNS` + the equality test.
3. **The `_CRITICAL_COLUMNS` self-heal dict** — authored in both files but PINNED
   EQUAL by the test (§1b). The right pattern would be a single shared
   definition; the current pattern (duplicate + equality-test) is acceptable and
   is what the JOURNAL's fix chose.

**Assessment:** the parity seam is SOUND. The single genuine residual risk — a
new column added to one ALTER loop but not the other — is mitigated by the
critical-column self-heal + the cross-backend equality test. The highest-severity
defect class this unit could hold (divergent table definitions) is STRUCTURALLY
IMPOSSIBLE (one definition author). A refactor note (§7): consider hoisting
`_CRITICAL_COLUMNS` to a single shared module so even the dict isn't authored
twice — but the equality test makes this low-priority.

---

## 2. Module inventory (real `wc -l`, size verdict, status, tests)

### 2.1 The schema layer

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `_core_schema.py` | 1185 | **BIG (justified)** | HOT (single table source) | `test_core_schema_parity` (34 `both_ddl`), `test_core_schema_groundwork` |
| `_schema_pg.py` | 906 | **BIG** | HOT | `test_pg_critical_column_selfheal`, `test_cache_schema_stability` |
| `_schema_sqlite.py` | 799 | **BIG** | HOT (fallback) | `test_sqlite_critical_column_selfheal` (incl. equality test) |
| `schema_registry.py` | 162 | OK | HOT | `test_schema_registry` |

`_core_schema.py` — **BIG but the size is intrinsic + single-purpose.** It is
46 table definitions + the dialect helpers (`jsonb_column`/`bigint_column`/
`bool_column`/`autoincrement_pk`/`epoch_now`/…) + `define_table`/`ddl_for`/
`upsert`/`both_ddl`. Every line serves "define a table once for both backends."
Splitting it (e.g. by domain) would fragment the single source of truth this unit
depends on — net-negative. **Do NOT split** (like `_sse_core` in Unit 2, big for
an intrinsic reason). It IS a §10.3 schema-change file (sign-off to edit).

`_schema_pg.py` / `_schema_sqlite.py` — **BIG mirrors.** Each is: fast-path
version cache + `_missing_critical_columns` self-heal + `create_if_absent` calls
for every Core table + backend-specific extras (tsvector/FTS5) + the ALTER
migration loop. Cohesive per backend; the size is the migration loop (~40 tables ×
guarded creates). A split isn't warranted — they must stay mirror-parallel, and
splitting one forces splitting the other identically. BIG-but-right.

`schema_registry.py` — OK. The pluggable-domain seam (`tofu.schema` entry-point)
— the DB-side mirror of the tools/providers/blueprints registries. Lets trading
(now external) contribute its schema without core edits. Clean, single-concern.

### 2.2 The connection + query layer

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `_core.py` | 2433 | **MISCUT** | HOT | `test_db_thread_conn_lifecycle`, broad e2e |
| `_wrappers.py` | 443 | OK | HOT | via all PG e2e |
| `_sql_translate.py` | 324 | OK | HOT | `test_sql_translate` |
| `aio.py` | 264 | OK | HOT (async facade) | `test_db_thread_conn_lifecycle` |

`_core.py` — **MISCUT, and it's the biggest file in the unit (2433).** Its own
docstring lists what it "retains": config constants + connection-resilience
params + the connection POOL + request-scoped/thread-local helpers + `init_db`
entry + backend auto-detection + (grepping confirms) the dead-connection
reconnect logic, the slow-query timing, the shutdown-race handling, TOAST
self-heal, and the write-retry helper. That is at least 5 separable concerns
(config / pool / thread-local `g` handling / retry+resilience / backend-detect).
The extraction pattern already worked here — `_sql_translate` and `_wrappers`
were pulled OUT of `_core` (both docstrings say "Extracted from _core.py for
modularity"). `_core` is the core left behind. Split candidate (§7).

`_wrappers.py` — OK. `DictRow` + `PgCursor` + `PgConnection` + PG param
sanitization (null-byte/surrogate strip, the `json_dumps_pg` JSONB-safety). One
cohesive concern (the PG cursor/connection wrapper that makes psycopg2 look like
sqlite3). The `PgCursor.execute` → `translate_sql` call is THE per-query seam.

`_sql_translate.py` — OK, and notable. The permanent SQLite→PG dialect bridge
(~14 transforms: `?`→`%s`, `json_extract`→jsonb, `strftime`→PG time, PRAGMA
no-ops, INSERT OR REPLACE→ON CONFLICT). Its docstring is careful that it is NOT a
migration leftover: it's load-bearing for all ~360 parametrized call-sites so the
codebase authors ONE SQLite-flavored query that runs on both backends. Only the
`_PK_MAP` upsert branch is superseded (by `_core_schema.upsert`), and `_PK_MAP`
now holds ONLY external `tofu-trading` tables (guarded by a no-dead-entries test).
Well-bounded.

`aio.py` — OK. The async facade (Stage-2 native-async migration): leak-safe
awaitable DB (`async_execute`/`async_fetchone`/`async_transaction`/`run_pooled`).
Clean single concern.

### 2.3 PG server lifecycle (the bootstrap cluster)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `_bootstrap.py` | 1354 | **BIG** | live (startup) | `test_pg_ensure_running_helpers`, `test_pg_explicit_local_autostart`, `test_pg_require_wait_retry` |
| `_pg_ownership.py` | 1156 | **BIG (just extracted)** | live | `test_pg_startup_lock`, `test_pg_stable_identity`, `test_pg_ip_flap_takeover`, `test_pg_copy_self_heal` |
| `_pg_backup.py` | 571 | OK | live (scheduled) | `test_db_pg_backup_split` |
| `_pg_seed.py` | 408 | OK | live (bootstrap) | via bootstrap e2e |
| `db_paths.py` | 213 | OK | HOT | via path e2e |
| `pg_admin.py` | 158 | OK | live | `test_pg_admin_direct_lane` |
| `wal_archive.py` | 181 | OK | live | via wal e2e |
| `_orphan_heal.py` | 93 | leaf | live | `test_orphan_heal` |

`_bootstrap.py` — **BIG (1354).** PG server management (start/stop/discover/
bootstrap, cross-platform). The JOURNAL shows `_pg_ownership.py` (ownership/lock/
heartbeat/host-identity) was extracted from it TODAY (2026-07-11 "Decoupling D",
§10-signed-off) — so this file is MID-DECOMPOSITION, actively shrinking via the
proven extract-with-facade pattern (it re-imports every relocated symbol so
`_bootstrap.<name>` still resolves). **Flag: recently-touched; the further split
is already in progress.** Not a fresh split recommendation — the owner is already
doing it.

`_pg_ownership.py` — **BIG (1156) but freshly extracted + cohesive.** Owns all 5
process-ownership mutable globals + their accessors in one place (the extraction's
whole point: "no `global` mutation straddles a module boundary"). Startup flock +
heartbeat + instance-stamp/copy-detection + host-identity. Big but single-purpose
(PG process ownership) and just landed clean. No action.

The rest (`_pg_backup`, `_pg_seed`, `db_paths`, `pg_admin`, `wal_archive`,
`_orphan_heal`) are all well-bounded single-concern modules.

### 2.4 The messages-as-rows migrator

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `messages_rows.py` | 250 | OK | live (flag-gated OFF) | `test_messages_rows` |

`messages_rows.py` — OK, and worth flagging as a LIVE-but-DARK migration (like
Unit 1's `segments`, Unit 4's `FlowExecutor`). It's the Phase-5 "messages-as-rows"
migrator: dual-write into `conversation_messages` behind `TOFU_MESSAGES_ROWS`
(default OFF) + a byte-identity `verify_*_parity` gate before any read cutover
(`TOFU_MESSAGES_ROWS_READ`, also OFF). Clean strangler-fig: JSONB array stays
authoritative, rows are mirrored + verified, reads flip only after parity proves.
Correctly bounded.

---

## 3. Dependencies (in / out)

**Inbound:** EVERYTHING persists through `from lib.database import get_thread_db,
DOMAIN_CHAT, db_execute_with_retry` — the entire app (tasks, conversations,
Project Brain, billing, memory, scheduler). `__init__.py` re-exports the whole
public surface from `_core` (+ `_bootstrap.backup_pg_database`, + the `aio` async
facade) so `from lib.database import get_db` is stable.

**Internal edges (the layering):**
- `_core` → `_wrappers` → `_sql_translate` (the per-query translate seam).
- `_core.init_db` → `_schema_pg.init_db` / `_schema_sqlite.init_db` (dialect-branch)
  → both call `_core_schema.create_if_absent(core_table)` + `schema_registry.run_registered`.
- `_schema_pg`/`_schema_sqlite` → `_core_schema` (import the Core tables).
- `_bootstrap` → `_pg_ownership` (facade re-import after today's extraction);
  both resolve their 2 core call-outs (`_audit`, `_pg_real_connect_ok`) LAZILY to
  avoid a cycle.
- `messages_rows` → `_core_schema.upsert` + `conversations.search_index.build_search_text`.

**Outbound:** `lib/log`, `lib/compat` (platform), `lib/env_compat`, `lib/ttl_cache`
(the translate cache), psycopg2 / sqlite3. `_bootstrap` shells out to `pg_ctl`/
`initdb`. **No back-edges up into `routes` or `tasks_pkg`** — the data tier is the
bottom of the stack (one exception: `messages_rows` reaches SIDEWAYS to
`conversations.search_index` for the parity blob — a leaf util, acceptable).

---

## 4. Invariants (must not be broken by a refactor)

1. **`_core_schema.py` is the SINGLE table-definition author** (§1a). A new table
   is defined ONCE here + a parity test — never hand-authored in `_schema_*`.
   §10.3 sign-off to edit.
2. **`_schema_pg` and `_schema_sqlite` are byte-parallel mirrors** — a fix to one
   fast-path/migration branch MUST be ported to the other (the JOURNAL's
   `21290a2` lesson). `_CRITICAL_COLUMNS` equality is pinned by
   `test_pg_and_sqlite_critical_sets_match`.
3. **`_CRITICAL_COLUMNS` self-heal runs BEFORE the version fast-path** on both
   backends — catches the FUSE non-atomic-ALTER "version-current-but-column-missing"
   divergence. Read-only, best-effort, missing-table-skipped.
4. **`_SCHEMA_VERSION` is identical across backends** (39) and bumps only on a
   real DDL change; a byte-identical Core migration needs no bump.
5. **`translate_sql` runs on EVERY statement on the PG path** — it is the seam
   that lets one SQLite-flavored query run on both backends. The `%`-doubling in
   `_translate_placeholders` is subtle and correctness-critical (a literal `%` in
   a `LIKE` pattern breaks psycopg2 interpolation otherwise).
6. **`INSERT OR REPLACE` into an unmapped table RAISES, never DO-NOTHING** — a
   silent DO NOTHING would drop the write (data loss). New upsert tables use
   `_core_schema.upsert`; only external trading tables remain in `_PK_MAP`.
7. **All string params are PG-sanitized** (null-byte + surrogate strip;
   `json_dumps_pg` for JSONB) — PG rejects `\x00`/`\u0000`/lone surrogates.
8. **The dead-connection transparent reconnect only fires on a CLEAN connection**
   (`not self._dirty`) — never mid-transaction, never on a statement_timeout
   cancellation (only genuine connection death).
9. **`messages_rows` dual-write is best-effort + never authoritative** — the JSONB
   array stays the source of truth until `verify_*_parity` passes and the read
   flag flips.
10. **The single-box install is byte-identical with PG off** (charter) — SQLite
    fallback is a first-class path, not a degraded stub; every Core table + the
    critical-column self-heal work identically on it.

---

## 5. Known debt (grounded)

- **`_core.py` (2433) is MISCUT** — 5 concerns (config / pool / thread-local /
  retry-resilience / backend-detect) in the core left behind after `_wrappers` +
  `_sql_translate` were extracted (§2.2).
- **`_bootstrap.py` (1354) is mid-decomposition** — `_pg_ownership` was extracted
  from it TODAY; the further split is already in progress (not a fresh finding).
- **`_CRITICAL_COLUMNS` is authored twice** (§1d) — mitigated by the equality
  test, but could be a single shared definition.
- **CLAUDE.md §1 undercounts this package** (6 modules listed, 18 on disk) — a
  doc-drift bug, especially the today-landed `_pg_ownership`.
- The mirror-parallel `_schema_pg`/`_schema_sqlite` structure is an inherent
  maintenance tax (every migration authored twice) — but it's the price of a
  genuine dual-backend, and the critical-column self-heal + parity tests are the
  correct mitigation, not a further split.

---

## 6. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
`_core_schema` (the single table source — do NOT split despite size),
`_wrappers`, `_sql_translate`, `aio`, `schema_registry`, `_pg_backup`,
`_pg_seed`, `db_paths`, `pg_admin`, `wal_archive`, `_orphan_heal`,
`messages_rows`, `_pg_ownership` (freshly + cleanly extracted).

**Miscut — should split:**
1. **`_core.py` (2433) → extract the connection POOL + resilience** (dead-conn
   reconnect, slow-query timing, shutdown-race, TOAST heal, retry) into
   `_pool.py` / `_resilience.py`, leaving config + `init_db` entry +
   backend-detect in `_core`. Follows the PROVEN pattern that already pulled
   `_wrappers`/`_sql_translate` out of this exact file. Behind
   `test_db_thread_conn_lifecycle` + the `test_pg_*` suite. RISK: bottom-of-stack
   hot path + §10 (DB-schema-adjacent) — owner sign-off, same discipline the
   `_pg_ownership` extraction used today.

**Mid-decomposition — no NEW verdict (already in progress):**
`_bootstrap.py` (1354) — `_pg_ownership` extracted today; let that settle.

**Do NOT split:** `_core_schema` (single source of truth — fragmenting it defeats
the parity seam), the `_schema_pg`/`_schema_sqlite` mirrors (must stay parallel).

**Low-priority hardening (not a split):** hoist `_CRITICAL_COLUMNS` to one shared
module so even the dict isn't authored twice (§1d). The equality test makes this
optional.

---

## 7. Comparison to Units 1–6 (the running thesis)

- **This unit has the STRONGEST single-source discipline in the survey.** Where
  Unit 6's `claims_by_conv` is single-sourced but two composite readers duplicate
  the shape, here the highest-consequence artifact (a table definition) has
  EXACTLY ONE author (`_core_schema`), gated by a parity test AND a cross-backend
  equality test. The feared backend-divergence is structurally impossible.
- **The one real drift-risk is the mirror-migration ALTER loops** — and the
  codebase already learned this lesson the hard way TODAY (`21290a2`) and pinned
  it with an equality test. That is the project's "convert symptom-patch into a
  mechanism" program working exactly as intended.
- **`_core.py` is the same miscut species as `manager.py` (Unit 1) and
  `api.py` (Unit 2)** — a hot core left behind after sibling concerns were
  extracted around it. Third instance of the pattern; the fix is the same proven
  extract-with-facade move (which `_pg_ownership`'s extraction today demonstrates
  works cleanly on this exact package).
- **Extraction-with-facade is the dominant healthy pattern here** — `_wrappers`,
  `_sql_translate`, `_pg_ownership`, `_pg_backup` were all pulled out of the two
  giants with re-export facades. This package is actively self-improving, not
  rotting.

---

*Next unit: Unit 8 (Auth / providers / billing — `oauth/`, `byo_*`, `api_keys`,
`billing/`, `pricing`, `rate_limit_*`).*
