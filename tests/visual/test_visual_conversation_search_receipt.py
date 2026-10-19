"""Same-query search receipt rendered from persisted history in the real app."""
import json
import uuid
import pytest

pytestmark = pytest.mark.visual

@pytest.mark.parametrize('width', [1280, 390])
def test_search_receipt(browser, live_server, tmp_path, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    context = browser.new_context(service_workers='block', viewport={'width': width, 'height': 900})
    cid = 'search-receipt-' + uuid.uuid4().hex[:12]
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');localStorage.setItem('tofu_ui_lang','zh');")
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId',{json.dumps(cid)});")
    keyword = 'test_opencode_exit_status.py'
    receipt = {'keyword': keyword, 'scope': 'all', 'returnedCount': 0, 'limit': 20,
               'coverage': 'ready', 'incomplete': False, 'items': []}
    row = {'roundNum': 1, 'toolCallId': 'search-call', 'toolName': 'list_conversations',
           'toolArgs': {'keyword': keyword}, 'status': 'done',
           'query': f'List conversations matching "{keyword}"',
           'toolContent': f"No conversations found matching '{keyword}'",
           'results': [{'source': 'Conversations', 'conversationSearch': receipt}]}
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        for actor, projection in [('human', {'content': '检索历史测试'}),
                                  ('assistant', {'content': '检索测试结束', 'toolRounds': [row]})]:
            body = {'commandId': cid + actor, 'actor': actor, 'kind': 'text', 'projection': projection}
            if actor == 'human':
                body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
            response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
            assert response.status == 200, response.text()
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        for iteration in range(2):
            chat = page.locator('#chatInner')
            expect(chat).to_contain_text('检索测试结束', timeout=20000)
            card = chat.locator('.ptool-convmeta-block').filter(has_text=keyword)
            expect(card).to_have_count(1)
            head = card.locator(':scope > summary')
            expect(head).to_contain_text(keyword)
            expect(head).to_contain_text('返回 0 条')
            expect(head).to_contain_text('你的其他会话')
            expect(head.locator('.ptool-convmeta-src')).to_have_count(0)
            head.click()
            expect(card).to_contain_text('未找到匹配的历史会话')
            assert 'No conversations found' not in card.inner_text()
            assert head.evaluate('node=>node.scrollWidth<=node.clientWidth+1')
            page.screenshot(path=str(tmp_path / f'search-receipt-{width}-{iteration}.png'), full_page=True)
            if iteration == 0:
                page.reload(wait_until='domcontentloaded')
                _dismiss_onboarding_modals(page)
        assert not errors
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
