"""Real report/review reading surfaces: source navigation, themes and mobile."""
import importlib
import json
from pathlib import Path

import pytest
from tests.visual.test_visual_surfaces import _wait_ready
from tests.conftest import _dismiss_onboarding_modals

pytestmark = pytest.mark.visual


def test_report_review_reading_surfaces(page, tmp_path, assert_no_js_errors):
    importlib.import_module('lib.llm_dispatch.config')
    pdf = pytest.importorskip('pymupdf').open()
    pdf.new_page().insert_text((42, 72), 'Source claim: an experiment with a limited sample.')
    source = tmp_path / 'Reading evidence.pdf'
    source.write_bytes(pdf.tobytes())
    pdf.close()
    starts = []
    depths = []
    def deepen_reply(route):
        depths.append(route.request.post_data_json)
        route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "cached": True, "content": "Depth for the selected canonical document."}))
    page.route("**/api/v4/paper/deepen/start", deepen_reply)
    page.on('request', lambda request: starts.append(request.url)
            if '/paper/report/start' in request.url or '/paper/translate/start' in request.url else None)
    report = '# A controlled experiment\n\nThe measured result depends on the stated assumptions.\n\n'
    report += '\n\n'.join('## ' + title + '\n\n' + ('Evidence remains limited to the source experiment. ' * 18)
                          for title in ['Problem', 'Method', 'Evidence', 'Limitations'])
    review = '# Peer review\n\nIndependent assessment of the supplied manuscript.\n\n'
    review += '\n\n'.join('## ' + title + '\n\n' + ('Assess the manuscript against explicit evidence. ' * 18)
                          for title in ['Summary', 'Strengths', 'Weaknesses', 'Questions'])
    page.route('**/api/v4/paper/report/lookup', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True,
            'lang': route.request.post_data_json.get('lang', 'en'),
            'report': review if route.request.post_data_json.get('lang', '').startswith('review:') else report})))
    page.add_init_script("localStorage.setItem('tofu_ui_lang','en');")
    _wait_ready(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible', timeout=15000)
    uploads = []
    page.on('response', lambda response: uploads.append(response) if 'upload' in response.url and response.request.method == 'POST' else None)
    page.set_input_files('.paper-upload-btn input[type="file"]', str(source))
    try:
        page.wait_for_selector('#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    except Exception as error:
        page.screenshot(path='/tmp/tofu-paper-reading-source-failure.png')
        raise AssertionError({'upload': [(response.url, response.status, response.json()) for response in uploads[-2:]], 'viewer': page.locator('#paperPdfViewer').inner_text()[:1000],
                              'source': page.locator('#paperLeftTitle').inner_text() if page.locator('#paperLeftTitle').count() else ''}) from error
    _dismiss_onboarding_modals(page)
    artifacts = Path('/tmp/tofu-paper-reading-previews')
    artifacts.mkdir(exist_ok=True)
    for tab in ['report', 'review']:
        page.locator(f'[data-paper-tab="{tab}"]').click()
        host = page.locator('#paperReportContent' if tab == 'report' else '#paperReviewContent')
        host.locator('.paper-report-article h2').first.wait_for()
        for theme in ['light', 'tofu', 'dark']:
            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
            for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
                page.set_viewport_size({'width': width, 'height': height})
                if name == 'mobile':
                    page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
                if name == 'mobile':
                    page.wait_for_function("document.getElementById('sidebar').getBoundingClientRect().right <= 1")
                host.evaluate('(el) => el.scrollTop = 0')
                host.screenshot(path=str(artifacts / f'{tab}-{theme}-{name}.png'))
                assert not host.evaluate('(el) => el.scrollWidth > el.clientWidth + 1'), f'{tab}/{theme}/{name}: overflow'
        menu = host.locator('.paper-document-controls [aria-haspopup="menu"]')
        menu.click()
        host.locator('.paper-document-controls [role="menuitem"]').first.focus()
        page.keyboard.press('End')
        page.keyboard.press('Enter')
        assert page.evaluate('document.activeElement.tagName') == 'H2'
        assert page.locator('[data-paper-tab="report"]').is_visible()
        host.locator('.paper-document-controls > button').click()
        assert page.locator('.paper-body').get_attribute('data-paper-view') == 'pdf'
        page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
        page.set_viewport_size({'width': 1440, 'height': 1000})
        host.evaluate('(el) => el.scrollTop = 0')
        host.locator('.paper-deepen-btn').first.click()
        host.locator('.paper-deepen-body').get_by_text('Depth for the selected canonical document.').wait_for()
        assert depths[-1]['lang'].startswith('review:') if tab == 'review' else depths[-1]['lang'] in ['en', 'zh']
        host.locator('.paper-report-article > p').first.scroll_into_view_if_needed()
        host.locator('.paper-report-article > p').first.evaluate('''(el) => {
            const range = document.createRange(); range.selectNodeContents(el);
            const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
            document.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
        }''')
        page.locator('#paperReportQuoteBtn button[data-tofu-action="_paperNoteFromSelection()"]').click()
        note = page.locator('dialog:visible')
        note.locator('textarea').fill('Verify this claim against the source.')
        for theme in ['light', 'tofu', 'dark']:
            page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
            page.set_viewport_size({'width': 390, 'height': 844})
            note.screenshot(path=str(artifacts / f'{tab}-note-{theme}-mobile.png'))
            assert note.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        with page.expect_response(lambda response: '/api/v4/paper/notes' in response.url and response.request.method == 'POST') as saved:
            note.locator('.paper-note-save').click()
        assert saved.value.ok
        note.wait_for(state='detached')
        host.locator('.paper-note-mark').first.click()
        assert page.locator('dialog:visible textarea').input_value() == 'Verify this claim against the source.'
        page.locator('dialog:visible .paper-note-cancel').click()
        page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('#paperRebuttalSection summary').click()
    page.locator('#paperRebuttalInput').fill('We added an ablation and clarified the limitations.')
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#paperRebuttalInput').scroll_into_view_if_needed()
    page.locator('#paperRebuttalSection').screenshot(path=str(artifacts / 'review-rebuttal-mobile.png'))
    assert page.locator('#paperRebuttalInput').input_value().startswith('We added an ablation')
    assert not starts, 'reading cached artifacts and editing rebuttal must not start paid work'
