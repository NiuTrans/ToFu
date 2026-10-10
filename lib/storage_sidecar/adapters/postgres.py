"""External PostgreSQL adapter with isolated Psycopg 3 read/write pools.

The adapter owns connections and transactions only.  Cluster lifecycle,
credentials, TLS, backups, and high availability belong to the deployment
platform; application startup validates but never migrates the schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import math
import threading
import time
import uuid
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import DictRow, dict_row
from psycopg.adapt import Dumper
from psycopg.types.json import set_json_loads

from lib.storage.errors import StorageError
from lib.storage.generated_worker_job_contract import WorkerLeaseAuthoritySnapshot
from lib.log import get_logger
from lib.storage_sidecar.adapters.base import Backend, Operation, receipt_cacheable
from lib.storage_sidecar.adapters.connection_pool import DemandConnectionPool
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.json_document import DecodedJsonString, JsonBytes
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


def _load_json_document(data: bytes) -> Any:
    value = json.loads(data)
    return DecodedJsonString(value) if isinstance(value, str) else value


class PostgresSession:
    backend = 'postgres'

    def __init__(
        self,
        connection: psycopg.Connection[DictRow],
        turn_projection_cache: TurnProjectionCache | None = None,
        event_read_epoch: str = '',
        worker_lease_clock=None,
    ) -> None:
        self.event_read_epoch = event_read_epoch
        self.worker_lease_clock = worker_lease_clock
        self.connection = connection
        self.turn_projection_cache = turn_projection_cache

    def worker_lease_authority(self) -> WorkerLeaseAuthoritySnapshot:
        """Read the database-server clock; local clocks are an explicit test seam.

        No fallback to Sidecar UTC/monotonic time is safe across replicas. The
        server's named state survives connection turnover and expires on restart.
        """
        from lib.storage.generated_worker_job_contract import (
            WORKER_SERVER_CLOCK_FORMAT, WORKER_SERVER_CLOCK_FIELDS, WORKER_SERVER_CLOCK_MAX_STATE_BYTES,
            WORKER_LEASE_EPOCH_CHARACTERS,
        )
        import hashlib

        if self.worker_lease_clock is not None:
            return WorkerLeaseAuthoritySnapshot(self._injected_worker_lease_epoch(),
                *self.worker_lease_clock.snapshot())
        try:
            row = self.fetch_one(
                'SELECT public.tofu_worker_lease_clock_v1() AS clock, '
                'oid AS database_oid FROM pg_database WHERE datname=current_database()')
        except psycopg.errors.UndefinedFunction as exc:
            raise StorageError('database_unavailable',
                'PostgreSQL worker leases require the shared server clock module') from exc
        value = (row or {}).get('clock')
        if (not isinstance(value, dict) or set(value) != WORKER_SERVER_CLOCK_FIELDS
                or value['format'] != WORKER_SERVER_CLOCK_FORMAT
                or not isinstance(value['epoch'], str)
                or len(value['epoch']) != WORKER_LEASE_EPOCH_CHARACTERS
                or any(byte not in '0123456789abcdef' for byte in value['epoch'])
                or type(value['nowMs']) is not int
                or type(value['utcMs']) is not int
                or type(value['stateBytes']) is not int
                or not 0 < value['stateBytes'] <= WORKER_SERVER_CLOCK_MAX_STATE_BYTES
                or type(row.get('database_oid')) is not int or row['database_oid'] <= 0):
            raise StorageError('database_internal', 'Invalid shared server worker lease clock')
        material = f"{WORKER_SERVER_CLOCK_FORMAT}\0{row['database_oid']}\0{value['epoch']}"
        epoch = hashlib.sha256(material.encode('utf-8')).hexdigest()[:WORKER_LEASE_EPOCH_CHARACTERS]
        return WorkerLeaseAuthoritySnapshot(epoch, value['nowMs'], value['utcMs'])

    def _injected_worker_lease_epoch(self) -> str:
        """Bind leases to the shared database server, never to one Sidecar.

        Fresh sessions follow a restarted server even when an adapter pool
        reconnects. Only engine-owned metadata enters this private identity.
        """
        from datetime import datetime, timezone
        import hashlib

        row = self.fetch_one(
            'SELECT pg_postmaster_start_time() AS started, '
            'oid AS database_oid FROM pg_database WHERE datname=current_database()')
        if (row is None or not isinstance(row.get('started'), datetime)
                or row['started'].tzinfo is None
                or type(row.get('database_oid')) is not int or row['database_oid'] <= 0):
            raise StorageError('database_internal', 'Worker database lease epoch is unavailable')
        started = row['started'].astimezone(timezone.utc).isoformat(timespec='microseconds')
        material = f"tofu.worker-lease.postgres/v1\0{row['database_oid']}\0{started}"
        return hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]

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

    def fetch_one_for_update(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None:
        """Serialize a specific row's read/decision/write in its transaction."""
        return self.fetch_one(f'{sql.rstrip()} FOR UPDATE', params)

    def fetch_one_for_update_skip_locked(
        self, sql: str, params: tuple[Any, ...] = (),
    ) -> Mapping[str, Any] | None:
        """Lock one available queue row without blocking sibling workers."""
        statement = f'{sql.rstrip()} FOR UPDATE SKIP LOCKED'
        return self.fetch_one(statement, params)


