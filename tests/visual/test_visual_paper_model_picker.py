"""Reading/Review model selection through the live delegated action boundary."""
import json

import pytest

from tests.conftest import _dismiss_onboarding_modals
from tests.support.paper_pdf import paper_pdf
from tests.visual.test_visual_surfaces import _wait_ready

pytestmark = pytest.mark.visual


def test_reading_review_model_picker(page, tmp_path, assert_no_js_errors):
    source = tmp_path / 'Model picker.pdf'
    source.write_bytes(paper_pdf())
    starts = []
    page.on('request', lambda request: starts.append(request.url)
            if '/paper/report/start' in request.url else None)
    page.route('**/api/v4/paper/report/lookup', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True,
            'lang': route.request.post_data_json.get('lang', 'en'),
            'report': '# Model picker fixture\n\n## Evidence\n\nA cached report.'})))
    _wait_ready(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible')
    page.set_input_files('.paper-upload-btn input[type="file"]', str(source))
    page.wait_for_selector('#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    _dismiss_onboarding_modals(page)
    page.evaluate('''() => {
        const state = window.TofuModules.resolveAction('_paperModelPickerState')();
        state.models.splice(0, state.models.length,
            {model_id: 'picker-alpha', capabilities: ['text']},
            {model_id: 'picker-beta', capabilities: ['text']});
        state.hiddenModels.clear();
    }''')
    for width, height in [(1440, 1000), (390, 844)]:
        page.set_viewport_size({'width': width, 'height': height})
        if width == 390:
            page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
        for tab in ['report', 'review']:
            page.locator(f'[data-paper-tab="{tab}"]').click()
            host = page.locator('#paperReportContent' if tab == 'report' else '#paperReviewContent')
            host.locator('.paper-report-article h2').first.wait_for()
            prefix = 'paperReport' if tab == 'report' else 'paperReview'
            dropdown = page.locator(f'#{prefix}ModelDropdown')
            button = page.locator(f'#{prefix}ModelLabel').locator('..')
            # Opening via an SVG descendant must survive the document listener.
            for model in ['picker-beta', 'picker-alpha', 'picker-beta']:
                button.locator('svg').first.click()
                dropdown.wait_for(state='visible')
                item = dropdown.get_by_text(model, exact=True)
                assert item.is_visible()
                item.click()
                dropdown.wait_for(state='hidden')
                assert page.locator(f'#{prefix}ModelLabel').inner_text() == model
            button.click()
            dropdown.wait_for(state='visible')
            button.click()
            dropdown.wait_for(state='hidden')
            button.click()
            dropdown.wait_for(state='visible')
            host.locator('.paper-report-article h2').first.click()
            dropdown.wait_for(state='hidden')
    assert not starts, 'model selection must not start paid report generation'
