"""Database schema initialization — SQLite backend: self-heal + FTS backfill.

Critical-column self-heal guard (catches the version-current-but-column-missing
divergence) and the one-time FTS5 search_text backfill migration.
"""

from lib.log import get_logger

from lib.database._schema_sqlite._meta import _column_exists, _table_exists

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
        'wait_paths', 'dispatch_target', 'write_set', 'blocked_by',
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
    # throws on every bookshelf load until re-migrated. The guarded ALTER
    # lives in _chat.py but only runs on a full DDL pass.
    # parser_version is named in the harvest cache probe's WHERE clause (see
    # the PG twin for the failure shape).
    'paper_library': (
        'folder_id', 'parser_version',
    ),
}


def _missing_critical_columns(conn):
    """Return a list of (table, column) that SHOULD exist but do not.

    Read-only (PRAGMA table_info); best-effort. A missing table is skipped —
    the normal create path owns that, this guard only catches the
    version-current-but-column-missing divergence on an EXISTING table.
    """
    missing = []
    for table, cols in _CRITICAL_COLUMNS.items():
        try:
            if not _table_exists(conn, table):
                continue
            for col in cols:
                if not _column_exists(conn, table, col):
                    missing.append((table, col))
        except Exception as e:
            logger.debug('[DB] critical-column probe failed for %s: %s', table, e)
    return missing


def _missing_core_tables(conn):
    """Return always-on Core table names absent from the database.

    The table-shaped twin of ``_missing_critical_columns``: the version
    fast-path trusts the stored integer as a proxy for "all DDL applied",
    but a table added to ``_core_schema`` WITHOUT bumping ``_SCHEMA_VERSION``
    is skipped by that proxy FOREVER — the 2026-07-25 paper_podcasts
    incident (L3 registered the table, version stayed 41, every existing
    deployment fast-pathed past the create DDL and 500'd UndefinedTable at
    runtime; fresh test DBs ran full DDL, which is why CI never saw it).
    Checked BEFORE the fast-path so the divergence forces a full
    re-migration. The probe list derives from the Core MetaData minus
    optional-domain tables (``core_boot_table_names``), so a NEW Core table
    is covered automatically. Read-only; best-effort — probe errors are
    logged at debug and skipped, never startup-fatal.
    """
    import lib.database._schema_sqlite as _ss
    table_exists = getattr(_ss, '_table_exists', _table_exists)
    try:
        from lib.database._core_schema._helpers import core_boot_table_names
        names = core_boot_table_names()
    except Exception as e:
        logger.debug('[DB] core-boot-table list unavailable: %s', e)
        return []
    missing = []
    for name in names:
        try:
            if not table_exists(conn, name):
                missing.append(name)
        except Exception as e:
            logger.debug('[DB] core-table probe failed for %s: %s', name, e)
    return missing


def _backfill_search_fts(conn):
    """One-time migration: populate FTS5 table from existing conversations."""
    import json
    from lib.conversations import build_search_text

    cur = conn._conn.cursor()
    cur.execute("SELECT id, messages FROM conversations WHERE search_text = '' AND msg_count > 0")
    rows = cur.fetchall()
    updated = 0
    for row in rows:
        row_id = row[0]
        messages_raw = row[1]
        try:
            messages = json.loads(messages_raw) if isinstance(messages_raw, str) else messages_raw
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[DB] Failed to parse messages for conv %s: %s', row_id, e)
            continue
        st = build_search_text(messages)
        if st:
            cur.execute("UPDATE conversations SET search_text = ? WHERE id = ?", (st, row_id))
            # Also insert into FTS5
            cur.execute(
                "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                "SELECT rowid, ? FROM conversations WHERE id = ?",
                (st, row_id)
            )
            updated += 1
    conn._conn.commit()
    logger.info('[DB] Backfilled search_text for %d/%d conversations', updated, len(rows))
