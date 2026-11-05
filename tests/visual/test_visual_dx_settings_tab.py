"""Real Chromium: Settings lists 大象 and its panel renders the live channel state.

The 大象 tab sits in the 连接 group right after 飞书. Opening it renders the
panel from the application's own /api/v4/channels endpoints over isolated test
storage (no route interception), and the assistant's ``setting:dx`` jump chip
lands on the same tab.

A connected account cannot exist without a real 大象 login, and creating a
command chat for real creates a real 大象 group, so the connected-account
journeys answer the /api/v4/channels endpoints in the page; the panel, its
styles and the native ``<dialog>`` are the shipped ones.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.support.channel_settings_layout import assert_channel_layout_aligned

pytestmark = pytest.mark.visual


def _open_app_in_chinese(page, live_server):
    page.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1');"
                         "localStorage.setItem('tofu_ui_lang', 'zh')")
    page.goto(live_server, wait_until='networkidle')
    page.wait_for_function("typeof window.openSettings === 'function'")


def test_dx_tab_follows_feishu_and_renders_live_channel_state(page, live_server, screenshot_dir):
    _open_app_in_chinese(page, live_server)
    page.evaluate('() => window.openSettings()')
    tab = page.locator('.settings-tab[data-tab="dx"]')
    tab.wait_for(state='visible')
    assert tab.inner_text().strip() == '大象'
    tabs = page.eval_on_selector_all('.settings-tab', 'els => els.map(el => el.dataset.tab)')
    assert tabs.index('dx') == tabs.index('feishu') + 1

    tab.click()
    page.wait_for_selector('#settingsTab_dx .channel-card', state='visible')
    titles = page.locator('#settingsTab_dx .channel-card-title').all_inner_texts()
    assert len(titles) == 4, titles
    assert page.locator('#settingsTab_dx .ui-alert--error').count() == 0
    account_card = page.locator('#settingsTab_dx .channel-card').first
    assert account_card.locator('button', has_text='启用').count() == 1
    # Until the account is live the command chat says what it waits for instead of offering dead buttons.
    command_chat = page.locator('#settingsTab_dx .channel-card').nth(1)
    assert command_chat.locator('button').count() == 0
    assert '启用大象账号后' in command_chat.inner_text()
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-desktop.png'))

    page.set_viewport_size({'width': 390, 'height': 844})
    assert not page.locator('#settingsTab_dx').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-mobile.png'))


def test_setting_dx_jump_chip_opens_the_dx_tab(page, live_server):
    _open_app_in_chinese(page, live_server)
    # Same markup markdown/html-policy.ts emits for an assistant link to setting:dx.
    page.evaluate("""() => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'md-ui-jump';
      chip.id = 'dxJumpChip';
      chip.setAttribute('data-tofu-action', "jumpToSetting('dx')");
      chip.textContent = '大象';
      document.body.appendChild(chip);
    }""")
    page.locator('#dxJumpChip').click()
    page.wait_for_selector('#settingsTab_dx .channel-card', state='visible')
    assert page.locator('.settings-tab[data-tab="dx"]').evaluate("el => el.classList.contains('active')")
    assert page.locator('#settingsTab_dx').evaluate("el => el.classList.contains('active')")


# ── A connected account, answered in the page ─────────────────────────

_CONNECTED = {'state': 'connected', 'detail': '', 'updatedAtMs': 1, 'connectedAtMs': 1}
_ROUTER_GID = 'dx:gid:900'


def _thread(key, title, kind, *, command=False):
    return {'key': key, 'kind': kind, 'title': title,
            'mode': 'command' if command else ('mentions' if kind == 'uid' else 'ignore'),
            'modeOverride': None, 'replyPolicy': 'draft', 'replyPolicyOverride': None,
            'isCommandChat': command, 'subscribers': [], 'lastEventAtMs': 1, 'watermark': None}


class _ConnectedDx:
    """The owner's connected 大象 account behind /api/v4/channels; records every write it receives."""

    def __init__(self, page):
        self.command_chat = None
        self.writes = []
        self.threads = [_thread('dx:gid:70565831967', 'CMU-YourProvider AutoResearch', 'gid'),
                        _thread('dx:uid:2002', 'Ann', 'uid')]
        page.route(re.compile(r'/api/v4/channels/dx(\?.*)?$'), self._account)
        page.route(re.compile(r'/api/v4/channels/dx/threads(\?.*)?$'), self._threads)
        page.route(re.compile(r'/api/v4/channels/dx/command-chat(\?.*)?$'), self._create)
        page.route(re.compile(r'/api/v4/channels/outbox(\?.*)?$'), self._outbox)

    def view(self):
        return {'channel': 'dx', 'enabled': True,
                'identity': {'uid': '1001', 'mis': 'owner', 'name': '负责人'},
                'commandChat': self.command_chat, 'status': _CONNECTED,
                'skillInstalled': True, 'routerConversationId': None}

    def _account(self, route):
        if route.request.method != 'GET':
            self.writes.append((route.request.method, urlparse(route.request.url).path))
        route.fulfill(json=self.view())

    def _threads(self, route):
        route.fulfill(json={'items': self.threads, 'nextCursor': None})

    def _create(self, route):
        self.writes.append((route.request.method, urlparse(route.request.url).path))
        self.command_chat = _ROUTER_GID
        self.threads.insert(0, _thread(_ROUTER_GID, 'Tofu Router', 'gid', command=True))
        route.fulfill(json=self.view())

    def _outbox(self, route):
        assert parse_qs(urlparse(route.request.url).query).get('channel') == ['dx'], route.request.url
        route.fulfill(json={'items': [{
            'id': 'ob1', 'thread': 'dx:uid:2002', 'threadTitle': 'Ann',
            'text': '[Tofu 代发] 方案 B 已合入，今晚发布。', 'replyTo': None, 'state': 'draft',
            'origin': {'kind': 'tool', 'conversationId': 'router-conv', 'turnId': 't'},
            'createdAtMs': 1, 'updatedAtMs': 1, 'externalId': None, 'error': None}]})


