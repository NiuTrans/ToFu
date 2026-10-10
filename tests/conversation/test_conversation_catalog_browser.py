"""Built sidebar retains absent rows until the current catalog proves deletion."""
import json
import os
from urllib.parse import urlparse, parse_qs
import pytest

pytestmark = pytest.mark.visual


def test_catalog_missing_count_never_evicts_sidebar_rows(browser, live_server):
    from playwright.sync_api import expect
    context = browser.new_context(service_workers='block')
    context.add_init_script('window.TOFU_API_V4 = ' + json.dumps(os.environ.get('TOFU_API_MAJOR', '4') == '4') + ';')
    page = context.new_page()
    stage = 0
    seen = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route_request(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path in ('/api/v4/conversations', '/api/v4/conversations') and parse_qs(parsed.query).get('meta') == ['1']:
            seen.append(stage)
            cid = 'catalog-ghost' if stage == 0 else 'catalog-keep'
            data = {'items': [{'id': cid, 'title': cid, 'messageCount': 1,
                              'createdAt': 1, 'updatedAt': 2, 'rev': 1, 'settings': {}}]}
            headers = {'X-Total-Count': '1'} if stage != 1 else {}
            if parsed.path.startswith('/api/v4/'):
                data = {'data': data, 'meta': {}}
            return route.fulfill(json=data, headers=headers)
        return route.continue_()

    context.route('**/*', route_request)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        ghost = page.locator('.conv-item[data-conv-id="catalog-ghost"]')
        keep = page.locator('.conv-item[data-conv-id="catalog-keep"]')
        expect(ghost).to_have_count(1, timeout=20000)
        stage = 1
        page.evaluate("""() => {
          const channel = new BroadcastChannel('tofu_conversation_invalidation');
          channel.postMessage({type:'conv_saved', sourceTab:'catalog-regression'});
          channel.close();
        }""")
        expect(keep).to_have_count(1, timeout=20000)
        expect(ghost).to_have_count(1)
        stage = 2
        page.evaluate("""() => {
          const channel = new BroadcastChannel('tofu_conversation_invalidation');
          channel.postMessage({type:'conv_saved', sourceTab:'catalog-regression'});
          channel.close();
        }""")
        expect(ghost).to_have_count(0, timeout=20000)
        expect(keep).to_have_count(1)
        assert {0, 1, 2} <= set(seen)
        assert not errors, errors
    finally:
        context.close()
