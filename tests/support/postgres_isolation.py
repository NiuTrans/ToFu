"""Private PostgreSQL schemas for explicit external-backend contract tests.

Creates and removes only a fresh UUID-named test schema. Existing database
objects are untouched; every sidecar connection receives the same private
search_path through the test-only DSN file.
"""
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


@contextmanager
def isolated_postgres_dsn(source_file: Path, directory: Path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    if not source_file.is_absolute() or not 0 < source_file.stat().st_size <= 16384:
        raise ValueError('PostgreSQL contract requires a bounded absolute DSN file')
    dsn = source_file.read_text().strip()
    schema = 'tofu_contract_' + uuid4().hex
    target = directory / 'isolated-postgres-dsn'
    created = False
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SET lock_timeout='2s'")
        connection.execute("SET statement_timeout='10s'")
        try:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            created = True
            options = conninfo_to_dict(dsn).get('options', '') + f' -c search_path={schema}'
            with target.open('x') as output:
                target.chmod(0o600)
                output.write(make_conninfo(dsn, options=options))
            yield target
        finally:
            target.unlink(missing_ok=True)
            if created:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
