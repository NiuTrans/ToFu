"""Stored-definition response projection and write concurrency protocol."""

from __future__ import annotations

import copy

from lib.orchestration.definition_contract_registry import (
    definition_write_contract,
)
from lib.orchestration.definition_version import (
    is_definition_version,
    require_definition_version,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_fields,
)
from lib.orchestration.wire_formats import (
    DEFINITION_ENTRY_FORMAT,
    DEFINITION_LIST_FORMAT,
)
def definition_entry_summary(entry: dict) -> dict | None:
    """Project one stored entry into the stable definition-list shape."""
    if not isinstance(entry, dict):
        return None
    orchestration_id = entry.get('id')
    if not isinstance(orchestration_id, str) or not orchestration_id:
        return None
    definition = entry.get('definition')
    definition = definition if isinstance(definition, dict) else {}
    nodes = definition.get('nodes')
    name = entry.get('name')
    if not isinstance(name, str):
        name = definition.get('name')
    summary = {
        'id': orchestration_id,
        'name': name if isinstance(name, str) else '',
        'nodeCount': len(nodes) if isinstance(nodes, list) else 0,
        'createdAt': None,
        'updatedAt': None,
    }
    for field in ('createdAt', 'updatedAt'):
        value = entry.get(field)
        if is_definition_version(value):
            summary[field] = value
    return summary


def project_definition_list(items: list[dict]) -> dict:
    return {
        'format': DEFINITION_LIST_FORMAT,
        'items': copy.deepcopy(items),
    }


def project_definition_entry(
    entry: dict,
    *,
    inspection: dict | None = None,
) -> dict:
    response = copy.deepcopy(entry)
    response['format'] = DEFINITION_ENTRY_FORMAT
    if inspection is not None:
        response.update(inspection_response_fields(inspection))
    return response


def parse_definition_write_precondition(raw_value: str | None) -> int | None:
    """Parse the definition ``If-Match`` token without HTTP dependencies."""
    if raw_value is None:
        return None
    token = str(raw_value).strip()
    if token.startswith('W/'):
        token = token[2:].strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    contract = definition_write_contract()
    header = contract['preconditionHeader']
    version_field = contract['versionField']
    if not token or not token.isascii() or not token.isdigit():
        raise ValueError(
            f'{header} must be a quoted {version_field} integer')
    version = int(token)
    if not is_definition_version(version):
        raise ValueError(
            f'{header} version exceeds the safe integer range')
    return version


def definition_write_version_token(version: int) -> str:
    """Format one response/precondition token from the published syntax."""
    version = require_definition_version(version)
    syntax = definition_write_contract()['tokenSyntax']
    if syntax == 'quoted-decimal':
        return f'"{version}"'
    raise ValueError(f'unsupported definition token syntax {syntax!r}')


def definition_write_conflict(
    expected_updated_at: int | None,
    current_updated_at: int | None,
    *,
    operation: str = 'replace',
) -> dict:
    contract = definition_write_contract()
    if operation not in contract['operations']:
        raise ValueError(f'unsupported definition write operation {operation!r}')
    reason = contract['conflictReason']
    fields = {
        semantic: spec['name']
        for semantic, spec in contract['conflictFields'].items()
    }
    expected_updated_at = require_definition_version(
        expected_updated_at, field=fields['expectedUpdatedAt'], nullable=True)
    current_updated_at = require_definition_version(
        current_updated_at, field=fields['currentUpdatedAt'], nullable=True)
    return {
        'conflict': reason,
        'write': {
            fields['format']: contract['format'],
            fields['reason']: reason,
            fields['operation']: operation,
            fields['expectedUpdatedAt']: expected_updated_at,
            fields['currentUpdatedAt']: current_updated_at,
        },
        fields['currentUpdatedAt']: current_updated_at,
    }


__all__ = [
    'definition_entry_summary',
    'definition_write_conflict',
    'definition_write_version_token',
    'parse_definition_write_precondition',
    'project_definition_entry',
    'project_definition_list',
]
