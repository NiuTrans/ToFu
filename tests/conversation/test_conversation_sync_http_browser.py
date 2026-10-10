"""Native HTTP-major sync writes, browser hydration and reload use one contract."""
import uuid
import pytest

pytestmark = pytest.mark.visual


def test_committed_turns_render_and_reload_on_v4(browser, live_server):
    from playwright.sync_api import expect
    context = browser.new_context(service_workers='block')
    context.add_init_script('window.TOFU_API_V4 = true;')
    page = context.new_page()
    errors, sync_requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: sync_requests.append(request.url)
            if '/sync?' in request.url or '/events?' in request.url else None)
    cid = 'http-sync-browser-' + uuid.uuid4().hex[:12]
    endpoint = f'{live_server}/api/v4/conversations/{cid}/turns/settled'
    try:
        created = context.request.post(endpoint, data={
            'commandId': 'http-major-human', 'actor': 'human', 'kind': 'text',
            'projection': {'content': 'HTTP major browser question'},
            'conversation': {'allowCreate': True, 'title': 'HTTP major journey',
                             'createdAt': 100, 'settings': {}},
        })
        assert created.status == 200, created.text()
        first = created.json()
        replay = context.request.post(endpoint, data={
            'commandId': 'http-major-human', 'actor': 'human', 'kind': 'text',
            'projection': {'content': 'HTTP major browser question'},
            'conversation': {'allowCreate': True, 'title': 'HTTP major journey',
                             'createdAt': 100, 'settings': {}},
        })
        assert replay.status == 200
        assert replay.json()['turn']['turnId'] == first['turn']['turnId']
        page.goto(live_server, wait_until='domcontentloaded')
        from tests.conftest import _dismiss_onboarding_modals
        _dismiss_onboarding_modals(page)
        item = page.locator(f'.conv-item[data-conv-id="{cid}"]')
        expect(item).to_have_count(1, timeout=20000)
        item.click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text('HTTP major browser question', timeout=20000)
        appended = context.request.post(endpoint, data={
            'commandId': 'http-major-assistant', 'actor': 'assistant', 'kind': 'text',
            'projection': {'content': 'HTTP major committed reply'},
        })
        assert appended.status == 200, appended.text()
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        item = page.locator(f'.conv-item[data-conv-id="{cid}"]')
        expect(item).to_have_count(1, timeout=20000)
        item.click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text('HTTP major committed reply', timeout=20000)
        assert sync_requests
        assert all('/api/v4/' in url for url in sync_requests), sync_requests
        assert not errors, errors
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
