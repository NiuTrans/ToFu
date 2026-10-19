"""Complete point-read validation must reject missing metadata and corrupt values."""
from copy import deepcopy
import pytest
from benchmarks.tofudb_cached_read import record_value, validate_read

pytestmark = pytest.mark.unit


def test_read_oracle_rejects_missing_fields_changed_version_and_nested_content():
    expected = {"version": 1, "updated_at_ms": 1800000000000,
                "value": record_value(7, 128)}
    validate_read(deepcopy(expected), expected)
    candidates = []
    for key in expected:
        candidate = deepcopy(expected)
        del candidate[key]
        candidates.append(candidate)
    for key in ("version", "updated_at_ms"):
        candidate = deepcopy(expected)
        candidate[key] += 1
        candidates.append(candidate)
    candidate = deepcopy(expected)
    candidate["value"]["nested"][2]["sequence"] += 1
    candidates.append(candidate)
    for actual in candidates:
        with pytest.raises(ValueError, match="complete record response"):
            validate_read(actual, expected)


def test_fixture_is_deterministic_distinguishes_keys_and_contains_real_escapes():
    value = record_value(7, 1024)
    assert record_value(7, 1024) == value
    assert record_value(8, 1024) != value
    assert '\n' in value["content"] and '\\' in value["content"] and '"' in value["content"]
    assert 1020 <= len(value["content"].encode()) <= 1024
