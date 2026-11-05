"""Chart measurements need evidence regardless of magnitude or page count."""

import pytest
import yaml

from lib.slides.author import _brief_fidelity_findings

pytestmark = pytest.mark.unit


def _chart(values):
    return yaml.safe_dump({'elements': [{
        'elementType': 'chart', 'data': {
            'categories': [str(index) for index in range(len(values))],
            'series': [{'name': 'Measured', 'values': values}],
        },
    }]})


@pytest.mark.parametrize('value', [0, 1, 3, 12, 20, 85])
def test_page_numbers_and_small_values_do_not_authorize_measurements(value):
    findings = _brief_fidelity_findings(
        {'content_notes': 'No measurements supplied; source [S1].'},
        _chart([value]), page_index=2, total=12)
    assert findings, f'unsupported measurement {value} was accepted'


@pytest.mark.parametrize(('evidence', 'values'), [
    ('Loss changed by -2.5 and +3.2.', [-2.5, 3.2]),
    ('Sample size: 1,200. Learning rate: 1e-3.', [1200, 0.001]),
    ('误差为0.5，基线为0，实验结果为12。', [0.5, 0, 12]),
])
def test_signed_grouped_and_scientific_measurements_keep_their_values(evidence, values):
    assert _brief_fidelity_findings(
        {'sources': [{'point': evidence}]}, _chart(values),
        page_index=0, total=3) == []


def test_negative_measurement_does_not_authorize_opposite_result():
    assert _brief_fidelity_findings(
        {'content_notes': 'Effect was -2.5%.'}, _chart([2.5]),
        page_index=0, total=3)
