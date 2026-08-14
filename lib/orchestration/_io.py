"""Compatibility facade for the physically split Typed-I/O modules."""

from lib.orchestration.io_contract import (
    DEFAULT_OUTPUT_NAME,
    IO_AUTHORING_PRESETS,
    IO_PORT_NAME_CONTRACT,
    IO_START_REF,
    IO_TYPE_ORDER,
    MAX_IO_PORTS,
    VALID_IO_TYPES,
    io_contract_document_schema,
    io_contract_schema,
)
from lib.orchestration.io_validation import _validate_node_io
from lib.orchestration.io_values import (
    _coerce_list,
    node_output_names,
    parse_io_ref,
)

__all__ = [
    'IO_TYPE_ORDER', 'VALID_IO_TYPES', 'MAX_IO_PORTS',
    'IO_PORT_NAME_CONTRACT', 'DEFAULT_OUTPUT_NAME', 'IO_START_REF',
    'IO_AUTHORING_PRESETS', 'io_contract_schema',
    'io_contract_document_schema', '_coerce_list', 'node_output_names',
    'parse_io_ref', '_validate_node_io',
]
