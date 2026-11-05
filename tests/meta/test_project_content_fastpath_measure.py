"""Search measurements refuse missing work before reporting a speedup."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_spec = importlib.util.spec_from_file_location(
    'project_content_measure', Path(__file__).parents[2] / 'benchmarks/project_content_fastpath.py')
measure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure)


@pytest.mark.parametrize('result', [
    (b'', False, False, None),
    (b'other\x001:needle\n', False, False, None),
    (b'file\x001:needle\n', True, False, None),
    (b'file\x001:needle\n', False, True, None),
    (b'file\x001:needle\n', False, False, {'total': 1}),
])
def test_missing_wrong_or_incomplete_work_refuses_timing(result):
    with pytest.raises(ValueError):
        measure.equivalent(result, [b'file\x001:needle'])


def test_real_search_fixture_records_every_pair_and_complete_match(tmp_path):
    result = measure.measure(str(tmp_path), str(tmp_path), files=8, pairs=2)
    assert result['completeMatches'] == result['files'] == 8
    assert result['sourceBytes'] > 0 and result['copySeconds'] >= 0
    assert len(result['samplesMs']['source']) == len(result['samplesMs']['cache']) == 2
    assert result['cacheHits'] == [8, 8, 8]
    assert result['liveRetries'] == 0
