"""Plugin registration, manifest, and dynamic-dispatch operation handlers."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import time
from typing import Any


from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage.manifest import (
    ManifestError,
    validate_document,
    validate_manifest,
)
from lib.storage_sidecar.adapters.base import Session


logger = get_logger(__name__)


from lib.storage_sidecar.operations_pkg._common import (
    _dump,
    _expected_version,
    _integer,
    _load,
    _required_text,
    _wire_document,
)


def _plugin_register(session: Session, payload: Mapping[str, Any]) -> Any:
    try:
        manifest = validate_manifest(payload.get("manifest"))
    except (ManifestError, TypeError) as exc:
        raise StorageError(
            "plugin_storage_incompatible", "Plugin storage manifest is incompatible"
        ) from exc
    namespace = manifest["namespace"]
    encoded = _dump(manifest)
    current = session.fetch_one(
        "SELECT manifest_version, manifest_json FROM storage_plugin_manifests "
        "WHERE namespace = ?",
        (namespace,),
    )
    if current:
        current_version = int(current["manifest_version"])
        if manifest["version"] < current_version:
            raise StorageError(
                "plugin_storage_incompatible", "Plugin storage version moved backwards"
            )
        if (
            manifest["version"] == current_version
            and bytes(current["manifest_json"]) != encoded
        ):
            raise StorageError(
                "plugin_storage_incompatible", "Plugin storage version was redefined"
            )
        if manifest["version"] > current_version:
            previous = _load(current["manifest_json"])
            previous_tables = {item["name"]: item for item in previous["tables"]}
            next_tables = {item["name"]: item for item in manifest["tables"]}
            previous_operations = {
                item["name"]: item for item in previous["operations"]
            }
            next_operations = {item["name"]: item for item in manifest["operations"]}
            incompatible = False
            for name, table in previous_tables.items():
                upgraded = next_tables.get(name)
                if upgraded is None:
                    incompatible = True
                    break
                old_columns = table["columns"]
                new_columns = upgraded["columns"]
                old_indexes = {item["name"]: item for item in table.get("indexes", [])}
                new_indexes = {
                    item["name"]: item for item in upgraded.get("indexes", [])
                }
                if (
                    new_columns[: len(old_columns)] != old_columns
                    or upgraded["primary_key"] != table["primary_key"]
                    or any(
                        new_indexes.get(index) != definition
                        for index, definition in old_indexes.items()
                    )
                    or any(
                        item.get("required") for item in new_columns[len(old_columns) :]
                    )
                    or any(
                        item.get("unique") and item["name"] not in old_indexes
                        for item in upgraded.get("indexes", [])
                    )
                ):
                    incompatible = True
                    break
            incompatible = incompatible or any(
                name not in next_operations or next_operations[name] != operation
                for name, operation in previous_operations.items()
            )
            if incompatible:
                raise StorageError(
                    "plugin_storage_incompatible",
                    "Plugin storage migration is not append-only compatible",
                )
    now = int(time.time() * 1000)
    session.execute(
        "INSERT INTO storage_plugin_manifests(namespace, manifest_version, manifest_json, updated_at_ms) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(namespace) DO UPDATE SET "
        "manifest_version = excluded.manifest_version, manifest_json = excluded.manifest_json, "
        "updated_at_ms = excluded.updated_at_ms",
        (namespace, manifest["version"], encoded, now),
    )
    return {"namespace": namespace, "version": manifest["version"]}


def _plugin_manifest_get(session: Session, payload: Mapping[str, Any]) -> Any:
    namespace = _required_text(payload, "namespace", 128)
    row = session.fetch_one(
        "SELECT manifest_json FROM storage_plugin_manifests WHERE namespace = ?",
        (namespace,),
    )
    return _load(row["manifest_json"]) if row else None


def _plugin_context(session: Session, operation: str):
    parts = operation.split(".")
    if len(parts) < 3 or parts[0] != "plugin":
        raise StorageError("database_protocol_error", "Unknown storage operation")
    namespace = ".".join(parts[1:-1])
    operation_name = parts[-1]
    row = session.fetch_one(
        "SELECT manifest_json FROM storage_plugin_manifests WHERE namespace = ?",
        (namespace,),
    )
    if row is None:
        raise StorageError(
            "plugin_storage_incompatible", "Plugin storage namespace is not registered"
        )
    manifest = _load(row["manifest_json"])
    operation_spec = next(
        (item for item in manifest["operations"] if item["name"] == operation_name),
        None,
    )
    if operation_spec is None:
        raise StorageError(
            "database_protocol_error", "Unknown plugin storage operation"
        )
    table = next(
        item for item in manifest["tables"] if item["name"] == operation_spec["table"]
    )
    return namespace, operation_spec, table


def _plugin_dynamic(
    session: Session,
    operation: str,
    kind: str,
    payload: Mapping[str, Any],
) -> Any:
    namespace, spec, table = _plugin_context(session, operation)
    if spec["kind"] != kind:
        raise StorageError("database_protocol_error", "Plugin operation kind mismatch")
    action = spec["action"]
    table_name = table["name"]
    primary_key = table["primary_key"][0]
    if action in {"get", "delete"}:
        key = _required_text(payload, primary_key)
    if action == "get":
        row = session.fetch_one(
            "SELECT document_json, version, updated_at_ms FROM storage_plugin_rows "
            "WHERE namespace = ? AND table_name = ? AND row_key = ?",
            (namespace, table_name, key),
        )
        if row is None:
            return None
        return {
            "document": _load(row["document_json"]),
            "version": int(row["version"]),
            "updated_at_ms": int(row["updated_at_ms"]),
        }
    if action == "list":
        limit = _integer(
            payload, "limit", default=100, minimum=1, maximum=spec["limit_max"]
        )
        key_prefix = payload.get("key_prefix", "")
        after_key = payload.get("after_key", "")
        if (
            not isinstance(key_prefix, str)
            or len(key_prefix) > 512
            or not isinstance(after_key, str)
            or len(after_key) > 512
            or (key_prefix and after_key and not after_key.startswith(key_prefix))
        ):
            raise StorageError(
                "database_protocol_error", "Plugin list cursor is invalid"
            )
        filters = payload.get("filters") or {}
        if not isinstance(filters, Mapping):
            raise StorageError(
                "database_protocol_error", "Plugin filters must be an object"
            )
        declared = {column["name"] for column in table["columns"]}
        if set(filters) - declared:
            raise StorageError("database_protocol_error", "Plugin filter is undeclared")
        # The validated query model is deliberately evaluated after a bounded
        # key-range read; no plugin expression is interpolated into SQL. The
        # optional prefix/cursor pair lets repositories traverse an arbitrarily
        # large logical collection through bounded pages while preserving the
        # original list response shape.
        read_limit = limit if key_prefix else min(1000, max(limit * 10, limit))
        rows = session.fetch_all(
            "SELECT document_json, version, updated_at_ms FROM storage_plugin_rows "
            "WHERE namespace = ? AND table_name = ? AND row_key > ? "
            "ORDER BY row_key LIMIT ?",
            (namespace, table_name, after_key or key_prefix, read_limit),
        )
        result = []
        for row in rows:
            document = _load(row["document_json"])
            if key_prefix and not str(document.get(primary_key, "")).startswith(
                key_prefix
            ):
                break
            if all(document.get(key) == value for key, value in filters.items()):
                result.append(
                    {
                        "document": document,
                        "version": int(row["version"]),
                        "updated_at_ms": int(row["updated_at_ms"]),
                    }
                )
                if len(result) >= limit:
                    break
        return result
    if action == "put":
        return _plugin_put_document(
            session, namespace, table_name, table, primary_key, payload
        )
    if action == "delete":
        return _plugin_delete_document(
            session, namespace, table_name, primary_key, payload
        )
    if action == "batch":
        mutations = payload.get("mutations")
        if (not isinstance(mutations, list) or not mutations
                or len(mutations) > spec["limit_max"]):
            raise StorageError(
                "database_protocol_error",
                "Plugin batch must contain a bounded mutation list",
            )
        results = []
        seen_keys: set[str] = set()
        for mutation in mutations:
            if not isinstance(mutation, Mapping):
                raise StorageError(
                    "database_protocol_error", "Plugin mutation must be an object"
                )
            mutation_action = str(mutation.get("action") or "")
            if mutation_action == "put":
                document = mutation.get("document")
                key_value = (
                    document.get(primary_key)
                    if isinstance(document, Mapping) else None
                )
            elif mutation_action == "delete":
                key_value = mutation.get(primary_key)
            else:
                raise StorageError(
                    "database_protocol_error", "Unsupported plugin batch mutation"
                )
            if (
                not isinstance(key_value, (str, int))
                or isinstance(key_value, bool)
            ):
                raise StorageError(
                    "database_protocol_error",
                    "Plugin batch keys must be strings or integers",
                )
            key = str(key_value)
            if not key or key in seen_keys:
                raise StorageError(
                    "database_protocol_error",
                    "Plugin batch keys must be non-empty and unique",
                )
            seen_keys.add(key)
            if mutation_action == "put":
                result = _plugin_put_document(
                    session, namespace, table_name, table, primary_key, mutation
                )
            else:
                result = _plugin_delete_document(
                    session, namespace, table_name, primary_key, mutation
                )
            results.append(result)
        return {"results": results}
    if action == "legacy_scan":
        return _plugin_legacy_scan(session, spec, payload)
    raise StorageError("database_protocol_error", "Unsupported plugin action")


def _plugin_put_document(
    session: Session,
    namespace: str,
    table_name: str,
    table: Mapping[str, Any],
    primary_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and persist one manifest-owned document."""
    try:
        document = validate_document(table, payload.get("document"))
    except ManifestError as exc:
        raise StorageError(
            "plugin_storage_incompatible", "Plugin document violates its manifest"
        ) from exc
    key_value = document.get(primary_key)
    if not isinstance(key_value, (str, int)) or isinstance(key_value, bool):
        raise StorageError(
            "plugin_storage_incompatible", "Plugin primary key is invalid"
        )
    key = str(key_value)
    current = session.fetch_one(
        "SELECT version FROM storage_plugin_rows "
        "WHERE namespace = ? AND table_name = ? AND row_key = ?",
        (namespace, table_name, key),
    )
    actual = int(current["version"]) if current else 0
    expected = _expected_version(payload)
    if expected is not None and expected != actual:
        raise StorageError("database_conflict", "Plugin row version conflict")
    version = actual + 1
    now = int(time.time() * 1000)
    unique_values = []
    for index in table.get("indexes", []):
        if not index.get("unique"):
            continue
        values = [document.get(column) for column in index["columns"]]
        if any(value is None for value in values):
            continue
        unique_value = hashlib.sha256(_dump(values)).hexdigest()
        owner = session.fetch_one(
            "SELECT row_key FROM storage_plugin_unique_values "
            "WHERE namespace = ? AND table_name = ? AND index_name = ? "
            "AND index_value = ?",
            (namespace, table_name, index["name"], unique_value),
        )
        if owner is not None and owner["row_key"] != key:
            raise StorageError(
                "database_conflict", "Plugin unique constraint conflict"
            )
        unique_values.append((index["name"], unique_value))
    session.execute(
        "DELETE FROM storage_plugin_unique_values "
        "WHERE namespace = ? AND table_name = ? AND row_key = ?",
        (namespace, table_name, key),
    )
    for index_name, unique_value in unique_values:
        session.execute(
            "INSERT INTO storage_plugin_unique_values("
            "namespace, table_name, index_name, index_value, row_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (namespace, table_name, index_name, unique_value, key),
        )
    session.execute(
        "INSERT INTO storage_plugin_rows(namespace, table_name, row_key, document_json, version, updated_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(namespace, table_name, row_key) DO UPDATE SET "
        "document_json = excluded.document_json, version = excluded.version, "
        "updated_at_ms = excluded.updated_at_ms",
        (namespace, table_name, key, _dump(_wire_document(document)), version, now),
    )
    return {"key": key, "version": version, "updated_at_ms": now}


