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
    # Row-store reads name this marker directly. A version-current database
    # missing it must rerun the guarded conversations ALTER instead of failing
    # every window read with UndefinedColumn.
    'conversations': ('messages_rows_rev',),
    # Window reads select this column directly. NULL values are valid during
    # online backfill, but a physically missing column must force DDL repair.
    'conversation_messages': (
        'meta_light', 'translation_state', 'message_ts', 'billing_meta'),
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
    # throws UndefinedColumn on every bookshelf load until re-migrated. The
    # guarded ALTER lives in _chat.py but only runs on a full DDL pass.
    # parser_version is named in the harvest cache probe's WHERE clause — a
    # DB missing it makes every probe throw (caught → treated as a miss →
    # needless re-parses) until re-migrated.
    'paper_library': (
        'folder_id', 'parser_version',
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
    import lib.database._schema_pg as _sp
    table_exists = getattr(_sp, '_table_exists', _table_exists)
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


def _backfill_search_text(conn):
    """One-time migration: populate search_text and search_tsv for existing conversations.

    Reads messages JSON in batches, extracts plaintext via build_search_text(),
    and writes back.  Runs only once (when search_text column is newly added).
    """
    import json
    from lib.conversations import build_search_text
    from lib.database._access_policy import rows_authority_configured

    cur = conn._conn.cursor()
    authority = rows_authority_configured()
    if authority:
        cur.execute(
            "SELECT id, msg_count, rev, messages_rows_rev FROM conversations "
            "WHERE search_text = '' AND msg_count > 0")
    else:
        cur.execute(
            "SELECT id, messages FROM conversations "
            "WHERE search_text = '' AND msg_count > 0")
    rows = cur.fetchall()
    updated = 0
    for row in rows:
        row_id = row[0]
        if authority:
            expected_count = int(row[1] or 0)
            rev = int(row[2] or 0)
            marker = -1 if row[3] is None else int(row[3])
            cur.execute(
                'SELECT meta FROM conversation_messages '
                'WHERE conv_id = %s ORDER BY seq', (row_id,))
            message_rows = cur.fetchall()
            if marker != rev or len(message_rows) != expected_count:
                raise RuntimeError(
                    'normalized transcript is incomplete during PostgreSQL '
                    f'search backfill: conv={row_id}')
            messages = []
            try:
                for message_row in message_rows:
                    raw = message_row[0]
                    messages.append(
                        json.loads(raw) if isinstance(raw, str) else raw)
            except (json.JSONDecodeError, TypeError) as e:
                raise RuntimeError(
                    'invalid normalized transcript during PostgreSQL search '
                    f'backfill: conv={row_id}') from e
        else:
            messages_raw = row[1]
            try:
                messages = (json.loads(messages_raw)
                            if isinstance(messages_raw, str)
                            else messages_raw)
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
