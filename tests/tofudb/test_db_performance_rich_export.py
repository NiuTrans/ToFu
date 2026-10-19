"""Measured reports export native UI data without hiding failed/unknown gates."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('dbcat_rich', ROOT / 'scripts/db_performance_catalog.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SCHEMA = json.loads((ROOT / 'contracts/rich_response_v1.schema.json').read_text())
pytestmark = pytest.mark.unit


def measured_report(gate):
    return {'baseline': 'sqlite', 'candidate': 'native', 'scope': 'fixed cohort',
            'limits': ['Warm only; no process-crash coverage.'],
            'metrics': [{'metric': 'stream_p99', 'unit': 'ms', 'direction': 'lower',
                         'baseline_value': 2700, 'candidate_value': 1267 if gate is False else 999,
                         'candidate': {'windows': [
                             {'gates': {'correctness': True, 'coverage': True, 'budget': gate},
                              'raw': {'budget_ms': 1000}}]}}]}


@pytest.mark.parametrize('gate,status', [(True, 'passed'), (False, 'failed'), (None, 'unknown')])
def test_export_matches_schema_and_keeps_absolute_acceptance(gate, status):
    report = measured_report(gate)
    before = copy.deepcopy(report)
    payload = MODULE.render_rich_response(report)
    Draft202012Validator(SCHEMA).validate(payload)
    metric = payload['blocks'][0]['metrics'][0]
    assert metric['status'] == status
    assert metric['candidate'] == (1267 if gate is False else 999)
    assert metric['budget'] == 1000
    assert report == before
    assert 'Relative improvement' in payload['summary']


def test_partial_coverage_never_became_green():
    report = measured_report(True)
    report['metrics'][0]['candidate']['windows'][0]['gates']['coverage'] = None
    assert MODULE.render_rich_response(report)['blocks'][0]['metrics'][0]['status'] == 'unknown'


def test_multiple_windows_budget_disagreement_not_invented():
    report = measured_report(False)
    next_window = copy.deepcopy(report['metrics'][0]['candidate']['windows'][0])
    next_window['raw']['budget_ms'] = 500
    report['metrics'][0]['candidate']['windows'].append(next_window)
    assert 'budget' not in MODULE.render_rich_response(report)['blocks'][0]['metrics'][0]


def test_no_zero_or_nonfinite_value_repair():
    report = measured_report(False)
    report['metrics'][0]['candidate_value'] = float('inf')
    with pytest.raises(MODULE.CatalogError):
        MODULE.render_rich_response(report)


def test_many_metrics_split_into_bounded_cards():
    report = measured_report(False)
    report['metrics'] *= 33
    payload = MODULE.render_rich_response(report)
    assert [len(b['metrics']) for b in payload['blocks'] if b['type'] == 'comparison'] == [32, 1]
    report['metrics'] *= 24
    with pytest.raises(MODULE.CatalogError):
        MODULE.render_rich_response(report)


def test_export_has_no_local_asset_urls_or_model_scripts():
    text = json.dumps(MODULE.render_rich_response(measured_report(False)))
    assert '<script' not in text
    assert 'data/benchmarks' not in text
    assert 'image_gallery' not in text
