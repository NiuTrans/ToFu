"""Compaction measurement refuses unequal work or a drifting source witness."""
import copy
import hashlib

import pytest

from benchmarks import compaction_compare as benchmark
from benchmarks.compaction_compare import (
    assert_same_work, make_case, run_case, verify_source_witness,
)
from lib.tasks_pkg.compaction._layer2 import _compact

pytestmark = pytest.mark.unit


@pytest.fixture
def accepted():
    case = make_case(4)
    result, _clock = run_case(_compact, case)
    assert_same_work(result, copy.deepcopy(result), case)
    return case, result


@pytest.mark.parametrize('changed', ['messages', 'owner', 'usage', 'effects', 'calls', 'fields'])
def test_measure_rejects_changed_work(accepted, changed):
    case, before = accepted
    after = copy.deepcopy(before)
    if changed == 'messages':
        after['messages'].pop(0)
    elif changed == 'owner':
        after['task']['_userId'] = 42
    elif changed == 'usage':
        after['meta']['summary_usage']['prompt_tokens'] += 1
    elif changed == 'effects':
        after['effects'].pop()
    elif changed == 'calls':
        after['calls']['archive'] += 1
    else:
        after['unexpected'] = None
    with pytest.raises(ValueError, match='L2 parity'):
        assert_same_work(before, after, case)


def test_equal_noops_cannot_qualify_as_compaction(accepted):
    case, result = accepted
    result['meta']['compacted'] = False
    with pytest.raises(ValueError, match='intended decision'):
        assert_same_work(result, copy.deepcopy(result), case)


def test_equal_failures_cannot_delete_source_history():
    case = make_case(4, 'cancel_summary')
    result, _clock = run_case(_compact, case)
    result['messages'].pop()
    with pytest.raises(ValueError, match='mutated source history'):
        assert_same_work(result, copy.deepcopy(result), case)


def test_managed_context_fixture_reaches_real_compaction():
    case = make_case(4, 'managed_context')
    result, _clock = run_case(_compact, case)
    assert_same_work(result, copy.deepcopy(result), case)


def test_measure_refuses_source_edits_during_sampling(tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('before')
    witness = {str(source): hashlib.sha256(source.read_bytes()).hexdigest()}
    verify_source_witness(witness)
    source.write_text('after')
    with pytest.raises(ValueError, match='sources changed'):
        verify_source_witness(witness)


def test_measure_freezes_baseline_before_loading_it(tmp_path, monkeypatch):
    baseline = tmp_path / 'baseline.py'
    baseline.write_text('# frozen baseline source')

    def drift_during_load(path):
        path.write_text('# edited after its code was loaded')
        return _compact

    monkeypatch.setattr(benchmark, 'load_baseline', drift_during_load)
    monkeypatch.setattr(benchmark.sys, 'argv', [
        'compaction_compare', '--baseline', str(baseline),
        '--output', str(tmp_path / 'result.json'), '--parity-only',
    ])
    with pytest.raises(ValueError, match='sources changed'):
        benchmark.main()
