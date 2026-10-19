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


def _supports_small_context_schema(schema: dict) -> bool:
    """Closed supported vocabulary; new schema semantics use the full oracle."""
    if not isinstance(schema, dict):
        return False
    allowed = {None: {'enum'}, 'object': {'properties', 'required', 'additionalProperties', 'enum'},
               'array': {'items', 'maxItems', 'enum'}, 'string': {'minLength', 'maxLength', 'enum'}}
    kind = schema.get('type')
    if not isinstance(kind, (str, type(None))):
        return False
    if kind not in allowed or set(schema) - (allowed[kind] | {'type'}):
        return False
    if 'enum' in schema and not all(isinstance(value, str) for value in schema['enum']):
        return False
    if 'additionalProperties' in schema and type(schema['additionalProperties']) is not bool:
        return False
    return (all(_supports_small_context_schema(value) for value in schema.get('properties', {}).values())
            and ('items' not in schema or _supports_small_context_schema(schema['items'])))


def _matches_small_context_schema(value, schema: dict) -> bool:
    """Evaluate only the admitted subset, using every bound from the schema."""
    if 'enum' in schema and (not isinstance(value, str) or value not in schema['enum']):
        return False
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict) or not set(schema.get('required', ())).issubset(value):
            return False
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - properties.keys():
            return False
        return all(_matches_small_context_schema(value[key], child)
                   for key, child in properties.items() if key in value)
    if kind == 'array':
        if not isinstance(value, list) or len(value) > schema.get('maxItems', len(value)):
            return False
        return 'items' not in schema or all(_matches_small_context_schema(item, schema['items']) for item in value)
    if kind == 'string':
        return (isinstance(value, str) and schema.get('minLength', 0) <= len(value)
                <= schema.get('maxLength', len(value)))
    return True


@lru_cache(maxsize=1)
def _task_started_context_schema() -> tuple[dict, bool]:
    schema = storage_schema()['$defs']['taskStartedContext']
    return schema, _supports_small_context_schema(schema)


def task_started_context_is_valid(value) -> bool:
    """Avoid optional schema-engine import for ordinary task-start evidence."""
    schema, supported = _task_started_context_schema()
    if supported:
        return _matches_small_context_schema(value, schema)
    return next(storage_input_validator('taskStartedContext').iter_errors(value), None) is None


@lru_cache(maxsize=10)
def storage_input_validator(kind: str):
    """Load a local semantic input schema only when its operation is used."""
    from jsonschema import Draft202012Validator
    if kind not in {'admit', 'advance', 'cancel', 'approve', 'definitionUpdate', 'clockJob', 'taskStartedContext', 'taskStartedJob', 'eventDeliveryCursor', 'eventSettle', 'projectUpdate'}:
        raise ValueError('Unknown automation storage input')
    schema = storage_schema()
    return Draft202012Validator({**schema, 'oneOf': [{'$ref': '#/$defs/' + kind}]})
