"""Real Chromium: Settings lists GitHub after 大象 and its panel renders the live channel state.

The GitHub tab sits in the 连接 group right after 大象. Opening it renders the
panel from the application's own /api/v4/channels endpoints over isolated test
storage (no route interception, no GitHub token, no network to GitHub), and the
assistant's ``setting:github`` jump chip lands on the same tab.
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
    assert len(titles) == 4, titles
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
