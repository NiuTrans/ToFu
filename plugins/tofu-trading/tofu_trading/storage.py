"""Sidecar-backed repository and bounded SQL compatibility evaluator.

Responsibility: keep all persistence behind named ``storage.v1`` operations,
partition every owner record by explicit identity, and provide the temporary
DB-API-shaped seam used by the extracted trading domain. SQL is evaluated only
against a private, bounded, in-memory SQLite projection; no SQLite file,
backend connection, or arbitrary SQL crosses the sidecar boundary.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable, Iterator, Mapping, Sequence
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any
import uuid

from lib.log import get_logger
from lib.storage import StorageError, get_storage_client

from tofu_trading.identity import DEFAULT_OWNER_ID
from tofu_trading.storage_manifest import MANIFEST, MANIFEST_VERSION, NAMESPACE
from tofu_trading.storage_schema import TABLE_SPECS, TableSpec, initialize_query_schema


# The host intentionally stores third-party INFO logs at WARNING+ only.  This
# module owns durable migration/commit evidence, so use the documented core
# business namespace and keep those lifecycle records in logs/app.log.
logger = get_logger("lib.plugins.tofu_trading.storage")

DOMAIN_TRADING = "trading"
SHARED_OWNER_ID = 0
ROW_SCHEMA_VERSION = 1
MIGRATION_MARKER_KEY = "meta|legacy-v1"
MAX_ROWS_PER_CONNECTION = 50_000
MAX_BYTES_PER_CONNECTION = 64 * 1024 * 1024
MAX_BATCH_ROWS = 100
MAX_BATCH_BYTES = 1024 * 1024
MAX_ATOMIC_BATCH_BYTES = 8 * 1024 * 1024

_TABLE_PATTERN = re.compile(r"\btrading_[a-z_]+\b", re.IGNORECASE)
_MUTATION_PATTERN = re.compile(
    r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"(trading_[a-z_]+)\b",
    re.IGNORECASE | re.DOTALL,
)
_DDL_PATTERN = re.compile(r"^\s*(?:CREATE|ALTER)\b", re.IGNORECASE)

_prepare_lock = threading.RLock()
_prepared = False


class TradingStorageError(RuntimeError):
    """Raised when repository state violates the declared trading contract."""


class StorageRow(Mapping[str, Any]):
    """Immutable row supporting mapping, integer indexing, and ``get``."""

    __slots__ = ("_keys", "_values", "_positions")

    def __init__(self, keys: Sequence[str], values: Sequence[Any]):
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._positions = {key: index for index, key in enumerate(self._keys)}

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._positions[key]]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self) -> tuple[str, ...]:
        return self._keys


class RepositoryCursor:
    """Materializing cursor facade over the private SQLite evaluator."""

    def __init__(self, connection: "TradingConnection", cursor: sqlite3.Cursor):
        self._connection = connection
        self._cursor = cursor

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def _row(self, value: sqlite3.Row | None) -> StorageRow | None:
        if value is None:
            return None
        return StorageRow(value.keys(), tuple(value))

    def fetchone(self) -> StorageRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[StorageRow]:
        return [self._row(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size: int | None = None) -> list[StorageRow]:
        rows = self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        return [self._row(row) for row in rows]

    def __iter__(self) -> Iterator[StorageRow]:
        for row in self._cursor:
            converted = self._row(row)
            if converted is not None:
                yield converted

    def close(self) -> None:
        self._cursor.close()

    def __enter__(self) -> "RepositoryCursor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _owner_for_row(spec: TableSpec, row: Mapping[str, Any], fallback: int) -> int:
    if not spec.owner_scoped:
        return SHARED_OWNER_ID
    value = row.get("user_id", fallback)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TradingStorageError(f"{spec.name}: invalid owner_user_id")
    return value


def _encoded_primary_key(spec: TableSpec, row: Mapping[str, Any]) -> str:
    values = []
    for column in spec.primary_key:
        value = row.get(column)
        if value is None:
            raise TradingStorageError(f"{spec.name}: primary key {column} is null")
        values.append(value)
    return base64.urlsafe_b64encode(_canonical_json(values)).decode("ascii").rstrip("=")


def _row_prefix(table_name: str, owner_user_id: int | None = None) -> str:
    prefix = f"row|{table_name}|"
    if owner_user_id is not None:
        prefix += f"{owner_user_id:020d}|"
    return prefix


def _row_key(spec: TableSpec, row: Mapping[str, Any], owner_user_id: int) -> str:
    key = _row_prefix(spec.name, owner_user_id) + _encoded_primary_key(spec, row)
    if len(key) > 512:
        raise TradingStorageError(f"{spec.name}: encoded primary key is too long")
    return key


def _document(
    spec: TableSpec,
    row: Mapping[str, Any],
    owner_user_id: int,
    *,
    source: str,
) -> dict[str, Any]:
    clean_row = {column: row.get(column) for column in spec.columns}
    return {
        "key": _row_key(spec, clean_row, owner_user_id),
        "logical_table": spec.name,
        "owner_user_id": owner_user_id,
        "row": clean_row,
        "source": source,
        "schema_version": ROW_SCHEMA_VERSION,
    }


class TradingDocumentRepository:
    """Typed wrapper around tofu-trading's named manifest operations."""

    def __init__(self, *, client_factory=get_storage_client):
        self._client_factory = client_factory

    def register(self) -> dict[str, Any]:
        return self._client_factory(write=True).command(
            "plugin.register",
            {"manifest": MANIFEST},
            f"trade-manifest:{uuid.uuid4().hex}",
            priority="maintenance",
            deadline=30,
        )

    def get(self, key: str) -> dict[str, Any] | None:
        return self._client_factory().query(
            f"plugin.{NAMESPACE}.get_row", {"key": key}, deadline=15
        )

    def list_prefix(self, prefix: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        result_bytes = 0
        after_key = ""
        while True:
            payload: dict[str, Any] = {"key_prefix": prefix, "limit": 1000}
            if after_key:
                payload["after_key"] = after_key
            page = self._client_factory().query(
                f"plugin.{NAMESPACE}.list_rows", payload, deadline=30
            )
            if not isinstance(page, list):
                raise TradingStorageError("plugin list_rows returned an invalid page")
            result.extend(page)
            result_bytes += sum(len(_canonical_json(item)) for item in page)
            if (
                len(result) > MAX_ROWS_PER_CONNECTION
                or result_bytes > MAX_BYTES_PER_CONNECTION
            ):
                raise TradingStorageError(
                    "trading repository scan exceeds its projection budget"
                )
            if len(page) < 1000:
                break
            next_key = str(page[-1]["document"]["key"])
            if next_key <= after_key:
                raise TradingStorageError("plugin list_rows cursor did not advance")
            after_key = next_key
        return result

    def batch(self, mutations: list[dict[str, Any]]) -> dict[str, Any]:
        if not mutations:
            return {"results": []}
        return self._client_factory(write=True).command(
            f"plugin.{NAMESPACE}.mutate_rows",
            {"mutations": mutations},
            f"trade-batch:{uuid.uuid4().hex}",
            deadline=30,
        )

    def scan_legacy(self, table_name: str) -> tuple[bool, list[dict[str, Any]]]:
        if table_name not in TABLE_SPECS:
            raise TradingStorageError(f"undeclared legacy table: {table_name}")
        operation = table_name.removeprefix("trading_")
        rows: list[dict[str, Any]] = []
        rows_bytes = 0
        offset = 0
        exists = False
        while True:
            page = self._client_factory().query(
                f"plugin.{NAMESPACE}.scan_{operation}",
                {"offset": offset, "limit": 500},
                deadline=30,
            )
            if not isinstance(page, Mapping):
                raise TradingStorageError("legacy scan returned an invalid page")
            exists = bool(page.get("exists"))
            page_rows = page.get("rows") or []
            if not isinstance(page_rows, list):
                raise TradingStorageError("legacy scan returned an invalid page")
            rows.extend(page_rows)
            rows_bytes += sum(len(_canonical_json(row)) for row in page_rows)
            if (
                len(rows) > MAX_ROWS_PER_CONNECTION
                or rows_bytes > MAX_BYTES_PER_CONNECTION
            ):
                raise TradingStorageError(
                    f"{table_name}: legacy migration exceeds its projection budget"
                )
            next_offset = page.get("next_offset")
            if next_offset is None:
                break
            next_offset = int(next_offset)
            if next_offset <= offset:
                raise TradingStorageError("legacy scan cursor did not advance")
            offset = next_offset
        return exists, rows


def _bounded_mutation_batches(
    mutations: Iterable[dict[str, Any]],
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    size = 0
    for mutation in mutations:
        mutation_size = len(_canonical_json(mutation))
        if mutation_size > MAX_BATCH_BYTES:
            raise TradingStorageError("one trading row exceeds the mutation budget")
        if batch and (
            len(batch) >= MAX_BATCH_ROWS or size + mutation_size > MAX_BATCH_BYTES
        ):
            yield batch
            batch = []
            size = 0
        batch.append(mutation)
        size += mutation_size
    if batch:
        yield batch


def _rows_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    hashes = sorted(hashlib.sha256(_canonical_json(row)).digest() for row in rows)
    digest = hashlib.sha256()
    for value in hashes:
        digest.update(value)
    return digest.hexdigest()


def migrate_legacy_storage(
    repository: TradingDocumentRepository | None = None,
) -> dict[str, Any]:
    """Copy, verify, and mark every declared legacy table without deleting it."""
    repository = repository or TradingDocumentRepository()
    marker = repository.get(MIGRATION_MARKER_KEY)
    if marker is not None:
        return dict(marker["document"]["row"])

    logger.info(
        "[tofu-trading] starting verified legacy migration tables=%d",
        len(TABLE_SPECS),
    )
    started_at_ms = int(time.time() * 1000)
    table_results: dict[str, dict[str, Any]] = {}
    total_rows = 0
    for table_name, spec in TABLE_SPECS.items():
        exists, source_rows = repository.scan_legacy(table_name)
        source_documents: dict[str, dict[str, Any]] = {}
        for source_row in source_rows:
            owner_user_id = _owner_for_row(spec, source_row, DEFAULT_OWNER_ID)
            document = _document(
                spec, source_row, owner_user_id, source="legacy-sidecar-import"
            )
            source_documents[document["key"]] = document
        if len(source_documents) != len(source_rows):
            raise TradingStorageError(f"{table_name}: duplicate legacy primary key")

        current = {
            item["document"]["key"]: item["document"]
            for item in repository.list_prefix(_row_prefix(table_name))
        }
        pending = [
            {"action": "put", "document": document}
            for key, document in source_documents.items()
            if current.get(key, {}).get("row") != document["row"]
        ]
        for batch in _bounded_mutation_batches(pending):
            repository.batch(batch)

        verified_documents = [
            item["document"]
            for item in repository.list_prefix(_row_prefix(table_name))
        ]
        verified_rows = [document["row"] for document in verified_documents]
        source_digest = _rows_digest(source_rows)
        target_digest = _rows_digest(verified_rows)
        if len(verified_rows) != len(source_rows) or target_digest != source_digest:
            raise TradingStorageError(
                f"{table_name}: legacy migration verification failed "
                f"(source={len(source_rows)}, target={len(verified_rows)})"
            )
        table_results[table_name] = {
            "legacy_table_present": exists,
            "rows": len(source_rows),
            "sha256": source_digest,
        }
        total_rows += len(source_rows)
        logger.info(
            "[tofu-trading] migrated table=%s rows=%d changed=%d",
            table_name,
            len(source_rows),
            len(pending),
        )

    result = {
        "migration": "legacy-v1",
        "manifest_version": MANIFEST_VERSION,
        "started_at_ms": started_at_ms,
        "finished_at_ms": int(time.time() * 1000),
        "total_rows": total_rows,
        "tables": table_results,
    }
    try:
        repository.batch(
            [
                {
                    "action": "put",
                    "document": {
                        "key": MIGRATION_MARKER_KEY,
                        "logical_table": "_meta",
                        "owner_user_id": SHARED_OWNER_ID,
                        "row": result,
                        "source": "migration-verification",
                        "schema_version": ROW_SCHEMA_VERSION,
                    },
                    "expected_version": 0,
                }
            ]
        )
    except StorageError as exc:
        # Multiple host processes can boot against one sidecar. An identical,
        # fully verified migration is success; any other marker conflict stays
        # fatal so a partial/incompatible import cannot be hidden.
        if exc.code != "database_conflict":
            raise
        concurrent = repository.get(MIGRATION_MARKER_KEY)
        if concurrent is None:
            raise
        concurrent_result = dict(concurrent["document"]["row"])
        if (
            concurrent_result.get("manifest_version") != MANIFEST_VERSION
            or concurrent_result.get("total_rows") != total_rows
            or concurrent_result.get("tables") != table_results
        ):
            raise TradingStorageError("concurrent legacy migration disagreed") from exc
        result = concurrent_result
    logger.info(
        "[tofu-trading] legacy migration verified tables=%d rows=%d",
        len(table_results),
        total_rows,
    )
    return result


def prepare_storage() -> dict[str, Any]:
    """Register the manifest and complete the idempotent legacy import once."""
    global _prepared
    with _prepare_lock:
        repository = TradingDocumentRepository()
        if _prepared:
            marker = repository.get(MIGRATION_MARKER_KEY)
            if marker is None:
                _prepared = False
            else:
                return dict(marker["document"]["row"])
        repository.register()
        result = migrate_legacy_storage(repository)
        _prepared = True
        return result


class TradingConnection:
    """Owner-bound DB-API seam backed by versioned sidecar row documents."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        repository: TradingDocumentRepository | None = None,
        prepare: bool = True,
    ):
        if not isinstance(owner_user_id, int) or owner_user_id <= 0:
            raise ValueError("owner_user_id must be a positive integer")
        if prepare:
            prepare_storage()
        self.owner_user_id = owner_user_id
        self._repository = repository or TradingDocumentRepository()
        self._sql: sqlite3.Connection
        self._loaded: set[str]
        self._versions: dict[str, int]
        self._loaded_rows = 0
        self._loaded_bytes = 0
        self._transaction_depth = 0
        self._baselines: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
        self._closed = False
        self._reset_query_state()

    @property
    def in_transaction(self) -> bool:
        return self._transaction_depth > 0 or self._sql.in_transaction

    def _reset_query_state(self) -> None:
        previous = getattr(self, "_sql", None)
        if previous is not None:
            previous.close()
        self._sql = sqlite3.connect(":memory:", isolation_level=None)
        self._sql.row_factory = sqlite3.Row
        initialize_query_schema(self._sql)
        self._loaded = set()
        self._versions = {}
        self._loaded_rows = 0
        self._loaded_bytes = 0
        self._transaction_depth = 0
        self._baselines = {}

    def _ensure_open(self) -> None:
        if self._closed:
            raise TradingStorageError("trading connection is closed")

    def _tables_in_sql(self, sql: str) -> tuple[str, ...]:
        names = tuple(dict.fromkeys(name.lower() for name in _TABLE_PATTERN.findall(sql)))
        unknown = set(names) - set(TABLE_SPECS)
        if unknown:
            raise TradingStorageError(f"query references undeclared tables: {unknown}")
        return names

    def _load_table(self, table_name: str) -> None:
        if table_name in self._loaded:
            return
        spec = TABLE_SPECS[table_name]
        owner_user_id = self.owner_user_id if spec.owner_scoped else SHARED_OWNER_ID
        items = self._repository.list_prefix(_row_prefix(table_name, owner_user_id))
        for item in items:
            document = item.get("document") or {}
            row = document.get("row")
            if (
                document.get("logical_table") != table_name
                or document.get("owner_user_id") != owner_user_id
                or not isinstance(row, Mapping)
                or set(row) != set(spec.columns)
            ):
                raise TradingStorageError(f"{table_name}: corrupt sidecar document")
            row_size = len(_canonical_json(row))
            self._loaded_rows += 1
            self._loaded_bytes += row_size
            if (
                self._loaded_rows > MAX_ROWS_PER_CONNECTION
                or self._loaded_bytes > MAX_BYTES_PER_CONNECTION
            ):
                raise TradingStorageError("trading query projection exceeds its budget")
            placeholders = ",".join("?" for _ in spec.columns)
            rendered_columns = ",".join(f'"{column}"' for column in spec.columns)
            self._sql.execute(
                f'INSERT INTO "{table_name}" ({rendered_columns}) '
                f"VALUES ({placeholders})",
                tuple(row[column] for column in spec.columns),
            )
            self._versions[str(document["key"])] = int(item["version"])
        self._loaded.add(table_name)

    def _snapshot(self, table_name: str) -> dict[tuple[Any, ...], dict[str, Any]]:
        spec = TABLE_SPECS[table_name]
        columns = ",".join(f'"{column}"' for column in spec.columns)
        order = ",".join(f'"{column}"' for column in spec.primary_key)
        rows = self._sql.execute(
            f'SELECT {columns} FROM "{table_name}" ORDER BY {order}'
        ).fetchall()
        result = {}
        for raw in rows:
            row = {column: raw[column] for column in spec.columns}
            key = tuple(row[column] for column in spec.primary_key)
            result[key] = row
        return result

    def _mutations(self) -> list[dict[str, Any]]:
        mutations: list[dict[str, Any]] = []
        for table_name, before in self._baselines.items():
            spec = TABLE_SPECS[table_name]
            after = self._snapshot(table_name)
            for primary_key in before.keys() - after.keys():
                row = before[primary_key]
                owner = _owner_for_row(spec, row, self.owner_user_id)
                key = _row_key(spec, row, owner)
                mutations.append(
                    {
                        "action": "delete",
                        "key": key,
                        "expected_version": self._versions.get(key, 0),
                    }
                )
            for primary_key, row in after.items():
                if before.get(primary_key) == row:
                    continue
                owner = _owner_for_row(spec, row, self.owner_user_id)
                if spec.owner_scoped and owner != self.owner_user_id:
                    raise TradingStorageError(
                        f"{table_name}: cross-owner write denied"
                    )
                document = _document(spec, row, owner, source="runtime")
                mutations.append(
                    {
                        "action": "put",
                        "document": document,
                        "expected_version": self._versions.get(document["key"], 0),
                    }
                )
        return mutations

    def _persist(self) -> None:
        mutations = self._mutations()
        if len(mutations) > 1000:
            raise TradingStorageError(
                "one trading transaction exceeds the 1000-row atomic budget"
            )
        mutation_bytes = sum(len(_canonical_json(item)) for item in mutations)
        if mutation_bytes > MAX_ATOMIC_BATCH_BYTES:
            raise TradingStorageError(
                "one trading transaction exceeds the 8 MiB atomic budget"
            )
        result = self._repository.batch(mutations)
        for mutation, stored in zip(mutations, result.get("results") or [], strict=True):
            key = (
                mutation["document"]["key"]
                if mutation["action"] == "put"
                else mutation["key"]
            )
            if mutation["action"] == "put":
                self._versions[key] = int(stored["version"])
            else:
                self._versions.pop(key, None)

    def execute(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> RepositoryCursor:
        self._ensure_open()
        if not isinstance(sql, str) or not sql.strip():
            raise TypeError("sql must be non-empty text")
        tables = self._tables_in_sql(sql)
        for table_name in tables:
            self._load_table(table_name)
        mutation_match = _MUTATION_PATTERN.match(sql)
        if mutation_match is None:
            if _DDL_PATTERN.match(sql):
                # All declared tables already exist. Runtime ensure-table DDL
                # remains harmless and cannot create sidecar structures.
                return RepositoryCursor(self, self._sql.execute(sql, params))
            return RepositoryCursor(self, self._sql.execute(sql, params))

        table_name = mutation_match.group(1).lower()
        if table_name not in self._baselines:
            self._baselines[table_name] = self._snapshot(table_name)
        if not self._sql.in_transaction:
            self._sql.execute("BEGIN")
        cursor = self._sql.execute(sql, params)
        return RepositoryCursor(self, cursor)

    def executemany(
        self, sql: str, params: Iterable[Sequence[Any] | Mapping[str, Any]]
    ) -> RepositoryCursor:
        self._ensure_open()
        tables = self._tables_in_sql(sql)
        for table_name in tables:
            self._load_table(table_name)
        mutation_match = _MUTATION_PATTERN.match(sql)
        if mutation_match is None:
            return RepositoryCursor(self, self._sql.executemany(sql, params))
        table_name = mutation_match.group(1).lower()
        if table_name not in self._baselines:
            self._baselines[table_name] = self._snapshot(table_name)
        if not self._sql.in_transaction:
            self._sql.execute("BEGIN")
        cursor = self._sql.executemany(sql, params)
        return RepositoryCursor(self, cursor)

    def begin(self) -> None:
        self._ensure_open()
        if self._transaction_depth == 0 and not self._sql.in_transaction:
            self._sql.execute("BEGIN")
        self._transaction_depth += 1

    def commit(self) -> None:
        self._ensure_open()
        if self._transaction_depth == 0 and not self._sql.in_transaction:
            return
        if self._transaction_depth > 1:
            self._transaction_depth -= 1
            return
        try:
            self._persist()
            self._sql.execute("COMMIT")
            self._transaction_depth = 0
            self._baselines.clear()
        except BaseException:
            logger.error(
                "[tofu-trading] sidecar commit failed owner_user_id=%d tables=%s",
                self.owner_user_id,
                sorted(self._baselines),
                exc_info=True,
            )
            self._reset_query_state()
            raise

    def rollback(self) -> None:
        self._ensure_open()
        if self._transaction_depth == 0 and not self._sql.in_transaction:
            self._baselines.clear()
            return
        self._reset_query_state()

    def cursor(self) -> RepositoryCursor:
        self._ensure_open()
        return RepositoryCursor(self, self._sql.cursor())

    def close(self) -> None:
        if self._closed:
            return
        if self._baselines:
            logger.warning(
                "[tofu-trading] rolling back uncommitted changes on close "
                "owner_user_id=%d tables=%s",
                self.owner_user_id,
                sorted(self._baselines),
            )
        if self.in_transaction or self._sql.in_transaction:
            self._sql.rollback()
        self._sql.close()
        self._closed = True

    def __enter__(self) -> "TradingConnection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            self.rollback()
        elif self.in_transaction:
            self.commit()
        self.close()


def _request_owner_id() -> int:
    from tofu_trading.identity import current_user_id

    return current_user_id()


def get_db(
    domain: str = DOMAIN_TRADING, *, owner_user_id: int
) -> TradingConnection:
    if domain != DOMAIN_TRADING:
        raise ValueError(f"unsupported trading domain: {domain}")
    return TradingConnection(int(owner_user_id))


def get_thread_db(
    domain: str = DOMAIN_TRADING, *, owner_user_id: int
) -> TradingConnection:
    if domain != DOMAIN_TRADING:
        raise ValueError(f"unsupported trading domain: {domain}")
    return TradingConnection(int(owner_user_id))


def _pool_get(*, owner_user_id: int) -> TradingConnection:
    return get_thread_db(owner_user_id=owner_user_id)


def _pool_put(connection: TradingConnection) -> None:
    connection.close()


def db_execute_with_retry(
    connection: TradingConnection, sql: str, params: Sequence[Any] = ()
) -> RepositoryCursor:
    """Compatibility seam; sidecar reads retry, commands remain fail-fast."""
    cursor = connection.execute(sql, params)
    connection.commit()
    return cursor


def _column_exists(connection: TradingConnection, table: str, column: str) -> bool:
    if table not in TABLE_SPECS:
        return False
    return column in TABLE_SPECS[table].columns


async def async_fetchall(
    sql: str,
    params: Sequence[Any] = (),
    *,
    domain: str = DOMAIN_TRADING,
    owner_user_id: int | None = None,
) -> list[StorageRow]:
    owner = _request_owner_id() if owner_user_id is None else owner_user_id

    def run() -> list[StorageRow]:
        connection = get_thread_db(domain, owner_user_id=owner)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    return await asyncio.to_thread(run)


async def async_fetchone(
    sql: str,
    params: Sequence[Any] = (),
    *,
    domain: str = DOMAIN_TRADING,
    owner_user_id: int | None = None,
) -> StorageRow | None:
    owner = _request_owner_id() if owner_user_id is None else owner_user_id

    def run() -> StorageRow | None:
        connection = get_thread_db(domain, owner_user_id=owner)
        try:
            return connection.execute(sql, params).fetchone()
        finally:
            connection.close()

    return await asyncio.to_thread(run)


async def async_execute(
    sql: str,
    params: Sequence[Any] = (),
    *,
    domain: str = DOMAIN_TRADING,
    owner_user_id: int | None = None,
) -> int:
    owner = _request_owner_id() if owner_user_id is None else owner_user_id

    def run() -> int:
        connection = get_thread_db(domain, owner_user_id=owner)
        try:
            cursor = connection.execute(sql, params)
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()

    return await asyncio.to_thread(run)


__all__ = [
    "DOMAIN_TRADING",
    "StorageRow",
    "TradingConnection",
    "TradingDocumentRepository",
    "TradingStorageError",
    "_column_exists",
    "_pool_get",
    "_pool_put",
    "async_execute",
    "async_fetchall",
    "async_fetchone",
    "db_execute_with_retry",
    "get_db",
    "get_thread_db",
    "migrate_legacy_storage",
    "prepare_storage",
]
