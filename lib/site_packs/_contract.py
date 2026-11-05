"""Browser-free access to the single Site Pack schema and its declared defaults.

Manifest admission and input projection borrow this cached validator; callers
copy data they need to change. No executable defaults live in a parallel table.
"""

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).parents[2] / 'contracts' / 'site_pack_v1.schema.json'


@cache
def schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def step_properties(kind: str) -> dict:
    return schema_validator().schema['$defs'][f'step_{kind}']['properties']
