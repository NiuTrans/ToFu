"""The configure contract distinguishes an unbounded header wait from omission."""
import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.unit
SCHEMA = json.loads((Path(__file__).resolve().parents[2]
                    / 'contracts/native_agent_stdio_v1.schema.json').read_text())
POLICY = jsonschema.Draft202012Validator(SCHEMA['$defs']['configure']['properties']['policy'])


@pytest.mark.parametrize('value', [None, 1, 1000])
def test_explicit_header_budget_accepts_unbounded_or_positive_milliseconds(value):
    POLICY.validate({'request_headers_ms': value})


@pytest.mark.parametrize('policy', [
    {}, {'request_headers_ms': 0}, {'request_headers_ms': -1},
    {'request_headers_ms': True}, {'request_headers_ms': '1000'},
    {'request_headers_ms': 1.5}, {'request_headers_ms': 1 << 64},
])
def test_missing_or_malformed_header_policy_is_not_an_unbounded_wait(policy):
    with pytest.raises(jsonschema.ValidationError):
        POLICY.validate(policy)


@pytest.mark.parametrize('value', [None, 1, 1000, (1 << 64) - 1])
def test_physical_write_budget_accepts_null_or_positive_milliseconds(value):
    POLICY.validate({'request_headers_ms': None, 'write_ms': value})


@pytest.mark.parametrize('value', [0, -1, True, '1000', 1.5, 1 << 64])
def test_invalid_physical_write_budget_refuses(value):
    with pytest.raises(jsonschema.ValidationError):
        POLICY.validate({'request_headers_ms': None, 'write_ms': value})
