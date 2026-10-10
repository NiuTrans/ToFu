"""Owned-browser measurement mechanics and per-frame canonical DOM oracles."""
import os

import pytest

from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin  # noqa: F401
from benchmarks.markdown_worker.reader_measure import measure

pytestmark = pytest.mark.visual


def test_reader_measurement_smoke(browser, frontend_visual_origin, tmp_path):
    report = measure(browser, frontend_visual_origin, tmp_path, tmp_path/'smoke.json', samples=2, pairs=1)
    assert len(report['cases']) == 2
    for case in report['cases']:
        for value in case['pairs'][0].values():
            assert value['latency_oracle_frames'] == 2
            assert len(value['wall_samples_ms']) == 2
            assert value['rss_samples'] > 0
            assert value['browser_pid'] != value['memory_browser_pid']


def test_reader_paired_measurement(browser, frontend_visual_origin, tmp_path):
    destination = os.environ.get('TOFU_READER_PERF_OUTPUT')
    if not destination:
        pytest.skip('set TOFU_READER_PERF_OUTPUT for full paired measurement')
    measure(browser, frontend_visual_origin, tmp_path, destination)
