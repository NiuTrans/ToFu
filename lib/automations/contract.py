"""Lightweight reader of the one automation schema and lifecycle vocabulary.

Entry points: definition_schema, storage_schema, storage_input_validator, LIMITS, RUNTIME. No generated or handwritten
copy may redefine limits or persisted states; schema callers receive copies.
"""

from functools import lru_cache
import json
from pathlib import Path
from types import MappingProxyType


@lru_cache(maxsize=1)
def _schema_json() -> str:
    path = Path(__file__).resolve().parents[2] / 'contracts' / 'automation_v1.schema.json'
    return path.read_text(encoding='utf-8')


def definition_schema() -> dict:
    return json.loads(_schema_json())


LIMITS = MappingProxyType(definition_schema()['x-limits'])
SCHEDULE = MappingProxyType(definition_schema()['x-schedule'])
RUNTIME = MappingProxyType({key: tuple(value) if isinstance(value, list) else value
                          for key, value in definition_schema()['x-runtime'].items()})


@lru_cache(maxsize=1)
def _storage_schema_json() -> str:
    return (Path(__file__).resolve().parents[2] / 'contracts/automation_storage_v1.schema.json').read_text(encoding='utf-8')


def storage_schema() -> dict:
    return json.loads(_storage_schema_json())


STORAGE_NAMESPACES = MappingProxyType(storage_schema()['x-namespaces'])
RESERVED_WORKER_TASK_KINDS = MappingProxyType(storage_schema()['x-reservedTaskKinds'])


@lru_cache(maxsize=10)
def storage_input_validator(kind: str):
    """Load a local semantic input schema only when its operation is used."""
    from jsonschema import Draft202012Validator
    if kind not in {'admit', 'advance', 'cancel', 'approve', 'definitionUpdate', 'clockJob', 'taskStartedContext', 'taskStartedJob', 'eventDeliveryCursor', 'eventSettle', 'projectUpdate'}:
        raise ValueError('Unknown automation storage input')
    schema = storage_schema()
    return Draft202012Validator({**schema, 'oneOf': [{'$ref': '#/$defs/' + kind}]})