def _plugin_delete_document(
    session: Session,
    namespace: str,
    table_name: str,
    primary_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Delete one document, optionally guarded by its observed version."""
    key = _required_text(payload, primary_key)
    expected = _expected_version(payload)
    if expected is not None:
        current = session.fetch_one(
            "SELECT version FROM storage_plugin_rows "
            "WHERE namespace = ? AND table_name = ? AND row_key = ?",
            (namespace, table_name, key),
        )
        actual = int(current["version"]) if current else 0
        if expected != actual:
            raise StorageError("database_conflict", "Plugin row version conflict")
    session.execute(
        "DELETE FROM storage_plugin_unique_values "
        "WHERE namespace = ? AND table_name = ? AND row_key = ?",
        (namespace, table_name, key),
    )
    count = session.execute(
        "DELETE FROM storage_plugin_rows "
        "WHERE namespace = ? AND table_name = ? AND row_key = ?",
        (namespace, table_name, key),
    )
    return {"key": key, "deleted": bool(count)}


def _plugin_legacy_scan(
    session: Session,
    spec: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Read one bounded page from an exactly manifest-declared legacy table."""
    limit = _integer(
        payload, "limit", default=100, minimum=1, maximum=spec["limit_max"]
    )
    offset = _integer(payload, "offset", default=0, minimum=0, maximum=10_000_000)
    legacy_table = spec["legacy_table"]
    if session.backend == "postgres":
        exists = session.fetch_one(
            "SELECT 1 AS present FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (legacy_table,),
        )
    else:
        exists = session.fetch_one(
            "SELECT 1 AS present FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            (legacy_table,),
        )
    if exists is None:
        return {"exists": False, "rows": [], "next_offset": None}
    columns = spec["legacy_columns"]
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    order_columns = spec.get("legacy_order_by") or columns[:1]
    quoted_order = ", ".join(f'"{column}"' for column in order_columns)
    rows = session.fetch_all(
        f'SELECT {quoted_columns} FROM "{legacy_table}" '
        f'ORDER BY {quoted_order} LIMIT ? OFFSET ?',
        (limit, offset),
    )
    documents = [
        _wire_document({column: row[column] for column in columns})
        for row in rows
    ]
    return {
        "exists": True,
        "rows": documents,
        "next_offset": offset + len(documents) if len(documents) == limit else None,
    }
