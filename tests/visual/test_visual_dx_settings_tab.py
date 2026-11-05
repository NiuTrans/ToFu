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
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from tests.support.channel_settings_layout import assert_channel_layout_aligned

pytestmark = pytest.mark.visual


def _open_app_in_chinese(page, live_server):
    page.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1');"
                         "localStorage.setItem('tofu_ui_lang', 'zh')")
    page.goto(live_server, wait_until='networkidle')
    page.wait_for_function("typeof window.openSettings === 'function'")


def _open_dx_tab(page):
    page.evaluate('() => window.openSettings()')
    page.locator('.settings-tab[data-tab="dx"]').click()
    return page.locator('#settingsTab_dx')


def _no_horizontal_overflow(panel) -> bool:
    return not panel.evaluate('(el) => el.scrollWidth > el.clientWidth + 2')


def test_dx_tab_follows_feishu_and_renders_live_channel_state(page, live_server, screenshot_dir):
    _open_app_in_chinese(page, live_server)
    page.evaluate('() => window.openSettings()')
    tab = page.locator('.settings-tab[data-tab="dx"]')
    tab.wait_for(state='visible')
    assert tab.inner_text().strip() == '大象'
    tabs = page.eval_on_selector_all('.settings-tab', 'els => els.map(el => el.dataset.tab)')
    assert tabs.index('dx') == tabs.index('feishu') + 1

    tab.click()
    # Each card renders when its own read answers.
    page.wait_for_function("() => document.querySelectorAll('#settingsTab_dx .channel-card').length === 4")
    titles = page.locator('#settingsTab_dx .channel-card-title').all_inner_texts()
    assert titles == ['状态', '指令会话', '会话', '发件箱'], titles
    assert page.locator('#settingsTab_dx .ui-alert--error').count() == 0
    account_card = page.locator('#settingsTab_dx .channel-card').first
    assert account_card.get_by_role('button', name='启用', exact=True).count() == 1
    # Until the account is live the command chat says what it waits for instead of offering dead buttons.
    command_chat = page.locator('#settingsTab_dx .channel-card').nth(1)
    assert command_chat.locator('button').count() == 0
    assert '启用并确认后' in command_chat.inner_text()
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-desktop.png'))

    page.set_viewport_size({'width': 390, 'height': 844})
    assert _no_horizontal_overflow(page.locator('#settingsTab_dx'))
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
_DEFAULT_MODE = {'uid': 'mentions', 'gid': 'ignore', 'pid': 'ignore'}
_OWNER = {'uid': '1001', 'mis': 'owner', 'name': '负责人'}


def _thread(key, title, kind, *, command=False, last=1):
    return {'key': key, 'kind': kind, 'title': title,
            'mode': 'command' if command else _DEFAULT_MODE[kind],
            'modeOverride': None, 'replyPolicy': 'draft', 'replyPolicyOverride': None,
            'isCommandChat': command, 'subscribers': [], 'lastEventAtMs': last, 'watermark': None}


