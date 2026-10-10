"""Real Paper shell, parsed source and canonical report in desktop/mobile themes."""
import json
import importlib
from pathlib import Path

import pytest
from tests.visual.test_visual_surfaces import _wait_ready
from tests.conftest import _dismiss_onboarding_modals

pytestmark = pytest.mark.visual


@pytest.fixture(autouse=True)
def warm_paper_test_catalog():
    # This focused UI fixture avoids the unrelated pricing/audio cold-import
    # race during simultaneous app bootstrap requests; no provider is called.
    importlib.import_module('lib.llm_dispatch.config')


def test_paper_workbench_source_report_navigation_and_studio(page, tmp_path, assert_no_js_errors):
    pymupdf = pytest.importorskip('pymupdf')
    document = pymupdf.open()
    document.new_page().insert_text((42, 72), 'Workbench evidence fixture: a real parsed PDF.')
    source = tmp_path / 'workbench-evidence.pdf'
    source.write_bytes(document.tobytes())
    document.close()
    starts = []
    page.on('request', lambda request: starts.append(request.url)
            if '/paper/report/start' in request.url else None)
    page.route('**/api/v4/paper/report/lookup', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({
            'ok': True, 'lang': 'en',
            'report': '# Evidence fixture\n\nA measured contribution grounded in source data.\n\n'
                      '## Core problem\n\nThe original question.\n\n'
                      '## Method\n\nA controlled experiment.\n\n'
                      '## Evidence\n\nResults under stated assumptions.\n\n'
                      '## Limitations\n\nGeneralization remains unproven.',
            'insight': {'items': {'thesis': 'The method improves the measured outcome under stated assumptions.'}},
        })))
    _wait_ready(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible', timeout=15000)
    page.set_input_files('.paper-upload-btn input[type="file"]', str(source))
    try:
        page.wait_for_selector('#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    except Exception:
        page.screenshot(path='/tmp/tofu-paper-workbench-failure.png')
        raise AssertionError(page.locator('#paperPdfViewer').inner_text()[:2000])
    _dismiss_onboarding_modals(page)
    page.locator('[data-paper-tab="overview"]').click()
    overview = page.locator('[data-tab="overview"]')
    overview.get_by_text('The method improves the measured outcome under stated assumptions.', exact=True).wait_for()
    assert overview.locator('ol li').count() == 4
    assert overview.locator('.paper-workbench-tool').count() == 4
    assert not starts, 'overview must not start paid report work'

    artifacts = Path('/tmp/tofu-paper-workbench-previews')
    artifacts.mkdir(exist_ok=True)
    for theme in ['light', 'tofu', 'dark']:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
            page.set_viewport_size({'width': width, 'height': height})
            if name == 'mobile':
                page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
            overview.wait_for(state='visible')
            overflow = overview.evaluate('(el) => el.scrollWidth > el.clientWidth + 1')
            assert not overflow, f'{theme}/{name}: overview overflow'
            overview.evaluate('(el) => el.scrollTop = 0')
            overview.screenshot(path=str(artifacts / f'{theme}-{name}.png'))
            overview.evaluate('(el) => el.scrollTop = el.scrollHeight')
            if name == 'mobile':
                last = overview.locator('.paper-workbench-tool-review button').bounding_box()
                switch = page.locator('.paper-mobile-switch').bounding_box()
                assert last['y'] + last['height'] <= switch['y'], 'Studio action covered by mobile switcher'
            overview.screenshot(path=str(artifacts / f'{theme}-{name}-studio.png'))
    page.set_viewport_size({'width': 1440, 'height': 1000})
    nav_top = page.locator('.paper-workbench-navigation').bounding_box()['y']
    overview.locator('ol button').nth(1).click()
    assert page.locator('[data-tab="report"]').is_visible()
    assert page.evaluate('document.activeElement.textContent') == 'Method'
    assert page.locator('[data-paper-tab="overview"]').is_visible()
    assert abs(page.locator('.paper-workbench-navigation').bounding_box()['y'] - nav_top) < 1
    page.locator('[data-paper-tab="overview"]').click()
    overview.locator('.paper-workbench-tool-podcast button').click()
    page.locator('#paperPodcastContent').wait_for(state='visible')
    assert page.locator('[data-paper-tab="podcast"]').get_attribute('aria-selected') == 'true'
    assert not starts
