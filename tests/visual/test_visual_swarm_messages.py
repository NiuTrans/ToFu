"""Chromium regression for mailbox roles, millisecond timestamps and availability."""
import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390])
def test_mailbox_parent_time_and_history_notice(frontend_visual_page, width):
    page = frontend_visual_page

    page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/08-swarm-messages.css'))
    page.set_viewport_size({'width': width, 'height': 900})
    page.add_script_tag(path=native_module_path(
        '.native/swarm-messages-visual.js',
        ROOT / 'frontend/src/conversation/ui/swarm-messages.ts'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate("""locale => {
      document.body.innerHTML = '<main id="fixture"></main>';
      window.snapshot = {swarm_id:'run', messageReceiptsAvailable:true,
        messageParticipants:[{id:'run',label:'Coordinator'}, {id:'worker',label:'coder'}],
        messageReceipts:[{swarmRunId:'run',agentId:'worker',messageId:'message',
          senderTaskId:'run',submittedAt:1792033483000,state:'input_confirmed',
          content:'Verified message body'}]};
      window.mailbox = mountSwarmMessages(document.querySelector('#fixture'), snapshot,
        key => locale[key] || key);
    }""", locale)
    direction = page.locator('.sw-messages-direction').inner_text()
    assert '协调者' not in direction
    assert '主' in direction
    assert page.locator('.sw-messages-time').inner_text().startswith('2026-10-')
    assert page.locator('.sw-messages-notice').is_hidden()
    page.locator('.sw-messages-content > summary').click()
    assert page.locator('.sw-messages-body').inner_text() == 'Verified message body'
    page.evaluate('mailbox.update({...snapshot, messageReceiptsAvailable:false})')
    assert page.locator('.sw-messages-notice').is_visible()
    assert page.locator('.sw-messages-body').inner_text() == 'Verified message body'
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')


@pytest.mark.parametrize('width,language', [(1280, 'en'), (390, 'zh')])
@pytest.mark.parametrize('content', [
    'Short unique message', '<img src=x onerror=alert(1)>\n' + 'Long unique message ' * 30, None,
])
def test_mailbox_has_one_content_presentation_per_disclosure_state(frontend_visual_page, width, language, content):
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    # Exercise the authored owner without regenerating unrelated delivery output.
    page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/08-swarm-messages.css'))
    page.add_script_tag(path=native_module_path(
        '.native/swarm-messages-visual.js',
        ROOT / 'frontend/src/conversation/ui/swarm-messages.ts'))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate("""({locale, content}) => {
      document.body.innerHTML = '<main id="fixture"></main>';
      window.snapshot = {swarm_id:'run', messageReceiptsAvailable:true,
        messageReceipts:[{swarmRunId:'run',agentId:'worker',messageId:'message',
          senderTaskId:'run',submittedAt:1792033483000,state:'submitted',content}]};
      window.mailbox = mountSwarmMessages(document.querySelector('#fixture'), snapshot,
        key => locale[key] || key);
    }""", {'locale': locale, 'content': content})
    body_text = content if content is not None else locale['tool.agentMessageUnavailable']
    preview_text = body_text[:160] + '…' if len(body_text) > 160 else body_text
    summary = page.locator('.sw-messages-content > summary')
    preview = summary.locator('.sw-messages-preview')
    collapse = summary.locator('.sw-messages-collapse')
    body = page.locator('.sw-messages-body')
    expect(preview).to_be_visible()
    expect(collapse).to_be_hidden()
    expect(body).to_be_hidden()
    expect(summary).to_have_accessible_name(preview_text)
    assert summary.inner_text() == ' '.join(preview_text.split())

    summary.focus()
    summary.press('Enter')
    expect(preview).to_be_hidden()
    expect(collapse).to_be_visible()
    expect(body).to_be_visible()
    expect(summary).to_have_accessible_name(locale['toolCmd.collapse'])
    assert summary.inner_text() == locale['toolCmd.collapse']
    assert body.text_content() == body_text
    expect(summary).to_be_focused()
    assert page.locator('#fixture img, #fixture script').count() == 0

    page.evaluate('mailbox.update({...snapshot, messageReceipts: snapshot.messageReceipts.map(r => ({...r, state:"input_confirmed"}))})')
    expect(preview).to_be_hidden()
    expect(body).to_be_visible()
    expect(summary).to_be_focused()
    summary.press('Space')
    expect(preview).to_be_visible()
    expect(collapse).to_be_hidden()
    expect(body).to_be_hidden()
    expect(summary).to_have_accessible_name(preview_text)
    expect(summary).to_be_focused()
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