class _ConnectedDx:
    """The owner's connected 大象 account behind /api/v4/channels; records every write it receives."""

    def __init__(self, page, threads=None):
        self.command_chat = None
        self.identity = dict(_OWNER)
        self.login_mis = 'owner'
        self.status = dict(_CONNECTED)
        self.writes = []
        self.threads = threads if threads is not None else [
            _thread('dx:gid:70565831967', 'CMU-Meituan AutoResearch', 'gid'), _thread('dx:uid:2002', 'Ann', 'uid')]
        page.route(re.compile(r'/api/v4/channels/dx(\?.*)?$'), self._account)
        page.route(re.compile(r'/api/v4/channels/dx/threads(\?.*)?$'), self._threads)
        page.route(re.compile(r'/api/v4/channels/dx/threads/[^/?]+$'), self._thread_update)
        page.route(re.compile(r'/api/v4/channels/dx/command-chat(\?.*)?$'), self._create)
        page.route(re.compile(r'/api/v4/channels/outbox(\?.*)?$'), self._outbox)

    def view(self):
        return {'channel': 'dx', 'enabled': True, 'loginMis': self.login_mis, 'identity': self.identity,
                'commandChat': self.command_chat, 'status': self.status,
                'skillInstalled': True, 'routerConversationId': None}

    def _account(self, route):
        if route.request.method == 'PUT':
            body = route.request.post_data_json
            self.writes.append(('PUT', urlparse(route.request.url).path, body))
            if 'mis' in body and body['mis'] != self.login_mis:
                # Another account: no identity until its login is approved in 大象.
                self.login_mis, self.identity, self.command_chat = body['mis'], None, None
                self.status = {'state': 'authorizing', 'detail': '', 'updatedAtMs': 2, 'connectedAtMs': None}
        route.fulfill(json=self.view())

    def _threads(self, route):
        route.fulfill(json={'items': self.threads, 'nextCursor': None})

    def _thread_update(self, route):
        key = unquote(urlparse(route.request.url).path.rsplit('/', 1)[1])
        body = route.request.post_data_json
        self.writes.append(('PUT', key, body))
        thread = next(thread for thread in self.threads if thread['key'] == key)
        if 'mode' in body:
            thread['modeOverride'] = body['mode']
            thread['mode'] = body['mode'] or _DEFAULT_MODE[thread['kind']]
        if 'replyPolicy' in body:
            thread['replyPolicyOverride'] = body['replyPolicy']
            thread['replyPolicy'] = body['replyPolicy'] or 'draft'
        route.fulfill(json=thread)

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
    panel = _open_dx_tab(page)
    chat = panel.locator('.channel-card').nth(1)
    create = chat.get_by_role('button', name='创建“Tofu Router”群', exact=True)
    create.wait_for(state='visible')
    assert chat.locator('.channel-card-head .channel-chip').inner_text() == '未选择'
    # An existing chat is picked from a searchable list that leaves out the chat already chosen.
    chat.get_by_role('button', name='选择已有会话', exact=True).click()
    assert chat.locator('.dx-chat-option').count() == 2
    assert chat.get_by_role('button', name='设为指令会话', exact=True).first.is_enabled()
    chat.get_by_role('button', name='选择已有会话', exact=True).click()
    assert chat.locator('.dx-chat-picker').count() == 0
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
    assert chat.get_by_role('button', name='更换', exact=True).is_visible()
    first_thread = panel.locator('.dx-thread-row').first
    assert 'Tofu Router' in first_thread.inner_text() and '指令' in first_thread.inner_text()
    assert_channel_layout_aligned(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-command-chat-created.png'), full_page=True)

    page.set_viewport_size({'width': 390, 'height': 844})
    assert _no_horizontal_overflow(panel)
    assert panel.locator('.dx-thread-row .dx-thread-mode label').first.is_visible()
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-connected-mobile.png'))


def test_switching_the_account_answers_at_once_and_follows_the_approval(page, live_server, screenshot_dir):
    dx = _ConnectedDx(page)
    _open_app_in_chinese(page, live_server)
    panel = _open_dx_tab(page)
    account = panel.locator('.channel-card').first
    account.get_by_text('负责人 (owner) · 1001').wait_for(state='visible')
    account.get_by_role('button', name='切换账号', exact=True).click()
    mis = account.get_by_label('大象账号（MIS）')
    assert mis.input_value() == 'owner'
    mis.fill('not an account')
    mis.press('Enter')
    assert account.get_by_text('请输入大象 MIS 账号').is_visible() and dx.writes == []
    assert_channel_layout_aligned(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-switch-account.png'))

    mis.fill(' Alice ')
    account.get_by_role('button', name='登录此账号', exact=True).click()
    # The command answers at once; the card then says what the owner has to do.
    account.get_by_text('请打开大象 App，同意 alice 的登录请求').wait_for(state='visible')
    assert dx.writes == [('PUT', '/api/v4/channels/dx', {'mis': 'alice', 'enabled': True})]
    assert account.locator('.channel-card-head .channel-chip').inner_text() == '等待大象确认登录'
    assert '正在确认 alice' in account.inner_text()
    assert account.locator('[aria-busy="true"]').count() == 0
    assert '启用并确认后' in panel.locator('.channel-card').nth(1).inner_text()
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-authorizing.png'))

    # Approved in 大象: the tab's own re-read of the account picks it up.
    dx.identity, dx.status = {'uid': '3003', 'mis': 'alice', 'name': 'Alice'}, dict(_CONNECTED)
    account.get_by_text('Alice (alice) · 3003').wait_for(state='visible', timeout=10_000)
    assert account.locator('.channel-card-head .channel-chip').inner_text() == '已连接'


def test_many_groups_are_searched_filtered_and_set_in_bulk(page, live_server, screenshot_dir):
    threads = [_thread(f'dx:gid:{7000 + n}', f'项目群 {n:02d}' if n % 3 else f'值班群 {n:02d}', 'gid', last=100 - n)
               for n in range(30)]
    threads += [_thread(f'dx:uid:{2000 + n}', f'同事 {n}', 'uid', last=10 - n) for n in range(4)]
    dx = _ConnectedDx(page, threads)
    _open_app_in_chinese(page, live_server)
    panel = _open_dx_tab(page)
    rows = panel.locator('.dx-thread-row:not([hidden])')
    rows.first.wait_for(state='visible')
    filters = panel.locator('.dx-thread-filter label')
    assert filters.all_inner_texts() == ['全部 34', '全部消息 0', '仅 @我 4', '忽略 30']
    assert rows.count() == 34
    assert_channel_layout_aligned(panel)
    panel.locator('.dx-threads').evaluate('(el) => el.scrollIntoView()')
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-threads.png'))

    # One click sets a thread's mode; the filter counts follow the acknowledged view.
    duty = rows.filter(has_text='值班群 03')
    duty.get_by_label('全部消息', exact=True).check()
    page.wait_for_function("() => document.querySelector('#settingsTab_dx .dx-thread-filter')"
                           "?.textContent.includes('全部消息 1')")
    assert dx.writes == [('PUT', 'dx:gid:7003', {'mode': 'watch'})]

    panel.get_by_placeholder('按名称或 ID 搜索').fill('值班')
    assert rows.count() == 10
    panel.locator('.dx-thread-bulk').get_by_label('全选', exact=True).check()
    assert panel.locator('.dx-thread-selected').inner_text() == '已选 10 个'
    panel.get_by_role('button', name='批量设置', exact=True).click()
    panel.locator('.dx-threads').evaluate('(el) => el.scrollIntoView()')
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-threads-bulk.png'))
    panel.get_by_role('menuitem', name='模式：仅 @我', exact=True).click()
    page.wait_for_function("() => document.querySelector('#settingsTab_dx .dx-thread-filter')"
                           "?.textContent.includes('仅 @我 14')")
    bulk = [write for write in dx.writes[1:]]
    assert len(bulk) == 10 and all(body == {'mode': 'mentions'} for _, _, body in bulk)
    assert {key for _, key, _ in bulk} == {f'dx:gid:{7000 + n}' for n in range(0, 30, 3)}

    panel.locator('.dx-thread-filter').get_by_label('仅 @我 14', exact=True).check()
    panel.get_by_placeholder('按名称或 ID 搜索').fill('')
    assert rows.count() == 14

    page.set_viewport_size({'width': 390, 'height': 844})
    assert _no_horizontal_overflow(panel)
    panel.locator('.dx-threads').evaluate('(el) => el.scrollIntoView()')
    page.screenshot(path=str(Path(screenshot_dir) / 'dx-settings-threads-mobile.png'))
