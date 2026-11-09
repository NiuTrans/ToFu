"""Real Chromium: one 消息平台 tab holds every chat platform, each collapsed with its status.

Settings shows a single 消息平台 tab instead of one tab per platform. Each
platform (飞书, 大象, GitHub, 企业微信, QQ, 微信) is a collapsed section whose
summary is its name and a one-line status read from the application's own
endpoints over isolated test storage (no route interception). Expanding a bot
platform renders its panel, and ``setting:<platform>`` jump chips open the tab
with that section expanded. Nothing here submits credentials or starts a 微信
login, so no platform is ever contacted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.channel_settings_layout import assert_channel_layout_aligned

pytestmark = pytest.mark.visual

PLATFORMS = ['feishu', 'dx', 'github', 'wecom', 'qq', 'wechat']
NAMES = ['飞书', '大象', 'GitHub', '企业微信', 'QQ', '微信']


def _open_app_in_chinese(page, live_server):
    page.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1');"
                         "localStorage.setItem('tofu_ui_lang', 'zh')")
    page.goto(live_server, wait_until='networkidle')
    page.wait_for_function("typeof window.openSettings === 'function'")


def _open_messaging_tab(page):
    page.evaluate('() => window.openSettings()')
    page.locator('.settings-tab[data-tab="messaging"]').click()
    # Every status line fills in from its own read.
    page.wait_for_function("""() => [...document.querySelectorAll('#messagingPlatforms .msg-platform-status')]
      .every((el) => el.textContent.trim())""")
    return page.locator('#settingsTab_messaging')


def _sections(page):
    return page.eval_on_selector_all('#messagingPlatforms > details', """els => els.map((el) => ({
      platform: el.dataset.platform, open: el.open,
      name: el.querySelector('.msg-platform-name').textContent.trim(),
      status: el.querySelector('.msg-platform-status').textContent.trim()}))""")


def _no_horizontal_overflow(panel) -> bool:
    return not panel.evaluate('(el) => el.scrollWidth > el.clientWidth + 2')


def test_one_tab_lists_every_platform_collapsed_with_a_status_line(page, live_server, screenshot_dir):
    _open_app_in_chinese(page, live_server)
    page.evaluate('() => window.openSettings()')
    tabs = page.eval_on_selector_all('.settings-tab', 'els => els.map(el => el.dataset.tab)')
    assert 'messaging' in tabs and not set(PLATFORMS) & set(tabs), tabs
    assert page.locator('.settings-tab[data-tab="messaging"]').inner_text().strip() == '消息平台'

    panel = _open_messaging_tab(page)
    sections = _sections(page)
    assert [section['platform'] for section in sections] == PLATFORMS
    assert [section['name'] for section in sections] == NAMES
    assert not any(section['open'] for section in sections)
    # Isolated storage holds no account of these platforms.
    assert [section['status'] for section in sections[1:]] == ['未设置'] * 5, sections
    for platform in PLATFORMS:
        assert not page.locator(f'#settingsTab_{platform}').is_visible()
    page.screenshot(path=str(Path(screenshot_dir) / 'messaging-settings-collapsed-desktop.png'))

    page.set_viewport_size({'width': 390, 'height': 844})
    assert _no_horizontal_overflow(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'messaging-settings-collapsed-mobile.png'))


def test_expanding_wecom_renders_its_live_panel_and_keeps_the_others_collapsed(page, live_server,
                                                                                 screenshot_dir):
    _open_app_in_chinese(page, live_server)
    _open_messaging_tab(page)
    page.locator('#msgPlatform_wecom > summary').click()
    panel = page.locator('#settingsTab_wecom')
    page.wait_for_function("() => document.querySelectorAll('#settingsTab_wecom .channel-card').length === 6")
    titles = panel.locator('.channel-card-title').all_inner_texts()
    assert titles == ['状态', '应用凭证', '绑定你的账号', '设置步骤', '会话', '发件箱'], titles
    assert panel.locator('.ui-alert--error').count() == 0
    account_card = panel.locator('.channel-card').first
    enable = account_card.get_by_role('button', name='启用', exact=True)
    assert enable.count() == 1 and enable.is_disabled()  # no credentials yet
    assert '先在下方填写应用凭证。' in account_card.inner_text()
    secret = panel.locator('input[type="password"]')
    assert secret.count() == 1 and secret.input_value() == ''
    assert panel.get_by_role('button', name='生成配对码', exact=True).is_disabled()
    assert panel.locator('.channel-steps li').count() == 3
    assert [section['open'] for section in _sections(page)] == [p == 'wecom' for p in PLATFORMS]
    assert_channel_layout_aligned(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'messaging-settings-wecom-desktop.png'))

    page.set_viewport_size({'width': 390, 'height': 844})
    assert _no_horizontal_overflow(panel)
    page.screenshot(path=str(Path(screenshot_dir) / 'messaging-settings-wecom-mobile.png'))


def test_wechat_binds_by_qr_login_without_a_secret_field(page, live_server, screenshot_dir):
    _open_app_in_chinese(page, live_server)
    _open_messaging_tab(page)
    page.locator('#msgPlatform_wechat > summary').click()
    panel = page.locator('#settingsTab_wechat')
    page.wait_for_function("() => document.querySelectorAll('#settingsTab_wechat .channel-card').length === 5")
    titles = panel.locator('.channel-card-title').all_inner_texts()
    assert titles == ['状态', '微信登录', '设置步骤', '会话', '发件箱'], titles
    assert panel.locator('input[type="password"]').count() == 0
    # Starting a login would ask 微信 for a QR code; the button is checked, never pressed.
    assert panel.get_by_role('button', name='开始扫码登录', exact=True).is_enabled()
    page.screenshot(path=str(Path(screenshot_dir) / 'messaging-settings-wechat-desktop.png'))


@pytest.mark.parametrize('platform', ['qq', 'feishu'])
def test_a_setting_jump_chip_opens_the_tab_with_its_platform_expanded(page, live_server, platform):
    _open_app_in_chinese(page, live_server)
    # Same markup markdown/html-policy.ts emits for an assistant link to setting:<platform>.
    page.evaluate("""(platform) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'md-ui-jump';
      chip.id = 'platformJumpChip';
      chip.setAttribute('data-tofu-action', `jumpToSetting('${platform}')`);
      chip.textContent = platform;
      document.body.appendChild(chip);
    }""", platform)
    page.locator('#platformJumpChip').click()
    page.locator(f'#settingsTab_{platform}').wait_for(state='visible')
    assert page.locator('.settings-tab[data-tab="messaging"]').evaluate("el => el.classList.contains('active')")
    assert [section['open'] for section in _sections(page)] == [p == platform for p in PLATFORMS]
