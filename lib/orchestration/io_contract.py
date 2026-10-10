"""Backend-owned Typed-I/O constants and authoring contract projection."""

from __future__ import annotations

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.io_issue_codes import io_client_failure_codes


IO_TYPE_ORDER = ('text', 'json', 'artifact', 'file', 'number', 'bool', 'any')
VALID_IO_TYPES = frozenset(IO_TYPE_ORDER)
MAX_IO_PORTS = 12
IO_PORT_NAME_CONTRACT = {
    'required': True,
    'uniqueWithinSide': True,
}
DEFAULT_OUTPUT_NAME = 'text'
IO_START_REF = 'start'
IO_AUTHORING_PRESETS = {
    'toolHeavyWorker': {
        'appliesTo': ['role'],
        'outputs': [
            {'name': 'summary', 'type': 'text'},
            {'name': 'changes', 'type': 'artifact'},
        ],
    },
}


def io_contract_schema() -> dict:
    """Return a detached, serializable Typed-I/O authoring contract."""
    return {
        'types': list(IO_TYPE_ORDER),
        'maxPorts': MAX_IO_PORTS,
        'portName': dict(IO_PORT_NAME_CONTRACT),
        'failureCodes': io_client_failure_codes(),
        'defaultOutput': {'name': DEFAULT_OUTPUT_NAME, 'type': 'text'},
        'startRef': IO_START_REF,
        'presets': {
            name: {
                'appliesTo': list(spec.get('appliesTo') or []),
                'outputs': [dict(port) for port in spec.get('outputs') or []],
            }
            for name, spec in IO_AUTHORING_PRESETS.items()
        },
    }


def io_contract_document_schema() -> dict:
    """Describe the serializable Typed-I/O contract metadata."""
    return contract_snapshot_schema(io_contract_schema())


__all__ = [
    'IO_TYPE_ORDER', 'VALID_IO_TYPES', 'MAX_IO_PORTS',
    'IO_PORT_NAME_CONTRACT', 'DEFAULT_OUTPUT_NAME', 'IO_START_REF',
    'IO_AUTHORING_PRESETS', 'io_contract_schema',
    'io_contract_document_schema',
]
