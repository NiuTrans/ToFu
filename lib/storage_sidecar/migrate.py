"""One-shot external PostgreSQL schema migration authority.

Entry point: ``python -m lib.storage_sidecar.migrate``.  It is intended for a
Kubernetes Job (or an equivalent maintenance process), never application
startup.  The deployment secret contract is validated before connecting and
the advisory lock serializes concurrent Job retries.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import psycopg
from psycopg.rows import DictRow, dict_row

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.postgres import PostgresSession
from lib.storage_sidecar.schema import (
    OBSOLETE_DEFERRED_INDEX_NAMES,
    deferred_index_statements,
    initialize_schema,
    validate_schema_version,
)
from runtime_guards import load_deployment_configuration


logger = get_logger('tofu.storage.sidecar.migration')


def migrate_external_postgres(postgres_dsn: str) -> int:
    """Apply forward migrations under one transaction-scoped advisory lock."""
    if not postgres_dsn:
        raise RuntimeError('external PostgreSQL DSN is missing')
    connection = cast(psycopg.Connection[DictRow], psycopg.connect(
        postgres_dsn,
        autocommit=False,
        row_factory=cast(Any, dict_row),
        application_name='tofu-storage-migration',
        connect_timeout=10,
    ))
    try:
        session = PostgresSession(connection)
        session.fetch_one(
            'SELECT pg_advisory_xact_lock(hashtext(?), hashtext(?)) AS locked',
            ('tofu.storage', 'schema-migration'),
        )
        initialize_schema(session)
        # Potentially large performance indexes belong in this explicit
        # migration window, never in application-pod startup.
        for statement in deferred_index_statements('postgres'):
            session.execute(statement)
        for index_name in sorted(OBSOLETE_DEFERRED_INDEX_NAMES):
            session.execute(f'DROP INDEX IF EXISTS {index_name}')
        connection.commit()
        version = validate_schema_version(session)
        connection.rollback()
        return version
    except BaseException:
        try:
            connection.rollback()
        except psycopg.Error:
            pass
        raise
    finally:
        connection.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    try:
        deployment = load_deployment_configuration()
        if deployment.mode != 'distributed':
            raise RuntimeError(
                'PostgreSQL migration requires TOFU_DEPLOYMENT_MODE=distributed')
        version = migrate_external_postgres(deployment.postgres_dsn)
    except StorageError as exc:
        logger.error('schema migration refused code=%s', exc.code)
        return 2
    except (RuntimeError, psycopg.Error) as exc:
        # Do not print the driver message: it may contain connection metadata.
        logger.error('schema migration failed type=%s', type(exc).__name__)
        return 2
    print(json.dumps({
        'ok': True,
        'backend': 'postgres',
        'schemaVersion': version,
    }, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = ['main', 'migrate_external_postgres']