class _PgPool:
    """Psycopg connection factory over shared demand lease ownership."""

    def __init__(
        self, dsn: str, size: int, config: SidecarConfig, *, name: str,
    ) -> None:
        self.dsn = dsn
        self.size = size
        self.config = config
        self.name = name
        self._leases = {}
        self.pool = DemandConnectionPool(self._connect, idle_reuse_hint=size,
            max_lifetime_s=config.max_lifetime_s, idle_lifetime_s=config.idle_lifetime_s,
            classify_error=lambda error, deadline_at: _map_postgres_error(error),
            driver_errors=(psycopg.Error,), reset=lambda connection: connection.rollback())

    def _connect(self, deadline_at):
        connection = psycopg.connect(self.dsn, autocommit=False, row_factory=dict_row,
            application_name=self.name,
            connect_timeout=(max(1, math.ceil(deadline_at - time.monotonic()))
                             if math.isfinite(deadline_at) else 0))
        try:
            self._configure(connection)
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _configure(connection: psycopg.Connection) -> None:
        # JSONDOC columns are jsonb here but BLOB-affinity on SQLite, so the
        # shared codecs hand the session bytes.  psycopg binds plain bytes as
        # bytea, which has no assignment cast to jsonb (SQLSTATE 42804 on
        # every document write); binding the JsonBytes marker with the jsonb
        # OID makes the server run jsonb_in instead.  Plain bytes keep the
        # bytea binding for genuinely binary BLOB columns.
        connection.adapters.register_dumper(JsonBytes, _JsonBytesDumper)
        # Mark decoded strings so they cannot be mistaken for SQLite JSON
        # text and parsed again. Preserve structured JSONB results: returning
        # normalized server text as original bytes would invalidate persisted
        # projection byte evidence.
        set_json_loads(_load_json_document, connection)
        with connection.cursor() as cursor:
            cursor.execute('SET SESSION idle_in_transaction_session_timeout = 5000')
        connection.commit()

    def acquire(self, deadline_at: float) -> psycopg.Connection:
        slot = self.pool.acquire(deadline_at)
        with self.pool.condition:
            self._leases[id(slot.connection)] = slot
        return slot.connection

    def release(self, connection: psycopg.Connection, *, broken=False) -> None:
        with self.pool.condition:
            slot = self._leases.pop(id(connection), None)
            if slot is None or slot.connection is not connection:
                raise RuntimeError('PostgreSQL connection lease is not owned')
        self.pool.release(slot, retire=broken)

    def metrics(self) -> dict[str, int]:
        snapshot = self.pool.snapshot()
        return {'pool_available': snapshot['idle'],
                'pool_size': snapshot['idle'] + snapshot['active'],
                'pool_min': 0, 'pool_max': 0,
                'idle_reuse_hint': snapshot['idle_reuse_hint'],
                'requests_waiting': 0, 'connections_num': snapshot['connections_created'],
                'active_leases': snapshot['active'], 'peak_leases': snapshot['peak'],
                'rotations': snapshot['rotations']}

    def begin_close(self) -> None:
        self.pool.begin_close()

    def close(self) -> None:
        self.pool.close()


class _JsonBytesDumper(Dumper):
    """Bind the JsonBytes marker as a jsonb-typed text parameter."""

    oid = 3802

    def dump(self, obj: bytes) -> bytes:
        return bytes(obj)


