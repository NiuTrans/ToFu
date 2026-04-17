# PostgreSQL → SQLite: Technical Analysis

**Date**: 2026-04-14
**Decision**: Replace PostgreSQL with SQLite

## Executive Summary

After thorough analysis of the codebase, PostgreSQL should be replaced with SQLite.
The project uses zero advanced PG features that justify the ~3,300 lines of infrastructure
overhead and the 12+ documented bug categories that PostgreSQL's complexity introduced.

## Features Used vs. Available

| PG Feature | Used? | SQLite Equivalent |
|---|---|---|
| JSONB columns | 2 columns — but stored as TEXT, parsed with json.loads() | TEXT + json.loads() (identical) |
| tsvector + GIN | Full-text search | FTS5 (built into Python's sqlite3) |
| pg_trgm + ILIKE | Substring fallback | LIKE (case-insensitive via COLLATE NOCASE) |
| SERIAL | Auto-increment IDs | INTEGER PRIMARY KEY AUTOINCREMENT |
| ON CONFLICT DO UPDATE | 2 upsert sites | INSERT OR REPLACE / ON CONFLICT (SQLite 3.24+) |
| TIMESTAMPTZ | Timestamps | INTEGER (epoch ms, already used this way) |
| Connection pooling | Yes (custom pool) | Not needed (file-based, connections are ~free) |
| Stored procedures | No | N/A |
| CTEs / window functions | No | N/A |
| Complex joins | No | N/A |
| Transactions | Basic COMMIT/ROLLBACK | Full ACID support |

## Complexity Cost of PostgreSQL

| Component | Lines | Purpose |
|---|---|---|
| `_bootstrap.py` | 1,055 | PG server lifecycle (initdb, pg_ctl, cross-machine discovery) |
| `_core.py` | 699 | Connection pool, semaphore, reaper threads, TCP keepalives |
| `_sql_translate.py` | 225 | SQLite→PG SQL translation (proves code is still SQLite-native) |
| `_wrappers.py` | 357 | Null-byte sanitization, PgCursor/PgConnection wrappers |
| `_schema.py` | 899 | PG-specific DDL, migrations, savepoint workarounds |
| **Total** | **3,235** | |

## Documented Bug Categories (from project memories)

1. JSONB \u0000 escape rejection (SQLite: no issue)
2. Naive null-byte stripping corrupted escaped backslashes
3. ALTER TABLE rollback causes ~30s FUSE WAL fsync (6.5 min startup)
4. Cross-machine PG ownership conflicts on shared FUSE
5. "Too many clients" deadlocks requiring semaphore infrastructure
6. macOS localhost DNS resolution failures with VPN
7. Cross-project data leaks from PG reuse
8. FUSE freeze connection cascading in crawl loops
9. Reserved word conflicts ("count")
10. Boolean=integer incompatibilities
11. Schema version caching needed to avoid 5+ min startup
12. SimHash unsigned 64-bit overflow in signed BIGINT

## User Experience Impact

**With PostgreSQL:**
- Requires `conda install postgresql` (unintuitive for many users)
- Or Docker (adds deployment complexity)
- Or pre-existing PG server (requires config)
- Cross-machine FUSE issues for shared storage users
- ~3,300 lines of infrastructure code to maintain

**With SQLite:**
- Zero installation — bundled with Python
- `python3 server.py` just works
- Single `data/chatui.db` file — portable, copyable
- WAL mode supports concurrent reads + serialized writes
- FTS5 provides equivalent full-text search

## Key Insight: The SQL Translation Layer

The existence of `_sql_translate.py` (225 lines) proves the codebase **never fully migrated
to PostgreSQL**. All SQL in routes/ and lib/ is written in SQLite-style syntax (`?` placeholders,
`INSERT OR REPLACE`, `PRAGMA`, etc.) and translated at runtime to PG syntax. Going back to
SQLite means this translation layer simply evaporates, and the SQL runs natively.

## Migration Plan

1. Rewrite `lib/database/` to use Python's built-in `sqlite3`
2. Replace tsvector search with FTS5
3. Replace ILIKE with LIKE (SQLite is case-insensitive by default for ASCII)
4. Replace `json_dumps_pg()` with `json.dumps(ensure_ascii=False)`
5. Remove all psycopg2 imports from consumer files
6. Update CI, Docker, install.py, export.py, CLAUDE.md
7. Provide optional migration script for existing PG data

## Risk Assessment

- **Thread safety**: SQLite WAL mode + `check_same_thread=False` supports the current
  usage pattern (mostly reads, serialized writes via `db_execute_with_retry`)
- **Concurrent writes**: SQLite uses file-level locking. Under heavy write load, writers
  queue up. For a single-user app, this is fine. The "database is locked" error is already
  handled by `_db_safe` decorator.
- **FTS5 quality**: SQLite FTS5 is mature, well-tested, and supports prefix matching,
  phrase queries, and ranking. It's used by Firefox, Chrome, and many production apps.
- **Data size**: SQLite handles databases up to 281 TB. Conversation data will never
  approach this limit.
