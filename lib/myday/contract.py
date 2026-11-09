"""My Day goal contract port over contracts/myday_goals_v1.schema.json.

The JSON file is the single authority for stored goal records, HTTP request
bodies and view shapes; this module only loads and validates it. Entry points:
CONTRACT, NAMESPACE, CAS_ATTEMPTS, MAX_ITEMS, validate, parse, request_body.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                       'myday_goals_v1.schema.json').read_text(encoding='utf-8'))
NAMESPACE = CONTRACT['namespaces']['goals']
CAS_ATTEMPTS = CONTRACT['policy']['casAttempts']
MAX_ITEMS = CONTRACT['$defs']['goal']['properties']['items']['maxItems']


@lru_cache(maxsize=None)
def _validator(name: str):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + name})


def validate(value, name: str):
    """Return ``value`` when it matches ``name``; raise ValueError naming the first error."""
    error = next(_validator(name).iter_errors(value), None)
    if error is not None:
        location = '.'.join(str(part) for part in error.absolute_path) or name
        raise ValueError(f'Invalid My Day {name} ({location}): {error.message}')
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return value


def request_body(name: str) -> dict:
    from lib.openapi import inline_schema_definition
    return {'required': True, 'content': {'application/json': {
        'schema': inline_schema_definition(CONTRACT, name)}}}


async def parse(name: str) -> dict:
    """Parse one exact JSON request body and validate it against ``name``."""
    from lib.request_parser import async_parse_body
    body = await async_parse_body(strict=True, exact=tuple(CONTRACT['$defs'][name]['properties']))
    return validate(dict(body), name)


__all__ = ['CAS_ATTEMPTS', 'CONTRACT', 'MAX_ITEMS', 'NAMESPACE', 'parse', 'request_body', 'validate']
