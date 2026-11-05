"""Research throughput comparisons preserve human effort, quality and coverage."""
from copy import deepcopy
import json
import math

import pytest

from benchmarks.research_quality_compare import main
from lib.research.quality_comparison import compare_quality
from tests.research.test_research_quality_comparison import versioned_pair

pytestmark = pytest.mark.unit


def measured_pair(identity='one'):
    row = versioned_pair(identity)
    for name, wall, human, count in [('control', 3600, 600, 5), ('treatment', 1800, 120, 1)]:
        row[name]['efficiency'] = {
            'format': 'tofu.research-efficiency/v1',
            'measurement_protocol_digest': 'e' * 64,
            'evidence_refs': [f'measurement:{identity}:{name}'],
            'wall_seconds': wall, 'human_active_seconds': human,
            'human_interventions': count}
    return row


def test_paired_efficiency_reports_raw_units_and_keeps_quality_visible():
    row = measured_pair()
    row['treatment']['metrics']['supported_claim_precision'] = 0.25
    before = deepcopy(row)
    report = compare_quality([row])
    assert row == before
    assert report['mean_paired_difference']['supported_claim_precision'] == -0.25
    efficiency = report['efficiency']
    assert efficiency['status'] == 'descriptive_only'
    assert efficiency['measured_pair_count'] == 1
    assert efficiency['measurement_protocol_digest'] == 'e' * 64
    for key, difference in [('wall_seconds', -1800), ('human_active_seconds', -480),
                            ('human_interventions', -4)]:
        assert efficiency['paired_metric_summary'][key]['mean'] == difference
        assert efficiency['paired_metric_summary'][key]['standard_error'] is None
    assert efficiency['valid_pairs'][0]['control']['human_interventions'] == 5
    assert efficiency['quality_regression_pair_ids'] == ['one']


def test_legacy_quality_never_implies_zero_human_effort():
    report = compare_quality([versioned_pair()])
    assert report['valid_pair_count'] == 1
    assert report['efficiency']['status'] == 'unavailable'
    assert report['efficiency']['measured_pair_count'] == 0
    assert report['efficiency']['paired_metric_summary'] == {}


@pytest.mark.parametrize('key,value', [
    ('wall_seconds', 0), ('wall_seconds', float('inf')), ('wall_seconds', True),
    ('human_active_seconds', -1), ('human_active_seconds', float('nan')),
    ('human_active_seconds', 10**400), ('human_interventions', 0.5),
    ('human_interventions', True), ('human_interventions', -1),
    ('measurement_protocol_digest', ''), ('measurement_protocol_digest', 'A'*64),
    ('format', 'unknown'), ('evidence_refs', []), ('evidence_refs', [' ']),
])
def test_invalid_measurements_cannot_become_throughput_evidence(key, value):
    row = measured_pair()
    row['treatment']['efficiency'][key] = value
    report = compare_quality([row])
    assert report['valid_pair_count'] == 1
    assert report['efficiency']['status'] == 'incomplete'
    assert report['efficiency']['measured_pair_count'] == 0
    assert report['efficiency']['excluded_pairs']


def test_explicit_zero_effort_is_measured_and_missing_effort_is_unknown():
    row = measured_pair()
    row['treatment']['efficiency'].update(human_active_seconds=0, human_interventions=0)
    assert compare_quality([row])['efficiency']['status'] == 'descriptive_only'
    del row['treatment']['efficiency']['human_interventions']
    assert compare_quality([row])['efficiency']['status'] == 'incomplete'


@pytest.mark.parametrize('change', ['missing_arm', 'missing_pair', 'protocol', 'generation', 'quality'])
def test_partial_or_incomparable_measurements_remain_visible(change):
    rows = [measured_pair('one'), measured_pair('two')]
    if change == 'missing_arm':
        del rows[1]['treatment']['efficiency']
    elif change == 'missing_pair':
        for arm in ('control', 'treatment'):
            del rows[1][arm]['efficiency']
    elif change == 'protocol':
        rows[1]['treatment']['efficiency']['measurement_protocol_digest'] = 'f'*64
    elif change == 'generation':
        for row in rows:
            for arm in ('control', 'treatment'):
                del row[arm]['system_digest']
    else:
        rows[1]['treatment']['adjudication_kind'] = 'model_self_score'
    result = compare_quality(rows)['efficiency']
    assert result['status'] == 'incomplete'
    assert result['submitted_pair_count'] == 2
    assert result['measured_pair_count'] + len(result['excluded_pairs']) == 2


def test_different_measurement_cohorts_cannot_be_pooled():
    rows = [measured_pair('one'), measured_pair('two')]
    for arm in ('control', 'treatment'):
        rows[1][arm]['efficiency']['measurement_protocol_digest'] = 'f'*64
    result = compare_quality(rows)['efficiency']
    assert result['status'] == 'incomplete'
    assert result['measured_pair_count'] == 0
    assert result['paired_metric_summary'] == {}
    assert all('mixed_measurement_protocols' in row['reasons'] for row in result['excluded_pairs'])


def test_large_finite_measurements_keep_statistics_finite():
    rows = [measured_pair('one'), measured_pair('two')]
    rows[0]['control']['efficiency']['wall_seconds'] = 1e308
    rows[1]['treatment']['efficiency']['wall_seconds'] = 1e308
    result = compare_quality(rows)['efficiency']
    assert result['status'] == 'descriptive_only'
    summary = result['paired_metric_summary']['wall_seconds']
    assert all(math.isfinite(value) for value in summary.values())
    assert summary['mean'] == 0
    assert summary['standard_error'] == pytest.approx(1e308)


def test_finite_integer_measurements_handle_large_intermediate_deviations():
    rows = [measured_pair(str(index)) for index in range(20)]
    for index, row in enumerate(rows):
        row['control']['efficiency']['human_interventions'] = 10**308 if index < 19 else 0
        row['treatment']['efficiency']['human_interventions'] = 0 if index < 19 else 10**308
    result = compare_quality(rows)['efficiency']
    assert result['status'] == 'descriptive_only'
    summary = result['paired_metric_summary']['human_interventions']
    assert all(math.isfinite(value) for value in summary.values())
    assert summary['standard_error'] == pytest.approx(1e307)


def test_cli_retains_incomplete_efficiency_with_unsuccessful_exit(tmp_path):
    row = measured_pair()
    del row['treatment']['efficiency']
    source = tmp_path / 'input.json'
    source.write_text(json.dumps([row]))
    target = tmp_path / 'report.json'
    assert main([str(source), '--output', str(target)]) == 3
    result = json.loads(target.read_text())
    assert result['comparison']['valid_pair_count'] == 1
    assert result['comparison']['efficiency']['status'] == 'incomplete'


@pytest.mark.parametrize('measured', [False, True])
def test_cli_can_require_measurements_without_breaking_legacy_comparisons(tmp_path, measured):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps([measured_pair() if measured else versioned_pair()]))
    target = tmp_path / 'report.json'
    assert main([str(source), '--output', str(target)]) == 0
    assert main([str(source), '--output', str(target), '--require-efficiency']) == (0 if measured else 3)
    assert json.loads(target.read_text())['efficiency_required'] is True
