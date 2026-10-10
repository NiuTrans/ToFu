"""Pure definition admission and dependency compilation.

Entry point: prepare_definition. The compiled snapshot owns its action versions
and dependency order. JSON Schema and action contracts are the only shape
authorities; no storage, subprocess, model, or browser work occurs here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .conditions import check_json_bounds
from .contract import LIMITS, definition_schema
from .errors import AutomationError
from .registry import ActionRegistry, builtin_registry

MAX_DEFINITION_BYTES = LIMITS['definitionBytes']


def _sources(value):
    if isinstance(value, dict):
        if '$source' in value:
            if set(value) != {'$source'}:
                raise AutomationError('invalid_source', 'References cannot contain literal fields')
            yield value['$source']
        elif 'source' in value and 'operator' in value:
            yield value['source']
        else:
            for child in value.values():
                yield from _sources(child)
    elif isinstance(value, list):
        for child in value:
            yield from _sources(child)


def _reference_schema(schema: dict) -> dict:
    """Allow a typed runtime source at any argument position.

    Concrete values still pass the action schema. Resolved arguments MUST pass
    the original schema before execution; reference acceptance grants no tool
    authority and cannot make an invalid runtime value executable.
    """
    if not isinstance(schema, dict):
        return schema
    literal = deepcopy(schema)
    if 'properties' in literal:
        literal['properties'] = {key: _reference_schema(child)
                                 for key, child in literal['properties'].items()}
    if isinstance(literal.get('items'), dict):
        literal['items'] = _reference_schema(literal['items'])
    for keyword in ('oneOf', 'anyOf', 'allOf'):
        if keyword in literal:
            literal[keyword] = [_reference_schema(child) for child in literal[keyword]]
    result = {'anyOf': [literal, {
        'type': 'object', 'additionalProperties': False, 'required': ['$source'],
        'properties': {'$source': deepcopy(definition_schema()['$defs']['source'])},
    }]}
    if '$defs' in schema:
        result['$defs'] = {key: _reference_schema(child) for key, child in schema['$defs'].items()}
    return result


@dataclass(frozen=True, slots=True)
class PreparedDefinition:
    """Serialized immutable executable data; properties return owned copies."""

    definition_json: str
    ordered_step_ids: tuple[str, ...]
    action_contracts_json: str
    digest: str

    @property
    def definition(self) -> dict:
        return json.loads(self.definition_json)

    @property
    def action_contracts(self) -> dict:
        return json.loads(self.action_contracts_json)


def prepare_definition(value: dict, registry: ActionRegistry | None = None) -> PreparedDefinition:
    from jsonschema import Draft202012Validator

    check_json_bounds(value)
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_DEFINITION_BYTES:
        raise AutomationError('definition_too_large', 'Automation definition exceeds 128 KiB')
    error = next(Draft202012Validator(definition_schema()).iter_errors(value), None)
    if error is not None:
        raise AutomationError('invalid_definition', error.message,
                              path='/' + '/'.join(map(str, error.absolute_path)))
    if not value['name'].strip():
        raise AutomationError('invalid_definition', 'Name cannot be whitespace', path='/name')
    if value['trigger']['kind'] == 'schedule':
        try:
            ZoneInfo(value['trigger']['timezone'])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AutomationError('invalid_timezone', 'Unknown IANA timezone', path='/trigger/timezone') from exc
        from lib.scheduler.calendar import CronSchedule
        try:
            CronSchedule(value['trigger']['schedule'], value['trigger']['timezone'])
        except ValueError as exc:
            raise AutomationError('invalid_schedule', str(exc), path='/trigger/schedule') from exc
    registry = registry or builtin_registry()
    steps = {}
    for step in value['steps']:
        if step['id'] in steps:
            raise AutomationError('duplicate_step', f'Duplicate step: {step["id"]}')
        steps[step['id']] = step
    for step in steps.values():
        if any(parent not in steps for parent in step['after']):
            raise AutomationError('unknown_dependency', f'Unknown dependency for {step["id"]}')
    ordered = []
    ancestors = {}
    while len(ordered) < len(steps):
        ready = [key for key, step in steps.items()
                 if key not in ancestors and all(parent in ancestors for parent in step['after'])]
        if not ready:
            raise AutomationError('dependency_cycle', 'Automation steps must form an acyclic graph')
        for key in ready:
            ancestors[key] = set(steps[key]['after'])
            for parent in steps[key]['after']:
                ancestors[key].update(ancestors[parent])
            ordered.append(key)
    contracts = {}
    source_validator = Draft202012Validator(definition_schema()['$defs']['source'])
    for key in ordered:
        step = steps[key]
        contract = registry.get(step['action'])
        contracts[contract.id] = contract.document()
        argument_error = next(Draft202012Validator(_reference_schema(contract.input_schema))
                              .iter_errors(step['arguments']), None)
        if argument_error is not None:
            raise AutomationError('invalid_action_arguments', argument_error.message,
                                  path=f'/steps/{key}/arguments')
        for source in _sources(step['arguments']):
            source_validator.validate(source)
            if source['from'] != 'event' and source['from'][6:] not in ancestors[key]:
                raise AutomationError('unbound_source', f'{key} references a step outside its dependencies')
        for source in _sources(step.get('when', True)):
            if source['from'] != 'event' and source['from'][6:] not in ancestors[key]:
                raise AutomationError('unbound_source', f'{key} condition references a step outside its dependencies')
    if any(item['effect'] == 'agent' for item in contracts.values()) and value['policy']['maxModelTokens'] == 0:
        raise AutomationError('agent_budget_missing', 'Agent actions require a positive token budget')
    action_json = json.dumps(contracts, allow_nan=False, sort_keys=True, separators=(',', ':'))
    return PreparedDefinition(encoded, tuple(ordered), action_json,
                              hashlib.sha256((encoded + '\n' + action_json).encode('utf-8')).hexdigest())
