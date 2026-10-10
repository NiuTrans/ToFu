"""UTF-8 byte-length expressions for bounded semantic text pages."""
from __future__ import annotations


def utf8_length_expression(backend: str, column: str) -> str:
    # Callers supply an owned column identifier, never request SQL.
    if not column.isidentifier():
        raise ValueError('Invalid byte-length column identifier')
    if backend == 'sqlite':
        return f'LENGTH(CAST({column} AS BLOB))'
    if backend == 'postgres':
        return f'OCTET_LENGTH({column})'
    raise ValueError(f'Unsupported text-size backend: {backend}')
