"""Load the paper-build wire authority and validate build boundary payloads.

Only immutable contract data is cached. jsonschema loads lazily at validation;
the dependency-free source/compiler modules are also usable on Desktop Agent.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .errors import BuildError


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads((Path(__file__).resolve().parents[3]
                       / 'contracts/paper_build_v1.schema.json').read_text())


def limit(name: str) -> int:
    return int(schema()['x-resource-limits'][name])


def validate(value: dict, definition: str = 'PaperBuildReceiptV1') -> dict:
    from jsonschema import Draft202012Validator
    if isinstance(value, dict) and 'schema_version' in value and type(value['schema_version']) is not int:
        raise BuildError('build_contract', 'Invalid paper build field: schema_version')
    authority = schema()
    validator = Draft202012Validator({**authority, '$ref': '#/$defs/' + definition})
    failure = next(validator.iter_errors(value), None)
    if failure:
        field = '.'.join(str(p) for p in failure.absolute_path) or definition
        raise BuildError('build_contract', f'Invalid paper build field: {field}')
    if definition == 'PaperBuildReceiptV1':
        names = [row['name'] for row in value['artifacts']]
        if len(names) != len(set(names)):
            raise BuildError('build_contract', 'Duplicate build artifact')
        if value['status'] == 'success' and ('document.pdf' not in names or any(
                d['severity'] == 'error' for d in value['diagnostics'])):
            raise BuildError('build_contract', 'Successful build requires a PDF and no errors')
    return value
