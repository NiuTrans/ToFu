"""Real PDF, explicit translation intent, chapters/export/Q&A and source teardown."""
import importlib
import json
from pathlib import Path

import pytest
from tests.visual.test_visual_surfaces import _wait_ready
from tests.conftest import _dismiss_onboarding_modals

pytestmark = pytest.mark.visual


@pytest.fixture(autouse=True)
def warm_catalog():
    importlib.import_module('lib.llm_dispatch.config')


def test_translation_reader_intent_artifact_and_source_switch(page, tmp_path, assert_no_js_errors):
    fitz = pytest.importorskip('pymupdf')
    document = fitz.open(); document.new_page().insert_text((42, 72), 'Controlled comparisons test a claim under stated assumptions.')
    source = tmp_path / 'Measured comparisons.pdf'; source.write_bytes(document.tobytes()); document.close()
    translated = '# 受控比较\n\n研究通过对照实验检验假设。\n\n## 研究问题\n\n在明确条件下，方法是否改善结果？\n\n## 方法与证据\n\n受控比较检验结论是否依赖实验条件。\n\n## 适用边界\n\n这些结果尚不能证明其他场景中的泛化能力。\n\n| 实验条件 | 已观察结果 |\n| --- | --- |\n| 受控设置 | 条件性改善 |\n\n```text\nsource_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_source_receipt_\n```'
    starts = []; aborts = []; pending_starts = []; pending_polls = []; finish = False
    def cache(route):
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'text': ''}))
    def start(route):
        starts.append(route.request.post_data_json)
        if len(starts) == 1: pending_starts.append(route)
        else: route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'task_id': 'translation-current' if len(starts) == 2 else 'translation-old-source', 'paper_hash': starts[-1]['paper_hash']}))
    def abort(route):
        aborts.append(route.request.url.rsplit('/', 1)[-1])
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True}))
    def poll(route):
        if route.request.url.find('translation-old-source') >= 0:
            pending_polls.append(route); return
        data = {'ok': True, 'status': 'done' if finish else 'running', 'text': translated if finish else '',
                'cursor': {'requested': 0, 'next': 1, 'reset': False},
                'events': [] if finish else [{'seq': 0, 'type': 'chunk', 'index': 0, 'total': 2, 'text': 'INCOMPLETE'}]}
        route.fulfill(content_type='application/json', body=json.dumps({'data': data, 'meta': {}}))
    page.route('**/api/v4/paper/translate/cache', cache)
    page.route('**/api/v4/paper/translate/start', start)
    page.route('**/api/v4/paper/translate/abort/*', abort)
    page.route('**/api/v4/paper/translate/poll?*', poll)
    page.route('**/api/v4/paper/report/lookup', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'report': '', 'lang': 'en'})))
    questions = []
    def qa_start(route):
        questions.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'task_id': 'translated-question', 'paper_hash': questions[-1]['paper_hash']}))
    page.route('**/api/v4/paper/qa/start', qa_start)
    page.route('**/api/v4/paper/qa/poll?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'status': 'done', 'answer': 'The evidence depends on the experimental setting.', 'events': [], 'cursor': {'requested': 0, 'next': 0, 'reset': False}})))
    _wait_ready(page); page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible')
    page.locator('#paperLibraryList .paper-lib-loading').wait_for(state='detached')
    page.set_input_files('.paper-upload-btn input[type="file"]', str(source))
    page.wait_for_selector('#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    _dismiss_onboarding_modals(page); page.locator('[data-paper-tab="translate"]').click()
    reader = page.locator('.paper-translation-reader'); reader.wait_for()
    assert not starts
    reader.locator('.ui-dropdown button').first.click(); page.get_by_role('menuitem', name='中文', exact=True).click()
    assert not starts, 'choosing a language is read-only'
    with page.expect_request('**/api/v4/paper/translate/start'):
        reader.get_by_role('button', name='生成译文', exact=True).click()
    reader.get_by_role('button', name='停止翻译', exact=True).click()
    with page.expect_response(lambda response: '/paper/translate/abort/' in response.url):
        pending_starts[0].fulfill(content_type='application/json', body=json.dumps({'ok': True, 'task_id': 'late-stopped', 'paper_hash': starts[0]['paper_hash']}))
    assert aborts == ['late-stopped']
    reader.get_by_role('button', name='生成译文', exact=True).click()
    page.locator('#babelPdfStatus').get_by_text('已翻译 1/2 段', exact=False).wait_for()
    assert 'INCOMPLETE' not in page.locator('#babelPdfBody').inner_text()
    assert page.get_by_role('progressbar').get_attribute('aria-valuenow') == '50'
    finish = True
    page.locator('.paper-translation-article h2').nth(2).wait_for()
    assert len(starts) == 2
    assert not reader.get_by_role('button', name='生成译文', exact=True).is_visible()
    assert not reader.get_by_role('button', name='停止翻译', exact=True).is_visible()
    assert reader.locator('.paper-translation-chapters button').count() == 3
    reader.locator('.paper-translation-chapters button').nth(1).click()
    assert page.evaluate('document.activeElement.textContent') == '方法与证据'
    with page.expect_download() as download:
        reader.get_by_role('button', name='下载 Markdown', exact=True).click()
    assert Path(download.value.path()).read_text() == translated
    artifacts = Path('/tmp/tofu-paper-translation-previews'); artifacts.mkdir(exist_ok=True)
    host = page.locator('#paperTranslateContent')
    for theme in ['light', 'tofu', 'dark']:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
            page.set_viewport_size({'width': width, 'height': height})
            if name == 'mobile': page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
            assert not host.evaluate('(element) => element.scrollWidth > element.clientWidth + 1')
            host.evaluate('(element) => element.scrollTop = 0')
            host.screenshot(path=str(artifacts / f'translation-{theme}-{name}.png'), animations='disabled')
            host.evaluate('(element) => element.scrollTop = element.scrollHeight')
            if name == 'mobile':
                footer = reader.locator('footer').bounding_box(); switcher = page.locator('.paper-mobile-switch').bounding_box()
                assert footer['y'] + footer['height'] <= switcher['y'] + 1
            host.screenshot(path=str(artifacts / f'translation-body-{theme}-{name}.png'), animations='disabled')
    reader.locator('.paper-translation-section-actions button').nth(1).click()
    page.locator('.paper-qa-assistant').get_by_text('The evidence depends on the experimental setting.', exact=True).wait_for()
    assert len(questions) == 1 and '受控比较' in questions[0]['question']
    page.locator('[data-paper-tab="translate"]').click(); assert len(starts) == 2
    reader.locator('.ui-dropdown button').first.click(); page.get_by_role('menuitem', name='English', exact=True).click()
    with page.expect_request('**/api/v4/paper/translate/poll?*'):
        reader.get_by_role('button', name='生成译文', exact=True).click()
    document = fitz.open(); document.new_page().insert_text((42, 72), 'A different paper must reject the former translation.')
    other = document.tobytes(); document.close()
    with page.expect_response(lambda response: '/paper/upload' in response.url) as uploaded:
        page.locator('#paperModeContainer').evaluate("""(element, bytes) => {
          const transfer = new DataTransfer();
          transfer.items.add(new File([new Uint8Array(bytes)], 'other-paper.pdf', {type: 'application/pdf'}));
          element.dispatchEvent(new DragEvent('drop', {dataTransfer:transfer,bubbles:true,cancelable:true}));
        }""", list(other))
    assert uploaded.value.ok
    page.locator('[data-paper-tab="translate"]').click(); reader.wait_for()
    pending_polls[0].fulfill(content_type='application/json', body=json.dumps({'ok': True, 'status': 'done', 'text': 'WRONG SOURCE', 'events': []}))
    page.wait_for_function("document.querySelector('.paper-translation-reader h1').textContent.includes('other-paper')")
    assert 'WRONG SOURCE' not in page.locator('#babelPdfBody').inner_text()
    assert 'translation-old-source' in aborts and len(starts) == 3
