"""External PostgreSQL adapter with isolated Psycopg 3 read/write pools.

The adapter owns connections and transactions only.  Cluster lifecycle,
credentials, TLS, backups, and high availability belong to the deployment
platform; application startup validates but never migrates the schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import time
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.storage_sidecar.adapters.base import Backend, Operation, receipt_cacheable
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.preflight import run_filesystem_preflight
from lib.storage_sidecar.receipt_codec import (
    COMMAND_RECEIPT_LOOKUP_SQL,
    command_receipt_identity_v2,
    decode_command_receipt_lookup,
    encode_receipt_response,
)
from lib.storage_sidecar.schema import (
    OBSOLETE_DEFERRED_INDEX_NAMES,
    deferred_index_statements,
    initialize_schema,
    validate_schema_version,
)
from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache


logger = get_logger('tofu.storage.sidecar.postgres')


class PostgresSession:
    backend = 'postgres'

    def __init__(
        self,
        connection: psycopg.Connection[DictRow],
        turn_projection_cache: TurnProjectionCache | None = None,
    ) -> None:
        self.connection = connection
        self.turn_projection_cache = turn_projection_cache

    def lock_key(self, namespace: str, key: str) -> None:
        # Two-key advisory locks serialize one semantic bucket without
        # coupling the operation catalog to PostgreSQL connection objects.
        self.fetch_one(
            'SELECT pg_advisory_xact_lock(hashtext(?), hashtext(?)) AS locked',
            (namespace, key),
        )

    def index_exists(self, index_name: str) -> bool:
        row = self.fetch_one(
            'SELECT to_regclass(?) AS registered_name',
            (str(index_name),),
        )
        return bool(row and row.get('registered_name'))

    @staticmethod
    def _sql(value: str) -> str:
        # Catalog SQL is authored internally with the SQLite-compatible
        # placeholder.  Plugins cannot submit text, so this replacement never
        # processes user-controlled SQL or string literals containing '?'.
        # psycopg's paramstyle also treats a literal percent as formatting;
        # double it before introducing its ``%s`` placeholders.
        return value.replace('%', '%%').replace('?', '%s')

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(self._sql(sql), params)
            return max(0, int(cursor.rowcount))

    def execute_many_exact(
        self, sql: str, params: Sequence[tuple[Any, ...]],
    ) -> int:
        """Pipeline a bounded DML batch that must match every input row."""
        if not params:
            return 0
        with self.connection.cursor() as cursor:
            cursor.executemany(self._sql(sql), params)
            affected = max(0, int(cursor.rowcount))
        if affected != len(params):
            raise StorageError(
                'database_conflict',
                'Bulk mutation did not affect every expected row',
            )
        return affected

    def fetch_one(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(self._sql(sql), params)
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def fetch_all(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> list[Mapping[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(self._sql(sql), params)
            return [dict(row) for row in cursor.fetchall()]

    def fetch_one_for_update_skip_locked(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None:
        """Lock one available queue row without blocking sibling workers."""
        statement = f'{sql.rstrip()} FOR UPDATE SKIP LOCKED'
        return self.fetch_one(statement, params)


class _PgPool:
    """Small deadline-aware facade over Psycopg's supported pool."""

    def __init__(
        self, dsn: str, size: int, config: SidecarConfig, *, name: str,
    ) -> None:
        self.dsn = dsn
        self.size = size
        self.config = config
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=size,
            max_size=size,
            name=name,
            open=False,
            timeout=config.acquire_timeout_s,
            max_idle=config.idle_lifetime_s,
            max_lifetime=config.max_lifetime_s,
            reconnect_timeout=max(5.0, config.acquire_timeout_s),
            kwargs={
                'autocommit': False,
                'row_factory': dict_row,
                'application_name': 'tofu-storage-sidecar',
            },
            configure=self._configure,
            check=ConnectionPool.check_connection,
        )
        try:
            self.pool.open(
                wait=True, timeout=max(5.0, config.acquire_timeout_s))
        except PoolTimeout as exc:
            self.pool.close()
            raise StorageError(
                'database_unavailable',
                'PostgreSQL connection pool could not become ready',
                True,
                100,
            ) from exc

    @staticmethod
    def _configure(connection: psycopg.Connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute('SET SESSION idle_in_transaction_session_timeout = 5000')
        connection.commit()

    def acquire(self, deadline_at: float) -> psycopg.Connection:
        timeout = max(0.0, min(
            self.config.acquire_timeout_s, deadline_at - time.monotonic()))
        try:
            return self.pool.getconn(timeout=timeout)
        except PoolTimeout as exc:
            raise StorageError(
                'database_timeout', 'PostgreSQL pool acquisition timed out', True, 25,
            ) from exc

    def release(
        self, connection: psycopg.Connection, *, broken: bool = False,
    ) -> None:
        if broken:
            connection.close()
        self.pool.putconn(connection)

    def metrics(self) -> dict[str, int]:
        return dict(self.pool.get_stats())

    def close(self) -> None:
        self.pool.close()


def _map_postgres_error(exc: BaseException) -> StorageError:
    if isinstance(exc, StorageError):
        return exc
    if not isinstance(exc, psycopg.Error):
        return StorageError('database_internal', 'Storage operation failed')
    state = exc.sqlstate or ''
    if state in {'40001', '40P01', '55P03'}:
        return StorageError('database_busy', 'PostgreSQL transaction is busy', True, 25)
    if state == '57014':
        return StorageError('database_timeout', 'PostgreSQL transaction timed out', True, 25)
    if state.startswith('08') or isinstance(
            exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return StorageError('database_unavailable', 'PostgreSQL is unavailable', True, 100)
    if state in {'23505', '23P01'}:
        return StorageError('database_conflict', 'PostgreSQL uniqueness conflict')
    if state.startswith('23') or isinstance(exc, pg_errors.DataException):
        return StorageError('database_integrity', 'PostgreSQL integrity constraint failed')
    return StorageError('database_internal', 'PostgreSQL operation failed')


class PostgresBackend(Backend):
    name = 'postgres'

    def __init__(self, config: SidecarConfig) -> None:
        self.config = config
        self._read_pool: _PgPool | None = None
        self._write_pool: _PgPool | None = None
        self._closed = False
        self._preflight: dict[str, Any] = {}
        self._metrics = {'queries': 0, 'commands': 0, 'retries': 0, 'failures': 0}
        self._turn_projection_cache = TurnProjectionCache(
            config.turn_projection_cache_mib * 1024 * 1024)
        self._turn_search_projection: Any = None
        self._turn_search_projection_error = ''

    def _transaction(
        self,
        pool: _PgPool,
        operation: Operation,
        deadline_at: float,
        *,
        readonly: bool,
        retries: int,
        transaction_timeout_s: float | None = None,
    ) -> Any:
        if transaction_timeout_s is not None and not (
            0.05 <= float(transaction_timeout_s) <= 300.0
        ):
            raise StorageError(
                'database_protocol_error',
                'Invalid storage transaction timeout override',
            )
        attempt = 0
        while True:
            connection = pool.acquire(deadline_at)
            broken = False
            retrying = False
            try:
                remaining_ms = max(1, int(min(
                    (
                        self.config.transaction_timeout_s
                        if transaction_timeout_s is None
                        else float(transaction_timeout_s)
                    ),
                    deadline_at - time.monotonic(),
                ) * 1000))
                with connection.cursor() as cursor:
                    # Psycopg opens a transaction before the first statement
                    # when autocommit is disabled. Sending BEGIN explicitly
                    # therefore emits PostgreSQL's "already a transaction"
                    # warning on every RPC. Read transactions only need their
                    # access mode declared before catalog work begins.
                    if readonly:
                        cursor.execute('SET TRANSACTION READ ONLY')
                    # PostgreSQL utility SET syntax does not accept bind
                    # parameters (PG18 rejects ``SET ... = $1``). set_config
                    # is parameterizable and ``is_local=true`` keeps both
                    # limits scoped to this transaction.
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(remaining_ms),),
                    )
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, true)",
                        (str(min(2000, remaining_ms)),),
                    )
                result = operation(PostgresSession(
                    connection, self._turn_projection_cache))
                if readonly:
                    connection.rollback()
                else:
                    connection.commit()
                return result
            except BaseException as exc:
                if not isinstance(exc, (StorageError, psycopg.Error)):
                    # Never log SQL or parameters here.  The exception class is
                    # enough to diagnose catalog bugs without exposing data.
                    logger.error(
                        'unclassified PostgreSQL semantic failure type=%s',
                        type(exc).__name__)
                try:
                    connection.rollback()
                except psycopg.Error as rollback_error:
                    broken = True
                    logger.debug('PostgreSQL rollback failed: %s',
                                 type(rollback_error).__name__)
                mapped = _map_postgres_error(exc)
                if (mapped.retryable and attempt < retries
                        and time.monotonic() + 0.02 < deadline_at):
                    attempt += 1
                    self._metrics['retries'] += 1
                    retrying = True
                else:
                    self._metrics['failures'] += 1
                    raise mapped from exc
            finally:
                try:
                    pool.release(connection, broken=broken)
                except (psycopg.Error, RuntimeError) as release_error:
                    logger.warning(
                        'PostgreSQL pool slot replacement failed: %s',
                        type(release_error).__name__)
            if retrying:
                time.sleep(0.01 * (2 ** attempt))
                continue

    def start(self) -> dict[str, Any]:
        report = run_filesystem_preflight(self.config.data_dir)
        if not self.config.postgres_dsn:
            raise StorageError(
                'database_unavailable', 'External PostgreSQL DSN is missing')
        try:
            probe = psycopg.connect(
                self.config.postgres_dsn,
                autocommit=False,
                row_factory=dict_row,
                application_name='tofu-storage-sidecar-startup',
                connect_timeout=5,
            )
        except psycopg.Error as exc:
            raise _map_postgres_error(exc) from exc
        try:
            with probe.cursor() as cursor:
                cursor.execute(
                    'SELECT current_setting(\'fsync\') AS fsync, '
                    'current_setting(\'synchronous_commit\') AS synchronous_commit, '
                    'current_setting(\'full_page_writes\') AS full_page_writes, '
                    'current_setting(\'data_checksums\') AS data_checksums, '
                    'current_setting(\'max_connections\')::int AS max_connections, '
                    'pg_is_in_recovery() AS in_recovery')
                settings = dict(cursor.fetchone())
            if (settings['fsync'] != 'on'
                    or settings['synchronous_commit'] not in {'on', 'remote_apply'}
                    or settings['full_page_writes'] != 'on'
                    or settings['in_recovery']):
                raise StorageError(
                    'database_unavailable', 'PostgreSQL durability preflight failed')
            budget = max(2, int(settings['max_connections'] * 0.8) - 2)
            requested = self.config.read_pool_size + self.config.write_pool_size
            if requested > budget:
                write_size = max(1, min(
                    self.config.write_pool_size,
                    round(budget * self.config.write_pool_size / requested),
                ))
                read_size = max(1, budget - write_size)
            else:
                read_size = self.config.read_pool_size
                write_size = self.config.write_pool_size
            startup_session = PostgresSession(probe)
            if self.config.allow_schema_migration:
                # This authority exists only for isolated repository contract
                # tests. Production migration uses the one-shot migration job.
                initialize_schema(startup_session)
                for statement in deferred_index_statements('postgres'):
                    startup_session.execute(statement)
                for index_name in sorted(OBSOLETE_DEFERRED_INDEX_NAMES):
                    startup_session.execute(
                        f'DROP INDEX IF EXISTS {index_name}')
                probe.commit()
            else:
                validate_schema_version(startup_session)
                probe.rollback()
        except BaseException as exc:
            try:
                probe.rollback()
            except psycopg.Error:
                pass
            if isinstance(exc, StorageError):
                raise
            if isinstance(exc, psycopg.Error):
                raise _map_postgres_error(exc) from exc
            raise
        finally:
            probe.close()
        try:
            self._read_pool = _PgPool(
                self.config.postgres_dsn,
                read_size,
                self.config,
                name='tofu-storage-read',
            )
            self._write_pool = _PgPool(
                self.config.postgres_dsn,
                write_size,
                self.config,
                name='tofu-storage-write',
            )
        except BaseException:
            if self._read_pool:
                self._read_pool.close()
            raise
        self._preflight = {
            **report.as_dict(),
            'durability': {
                key: settings[key] for key in (
                    'fsync', 'synchronous_commit', 'full_page_writes', 'data_checksums')
            },
            'read_pool_capacity': read_size,
            'write_pool_capacity': write_size,
            'connection_budget_80pct': budget,
            'schema_migration_authority': (
                'test-only' if self.config.allow_schema_migration else 'external-job'),
        }
        try:
            from lib.storage_sidecar.turn_search_projection import (
                AuthorityTurnSearchTarget,
                TurnSearchProjectionRuntime,
            )

            projection = TurnSearchProjectionRuntime(
                self,
                AuthorityTurnSearchTarget(self),
                backfill_delay_s=self.config.turn_search_backfill_delay_s,
            )
            projection.start()
            self._turn_search_projection = projection
        except BaseException as exc:
            self._turn_search_projection_error = type(exc).__name__
            logger.exception(
                '[turn-search] PostgreSQL projection failed to start; '
                'conversation search is degraded')
        return self.health()

    def query(
        self, operation_name: str, operation: Operation, deadline_at: float,
    ) -> Any:
        if self._read_pool is None:
            raise StorageError('database_unavailable', 'PostgreSQL read pool is not ready')
        result = self._transaction(
            self._read_pool, operation, deadline_at, readonly=True, retries=1)
        self._metrics['queries'] += 1
        return result

    def command(
        self,
        operation_name: str,
        payload_digest: str,
        command_id: str | None,
        priority: str,
        operation: Operation,
        deadline_at: float,
        *,
        receipt_required: bool,
        transaction_timeout_s: float | None = None,
    ) -> Any:
        del priority  # PostgreSQL uses its isolated write pool, not SQLite lanes.
        if receipt_required and (
                not isinstance(command_id, str)
                or not command_id
                or len(command_id) > 200):
            raise StorageError(
                'database_protocol_error', 'A valid command_id is required')
        receipt_identity = (
            command_receipt_identity_v2(
                command_id, operation_name, payload_digest)
            if receipt_required else None
        )
        if self._write_pool is None:
            raise StorageError('database_unavailable', 'PostgreSQL write pool is not ready')

        def transactional(session: PostgresSession) -> Any:
            if receipt_required:
                # Serialize identical command IDs before the receipt probe.
                # This closes the concurrent-first-delivery race where two
                # transactions both observe "no receipt" and one later loses
                # the receipt PK insert after repeating the business mutation.
                session.fetch_one(
                    'SELECT pg_advisory_xact_lock(hashtext(?)) AS locked',
                    (command_id,),
                )
                assert receipt_identity is not None
                command_key, request_digest = receipt_identity
                found, replay = decode_command_receipt_lookup(
                    session.fetch_all(
                        COMMAND_RECEIPT_LOOKUP_SQL,
                        (
                            operation_name, payload_digest, command_id,
                            operation_name, request_digest, command_key,
                        ),
                    )
                )
                if found:
                    return replay
            response = operation(session)
            # Clean refusals (ok=False) mutate nothing — memoizing them as
            # receipts would freeze a stale verdict (see base.receipt_cacheable).
            if receipt_required and receipt_cacheable(response):
                assert receipt_identity is not None
                command_key, request_digest = receipt_identity
                encoded = encode_receipt_response(response)
                session.execute(
                    'INSERT INTO storage_command_receipts_v2('
                    'command_key, operation, request_digest, response_json, '
                    'committed_at_ms) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (command_key, operation_name, request_digest, encoded,
                     int(time.time() * 1000)),
                )
            return response

        result = self._transaction(
            self._write_pool,
            transactional,
            deadline_at,
            readonly=False,
            retries=3,
            transaction_timeout_s=transaction_timeout_s,
        )
        self._metrics['commands'] += 1
        if self._turn_search_projection is not None:
            self._turn_search_projection.wake()
        return result

    def health(self) -> dict[str, Any]:
        result = {
            'ready': not self._closed and self._read_pool is not None
                     and self._write_pool is not None,
            'backend': self.name,
            'protocol': 'storage.v1',
            'preflight': self._preflight,
        }
        if self._turn_search_projection is not None:
            result['turn_search_projection'] = (
                self._turn_search_projection.status())
        elif self._turn_search_projection_error:
            result['turn_search_projection'] = {
                'state': 'unavailable',
                'error_type': self._turn_search_projection_error,
            }
        return result

    def metrics(self) -> dict[str, Any]:
        read_pool = self._read_pool.metrics() if self._read_pool else {}
        write_pool = self._write_pool.metrics() if self._write_pool else {}
        return {
            'backend': self.name,
            **self._metrics,
            'read_pool_available': int(read_pool.get('pool_available', 0)),
            'write_pool_available': int(write_pool.get('pool_available', 0)),
            'read_pool_rotations': max(
                0,
                int(read_pool.get('connections_num', 0))
                - int(read_pool.get('pool_size', 0)),
            ),
            'write_pool_rotations': max(
                0,
                int(write_pool.get('connections_num', 0))
                - int(write_pool.get('pool_size', 0)),
            ),
            'read_pool': read_pool,
            'write_pool': write_pool,
            'turn_projection_cache': self._turn_projection_cache.stats(),
            'turn_search_projection': (
                self._turn_search_projection.status()
                if self._turn_search_projection is not None else {
                    'state': 'unavailable',
                    'error_type': self._turn_search_projection_error,
                }
            ),
        }

    def integrity_check(self, deadline_at: float) -> dict[str, Any]:
        def check(session: PostgresSession) -> dict[str, Any]:
            row = session.fetch_one(
                "SELECT NOT pg_is_in_recovery() AS ok, "
                "current_setting('data_checksums') AS checksums")
            return {'ok': bool(row['ok']), 'checksums': row['checksums']}

        return self.query('system.integrity_check', check, deadline_at)

    def backup(self, deadline_at: float) -> dict[str, Any]:
        del deadline_at
        raise StorageError(
            'database_protocol_error',
            'External PostgreSQL backups are platform-managed',
        )

    def baseline(self, deadline_at: float) -> dict[str, Any]:
        def collect(session: PostgresSession):
            rows = session.fetch_all(
                "SELECT tablename AS name FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename")
            tables = []
            for row in rows:
                if time.monotonic() >= deadline_at:
                    raise StorageError(
                        'database_timeout', 'Storage baseline deadline expired',
                        True, 100)
                identifier = str(row['name']).replace('"', '""')
                count = session.fetch_one(
                    f'SELECT COUNT(*) AS count FROM "{identifier}"')
                tables.append({'name': row['name'], 'rows': int(count['count'])})
            indexes = session.fetch_all(
                "SELECT indexname AS name FROM pg_indexes "
                "WHERE schemaname = 'public' ORDER BY indexname")
            version = session.fetch_one(
                'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                ('schema_version',))
            return {
                'backend': self.name,
                'schema_version': int(version['meta_value']) if version else None,
                'tables': tables,
                'indexes': [row['name'] for row in indexes],
            }

        return self.query('system.baseline', collect, deadline_at)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._turn_search_projection is not None:
            self._turn_search_projection.close()
            self._turn_search_projection = None
        self._turn_projection_cache.clear()
        if self._read_pool:
            self._read_pool.close()
            self._read_pool = None
        if self._write_pool:
            self._write_pool.close()
            self._write_pool = None


__all__ = ['PostgresBackend', 'PostgresSession']