def _map_postgres_error(exc: BaseException) -> StorageError:
    if isinstance(exc, StorageError):
        return exc
    if not isinstance(exc, psycopg.Error):
        return StorageError('database_internal', 'Storage operation failed')
    state = exc.sqlstate or ''
    if state in {'40001', '40P01', '55P03'}:
        return StorageError('database_busy', 'PostgreSQL transaction is busy', True, 25)
    if state in {'57014', '25P03'}:
        # 57014 (statement_timeout) and 25P03
        # (idle_in_transaction_session_timeout) are server-enforced
        # transaction-budget expiries: the transaction was rolled back, so a
        # bounded retry is safe and the budget owner is the transaction
        # deadline — same class as SQLite's watchdog-expired timeout.
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
        self._event_read_epoch = uuid.uuid4().hex
        # Production leases read one server-shared clock, never a local clock.
        self._worker_lease_clock = None
        self._read_pool: _PgPool | None = None
        self._write_pool: _PgPool | None = None
        self._closed = False
        self._close_complete = False
        self._close_lock = threading.RLock()
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
        replay_safe: bool = False,
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
            phase = 'begin'
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
                    # The session-level 5 s idle-in-transaction default (set
                    # at pool configure) contradicts declared transaction
                    # budgets of up to 300 s: an operation doing seconds of
                    # non-SQL work between statements was killed mid-
                    # transaction. Derive the per-transaction bound from the
                    # same declared budget as statement_timeout; the session
                    # default remains for connections outside one.
                    cursor.execute(
                        "SELECT set_config("
                        "'idle_in_transaction_session_timeout', %s, true)",
                        (str(remaining_ms),),
                    )
                phase = 'execute'
                result = operation(PostgresSession(
                    connection, self._turn_projection_cache, self._event_read_epoch, self._worker_lease_clock))
                if readonly:
                    phase = 'rollback'
                    connection.rollback()
                else:
                    phase = 'commit'
                    connection.commit()
                phase = 'post_commit'
                return result
            except BaseException as exc:
                if isinstance(exc, psycopg.Error):
                    # Never log SQL or parameters here.  The driver class and
                    # SQLSTATE diagnose catalog/translation bugs without
                    # exposing row data; without this the mapped StorageError
                    # is the only trace and fresh-cluster failures are
                    # undiagnosable.
                    logger.error(
                        'PostgreSQL failure type=%s sqlstate=%s phase=%s',
                        type(exc).__name__, exc.sqlstate, phase)
                elif not isinstance(exc, StorageError):
                    # Never log SQL or parameters here.  The exception class is
                    # enough to diagnose catalog bugs without exposing data.
                    logger.error(
                        'unclassified PostgreSQL semantic failure type=%s',
                        type(exc).__name__)
                try:
                    connection.rollback()
                except psycopg.Error:
                    broken = True
                    logger.warning(
                        'PostgreSQL rollback failed; discarding the '
                        'connection', exc_info=True)
                mapped = _map_postgres_error(exc)
                # A COMMIT-phase failure is a lost ack: the transaction may
                # have landed. Re-executing is only safe when the command
                # carries a durable receipt (advisory lock + receipt probe
                # deduplicate the replay); anything else must surface the
                # ambiguous outcome without re-executing the operation.
                if (mapped.retryable and attempt < retries
                        and (phase != 'commit' or replay_safe)
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
                except (psycopg.Error, StorageError, RuntimeError) as release_error:
                    logger.warning(
                        'PostgreSQL connection retirement failed: %s',
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
            'read_pool_capacity': 0,
            'write_pool_capacity': 0,
            'read_idle_reuse_hint': read_size,
            'write_idle_reuse_hint': write_size,
            'server_max_connections': settings['max_connections'],
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
        if operation_name == 'conversation.search' and self._turn_search_projection is None:
            raise StorageError('database_unavailable', 'Conversation search projection is unavailable', True, 250)
        result = self._transaction(
            self._read_pool, operation, deadline_at, readonly=True, retries=1)
        self._metrics['queries'] += 1
        if operation_name == 'conversation.search':
            from lib.storage_sidecar.turn_search_query import attach_search_coverage
            result = attach_search_coverage(result, self._turn_search_projection.search_status())
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
            # Only a receipt-backed command may re-execute after a COMMIT-phase
            # failure: the advisory lock plus receipt probe make that replay
            # exactly-once. Receipt-free commands (maintenance is forced
            # receipt-free by the server) surface the ambiguous outcome.
            replay_safe=receipt_required,
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
        with self._close_lock:
            self._close_owned_resources()

    def _close_owned_resources(self) -> None:
        if self._close_complete:
            return
        self._closed = True
        for pool in (self._read_pool, self._write_pool):
            if pool is not None:
                pool.begin_close()
        if self._turn_search_projection is not None:
            self._turn_search_projection.close()
            self._turn_search_projection = None
        if self._read_pool:
            self._read_pool.close()
            self._read_pool = None
        if self._write_pool:
            self._write_pool.close()
            self._write_pool = None
        # Borrowers cannot repopulate a cache after joined connection teardown.
        self._turn_projection_cache.clear()
        self._close_complete = True


__all__ = ['PostgresBackend', 'PostgresSession']
