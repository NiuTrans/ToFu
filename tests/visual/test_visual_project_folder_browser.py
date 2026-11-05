"""Project Co-Pilot workbench: the shared folder browser in the real app.

Real server and filesystem (tmp_path); no provider call. Covers the pane's
geometry and isolation from the legacy ``.modal`` input rules in every theme,
the typed-path jump, inline folder creation and recoverable deletion.
Component behavior lives in ``frontend/src/features/project/folder-browser.test.ts``.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('theme,width', [('tofu', 1280), ('dark', 1280), ('light', 1280), ('tofu', 390)])
def test_copilot_folder_browser_create_delete_and_layout(browser, live_server, tmp_path, theme, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    root = tmp_path / 'browse-root'
    (root / 'alpha').mkdir(parents=True)
    (root / 'alpha' / 'package.json').write_text('{}', encoding='utf-8')
    (root / 'beta').mkdir()
    context = browser.new_context(service_workers='block', viewport={'width': 1280, 'height': 800})
    context.add_init_script(
        "localStorage.setItem('tofu_onboarding_v1_done', '1');"
        f"localStorage.setItem('claude_ui_theme', '{theme}');")
    page = context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        page.wait_for_function('(theme) => document.documentElement.dataset.theme === theme', arg=theme)
        page.locator('#chatModeToggle').click()
        expect(page.locator('#projectModal')).to_have_class(re.compile(r'.*open.*'))
        page.set_viewport_size({'width': width, 'height': 800})
        pane = page.locator('#folderBrowser')
        search = pane.locator('.folder-browser-filter')
        search.fill(str(root))
        search.press('Enter')
        alpha = pane.locator(f'.folder-browser-row[data-dir-path="{root / "alpha"}"]')
        expect(alpha).to_be_visible(timeout=20000)
        expect(pane.locator('[aria-current="location"]')).to_have_text('browse-root')

        metrics = pane.evaluate('''el => {
          const card = el.getBoundingClientRect();
          const pane = el.parentElement.getBoundingClientRect();
          const input = getComputedStyle(el.querySelector('.folder-browser-filter'));
          const row = el.querySelector('.folder-browser-row').getBoundingClientRect();
          return {fillsPane: Math.abs(card.height - pane.height) <= 2 && card.width <= pane.width + 1,
            overflow: el.scrollWidth > el.clientWidth + 1, rowHeight: row.height,
            inputMargin: input.marginBottom, inputBorder: input.borderTopWidth,
            inputBackground: input.backgroundColor};
        }''')
        assert metrics['fillsPane'] and not metrics['overflow'], metrics
        # Legacy `.modal input` and Tofu bleed rules must not box the filter again.
        assert metrics['inputMargin'] == '0px' and metrics['inputBorder'] == '0px', metrics
        assert metrics['inputBackground'] == 'rgba(0, 0, 0, 0)', metrics
        assert 30 <= metrics['rowHeight'] <= 40, metrics
        out = Path(__file__).resolve().parents[2] / 'output/playwright/project-folder-browser'
        out.mkdir(parents=True, exist_ok=True)
        alpha.hover()
        page.screenshot(path=str(out / f'{theme}-{width}.png'))

        # Inline creation lands inside the new folder.
        pane.locator('.folder-browser-bar > button').nth(1).click()
        name = pane.locator('.folder-browser-create-input')
        expect(name).to_be_focused()
        name.fill('gamma')
        name.press('Enter')
        expect(pane.locator('[aria-current="location"]')).to_have_text('gamma', timeout=15000)
        assert (root / 'gamma').is_dir()

        # Up, then a confirmed delete moves it to the recoverable trash bin.
        pane.locator('.folder-browser-bar > button').first.click()
        gamma = pane.locator(f'.folder-browser-row[data-dir-path="{root / "gamma"}"]')
        expect(gamma).to_be_visible(timeout=15000)
        gamma.hover()
        gamma.locator('[data-action="delete"]').click()
        confirm = gamma.locator('.folder-browser-confirm')
        expect(confirm).to_be_visible()
        page.screenshot(path=str(out / f'{theme}-{width}-confirm.png'))
        confirm.locator('.ui-btn--danger').click()
        expect(gamma).to_have_count(0)
        deadline = time.monotonic() + 15
        while (root / 'gamma').exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not (root / 'gamma').exists()
        assert (root / '.tofu_trash').is_dir()
        expect(alpha).to_be_visible()

        page.keyboard.press('Escape')
        expect(page.locator('#projectModal')).not_to_have_class(re.compile(r'.*open.*'))
        assert errors == []
    finally:
        context.close()
