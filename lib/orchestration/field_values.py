"""Canonical FieldSpec values for saved orchestration definitions.

The validator owns which values are legal; this module owns the successful
write projection. Keeping that distinction lets rolling clients retain useful
validation diagnostics while ensuring every persisted definition has one
stable representation regardless of whether it came from Studio, REST, or an
AI composer.
"""

from __future__ import annotations

import copy

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration._control_specs import control_param_schema
from lib.orchestration.field_issue_codes import field_client_failure_codes
from lib.orchestration.io_values import _coerce_list
from lib.orchestration._role_specs import role_param_schema
from lib.orchestration.wire_formats import FIELD_VALUE_FORMAT


_OMIT = object()


def field_value_contract() -> dict:
    """Describe canonical FieldSpec values at definition write boundaries."""
    return {
        'format': FIELD_VALUE_FORMAT,
        'optionalEmpty': 'omit',
        'failureCodes': field_client_failure_codes(),
        'kinds': {
            'text': {'wire': 'string'},
            'textarea': {'wire': 'string'},
            'select': {'wire': 'declared option'},
            'list': {
                'wire': 'array<string>',
                'editor': 'newline',
                'trimItems': True,
                'dropEmptyItems': True,
            },
            'int': {'wire': 'integer', 'finite': True},
            'bool': {'wire': 'boolean'},
        },
    }


def field_value_contract_schema() -> dict:
    """Describe the canonical persisted FieldSpec value policy."""
    return contract_snapshot_schema(field_value_contract())


def _canonical_field_value(kind: str, value):
    """Canonicalize one already-validated FieldSpec value."""
    if value is None or value == '':
        return _OMIT
    if kind == 'list':
        items = _coerce_list(value)
        return items if items else _OMIT
    return copy.deepcopy(value)


def canonicalize_field_params(schema: list[dict], params: dict) -> dict:
    """Return detached params with known FieldSpec values canonicalized."""
    if not isinstance(params, dict):
        return {}
    result = copy.deepcopy(params)
    for spec in schema:
        key = spec.get('key')
        if not key or key not in params:
            continue
        value = _canonical_field_value(spec.get('kind') or 'text', params[key])
        if value is _OMIT:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def canonicalize_definition_field_values(definition: dict) -> dict:
    """Return a detached definition with canonical known parameter values.

    Embedded subflow definitions are normalized recursively. Graph structure,
    unknown fields, execution axes and Typed-I/O metadata are left untouched.
    """
    snapshot = copy.deepcopy(definition)
    if not isinstance(snapshot, dict):
        return {}
    nodes = snapshot.get('nodes')
    if not isinstance(nodes, list):
        return snapshot

    for node in nodes:
        if not isinstance(node, dict):
            continue
        params = node.get('params')
        if not isinstance(params, dict):
            continue
        if node.get('type') == 'role':
            params = canonicalize_field_params(
                role_param_schema(str(node.get('role') or '')), params)
        elif node.get('type') == 'control':
            params = canonicalize_field_params(
                control_param_schema(str(node.get('kind') or '')), params)

        child = params.get('definition') if node.get('type') == 'subflow' \
            else None
        if isinstance(child, dict):
            params['definition'] = canonicalize_definition_field_values(child)
        node['params'] = params
    return snapshot


__all__ = [
    'field_value_contract', 'field_value_contract_schema',
    'canonicalize_field_params', 'canonicalize_definition_field_values',
]
