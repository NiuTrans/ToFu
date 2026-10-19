"""Actual app revision journey: edit disclosure -> quote -> draft -> ordinary send.

Seeds only retained conversation evidence through the public settled-turn API.
The submission transport deliberately fails, proving existing draft/quote
rollback without running a model or writing a project file.
"""
import json
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
@pytest.mark.parametrize('width', [390, 1280])
def test_diff_revision_quote_and_send_rollback(browser, live_server, tmp_path, theme, width):
    from tests.conftest import _dismiss_onboarding_modals
    from tests.visual.test_visual_surfaces import _wait_ready

    context = browser.new_context(service_workers='block', viewport={'width': width, 'height': 900})
    context.add_init_script(f"localStorage.setItem('tofu_onboarding_v1_done','1');localStorage.setItem('tofu_ui_lang','zh');localStorage.setItem('claude_ui_theme','{theme}');")
    cid = 'diff-review-' + uuid.uuid4().hex[:12]
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId',{json.dumps(cid)});")
    row = {'roundNum': 1, 'toolCallId': 'retry-edit', 'toolName': 'apply_diff', 'status': 'done',
           'query': 'src/retry.ts', 'toolArgs': {'path': 'src/retry.ts',
                                              'search': 'return retryImmediately();',
                                              'replace': 'return retryWithBackoff();'},
           'results': [{'writeOk': True}], 'toolContent': 'Applied the change.'}
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        for actor, projection in [('human', {'content': 'Improve retries.'}),
                                  ('assistant', {'content': 'Retry behavior updated.', 'toolRounds': [row]})]:
            body = {'commandId': cid + actor, 'actor': actor, 'kind': 'text', 'projection': projection}
            if actor == 'human':
                body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
            response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
            assert response.status == 200, response.text()
        page.goto(live_server, wait_until='domcontentloaded')
        _wait_ready(page)
        _dismiss_onboarding_modals(page)
        item = page.locator(f'.conv-item[data-conv-id="{cid}"]')
        expect(item).to_have_count(1, timeout=20000)
        expect(page.locator('#chatInner')).to_contain_text('Retry behavior updated.', timeout=20000)
        process = page.locator('#chatInner .conversation-history-process')
        if process.count() and not process.evaluate('node => node.open'):
            process.locator(':scope > summary').click()
        card = page.locator('#chatInner .ptool-batch-done-block')
        expect(card).to_have_count(1, timeout=20000)
        card.locator(':scope > summary').click()
        action = card.get_by_role('button', name='要求修改', exact=True)
        expect(action).to_be_visible()
        code = card.locator('.bdiff-add code')
        code.evaluate("""node => {
            const range=document.createRange();range.selectNodeContents(node);
            const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
        }""")
        draft = page.locator('#userInput')
        # Preserve a pre-existing request while selecting code in the transcript.
        page.evaluate("document.getElementById('userInput').value='Add a retry limit.'")
        action.click()
        expect(page.locator('.reply-quote-chip')).to_have_count(1)
        expect(page.locator('.reply-quote-chip-label')).to_contain_text('src/retry.ts')
        assert draft.input_value() == 'Add a retry limit.'
        assert draft.evaluate('node => node===document.activeElement')
        assert draft.evaluate('node => node.selectionStart===node.value.length')
        # Removing context never clears the typed requirement.
        page.locator('.reply-quote-chip-close').click()
        expect(page.locator('#reply-quote-container')).to_be_hidden()
        assert draft.input_value() == 'Add a retry limit.'
        code.evaluate("""node => {
            const range=document.createRange();range.selectNodeContents(node);
            getSelection().removeAllRanges();getSelection().addRange(range);
        }""")
        action.click()
        expect(page.locator('.reply-quote-chip')).to_have_count(1)
        # Native keyboard activation exercises the whole-change fallback too.
        action.focus()
        action.press('Enter')
        expect(page.locator('.reply-quote-chip')).to_have_count(2)
        action.press('Enter')
        expect(page.locator('.reply-quote-chip')).to_have_count(2)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.screenshot(path=str(tmp_path / f'diff-review-{theme}-{width}.png'), full_page=True, animations='disabled')
        submissions = []

        def fail_submission(route):
            submissions.append(route.request.post_data_json)
            route.fulfill(status=503, json={'ok': False, 'error': 'controlled_transport_failure',
                                           'message': 'Controlled test failure'})

        page.route(f'**/api/v4/conversations/{cid}/turns', fail_submission)
        with page.expect_request(lambda request: request.method == 'POST'
                                 and request.url.endswith(f'/api/v4/conversations/{cid}/turns')):
            page.locator('#sendBtn').click()
        page.wait_for_function("document.getElementById('userInput').value==='Add a retry limit.'")
        expect(page.locator('.reply-quote-chip')).to_have_count(2)
        assert submissions
        assert len({body['commandId'] for body in submissions}) == 1
        quote = submissions[0]['message']['replyQuotes'][0]
        assert submissions[0]['message']['text'] == 'Add a retry limit.'
        assert 'src/retry.ts' in quote and '+return retryWithBackoff();' in quote
        assert '-return retryImmediately();' not in quote
        assert 'retry-edit' in quote
        assert '修改前请先读取并核对当前文件' in quote
        assert not errors
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
