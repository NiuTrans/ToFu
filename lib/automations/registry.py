"""Explicit action-contract catalogue; never resolves payloads to Python names.

Entry points: ActionContract, ActionRegistry, builtin_registry. Contracts are
immutable serialized schemas; executors register implementations separately.
Dependencies: standard library, JSON Schema, and the shipped action catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable

from .errors import AutomationError


@dataclass(frozen=True, slots=True)
class ActionContract:
    id: str
    version: int
    input_schema_json: str
    output_schema_json: str
    effect: str
    recovery: str
    required_scopes: tuple[str, ...]

    @property
    def input_schema(self) -> dict:
        return json.loads(self.input_schema_json)

    @property
    def output_schema(self) -> dict:
        return json.loads(self.output_schema_json)

    @classmethod
    def from_document(cls, value: dict) -> ActionContract:
        from jsonschema import Draft202012Validator, SchemaError
        from .conditions import check_json_bounds

        check_json_bounds(value)
        if not isinstance(value, dict) or set(value) != {
            'id', 'version', 'input', 'output', 'effect', 'recovery', 'requiredScopes',
        }:
            raise AutomationError('invalid_action_contract', 'Invalid action descriptor')
        if (not isinstance(value['id'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', value['id'])
                or type(value['version']) is not int or value['version'] < 1
                or value['effect'] not in {'read', 'presentation', 'write', 'agent'}
                or value['recovery'] not in {'replay', 'reconcile'}):
            raise AutomationError('invalid_action_contract', 'Invalid action identity or effect policy')
        scopes = value['requiredScopes']
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(s, str) or not 1 <= len(s) <= 128 for s in scopes)
                or len(set(scopes)) != len(scopes)):
            raise AutomationError('invalid_action_contract', 'Action requires explicit unique scopes')
        if value['effect'] in {'write', 'agent'} and value['recovery'] != 'reconcile':
            raise AutomationError('unsafe_action_recovery', 'External effects must declare reconciliation')
        for schema in (value['input'], value['output']):
            if not isinstance(schema, dict):
                raise AutomationError('invalid_action_contract', 'Action schema must be an object')
            pending = [schema]
            while pending:
                node = pending.pop()
                if isinstance(node, dict):
                    if any(key in node for key in ('$id', '$anchor', '$dynamicAnchor')):
                        raise AutomationError('invalid_action_contract', 'Action schemas cannot redefine resource scope')
                    for key in ('$ref', '$dynamicRef'):
                        if key in node and (not isinstance(node[key], str) or not node[key].startswith('#/')):
                            raise AutomationError('invalid_action_contract', 'Action schemas cannot fetch remote resources')
                    pending.extend(node.values())
                elif isinstance(node, list):
                    pending.extend(node)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                raise AutomationError('invalid_action_contract', 'Malformed action schema') from exc
        return cls(value['id'], value['version'],
                   json.dumps(value['input'], allow_nan=False, sort_keys=True),
                   json.dumps(value['output'], allow_nan=False, sort_keys=True),
                   value['effect'], value['recovery'], tuple(scopes))

    def validate(self, value: Any, *, output: bool = False) -> None:
        from jsonschema import Draft202012Validator
        from .conditions import check_json_bounds

        check_json_bounds(value)
        schema = self.output_schema if output else self.input_schema
        error = next(Draft202012Validator(schema).iter_errors(value), None)
        if error is not None:
            raise AutomationError('invalid_action_output' if output else 'invalid_action_arguments',
                                  error.message, path='/' + '/'.join(map(str, error.absolute_path)))

    def document(self) -> dict:
        return {'id': self.id, 'version': self.version, 'input': self.input_schema,
                'output': self.output_schema, 'effect': self.effect,
                'recovery': self.recovery, 'requiredScopes': list(self.required_scopes)}


class ActionRegistry:
    """Closed, immutable catalogue assembled at the application boundary."""

    def __init__(self, contracts: Iterable[ActionContract]):
        catalogue = {}
        for contract in contracts:
            if not isinstance(contract, ActionContract) or contract.id in catalogue:
                raise AutomationError('duplicate_action', 'Action registrations must be unique contracts')
            # Dataclass construction is not admission: extension code can
            # construct a malformed descriptor directly. Revalidate at the
            # registry boundary before exposing a callable action identity.
            try:
                catalogue[contract.id] = ActionContract.from_document(contract.document())
            except (json.JSONDecodeError, TypeError) as exc:
                raise AutomationError('invalid_action_contract', 'Malformed action registration') from exc
        self._catalogue = MappingProxyType(catalogue)

    def get(self, action_id: str) -> ActionContract:
        try:
            return self._catalogue[action_id]
        except KeyError as exc:
            raise AutomationError('unknown_action', f'Unregistered action: {action_id}') from exc

    def documents(self) -> list[dict]:
        return [self._catalogue[key].document() for key in sorted(self._catalogue)]


@lru_cache(maxsize=1)
def builtin_registry() -> ActionRegistry:
    path = Path(__file__).resolve().parents[2] / 'contracts' / 'automation_actions_v1.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(document, dict) or set(document) != {'schemaVersion', 'contract', 'actions'}
            or type(document['schemaVersion']) is not int or document['schemaVersion'] != 1
            or document['contract'] != 'tofu.automation-actions/v1'
            or not isinstance(document['actions'], list)):
        raise AutomationError('invalid_action_catalogue', 'Unsupported automation action catalogue')
    return ActionRegistry(ActionContract.from_document(item) for item in document['actions'])
