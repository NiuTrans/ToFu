"""Declarative, cross-backend plugin storage manifest validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_NAME = re.compile(r'^[a-z][a-z0-9_]{0,62}$')
_NAMESPACE = re.compile(r'^[a-z][a-z0-9_.-]{2,127}$')
_TYPES = {'string', 'integer', 'number', 'boolean', 'json', 'bytes', 'timestamp'}
_ACTIONS = {'get', 'list', 'put', 'delete'}


class ManifestError(ValueError):
    pass


def _name(value: Any, label: str) -> str:
    value = str(value or '')
    if not _NAME.fullmatch(value):
        raise ManifestError(f'invalid {label}')
    return value


def validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized manifest or fail before it reaches a driver."""
    if not isinstance(value, Mapping):
        raise ManifestError('manifest must be an object')
    namespace = str(value.get('namespace') or '')
    if not _NAMESPACE.fullmatch(namespace):
        raise ManifestError('invalid namespace')
    version = value.get('version')
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ManifestError('version must be a positive integer')
    tables_in = value.get('tables')
    operations_in = value.get('operations')
    if not isinstance(tables_in, list) or not tables_in:
        raise ManifestError('at least one table is required')
    if not isinstance(operations_in, list) or not operations_in:
        raise ManifestError('at least one operation is required')

    tables: list[dict[str, Any]] = []
    table_map: dict[str, dict[str, Any]] = {}
    for raw_table in tables_in:
        if not isinstance(raw_table, Mapping):
            raise ManifestError('table must be an object')
        table_name = _name(raw_table.get('name'), 'table name')
        if table_name in table_map:
            raise ManifestError('duplicate table')
        columns_in = raw_table.get('columns')
        if not isinstance(columns_in, list) or not columns_in:
            raise ManifestError(f'{table_name}: columns are required')
        columns = []
        column_names: set[str] = set()
        for raw_column in columns_in:
            if not isinstance(raw_column, Mapping):
                raise ManifestError(f'{table_name}: column must be an object')
            column_name = _name(raw_column.get('name'), 'column name')
            column_type = str(raw_column.get('type') or '')
            if column_name in column_names or column_type not in _TYPES:
                raise ManifestError(f'{table_name}: invalid or duplicate column')
            column_names.add(column_name)
            columns.append({
                'name': column_name,
                'type': column_type,
                'required': bool(raw_column.get('required', False)),
            })
        primary_key = raw_table.get('primary_key')
        if not isinstance(primary_key, list) or len(primary_key) != 1:
            raise ManifestError(f'{table_name}: exactly one primary key is required')
        primary_key = [_name(primary_key[0], 'primary key')]
        if primary_key[0] not in column_names:
            raise ManifestError(f'{table_name}: unknown primary key')
        indexes = []
        for raw_index in raw_table.get('indexes') or []:
            if not isinstance(raw_index, Mapping):
                raise ManifestError(f'{table_name}: index must be an object')
            index_name = _name(raw_index.get('name'), 'index name')
            index_columns = raw_index.get('columns')
            if (not isinstance(index_columns, list) or not index_columns
                    or any(str(c) not in column_names for c in index_columns)):
                raise ManifestError(f'{table_name}: invalid index columns')
            indexes.append({
                'name': index_name,
                'columns': [str(c) for c in index_columns],
                'unique': bool(raw_index.get('unique', False)),
            })
        table = {
            'name': table_name,
            'columns': columns,
            'primary_key': primary_key,
            'indexes': indexes,
        }
        tables.append(table)
        table_map[table_name] = table

    operations = []
    operation_names: set[str] = set()
    for raw_operation in operations_in:
        if not isinstance(raw_operation, Mapping):
            raise ManifestError('operation must be an object')
        name = _name(raw_operation.get('name'), 'operation name')
        action = str(raw_operation.get('action') or '')
        table_name = _name(raw_operation.get('table'), 'operation table')
        if name in operation_names or action not in _ACTIONS or table_name not in table_map:
            raise ManifestError('invalid or duplicate operation')
        operation_names.add(name)
        expected_kind = 'query' if action in {'get', 'list'} else 'command'
        kind = str(raw_operation.get('kind') or expected_kind)
        if kind != expected_kind:
            raise ManifestError(f'{name}: action/kind mismatch')
        limit_max = raw_operation.get('limit_max', 100)
        if not isinstance(limit_max, int) or not 1 <= limit_max <= 1000:
            raise ManifestError(f'{name}: limit_max must be 1..1000')
        operations.append({
            'name': name, 'kind': kind, 'action': action, 'table': table_name,
            'limit_max': limit_max,
        })

    return {
        'namespace': namespace,
        'version': version,
        'tables': tables,
        'operations': operations,
    }


def validate_document(table: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError('document must be an object')
    columns = {column['name']: column for column in table['columns']}
    if set(value) - set(columns):
        raise ManifestError('document contains undeclared fields')
    document = dict(value)
    for name, column in columns.items():
        if column['required'] and name not in document:
            raise ManifestError(f'missing required field: {name}')
        if name not in document or document[name] is None:
            continue
        item = document[name]
        expected = column['type']
        valid = {
            'string': isinstance(item, str),
            'integer': isinstance(item, int) and not isinstance(item, bool),
            'number': isinstance(item, (int, float)) and not isinstance(item, bool),
            'boolean': isinstance(item, bool),
            'json': isinstance(item, (dict, list, str, int, float, bool)),
            'bytes': isinstance(item, (bytes, str)),
            'timestamp': isinstance(item, (int, float, str)) and not isinstance(item, bool),
        }[expected]
        if not valid:
            raise ManifestError(f'invalid type for field: {name}')
    return document


__all__ = ['ManifestError', 'validate_document', 'validate_manifest']
