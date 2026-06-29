"""lib/database/_orphan_heal.py — startup self-heal for orphan tables.

Some tables get created in a deployment's DB by code that later disappears,
leaving the table behind with no Core definition and no live reader/writer:

  * ``agent_sessions`` — backed the ``lib/agent_backends/session_store.py``
    CLI-backend session store (Claude Code / Codex ``--resume``). The whole
    ``agent_backends`` subsystem was removed (trashed 2026-06-21); the table
    was orphaned in every deployment that had booted the old code. It is
    NEVER written by current code, so an EMPTY one is pure litter. We drop it
    ONLY when empty — if some out-of-tree fork still writes to it, the row
    guard prevents destroying data.
  * ``_aio_test`` — a scratch table created by ``tests/test_db_aio.py`` that
    leaked into a real DB when the test harness resolved to a non-test backend
    (the historical ambient-``TOFU_DB_BACKEND=postgres`` hole). It is never a
    real table, so it is dropped unconditionally.

This runs on EVERY ``init_db()`` (before the schema-version fast-path check),
because existing deployments are already at the current ``_SCHEMA_VERSION`` and
would otherwise skip all DDL — and the whole point is that an instance we never
touch converges by itself on its next startup. It is cheap (a couple of
``_table_exists`` probes) and idempotent: once an orphan is gone the probe is a
no-op. The acceptance criterion this enforces, deployment-wide, is
"live-DB ∖ Core ∖ external-plugin tables = ∅".

Adding a new entry: append to ``_ORPHANS``. ``require_empty=True`` makes the
drop conditional on the table holding zero rows (fail-safe for a table that
MIGHT still be written somewhere); ``require_empty=False`` is for tables that
are unambiguously never-real (test scratch).
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# (table_name, require_empty, reason) — the registry of known orphans.
_ORPHANS = (
    ('agent_sessions', True,
     'removed agent_backends subsystem (trashed 2026-06-21); no Core def, no live reader'),
    ('_aio_test', False,
     'scratch table leaked from tests/test_db_aio.py; never a real table'),
)


def heal_orphan_tables(conn, *, table_exists, count_rows) -> list:
    """Drop known orphan tables from this DB. Best-effort, never raises.

    Args:
        conn: project DB connection wrapper (has ``_conn``).
        table_exists: backend ``_table_exists(conn, name)`` callable.
        count_rows: callable ``(conn, name) -> int`` returning the table's row
            count (used only for ``require_empty`` orphans).

    Returns:
        The list of table names actually dropped (possibly empty).
    """
    dropped = []
    for name, require_empty, reason in _ORPHANS:
        try:
            if not table_exists(conn, name):
                continue
            if require_empty:
                try:
                    n = count_rows(conn, name)
                except Exception as e:
                    # Could not count → refuse to drop (fail safe; never risk
                    # destroying data we couldn't measure).
                    logger.warning('[DB-OrphanHeal] %s present but row count failed '
                                   '(%s) — NOT dropping (fail-safe)', name, e)
                    continue
                if n > 0:
                    logger.warning('[DB-OrphanHeal] orphan %s has %d row(s) — '
                                   'NOT dropping (a live writer may exist); '
                                   'leaving in place for manual review', name, n)
                    audit_log('db_orphan_table_nonempty_kept', table=name,
                              rows=n, reason=reason)
                    continue
            conn._conn.cursor().execute(f'DROP TABLE IF EXISTS {name}')
            conn._conn.commit()
            dropped.append(name)
            logger.warning('[DB-OrphanHeal] dropped orphan table %s (%s)', name, reason)
            audit_log('db_orphan_table_dropped', table=name, reason=reason)
        except Exception as e:
            # Never let a self-heal failure abort schema init.
            logger.warning('[DB-OrphanHeal] could not drop orphan %s: %s', name, e)
            try:
                conn._conn.rollback()
            except Exception as _re:
                logger.debug('[DB-OrphanHeal] rollback after failed drop: %s', _re)
    return dropped
