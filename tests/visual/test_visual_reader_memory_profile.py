"""Real browser phase diagnosis; never treats instrumented results as admission."""
import os

import pytest

from benchmarks.markdown_worker.reader_memory_profile import profile
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin  # noqa: F401

pytestmark = pytest.mark.visual


def test_reader_memory_profile_smoke(browser, frontend_visual_origin, tmp_path):
    report = profile(browser, frontend_visual_origin, tmp_path, tmp_path/'profile.json', samples=2, pairs=1)
    expected = ['loaded', 'warm', 'latency', 'cpu_warm', 'cpu', 'oracle', 'disposed', 'disposed_after_gc']
    for row in report['cases']:
        for value in row['pairs'][0].values():
            assert [p['phase'] for p in value['phases']] == expected
            assert value['canonical_final_cpu_dom_verified']
            assert all(p['renderer_rss_bytes'] > 0 and p['js_heap_used_bytes'] > 0 for p in value['phases'])
            assert not value['phases'][-1]['fixture_mounted']
            assert value['phases'][-1]['dom']['nodes'] <= value['phases'][-2]['dom']['nodes']


def test_reader_memory_profile_pairs(browser, frontend_visual_origin, tmp_path):
    destination = os.environ.get('TOFU_READER_MEMORY_PROFILE_OUTPUT')
    if not destination:
        pytest.skip('set TOFU_READER_MEMORY_PROFILE_OUTPUT for phase-level diagnosis')
    profile(browser, frontend_visual_origin, tmp_path, destination)
