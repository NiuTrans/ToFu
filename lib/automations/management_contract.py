"""Public management schemas resolved against the canonical definition authority.

Entry points: schema, fragment, validate, limits. No schema downloads, storage,
or workers run during discovery; OpenAPI receives self-contained local refs.
"""
from functools import lru_cache
import json
from pathlib import Path

from .conditions import check_json_bounds
from .contract import LIMITS, definition_schema
from .errors import AutomationError


@lru_cache(maxsize=1)
def _resolved_json():
    source = json.loads((Path(__file__).resolve().parents[2] /
                        'contracts/automation_management_v1.schema.json').read_text())
    definition = definition_schema()

    def rebase(value):
        if isinstance(value, dict):
            return {key: ('#/$defs/Definition_' + child[len('#/$defs/'):]
                          if key == '$ref' and child.startswith('#/$defs/') else rebase(child))
                    for key, child in value.items()}
        if isinstance(value, list):
            return [rebase(child) for child in value]
        return value

    source['$defs'].update({'Definition_' + key: rebase(value)
                            for key, value in definition.pop('$defs').items()})
    definition.pop('$id', None)
    definition.pop('$schema', None)
    source['$defs']['AutomationDefinition'] = rebase(definition)

    def resolve(value):
        if isinstance(value, dict):
            if value.get('$ref') == 'tofu.automation-action-output/task.show_time':
                from .registry import builtin_registry
                return builtin_registry().get('task.show_time').output_schema
            if value.get('$ref') == 'tofu.automation/v1':
                return {'$ref': '#/$defs/AutomationDefinition'}
            return {key: resolve(child) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child) for child in value]
        return value

    return json.dumps(resolve(source))


def schema():
    return json.loads(_resolved_json())


def fragment(name):
    from lib.openapi import schema_definition
    return schema_definition(schema(), name)


def limits():
    return schema()['x-limits']


@lru_cache(maxsize=16)
def _validator(name):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({**schema(), '$ref': '#/$defs/' + name})


def validate(name, value):
    # Reject adversarial nesting iteratively before recursive JSON Schema work.
    if name.endswith('Request'):
        check_json_bounds(value, max_depth=LIMITS['jsonDepth'] + 1,
                          max_nodes=LIMITS['jsonNodes'] + 16)
    error = next(_validator(name).iter_errors(value), None)
    if error:
        raise AutomationError('invalid_management_request', 'Invalid automation ' + name,
                              path='/' + '/'.join(map(str, error.absolute_path)))
