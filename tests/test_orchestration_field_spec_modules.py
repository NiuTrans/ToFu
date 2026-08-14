"""FieldSpec owner modules and compatibility facade contracts."""

from __future__ import annotations

import inspect

import pytest

import lib.orchestration._field_specs as field_spec_facade
from lib.orchestration import field_spec_contract
from lib.orchestration import field_spec_validation
from lib.orchestration import field_values
from lib.orchestration.field_issue_codes import field_client_failure_codes


pytestmark = pytest.mark.unit


def test_field_spec_facade_has_no_implementation_and_preserves_identity():
    assert '\ndef ' not in inspect.getsource(field_spec_facade)
    assert field_spec_facade.field_spec is field_spec_contract.field_spec
    assert field_spec_facade.field_spec_schema is \
        field_spec_contract.field_spec_schema
    assert field_spec_facade.field_value_contract is \
        field_values.field_value_contract
    assert field_spec_facade.canonicalize_field_params is \
        field_values.canonicalize_field_params
    assert field_spec_facade.validate_field_specs is \
        field_spec_validation.validate_field_specs


def test_field_value_contract_publishes_backend_diagnostic_codes():
    contract = field_values.field_value_contract()

    assert contract['failureCodes'] == field_client_failure_codes()
    assert contract['failureCodes'] == {
        'unsupportedContract': 'field.contract.unsupported',
        'invalidNumber': 'field.type.integer',
        'invalidBoolean': 'field.type.boolean',
        'maxLength': 'field.max_length',
        'maxItems': 'field.max_items',
        'maxItemLength': 'field.max_item_length',
    }
    contract['failureCodes']['maxItems'] = 'consumer.changed'
    assert field_values.field_value_contract()['failureCodes'][
        'maxItems'] == 'field.max_items'
