"""Real PDF.js verification from settled Agent Q&A, including an invented quote."""
import importlib
import json
from pathlib import Path

import pytest
from tests.visual.test_visual_surfaces import _wait_ready
from tests.conftest import _dismiss_onboarding_modals

pytestmark = pytest.mark.visual


@pytest.fixture(autouse=True)
def warm_paper_test_catalog():
    importlib.import_module('lib.llm_dispatch.config')


def test_qa_quotes_verify_actual_pdf_pages_and_mobile_navigation(page, tmp_path, assert_no_js_errors):
    fitz = pytest.importorskip('pymupdf')
    document = fitz.open()
    for number in range(1, 5):
        sheet = document.new_page()
        sheet.insert_text((42, 72), f'Source page {number}: controlled evidence fixture.')
        if number == 3:
            sheet.insert_text((42, 110), 'The measured outcome improves under stated assumptions.')
    source = tmp_path / 'source-evidence.pdf'
    source.write_bytes(document.tobytes())
    document.close()
    starts = []
    answer = ('The paper reports a conditional improvement (the model page number 99 is unverified).\n\n'
              '> The measured outcome improves under stated assumptions.\n\n'
              'A fabricated quotation must remain unverified:\n\n'
              '> This invented quotation does not exist in the source.')

    def qa_start(route):
        starts.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({
            'ok': True, 'task_id': 'evidence-fixture', 'paper_hash': starts[-1]['paper_hash'],
        }))

    page.route('**/api/v4/paper/qa/start', qa_start)
    page.route('**/api/v4/paper/qa/poll?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({
            'ok': True, 'status': 'done', 'done': True, 'answer': answer,
            'events': [], 'cursor': {'requested': 0, 'next': 0, 'reset': False},
        })))
    page.route('**/api/v4/paper/report/lookup', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True, 'report': '', 'lang': 'en'})))
    _wait_ready(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible')
    page.locator('#paperLibraryList .paper-lib-loading').wait_for(state='detached')
    with page.expect_response(lambda response: '/paper/upload' in response.url) as uploaded:
        page.set_input_files('.paper-upload-btn input[type="file"]', str(source))
    assert uploaded.value.ok, uploaded.value.text()[:1000]
    try:
        page.wait_for_selector('#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    except Exception:
        page.screenshot(path='/tmp/tofu-paper-evidence-load-failure.png')
        raise AssertionError(page.locator('#paperPdfViewer').inner_text()[:2000])
    _dismiss_onboarding_modals(page)
    page.locator('[data-paper-tab="qa"]').click()
    assert page.locator('.paper-qa-starters button').count() == 4
    artifacts = Path('/tmp/tofu-paper-evidence-previews')
    artifacts.mkdir(exist_ok=True)
    for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
        page.set_viewport_size({'width': width, 'height': height})
        if name == 'mobile':
            page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
        assert not page.locator('#paperQAMessages').evaluate('(el) => el.scrollWidth > el.clientWidth + 1')
        if name == 'mobile':
            page.wait_for_function("""() => {
              const input=document.querySelector('.paper-qa-input-area').getBoundingClientRect();
              const bar=document.querySelector('.paper-mobile-switch').getBoundingClientRect();
              return input.bottom <= bar.top + 1;
            }""")
        page.locator('[data-tab="qa"]').screenshot(path=str(artifacts / f'qa-starters-{name}.png'), animations='disabled')
    page.locator('.paper-qa-starters button').nth(2).click()
    page.locator('.paper-evidence-action button').nth(1).wait_for(timeout=15000)
    assert len(starts) == 1
    assert starts[0]['paper_hash'] and starts[0]['question']

    for theme in ['light', 'tofu', 'dark']:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
            page.set_viewport_size({'width': width, 'height': height})
            if name == 'mobile':
                page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
            page.locator('.paper-evidence-action button').first.click()
            dialog = page.locator('dialog.paper-evidence-dialog')
            dialog.locator('.paper-evidence-matches button').wait_for()
            assert dialog.locator('.paper-evidence-matches button').count() == 1
            assert '3' in dialog.locator('.paper-evidence-matches button').inner_text()
            assert '99' not in dialog.inner_text()
            assert 'The measured outcome improves' in dialog.locator('.paper-evidence-matches p').inner_text()
            assert not dialog.evaluate('(el) => el.scrollWidth > el.clientWidth + 1')
            dialog.screenshot(path=str(artifacts / f'evidence-{theme}-{name}.png'), animations='disabled')
            dialog.locator('.paper-evidence-matches button').click()
            dialog.wait_for(state='detached')
            assert page.locator('.paper-body').get_attribute('data-paper-view') == 'pdf'
            page.wait_for_function("document.querySelector('#paperPdfViewer').scrollTop > 0")
            try:
                page.wait_for_function("""() => {
                  const viewer=document.querySelector('#paperPdfViewer').getBoundingClientRect();
                  const match=document.querySelector('#paperPdfViewer [data-page="3"]').getBoundingClientRect();
                  return match.top >= viewer.top - 1 && match.top < viewer.bottom;
                }""")
            except Exception:
                page.screenshot(path='/tmp/tofu-paper-evidence-navigation-failure.png')
                raise AssertionError(page.evaluate("""() => ({
                  viewer:document.querySelector('#paperPdfViewer').getBoundingClientRect().toJSON(),
                  match:document.querySelector('#paperPdfViewer [data-page="3"]').getBoundingClientRect().toJSON(),
                  scroll:document.querySelector('#paperPdfViewer').scrollTop
                })"""))
            page.wait_for_function("document.activeElement.id === 'paperPdfViewer'")
    page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
    page.locator('.paper-evidence-action button').nth(1).click()
    dialog = page.locator('dialog.paper-evidence-dialog')
    page.wait_for_function("!document.querySelector('.paper-evidence-status').textContent.includes('…')")
    assert dialog.locator('.paper-evidence-matches button').count() == 0
    dialog.screenshot(path=str(artifacts / 'evidence-unverified-mobile.png'))
    page.keyboard.press('Escape')
    dialog.wait_for(state='detached')
    assert len(starts) == 1, 'source verification must not invoke another model task'
