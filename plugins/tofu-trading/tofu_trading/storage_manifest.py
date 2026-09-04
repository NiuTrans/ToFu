"""Declarative sidecar storage contract for tofu-trading.

This object is loaded through the host's ``tofu.storage`` entry point. It is
data only: registration, SQL ownership, transactions, and legacy reads remain
inside the host sidecar process.
"""

from __future__ import annotations

from tofu_trading.storage_schema import TABLE_SPECS


NAMESPACE = "tofu.trading"
MANIFEST_VERSION = 1

_DOCUMENT_TABLE = {
    "name": "rows",
    "columns": [
        {"name": "key", "type": "string", "required": True},
        {"name": "logical_table", "type": "string", "required": True},
        {"name": "owner_user_id", "type": "integer", "required": True},
        {"name": "row", "type": "json", "required": True},
        {"name": "source", "type": "string", "required": True},
        {"name": "schema_version", "type": "integer", "required": True},
    ],
    "primary_key": ["key"],
    "indexes": [
        {
            "name": "by_logical_owner",
            "columns": ["logical_table", "owner_user_id"],
        }
    ],
}

_OPERATIONS = [
    {"name": "get_row", "kind": "query", "action": "get", "table": "rows"},
    {
        "name": "list_rows",
        "kind": "query",
        "action": "list",
        "table": "rows",
        "limit_max": 1000,
    },
    {"name": "put_row", "kind": "command", "action": "put", "table": "rows"},
    {
        "name": "mutate_rows",
        "kind": "command",
        "action": "batch",
        "table": "rows",
        "limit_max": 1000,
    },
]

for _table_name, _spec in TABLE_SPECS.items():
    _suffix = _table_name.removeprefix("trading_")
    _OPERATIONS.append(
        {
            "name": f"scan_{_suffix}",
            "kind": "query",
            "action": "legacy_scan",
            "table": "rows",
            "legacy_table": _table_name,
            "legacy_columns": list(_spec.columns),
            "legacy_order_by": list(_spec.primary_key),
            "limit_max": 500,
        }
    )

MANIFEST = {
    "namespace": NAMESPACE,
    "version": MANIFEST_VERSION,
    "tables": [_DOCUMENT_TABLE],
    "operations": _OPERATIONS,
}


__all__ = ["MANIFEST", "MANIFEST_VERSION", "NAMESPACE"]
