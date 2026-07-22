"""Self-heal + backfill helpers — PostgreSQL backend.

Critical-column divergence detection (``_CRITICAL_COLUMNS`` /
``_missing_critical_columns``), the one-time search_text/search_tsv backfills,
and the savepoint-guarded ``_safe_create_table``. ``_CRITICAL_COLUMNS`` is
single-homed here and re-exported from the package facade; it is consumed by
name.
"""

from lib.log import get_logger

from lib.database._schema_pg._meta import _column_exists, _table_exists

logger = get_logger(__name__)


# Critical columns that MUST exist on an already-current database. The version
# fast-path trusts the stored _schema_version integer as a proxy for "all DDL
# applied", but the version write and the per-column ALTERs are NOT atomic
# across restarts on slow (FUSE) storage: a migration run can commit the version
# yet have a later ADD COLUMN fail / time out, leaving the DB current-by-version
# but column-missing — after which every future boot fast-paths past the fix.
# Checked BEFORE the fast-path so such a divergence forces a full re-migration.
# Keep this to load-bearing columns whose absence makes a subsystem throw
# (a broad audit belongs in the parity test, not the boot path).
_CRITICAL_COLUMNS = {
    'project_tasks': (
        'blocked_until', 'block_count', 'block_reason',
        'wait_paths', 'dispatch_target', 'write_set',
    ),
    # Scheduler predicate-condition paradigm: these columns are named directly
    # in the create_timer / create_task / _record_poll INSERTs, so an existing
    # DB missing them throws on every timer/proactive create until re-migrated.
    'timer_watchers': (
        'condition_kind', 'condition_command', 'condition_regex', 'origin',
    ),
    'scheduled_tasks': (
        'condition_kind', 'condition_command', 'condition_regex',
    ),
    'timer_poll_log': (
        'tier', 'predicate_matched', 'llm_agreed',
    ),
    'proactive_poll_log': (
        'tier', 'predicate_matched', 'llm_agreed',
    ),
    # paper_library.folder_id is named directly in _PAPER_LIB_COLUMNS (the
    # GET /api/v1/paper/library SELECT), so a version-current DB missing it
    # throws UndefinedColumn on every bookshelf load until re-migrated. The
    # guarded ALTER lives in _chat.py but only runs on a full DDL pass.
    'paper_library': (
        'folder_id',
    ),
}


def _missing_critical_columns(conn):
    """Return a list of (table, column) that SHOULD exist but do not.

    Read-only (information_schema); best-effort. A missing table is skipped —
    the normal create path owns that, this guard only catches the
    version-current-but-column-missing divergence on an EXISTING table.
    """
    # Resolve the probes through the package facade so tests that
    # ``monkeypatch.setattr(lib.database._schema_pg, '_table_exists', ...)``
    # (the historical single-module namespace) still intercept them —
    # byte-identical to the pre-split behaviour.
    import lib.database._schema_pg as _sp
    table_exists = getattr(_sp, '_table_exists', _table_exists)
    column_exists = getattr(_sp, '_column_exists', _column_exists)

    missing = []
    for table, cols in _CRITICAL_COLUMNS.items():
        try:
            if not table_exists(conn, table):
                continue
            for col in cols:
                if not column_exists(conn, table, col):
                    missing.append((table, col))
        except Exception as e:
            logger.debug('[DB] critical-column probe failed for %s: %s', table, e)
    return missing


def _backfill_search_text(conn):
    """One-time migration: populate search_text and search_tsv for existing conversations.

    Reads messages JSON in batches, extracts plaintext via build_search_text(),
    and writes back.  Runs only once (when search_text column is newly added).
    """
    import json
    from lib.conversations import build_search_text

    cur = conn._conn.cursor()
    cur.execute("SELECT id, messages FROM conversations WHERE search_text = '' AND msg_count > 0")
    rows = cur.fetchall()
    updated = 0
    for row_id, messages_raw in rows:
        try:
            messages = json.loads(messages_raw) if isinstance(messages_raw, str) else messages_raw
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[DB] Failed to parse messages for conv %s: %s', row_id, e)
            continue
        st = build_search_text(messages)
        if st:
            cur.execute(
                "UPDATE conversations SET search_text = %s, "
                "search_tsv = to_tsvector('simple', left(%s, 50000)) WHERE id = %s",
                (st, st, row_id))
            updated += 1
    conn._conn.commit()
    logger.info('[DB] Backfilled search_text for %d/%d conversations', updated, len(rows))


def _backfill_search_tsv(conn):
    """One-time migration: populate search_tsv from existing search_text.

    Runs when the search_tsv column is added but search_text already exists.
    """
    cur = conn._conn.cursor()
    cur.execute(
        "UPDATE conversations SET search_tsv = to_tsvector('simple', left(search_text, 50000)) "
        "WHERE search_text != '' AND search_tsv IS NULL")
    count = cur.rowcount
    conn._conn.commit()
    logger.info('[DB] Backfilled search_tsv for %d conversations', count)


def _safe_create_table(cur, ddl):
    """Execute CREATE TABLE IF NOT EXISTS, tolerating pg_type conflicts.

    PostgreSQL auto-creates a composite type for each table.  If a prior
    init crashed after CREATE TABLE but before the schema version was
    persisted, re-running the same CREATE can hit:
        UniqueViolation on pg_type_typname_nsp_index
    because the type already exists even though IF NOT EXISTS should
    handle the table itself.  We wrap in a savepoint so one failure
    doesn't abort the entire transaction.
    """
    try:
        cur.execute('SAVEPOINT _safe_ddl')
        cur.execute(ddl)
        cur.execute('RELEASE SAVEPOINT _safe_ddl')
    except Exception as e:
        err_str = str(e)
        # Known harmless: table/type already exists from a previous partial init
        if 'already exists' in err_str or 'UniqueViolation' in type(e).__name__:
            cur.execute('ROLLBACK TO SAVEPOINT _safe_ddl')
            cur.execute('RELEASE SAVEPOINT _safe_ddl')
            logger.debug('[DB] Table already exists (tolerating): %.200s', err_str)
        else:
            cur.execute('ROLLBACK TO SAVEPOINT _safe_ddl')
            cur.execute('RELEASE SAVEPOINT _safe_ddl')
            raise
