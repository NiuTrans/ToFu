"""FieldSpec owner-module contracts."""

from __future__ import annotations

import pytest

import lib.orchestration.field_values as field_values
from lib.orchestration.field_issue_codes import field_client_failure_codes


pytestmark = pytest.mark.unit


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
