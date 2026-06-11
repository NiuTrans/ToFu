# Hot-Trio Queries-on-Core Conversion — Plan & Audit

> Convert the remaining `INSERT OR REPLACE` writers for **conversations**,
> **task_results**, **task_events** onto `lib/database/_core_schema.upsert()`,
> then retire their `_PK_MAP` entries. This is the final, highest-risk batch of
> the queries-on-Core track; it gets its own restart verification.
>
> Audit date: 2026-06-10. All three tables are already **schema-on-Core**
> (Core `Table` defs + parity tests + `create_if_absent` wiring exist and are
> live-verified). This batch is **queries-only** — no schema/DDL changes.

## Scope rule (what counts)

Only **`INSERT OR REPLACE INTO <table>`** production statements are in scope.
Explicitly OUT of scope:
- `UPDATE conversations SET … WHERE … AND updated_at=?` — the **CAS optimistic-lock**
  sync writers (`_sync_result_to_conversation` manager.py:~941, `_sync_partial_to_conversation`
  manager.py:~1347, and the endpoint settings writer ~1519). They deliberately use raw
  `db.execute()+commit()` to preserve `cur.rowcount` for CAS-miss detection and FTS gating
  (see memory `sync-fixes-2026-05-31`). Converting them would break the lock. **Leave as-is.**
- `conversations_fts` (FTS5 virtual table, SQLite-only) — permanently exempt.
- `debug/**` and `tests/**` — not production; left alone (tests updated only at retirement).

## Call-site inventory (production)

### conversations — 7 sites, all `INSERT OR REPLACE`, all partial-column
| # | File:line | Cols | retry? | Surrounding context |
|---|-----------|------|--------|---------------------|
| 1 | routes/conversations.py:510 (`save_conv`) | 9 (no search_tsv) | retry | `build_search_text` → write → `update_conversation_fts` → `_invalidate_meta_cache` |
| 2 | routes/conversations.py:718 (delete_message) | 9 | retry | preserve `created_at` SELECT → write → FTS → meta-invalidate → day-cost invalidate |
| 3 | routes/conversations.py:849 (patch_msg) | 9 | retry | preserve `created_at` → write → FTS → audit_log |
| 4 | routes/conversations.py:961 (patch_msg_id) | 9 | retry | preserve `created_at` → write → FTS → audit_log |
| 5 | routes/conversations.py:1059 (delete_branch) | 9 | retry | preserve `created_at` → write → FTS |
| 6 | lib/chat/persistence.py:172 (`persist_conv_messages`) | 9 | retry | central send/regen/edit/continue writer; preserve `created_at`; `_assign_message_ids` upstream; → FTS |
| 7 | lib/feishu/conversation.py:155 (feishu sync) | **8** (no settings, no search_tsv) | **plain `db.execute`** | folded in from earlier batch; → `update_conversation_fts` |

- **All 7 are partial-column** → need `insert_cols=[...]` (the capability we just added).
  - Sites 1–6: `insert_cols=['id','user_id','title','messages','created_at','updated_at','settings','msg_count','search_text']` (omit `search_tsv` → trigger fills).
  - Site 7 (feishu): `insert_cols=['id','user_id','title','messages','created_at','updated_at','msg_count','search_text']` (omit `settings` → DEFAULT `'{}'`, and `search_tsv` → trigger).
  - `conflict_cols` = `['id','user_id']` (the composite PK) — the default, so can be omitted.
  - `update_cols` defaults to inserted-minus-conflict → correct full-replace-of-written.
- **retry**: sites 1–6 use `db_execute_with_retry` → `retry=True`. Site 7 uses plain
  `db.execute` then `db.commit()` → `retry=False, commit=True`.
- **FTS coupling**: every site calls `update_conversation_fts(db, conv_id, search_text)`
  AFTER the write. KEEP that call unchanged — it's separate from the upsert. The `search_tsv`
  trigger (PG) fires on the upsert itself; FTS5 (SQLite) is updated by that explicit call.
- **created_at preservation**: sites 2–6 SELECT existing `created_at` first. KEEP that —
  it feeds the row dict. (INSERT OR REPLACE would otherwise reset it; our upsert writes the
  preserved value explicitly, same as today.)

### task_results — 2 sites, both `INSERT OR REPLACE`, partial-column, retry
| # | File:line | Cols | retry? | Context |
|---|-----------|------|--------|---------|
| 1 | lib/tasks_pkg/manager.py:409 (`persist_task_result`, terminal) | 10 (no search_results) | retry | status=task's; followed by `_sync_result_to_conversation` (CAS, separate) |
| 2 | lib/tasks_pkg/manager.py:1194 (checkpoint, partial) | 10 (no search_results) | retry | status='running'; followed by `_sync_partial_to_conversation` (CAS, separate) |

