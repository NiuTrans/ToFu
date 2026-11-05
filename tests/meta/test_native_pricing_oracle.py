"""The retained native pricing oracle stays executable against its Python owner."""

import json

import pytest

pytestmark = pytest.mark.unit


def test_canonical_price_and_billing_cases_match_retained_native_evidence():
    from scripts import gen_rust_pricing_oracle as oracle

    expected = json.loads(oracle.TARGET.read_text())
    actual = oracle.capture()

    assert actual == expected
    assert actual['cases'] and actual['tier_cases'] and actual['billing_cases']
