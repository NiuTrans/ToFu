"""Compatibility facade for the physically split FieldSpec modules."""

from lib.orchestration.field_spec_contract import (
    VALID_PARAM_KINDS,
    field_spec,
    field_spec_list_schema,
    field_spec_registry_schema,
    field_spec_schema,
)
from lib.orchestration.field_spec_validation import validate_field_specs
from lib.orchestration.field_values import (
    canonicalize_field_params,
    field_value_contract,
    field_value_contract_schema,
)

__all__ = [
    'VALID_PARAM_KINDS', 'field_spec', 'field_spec_schema',
    'field_spec_list_schema', 'field_spec_registry_schema',
    'field_value_contract', 'field_value_contract_schema',
    'canonicalize_field_params', 'validate_field_specs',
]