- Both **partial-column** (omit `search_results`, which is nullable) →
  `insert_cols=['task_id','conv_id','content','thinking','error','status','tool_rounds','metadata','created_at','completed_at']`.
- `conflict_cols=['task_id']` (PK, default). `retry=True`.
- The CAS `_sync_*_to_conversation` calls that follow are OUT of scope (UPDATE, not upsert).

### task_events — 1 site, `INSERT OR IGNORE` (NOT `INSERT OR REPLACE`)
| # | File:line | Form | Context |
|---|-----------|------|---------|
| 1 | lib/tasks_pkg/event_log.py:105 (`append_persistent_event`) | `INSERT OR IGNORE` | **highest-frequency write in the system** (every SSE delta); checks `cur.rowcount==0` to log a collision canary (data-loss detector) |

- This is a **DO NOTHING** upsert → `upsert(insert_cols=[all 5], update_cols=[], conflict_cols=['task_id','event_id'])`.
- ⚠️ **CRITICAL: must NOT use `retry=True`** — `retry` returns `None`, destroying the
  `cur.rowcount` the collision-canary depends on. Use `retry=False`, capture the returned
  cursor, keep the `rc == 0` WARNING logic verbatim. Verify `ON CONFLICT … DO NOTHING`
  reports `rowcount=0` on a real duplicate on BOTH backends (psycopg2 + sqlite3) — this is
  the one behavioral risk in the batch and gets a dedicated test.
- `task_events` already has `update_cols=[] → DO NOTHING` coverage in groundwork tests; add
  a rowcount-on-conflict assertion.

## Totals
- **10 production sites**: 7 conversations + 2 task_results + 1 task_events.
- All 3 tables: partial-column (need `insert_cols`). 9 of 10 are retry-wrapped; the 2
  exceptions are feishu (plain execute) and event_log (must stay plain to keep rowcount).
- 0 of the in-scope sites are CAS writers — the 3 CAS `UPDATE` paths are correctly out of scope.

## Risks & mitigations
1. **event_log rowcount canary** (highest): `DO NOTHING` + non-retry + cursor capture; test
   rowcount==0 on duplicate insert, both backends. If `ON CONFLICT DO NOTHING` rowcount
   semantics differ on PG vs sqlite3, keep event_log on its current literal SQL (it's the one
   site where the translator earns its keep) rather than force-fit.
2. **search_tsv trigger × partial insert**: the trigger fires on the Core-emitted INSERT
   (it's BEFORE INSERT/UPDATE on the table); `insert_cols` omitting `search_tsv` is exactly
   right (the column must NOT be in the INSERT for the trigger to own it). Verified shape via
   `upsert_sql` earlier. Live-verify on PG: insert a probe conv, confirm `search_tsv` populated.
3. **created_at preservation**: unchanged — still SELECT-then-pass; upsert writes the value.
4. **FTS double-update**: none — `update_conversation_fts` stays as the explicit post-write
   call it is today.
5. **Volume**: task_events is per-delta. The `_UPSERT_SQL_CACHE` memoization means one compile
   per (table, cols, backend); no per-call SQL build. Confirm cache hit in a smoke test.

## Sequencing (each step independently reviewable; one restart at the end)
1. **task_events first, in isolation** — it's the riskiest (rowcount canary) and self-contained.
   Convert event_log.py:105, keep rowcount logic, add the DO-NOTHING-rowcount test (both backends).
2. **task_results** (2 sites, manager.py) — uniform, retry=True, partial-column.
3. **conversations sites 1–6** (routes + persistence) — uniform shape; convert together,
   keep every `update_conversation_fts` + `created_at` SELECT.
4. **feishu (site 7)** — the one plain-execute / 8-col variant.
5. **Retire `_PK_MAP`**: after grep shows ZERO `INSERT OR REPLACE INTO {conversations,task_results,task_events}`,
   remove all three entries + their `test_known_tables_emit_conflict_target` params.
   (`conversations_fts` stays in `SQLITE_ONLY`.)
6. **Verify**: full suite green → user restart → live-PG checks:
   conversations probe (insert+conflict, search_tsv populated, FTS hit, row count 1),
   task_results probe (insert+update status), task_events probe (insert, duplicate→rowcount 0
   + no dup row), and a real chat round-trip (send → checkpoint → done persists).

## Notes
- All conversions are EXECUTION-path → live immediately on deploy of the code (no restart
  needed for the query change itself); the restart in step 6 is to exercise a clean init +
  confirm nothing regressed, not because the upsert needs it.
- No `_SCHEMA_VERSION` bump (no DDL change).
