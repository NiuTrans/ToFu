"""Performance evidence requires equal work and samples from both arms."""
from __future__ import annotations

import pytest

from benchmarks.wire_fingerprint_compare import compare_case

pytestmark = pytest.mark.unit


def test_measurement_refuses_dropped_diagnostic_evidence():
    with pytest.raises(ValueError, match='changed diagnostic output'):
        compare_case(lambda *_: ['message.content'], lambda *_: [],
                     ([], []), samples=3, repeats=1)


def test_measurement_retains_complete_outputs_and_both_sample_series():
    result = compare_case(lambda *_: ['content', '…'], lambda *_: ['content', '…'],
                          ([], []), samples=3, repeats=2)
    assert result['output'] == ['content', '…']
    for arm in ('baseline', 'candidate'):
        assert len(result['samples'][arm]) == 3
        assert all(sample['cpu_ns'] >= 0 and sample['wall_ns'] >= 0
                   for sample in result['samples'][arm])
        assert result[arm]['peak_bytes'] >= 0


def test_measurement_refuses_a_baseline_that_is_the_candidate():
    """A frozen wire_fingerprint.py re-imports the live comparison owner, so
    its functions ARE the candidate's; that run would prove nothing."""
    def compare(*_):
        return []

    with pytest.raises(ValueError, match='same implementation'):
        compare_case(compare, compare, ([], []), samples=3, repeats=1)
