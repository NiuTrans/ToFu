"""Post-install database acceptance through the canonical data layer."""

from __future__ import annotations

import uuid

from lib.database._core import (
    DOMAIN_SYSTEM,
    _BACKEND,
    db_execute_with_retry,
    init_db,
    pooled_db,
    pooled_write_transaction,
)
from lib.database.sqlite_owner import maintenance_write_authority


def verify_database_roundtrip() -> str:
    """Create schema, commit a probe, read it on a new lease, then remove it.

    Returns the resolved backend name. Cleanup is durable even when the
    read-back assertion fails, so installation never leaves a smoke row.
    """
    # A post-install CLI is deliberately not a server process. Make its narrow
    # canonical write authority visible and scoped instead of teaching every
    # arbitrary Python process that importing lib.database grants write power.
    with maintenance_write_authority('post-install database round-trip'):
        init_db()
        key = f'_install_smoke_{uuid.uuid4().hex}'
        try:
            with pooled_write_transaction(
                    DOMAIN_SYSTEM, label='post-install database smoke write') as db:
                db.execute(
                    'INSERT INTO schema_meta(key, value) VALUES (?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, 'ok'),
                )
            with pooled_db(DOMAIN_SYSTEM) as db:
                row = db.execute(
                    'SELECT value FROM schema_meta WHERE key=?', (key,)).fetchone()
                value = row['value'] if row and hasattr(row, 'keys') else (
                    row[0] if row else None)
            if value != 'ok':
                raise RuntimeError('database smoke read-back mismatch')
            return _BACKEND
        finally:
            with pooled_db(DOMAIN_SYSTEM) as db:
                db_execute_with_retry(
                    db, 'DELETE FROM schema_meta WHERE key=?', (key,))


__all__ = ['verify_database_roundtrip']