_CONFIRM_GONE = "() => ![...document.querySelectorAll('dialog')].some((node) => node.textContent.includes('只有你一个成员'))"


def test_create_command_chat_confirms_in_a_native_modal_then_rereads(page, live_server, screenshot_dir):
    dx = _ConnectedDx(page)
    _open_app_in_chinese(page, live_server)
    page.evaluate('() => window.openSettings()')
    page.locator('.settings-tab[data-tab="dx"]').click()
    panel = page.locator('#settingsTab_dx')
    chat = panel.locator('.channel-card').nth(1)
    create = chat.get_by_role('button', name='创建“Tofu Router”群', exact=True)
    create.wait_for(state='visible')
    assert chat.locator('.channel-card-head .channel-chip').inner_text() == '未选择'
    assert chat.get_by_role('button', name='设为指令会话', exact=True).is_enabled()
    assert_channel_layout_aligned(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-connected.png'))

    # Escape is the platform's cancel: nothing is created and the dialog leaves the document.
    create.click()
    dialog = page.locator('dialog.ui-dialog[open]', has_text='只有你一个成员')
    dialog.wait_for(state='visible')
    page.keyboard.press('Escape')
    page.wait_for_function(_CONFIRM_GONE)
    assert dx.writes == [] and create.is_enabled()

    create.click()
    dialog.wait_for(state='visible')
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-create-confirm.png'), animations='disabled')
    dialog.get_by_role('button', name='创建“Tofu Router”群', exact=True).click()
    page.wait_for_function(_CONFIRM_GONE)
    page.wait_for_function("""() => document.querySelectorAll('#settingsTab_dx .channel-card')[1]
      ?.querySelector('.channel-card-head .channel-chip')?.textContent === 'Tofu Router'""")
    assert dx.writes == [('POST', '/api/v4/channels/dx/command-chat')]
    assert chat.get_by_role('button', name='创建“Tofu Router”群', exact=True).count() == 0
    assert chat.get_by_role('button', name='设为指令会话', exact=True).is_disabled()
    first_thread = panel.locator('.channel-row').first
    assert 'Tofu Router' in first_thread.inner_text() and '指令' in first_thread.inner_text()
    assert_channel_layout_aligned(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-command-chat-created.png'), full_page=True)

    page.set_viewport_size({'width': 390, 'height': 844})
    assert not panel.evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    assert panel.locator('.channel-table-head').is_hidden()
    assert panel.locator('.channel-row .channel-cell label').first.is_visible()
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-connected-mobile.png'))
