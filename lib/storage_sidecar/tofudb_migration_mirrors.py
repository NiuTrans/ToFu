"""Prove retired trading rows are duplicated in the mapped plugin authority.

Reads only the pinned migration source. Registered legacy scan declarations
supply exact source columns/keys; names alone never admit a table. Every source
column and every row must match the current mapped document multiset. Digests
are bounded to the plugin's 50,000-row projection budget; no source is deleted.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any

from lib.storage_sidecar.tofudb_migration_source import SQLiteMigrationSource

_MAX_ROWS = 50_000
_MAX_OPERATIONS = 256


def _digest(row: Any) -> bytes:
    return hashlib.sha256(json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')).digest()


def verified_trading_mirrors(source: SQLiteMigrationSource) -> dict[str, Any]:
    """Return evidence only for complete, equal legacy/current row multisets."""
    required = {'storage_plugin_manifests', 'storage_plugin_rows'}
    if not required.issubset(source.tables):
        return {}
    manifest_row = source.point('storage_plugin_manifests',
                                ['manifest_json'], ['namespace'], ['tofu.trading'])
    if manifest_row is None:
        return {}
    manifest = json.loads(manifest_row['manifest_json'])
    operations = manifest.get('operations', [])
    if (manifest.get('namespace') != 'tofu.trading'
            or not isinstance(operations, list) or len(operations) > _MAX_OPERATIONS):
        raise ValueError('invalid trading mirror manifest')
    declarations = {}
    for operation in operations:
        if operation.get('action') != 'legacy_scan':
            continue
        name = operation.get('legacy_table')
        if (not isinstance(name, str) or not name.startswith('trading_')
                or operation.get('table') != 'rows' or name in declarations):
            raise ValueError('invalid trading mirror declaration')
        declarations[name] = operation
    if not declarations:
        return {}
    hashes: dict[str, Counter] = {name: Counter() for name in declarations}
    keys = ['namespace', 'table_name', 'row_key']
    rows = source.rows('storage_plugin_rows', keys + ['document_json'], keys,
                       key_prefix=['tofu.trading', 'rows'])
    try:
        for count, row in enumerate(rows, 1):
            if count > _MAX_ROWS:
                raise ValueError('trading mirror row budget exceeded')
            document = json.loads(row['document_json'])
            name = document.get('logical_table')
            if name in hashes:
                hashes[name][_digest(document['row'])] += 1
    finally:
        rows.close()
    result = {}
    total_rows = 0
    for name, declaration in declarations.items():
        if name not in source.tables:
            continue
        columns = source.table_columns(name)
        if set(columns) != set(declaration.get('legacy_columns', [])):
            continue  # A column omitted by the plugin may contain unique data.
        source_hashes: Counter = Counter()
        rows = source.rows(name, columns, declaration.get('legacy_order_by', []))
        try:
            for row in rows:
                total_rows += 1
                if total_rows > _MAX_ROWS:
                    raise ValueError('trading mirror row budget exceeded')
                source_hashes[_digest(row)] += 1
        finally:
            rows.close()
        if source_hashes == hashes[name]:
            digest = hashlib.sha256()
            for value, count in sorted(source_hashes.items()):
                for _ in range(count):
                    digest.update(value)
            result[name] = {
                'authorityTable': 'storage_plugin_rows', 'namespace': 'tofu.trading',
                'rows': sum(source_hashes.values()), 'sha256': digest.hexdigest(),
                'proof': 'exact-current-row-multiset;all-source-columns',
            }
    return result
