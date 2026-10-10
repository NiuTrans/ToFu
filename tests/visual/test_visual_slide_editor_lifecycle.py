"""Production editor ownership and teardown with delayed, owner-scoped mock receipts."""
from time import monotonic

import pytest

pytestmark = pytest.mark.visual


def _wait_routes(page, routes, count):
    deadline = monotonic() + 10
    while len(routes) < count and monotonic() < deadline:
        page.wait_for_timeout(20)  # Dispatch pending Playwright route callbacks.
    assert len(routes) >= count


def _open(page, task='lifecycle-fixture', number=1, total=3):
    page.evaluate("""({task,number,total}) => {
      const button=document.createElement('button');
      button.setAttribute('data-tofu-action', `openSlidesEditor(${JSON.stringify(task)},${number},${total})`);
      document.body.append(button);button.click();button.remove();
    }""", {'task': task, 'number': number, 'total': total})
    page.locator('.sled-overlay').wait_for(state='visible', timeout=20000)


def _elements(text):
    return {'elements': [{'elementId': 'title', 'elementType': 'text', 'text': text,
                          'style': {'fontSize': 32, 'color': '$primary'}}]}


def _fixture(page, live_server, handler):
    from tests.conftest import _dismiss_onboarding_modals

    def route(request):
        url = request.request.url.split('?')[0]
        if url.endswith('.html'):
            request.fulfill(status=200, content_type='text/html', body='<html><head></head><body><h1 data-element-id="title">Title</h1></body></html>')
        elif url.endswith('.png'):
            request.fulfill(status=200, content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"></svg>')
        else:
            handler(request)

    page.route('**/api/v4/slides/**', route)
    page.goto(live_server)
    _dismiss_onboarding_modals(page)


def test_close_releases_listener_focus_and_live_iframe_handlers(browser, live_server):
    from playwright.sync_api import expect
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        _fixture(page, live_server, lambda route: route.fulfill(json=_elements('Fixture title')))
        page.evaluate("""() => {
          const add=document.addEventListener.bind(document), remove=document.removeEventListener.bind(document);
          window.editorKeys=new Set();
          document.addEventListener=(type,listener,options)=> {
            if(type==='keydown') { editorKeys.add(listener); options?.signal?.addEventListener('abort',()=>editorKeys.delete(listener),{once:true}); }
            add(type,listener,options);
          };
          document.removeEventListener=(type,listener,options)=> { if(type==='keydown')editorKeys.delete(listener);remove(type,listener,options); };
          const origin=document.createElement('button');origin.id='editor-origin';origin.textContent='Open';document.body.append(origin);origin.focus();
        }""")
        for _ in range(12):
            _open(page)
            expect(page.locator('.sled-close')).to_be_focused()
            assert page.evaluate('editorKeys.size') == 1
            page.locator('.sled-close').click()
            expect(page.locator('.sled-overlay')).to_have_count(0)
            expect(page.locator('#editor-origin')).to_be_focused()
            assert page.evaluate('editorKeys.size') == 0
        _open(page)
        title = page.frame_locator('.sled-canvas-frame').locator('[data-element-id=title]')
        title.click()
        expect(page.locator('#sledText')).to_have_value('Fixture title')
        title.press('Escape')
        expect(page.locator('.sled-overlay')).to_have_count(0)
        assert page.evaluate('editorKeys.size') == 0
        _open(page, number=2)
        page.frame_locator('.sled-canvas-frame').locator('[data-element-id=title]').click()
        page.locator('[data-sled-ask]').click()
        expect(page.locator('.sled-overlay')).to_have_count(0)
        expect(page.locator('#userInput')).to_have_value('编辑这个 PPT（任务 lifecycle-fixture）第 2 页：')
        expect(page.locator('#userInput')).to_be_focused()
        assert page.evaluate('editorKeys.size') == 0
        assert not errors
    finally:
        page.close()


def test_late_reads_and_patch_receipts_cannot_overwrite_a_new_visit(browser, live_server):
    from playwright.sync_api import expect
    page = browser.new_page()
    reads, patches = [], []
    def hold(route):
        (patches if route.request.method == 'POST' else reads).append(route)
    try:
        _fixture(page, live_server, hold)
        _open(page)
        page.wait_for_function('() => document.querySelector(".sled-page-indicator").textContent.includes("1")')
        _wait_routes(page, reads, 1)
        page.locator('[data-sled-page="2"]').click()
        page.locator('[data-sled-page="3"]').click()
        assert len(reads) == 1
        reads[0].fulfill(json=_elements('Old page'))
        _wait_routes(page, reads, 2)
        assert len(reads) == 2 and '/pages/3/elements' in reads[1].request.url
        page.frame_locator('.sled-canvas-frame').locator('[data-element-id=title]').click()
        expect(page.locator('#sledText')).to_have_count(0)
        reads[1].fulfill(json=_elements('Current page'))
        expect(page.locator('#sledText')).to_have_value('Current page')
        page.locator('#sledText').fill('Patch old visit')
        page.locator('[data-sled-apply]').click()
        _wait_routes(page, patches, 1)
        assert len(patches) == 1
        assert patches[0].request.post_data_json == {'ops': [{'op': 'set_text', 'target': 'title', 'text': 'Patch old visit'}]}
        page.locator('.sled-close').click()
        _open(page, task='replacement-fixture', number=2)
        _wait_routes(page, reads, 3)
        reads[2].fulfill(json=_elements('Replacement page'))
        page.frame_locator('.sled-canvas-frame').locator('[data-element-id=title]').click()
        expect(page.locator('#sledText')).to_have_value('Replacement page')
        expect(page.locator('[data-sled-apply]')).to_be_disabled()
        frame_url = page.locator('.sled-canvas-frame').get_attribute('src')
        patches[0].fulfill(json={'ok': False, 'errors': ['Old page rollback']})
        expect(page.locator('[data-sled-apply]')).to_be_enabled()
        expect(page.locator('#sledText')).to_have_value('Replacement page')
        expect(page.locator('#sledErrors')).to_be_empty()
        assert page.locator('.sled-canvas-frame').get_attribute('src') == frame_url
        page.locator('#sledText').fill('Replacement edit')
        page.locator('[data-sled-apply]').click()
        _wait_routes(page, patches, 2)
        assert '/replacement-fixture/pages/2/patch' in patches[1].request.url
        patches[1].fulfill(json={'ok': False, 'errors': ['Palette invalid; deck unchanged']})
        expect(page.locator('#sledErrors')).to_have_text('Palette invalid; deck unchanged')
        expect(page.locator('#sledText')).to_have_value('Replacement edit')
    finally:
        page.close()
