"""Real Chromium: Settings lists GitHub after 大象 and its panel renders the live channel state.

The GitHub tab sits in the 连接 group right after 大象. Opening it renders the
panel from the application's own /api/v4/channels endpoints over isolated test
storage (no route interception, no GitHub token, no network to GitHub), and the
assistant's ``setting:github`` jump chip lands on the same tab.

A connected account needs a real GitHub token, so the connected-account
journey answers the /api/v4/channels endpoints in the page; the panel and its
styles are the shipped ones.
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


def test_github_tab_follows_dx_and_renders_live_channel_state(page, live_server, screenshot_dir):
    _open_app_in_chinese(page, live_server)
    page.evaluate('() => window.openSettings()')
    tab = page.locator('.settings-tab[data-tab="github"]')
    tab.wait_for(state='visible')
    assert tab.inner_text().strip() == 'GitHub'
    tabs = page.eval_on_selector_all('.settings-tab', 'els => els.map(el => el.dataset.tab)')
    assert tabs.index('github') == tabs.index('dx') + 1

    tab.click()
    page.wait_for_selector('#settingsTab_github .channel-card', state='visible')
    titles = page.locator('#settingsTab_github .channel-card-title').all_inner_texts()
    assert len(titles) == 5, titles
    assert page.locator('#settingsTab_github .ui-alert--error').count() == 0
    account_card = page.locator('#settingsTab_github .channel-card').first
    enable = account_card.locator('button', has_text='启用')
    assert enable.count() == 1 and enable.is_disabled()  # no token yet
    assert '先在下方添加 GitHub 令牌' in account_card.inner_text()
    token_card = page.locator('#settingsTab_github .channel-card').nth(1)
    verify = token_card.get_by_role('button', name='验证并启用', exact=True)
    assert verify.count() == 1 and verify.is_enabled()  # the token is the one remaining step
    assert page.locator('#settingsTab_github input[type="password"]').count() == 1
    page.screenshot(path=str(Path(screenshot_dir) / 'github-settings-desktop.png'))

    page.set_viewport_size({'width': 390, 'height': 844})
    assert not page.locator('#settingsTab_github').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    page.screenshot(path=str(Path(screenshot_dir) / 'github-settings-mobile.png'))


def test_a_refused_token_is_explained_beside_its_input(page, live_server, screenshot_dir):
    _open_app_in_chinese(page, live_server)
    page.evaluate('() => window.openSettings()')
    page.locator('.settings-tab[data-tab="github"]').click()
    token_card = page.locator('#settingsTab_github .channel-card').nth(1)
    secret = token_card.locator('input[type="password"]')
    secret.wait_for(state='visible')
    # The server refuses this form before it would ask GitHub, so nothing leaves the machine.
    secret.fill('ghp_not a token')
    verify = token_card.get_by_role('button', name='验证并启用', exact=True)
    verify.click()
    refusal = token_card.locator('.channel-feedback')
    refusal.wait_for(state='visible')
    text = refusal.inner_text()
    assert text.startswith('令牌未通过验证') and 'personal access token' in text, text
    assert secret.input_value() == '' and verify.is_enabled()
    refusal.scroll_into_view_if_needed()
    page.screenshot(path=str(Path(screenshot_dir) / 'github-settings-token-refused.png'))


def test_setting_github_jump_chip_opens_the_github_tab(page, live_server):
    _open_app_in_chinese(page, live_server)
    # Same markup markdown/html-policy.ts emits for an assistant link to setting:github.
    page.evaluate("""() => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'md-ui-jump';
      chip.id = 'githubJumpChip';
      chip.setAttribute('data-tofu-action', "jumpToSetting('github')");
      chip.textContent = 'GitHub';
      document.body.appendChild(chip);
    }""")
    page.locator('#githubJumpChip').click()
    page.wait_for_selector('#settingsTab_github .channel-card', state='visible')
    assert page.locator('.settings-tab[data-tab="github"]').evaluate("el => el.classList.contains('active')")
    assert page.locator('#settingsTab_github').evaluate("el => el.classList.contains('active')")


def _issue(key, title, *, mode=None):
    return {'key': key, 'kind': 'issue', 'title': title, 'mode': mode or 'mentions', 'modeOverride': mode,
            'replyPolicy': 'notify', 'replyPolicyOverride': None, 'isCommandChat': False, 'subscribers': [],
            'lastEventAtMs': 1, 'watermark': None, 'note': ''}


def _route_connected_github(page):
    account = {
        'channel': 'github', 'enabled': True,
        'identity': {'uid': '583231', 'mis': 'octo-owner', 'name': 'Octo Owner'},
        'status': {'state': 'connected', 'detail': '', 'updatedAtMs': 1, 'connectedAtMs': 1},
        'credential': {'name': 'channel.u1.github.token', 'hint': 'ghp_…0001', 'managed': True},
        'apiBaseUrl': 'https://api.github.com', 'webBaseUrl': 'https://github.com',
        'poll': {'intervalS': 60, 'lastPollAtMs': 1, 'nextPollAtMs': 61_000},
        'routerConversationId': None,
    }
    threads = [_issue('github:octo/repo#12', 'Fix the poller', mode='watch'),
               _issue('github:octo/other#3', 'Crash when the token expires')]
    outbox = [{'id': 'ob1', 'thread': 'github:octo/repo#12', 'threadTitle': 'Fix the poller',
               'text': '[Tofu 代发] Plan B is in; releasing tonight.', 'replyTo': None, 'state': 'draft',
               'origin': {'kind': 'tool', 'conversationId': 'router-conv', 'turnId': 't'},
               'createdAtMs': 1, 'updatedAtMs': 1, 'externalId': None, 'error': None, 'attachments': [],
               'approvalCode': '0A1B2C', 'authority': 'third_party', 'decision': None}]

    def answer_outbox(route):
        assert parse_qs(urlparse(route.request.url).query).get('channel') == ['github'], route.request.url
        route.fulfill(json={'items': outbox})

    page.route(re.compile(r'/api/v4/channels/github(\?.*)?$'), lambda route: route.fulfill(json=account))
    page.route(re.compile(r'/api/v4/channels/github/threads(\?.*)?$'),
               lambda route: route.fulfill(json={'items': threads, 'nextCursor': None}))
    page.route(re.compile(r'/api/v4/channels/outbox(\?.*)?$'), answer_outbox)


def test_connected_github_tab_keeps_its_rows_aligned(page, live_server, screenshot_dir):
    _route_connected_github(page)
    _open_app_in_chinese(page, live_server)
    page.evaluate('() => window.openSettings()')
    page.locator('.settings-tab[data-tab="github"]').click()
    panel = page.locator('#settingsTab_github')
    panel.locator('.channel-row').first.wait_for(state='visible')
    assert panel.locator('.channel-card').first.locator('.channel-card-head .channel-status').inner_text() == '已连接'
    assert panel.locator('.channel-row').count() == 2
    assert panel.locator('.channel-outbox-item').count() == 1
    panel.locator('.channel-advanced > summary').click()
    assert panel.get_by_role('button', name='保存', exact=True).is_disabled()  # base URL is fixed while enabled
    assert_channel_layout_aligned(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'github-settings-connected.png'), full_page=True)

    page.set_viewport_size({'width': 390, 'height': 844})
    assert not panel.evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    assert panel.locator('.channel-table-head').is_hidden()
    page.screenshot(path=str(Path(screenshot_dir) / 'github-settings-connected-mobile.png'))
