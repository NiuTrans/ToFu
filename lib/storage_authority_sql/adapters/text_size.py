"""UTF-8 byte-length expressions for bounded semantic text pages."""
from __future__ import annotations
import re


def utf8_length_expression(backend: str, column: str) -> str:
    # Callers supply an owned column identifier, never request SQL.
    if not column.isidentifier():
        raise ValueError('Invalid byte-length column identifier')
    if backend == 'sqlite':
        return f'LENGTH(CAST({column} AS BLOB))'
    if backend == 'postgres':
        return f'OCTET_LENGTH({column})'
    raise ValueError(f'Unsupported text-size backend: {backend}')


def json_utf8_length_expression(backend: str, column: str) -> str:
    """Bound JSONDOC materialization; PostgreSQL jsonb needs a text projection."""
    if not column.isidentifier():
        raise ValueError('Invalid JSON byte-length column identifier')
    if backend == 'postgres':
        return f'OCTET_LENGTH(CAST({column} AS TEXT))'
    return utf8_length_expression(backend, column)


def replay_length_expressions(sql: str, source: str, target: str) -> str:
    """Translate only the owned bounded-read expressions in checkpoint traces."""
    if source == target:
        return sql
    column = r'([a-zA-Z_][a-zA-Z_0-9]*(?:\.[a-zA-Z_][a-zA-Z_0-9]*)?)'
    if source == 'sqlite' and target == 'postgres':
        return re.sub(r'LENGTH\(CAST\(' + column + r' AS BLOB\)\)',
                      r'OCTET_LENGTH(CAST(\1 AS TEXT))', sql)
    if source == 'postgres' and target == 'sqlite':
        sql = re.sub(r'OCTET_LENGTH\(CAST\(' + column + r' AS TEXT\)\)',
                     r'LENGTH(CAST(\1 AS BLOB))', sql)
        return re.sub(r'OCTET_LENGTH\(' + column + r'\)', r'LENGTH(CAST(\1 AS BLOB))', sql)
    raise ValueError('Unsupported replay size-expression backend')
