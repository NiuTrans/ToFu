"""Real Chromium: Settings lists 大象 and its panel renders the live channel state.

The 大象 tab sits in the 连接 group right after 飞书. Opening it renders the
panel from the application's own /api/v4/channels endpoints over isolated test
storage (no route interception), and the assistant's ``setting:dx`` jump chip
lands on the same tab.
"""
from __future__ import annotations

from pathlib import Path

import pytest

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
