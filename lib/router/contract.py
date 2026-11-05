"""Router contract port over contracts/router_v1.schema.json.

The JSON file is the single authority for router tool arguments, standing
instruction records and the /api/v4/router wire shapes; this module only loads,
inlines and validates it. Entry points: CONTRACT, NAMESPACES, schema,
tool_arguments, validate, request_body, query_parameters, parse.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                       'router_v1.schema.json').read_text(encoding='utf-8'))
NAMESPACES = CONTRACT['namespaces']


def schema(name: str) -> dict:
    """One definition with its local references inlined (OpenAPI/tool safe)."""
    from lib.openapi import inline_schema_definition
    return inline_schema_definition(CONTRACT, name)


def tool_arguments(name: str) -> dict:
    """The model-facing argument object of one router tool."""
    definition = schema(name)
    return {'properties': definition['properties'], 'required': list(definition['required'])}


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
        raise ValueError(f'Invalid router {name} ({location}): {error.message}')
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return value


def request_body(name: str) -> dict:
    return {'required': True, 'content': {'application/json': {'schema': schema(name)}}}


def query_parameters(name: str) -> list[dict]:
    definition = schema(name)
    return [{'name': key, 'in': 'query', 'required': key in definition['required'], 'schema': value}
            for key, value in definition['properties'].items()]


def parse(name: str) -> dict:
    """Parse one exact JSON request body and validate it against ``name``."""
    from lib.request_parser import parse_body
    body = parse_body(exact=tuple(CONTRACT['$defs'][name]['properties']))
    return validate(dict(body), name)


__all__ = ['CONTRACT', 'NAMESPACES', 'parse', 'query_parameters', 'request_body', 'schema',
           'tool_arguments', 'validate']
