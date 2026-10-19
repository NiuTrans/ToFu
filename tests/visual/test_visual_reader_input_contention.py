"""Real-keyboard reader contention measurement and canonical DOM oracles."""
import os
import pytest
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin  # noqa: F401
from benchmarks.markdown_worker.reader_input import measure

pytestmark = pytest.mark.visual


def test_reader_input_contention_smoke(browser, frontend_visual_origin, tmp_path):
    report = measure(browser, frontend_visual_origin, tmp_path, tmp_path / 'input-smoke.json', samples=2, pairs=1)
    for case in report['cases']:
        for value in case['pairs'][0].values():
            assert len(value['raw']) == 2 and value['warmup_keys'] == 20
            assert all(row['keydown_to_second_raf_ms'] > 0 and row['render_and_layout_ms'] >= 0 for row in value['raw'])


def test_reader_input_contention_pairs(browser, frontend_visual_origin, tmp_path):
    destination = os.environ.get('TOFU_READER_INPUT_OUTPUT')
    if not destination:
        pytest.skip('set TOFU_READER_INPUT_OUTPUT for paired browser cohorts')
    report = measure(browser, frontend_visual_origin, tmp_path, destination)
    assert report['cases']
    for case in report['cases']:
        assert len(case['pairs']) == 3
        for pair in case['pairs']:
            for value in pair.values():
                assert len(value['raw']) == 100 and value['warmup_keys'] == 20
                assert all(row['keydown_to_second_raf_ms'] > 0
                           and row['render_and_layout_ms'] >= 0 for row in value['raw'])
