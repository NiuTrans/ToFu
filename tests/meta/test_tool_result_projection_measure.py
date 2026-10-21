"""The documented offline benchmark retains evidence without application storage."""
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def measurement():
    source = Path(__file__).parents[2] / 'benchmarks/tool_result_projection.py'
    spec = importlib.util.spec_from_file_location('projection_measure_fixture', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_documented_projection_measurement_checks_exact_canonical_evidence(measurement):
    report = measurement.measure()
    assert report['scope'] == 'six fixed synthetic native receipts, production capture/budget/delivery'
    assert report['records'] and report['after_utf8_bytes'] < report['before_utf8_bytes']
    assert report['provider_wire_verified'] is False
    assert report['source_authority_changed'] is False
    for record in report['records']:
        assert record['canonical_before_sha256'] == record['source_sha256']
        assert record['canonical_after_sha256'] == record['source_sha256']
        assert record['control_view_complete'] and record['candidate_view_complete']


@pytest.mark.parametrize('corruption', ['canonical', 'clipped_view'])
def test_measurement_refuses_savings_that_lose_evidence(measurement, monkeypatch, corruption):
    if corruption == 'canonical':
        original = measurement.budget_tool_result_v2

        def corrupt(*args, **kwargs):
            value = original(*args, **kwargs)
            value.canonical = SimpleNamespace(text=lambda: 'lost original evidence')
            return value

        monkeypatch.setattr(measurement, 'budget_tool_result_v2', corrupt)
    else:
        original = measurement.split_tool_result_delivery

        def corrupt(value):
            delivery = original(value)
            return replace(delivery, evidence={**delivery.evidence, 'truncated': True})

        monkeypatch.setattr(measurement, 'split_tool_result_delivery', corrupt)
    with pytest.raises(AssertionError):
        measurement.measure()
