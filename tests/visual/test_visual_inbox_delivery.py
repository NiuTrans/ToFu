"""Real app inbox notes from append_injection -> Turn projection -> persisted HTTP.

No provider, queue ACK or mailbox state is mocked/mutated. Dedicated completion
cards and generic incoming messages stay separate on desktop, phone and reload.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.visual

AGENT_TEXT = 'Agent message: <img src=x onerror="window.inboxXss=1"> & <script>window.inboxXss=2</script>'
LATER_TEXT = 'Later message: ' + 'unbroken-payload-' * 32
UNKNOWN_REASON = 'future-inbox-<svg/onload=window.inboxXss=3>'
UNKNOWN_TEXT = 'Unknown-reason message remains readable.'
ANSWER = 'Inbox delivery browser verification complete.'
COMPLETION = '<swarm-update agent-id="worker-one" role="reviewer" status="completed"><summary>Dedicated completion report</summary></swarm-update>'
LEGACY = 'Historical exact duplicate report'


def _backend_projection():
    from lib.tasks_pkg.context_composer.injections import append_injection
    from lib.turn_lifecycle import _task_projection

    task = {'id': 'inbox-browser-producer', '_attemptId': 'inbox-browser-attempt',
            'status': 'completed', 'content': ANSWER, 'toolRounds': []}
    messages = []
    completion = append_injection(messages, 'inbox_delivery', COMPLETION, task=task,
                                  round_num=0, display_owner='inbox-lanes')
    append_injection(messages, 'inbox_delivery', AGENT_TEXT, task=task, round_num=0)
    append_injection(messages, 'inbox_delivery', LATER_TEXT, task=task, round_num=1)
    # An old document contains both a generic note and the dedicated receipt.
    append_injection(messages, 'inbox_delivery', LEGACY, task=task, round_num=2)
    append_injection(messages, 'inbox_delivery', UNKNOWN_TEXT, task=task,
                     round_num=3, reason=UNKNOWN_REASON)
    task['_inboxInjects'] = [
        {'round': 1, 'count': 1, 'previews': [{'agentId': 'worker-one', 'text': COMPLETION}],
         'agentIds': ['worker-one'], 'taskId': task['id'], 'attemptId': task['_attemptId']},
        {'round': 3, 'count': 1, 'previews': [{'agentId': 'old-worker', 'text': LEGACY}],
         'agentIds': ['old-worker'], 'taskId': task['id'], 'attemptId': task['_attemptId']},
    ]
    projection = _task_projection(task, {})
    assert completion['_contextInjection']['displaySurface'] == 'sidecar'
    assert len([s for s in projection['segments'] if s['type'] == 'system_note']) == 4
    assert all(m['_contextInjection']['delivery'] == 'appended_not_acknowledged' for m in messages)
    return projection


@pytest.mark.parametrize('width,language,label', [(1280, 'en', 'Incoming message'), (390, 'zh', '外来消息')])
def test_inbox_delivery_notes_survive_real_surface_reload(browser, live_server, tmp_path, width, language, label):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    projection = _backend_projection()
    context = browser.new_context(service_workers='block', viewport={'width': width, 'height': 900})
    cid = 'inbox-display-' + uuid.uuid4().hex[:12]
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');"
                            f"localStorage.setItem('tofu_ui_lang',{json.dumps(language)});"
                            f"sessionStorage.setItem('tofu_activeConvId',{json.dumps(cid)});")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def append(suffix, actor, value, create=False):
        body = {'commandId': cid + suffix, 'actor': actor, 'kind': 'text', 'projection': value}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    try:
        append('-human', 'human', {'content': 'Synthetic inbox display verification'}, create=True)
        append('-assistant', 'assistant', projection)
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        for iteration in range(2):
            chat = page.locator('#chatInner')
            expect(chat).to_contain_text(ANSWER, timeout=20000)
            notes = chat.locator('.sw-system-note-row')
            expect(notes).to_have_count(3)
            incoming = chat.locator('.sw-system-note-row[data-reason="inbox_delivery"]')
            expect(incoming).to_have_count(2)
            for note in incoming.all():
                expect(note.locator('.sw-note-reason')).to_have_text(label)
                assert not note.evaluate('node=>node.open')
                note.locator(':scope > summary').click()
            expect(incoming.nth(0).locator('pre')).to_have_text(AGENT_TEXT)
            expect(incoming.nth(1).locator('pre')).to_have_text(LATER_TEXT)
            unknown = notes.filter(has_text=UNKNOWN_TEXT)
            expect(unknown).to_have_count(1)
            expect(unknown.locator('.sw-note-item-title')).to_have_text(UNKNOWN_REASON)
            unknown.locator(':scope > summary').click()
            expect(unknown.locator('pre')).to_have_text(UNKNOWN_TEXT)

            dedicated = chat.locator('.sw-inbox-row:not(.sw-system-note-row)')
            expect(dedicated).to_have_count(2)
            for card in dedicated.all():
                card.locator(':scope > summary').click()
            expect(dedicated.filter(has_text='Dedicated completion report')).to_have_count(1)
            expect(chat.get_by_text(LEGACY, exact=True)).to_have_count(1)
            assert chat.locator('.sw-system-note-row img, .sw-system-note-row script, .sw-note-reason svg').count() == 0
            assert page.evaluate('window.inboxXss || 0') == 0
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert incoming.evaluate_all('nodes=>nodes.every(node=>node.scrollWidth<=node.clientWidth+1)')
            page.screenshot(path=str(tmp_path / f'inbox-delivery-{width}-{iteration}.png'), full_page=True)
            if iteration == 0:
                page.reload(wait_until='domcontentloaded')
                _dismiss_onboarding_modals(page)
        assert not errors, errors
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
