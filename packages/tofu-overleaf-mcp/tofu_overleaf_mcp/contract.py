"""Packaged wire schema, bounded validation and stable export error codes."""
from __future__ import annotations

from functools import cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator


class ExportError(ValueError):
    """Public code only: never include URLs, cookies or upstream response bodies."""


@cache
def schema() -> dict:
    return json.loads(Path(__file__).with_name('paper_build.generated.json').read_text())


def limit(name: str) -> int:
    return schema()['x-resource-limits'][name]


def definition(name: str) -> dict:
    result = dict(schema()['$defs'][name])
    definitions = {}

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            reference = value.get('$ref', '')
            if reference.startswith('#/$defs/'):
                key = reference.removeprefix('#/$defs/')
                if key not in definitions:
                    definitions[key] = schema()['$defs'][key]
                    collect(definitions[key])
            for item in value.values():
                collect(item)

    collect(result)
    if definitions:
        result['$defs'] = definitions
    return result


@cache
def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(definition(name))


def validate(value: dict, name: str) -> dict:
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or not validator(name).is_valid(value)):
        raise ExportError('export_contract')
    return value
