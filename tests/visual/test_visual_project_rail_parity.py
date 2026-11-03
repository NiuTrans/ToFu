"""Project rail parity in the real app.

The Project Co-Pilot modal and the sidebar folder settings dialog mount the
same workspace rail (``frontend/src/features/project/workspace-rail.ts``), so
its parts must compute the same styles in both hosts in every theme. This
guards host-context CSS (the Co-Pilot's legacy ``.modal`` rules, the UI kit's
project dialog tokens) from splitting the two dialogs apart again.
Component behavior lives in ``frontend/src/features/project/workspace-rail.test.ts``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]

_SCREENSHOTS = Path('output/playwright/project-rail-parity')
_SIGNATURE = '''rail => {
  const box = ['border-top-width', 'border-top-style', 'border-top-color', 'border-top-left-radius',
    'background-color', 'box-shadow', 'color'];
  const type = ['font-family', 'font-size', 'font-weight', 'text-transform', 'letter-spacing', 'color'];
  const parts = {
    railRoot: ['', [...type, 'line-height']],
    workspaceCard: ['.pm-rail-card.pm-rail-workspace', [...box, 'padding-top', 'padding-left']],
    recentCard: ['.pm-rail-card.recent-paths', [...box, 'padding-top', 'padding-left']],
    label: ['.pm-rail-workspace .pm-section-label', type],
    count: ['.pm-rail-workspace .pm-count', ['background-color', 'color', 'font-size', 'font-weight']],
    pathInput: ['.mp-add-row .mp-path-input', [...box, ...type, 'height', 'padding-left']],
    addButton: ['.mp-add-row button', [...box, ...type, 'height', 'padding-left', 'padding-top', 'line-height']],
    row: ['.mp-row', [...box, 'height', 'padding-left']],
    rowName: ['.mp-row .mp-row-name', type],
    rowPath: ['.mp-row .mp-row-path', type],
    rootBadge: ['.mp-row .mp-row-badge', [...box, ...type]],
    grip: ['.mp-row .mp-row-grip', [...box, 'width', 'height', 'opacity']],
    lock: ['.mp-row .mp-row-lock', [...box, 'width', 'height', 'opacity']],
    remove: ['.mp-row .mp-row-remove', [...box, 'width', 'height', 'opacity']],
    recentSearch: ['.recent-search .recent-search-input', [...box, ...type, 'height']],
  };
  return Object.fromEntries(Object.entries(parts).map(([part, [selector, names]]) => {
    const el = selector ? rail.querySelector(selector) : rail;
    if (!el) return [part, null];
    const style = getComputedStyle(el);
    return [part, Object.fromEntries(names.map(name => [name, style.getPropertyValue(name)]))];
  }));
}'''


_BROWSER_FRAME = '''el => {
  const style = getComputedStyle(el);
  return Object.fromEntries(['border-top-width', 'border-top-style', 'border-top-color', 'border-top-left-radius',
    'box-shadow', 'background-color', 'font-family', 'color'].map(name => [name, style.getPropertyValue(name)]));
}'''
_SETTLED = '''el => Promise.all(el.getAnimations({subtree: true})
  .filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity)
  .map(animation => animation.finished))'''


def _stage(rail, path: str) -> None:
    field = rail.locator('.mp-add-row .mp-path-input')
    field.fill(path)
    field.press('Enter')
    rail.locator(f'.mp-row[data-path="{path}"]').wait_for(state='visible')


@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
def test_copilot_and_folder_settings_render_the_same_rail(browser, live_server, tmp_path, theme):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    staged = str(tmp_path / 'parity' / 'alpha')
    context = browser.new_context(service_workers='block', viewport={'width': 1280, 'height': 860})
    context.add_init_script(
        "localStorage.setItem('tofu_onboarding_v1_done', '1');"
        f"localStorage.setItem('claude_ui_theme', '{theme}');")
    page = context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    _SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        page.wait_for_function('(theme) => document.documentElement.dataset.theme === theme', arg=theme)

        page.locator('#chatModeToggle').click()
        modal = page.locator('#projectModal')
        expect(modal).to_have_class(re.compile(r'.*open.*'))
        copilot = modal.locator('.pm-body > .pm-rail')
        expect(copilot).to_have_count(1)
        _stage(copilot, staged)
        modal.evaluate(_SETTLED)
        copilot_signature = copilot.evaluate(_SIGNATURE)
        copilot_signature['browserCard'] = modal.locator('.pm-browser-pane > .folder-browser').evaluate(_BROWSER_FRAME)
        page.screenshot(path=str(_SCREENSHOTS / f'{theme}-copilot.png'))
        modal.locator('.pm-close').click()
        expect(modal).not_to_have_class(re.compile(r'.*open.*'))

        page.locator('#folderTabs .folder-tab-add:visible, #folderQuickAdd:visible').first.click()
        dialog = page.locator('dialog.ui-dialog--project[open]')
        dialog.wait_for(state='visible')
        folder = dialog.locator('.folder-settings-workbench > .pm-rail')
        expect(folder).to_have_count(1)
        _stage(folder, staged)
        dialog.evaluate(_SETTLED)
        folder_signature = folder.evaluate(_SIGNATURE)
        folder_signature['browserCard'] = dialog.locator('.folder-settings-workbench > .folder-browser').evaluate(_BROWSER_FRAME)
        page.screenshot(path=str(_SCREENSHOTS / f'{theme}-folder-settings.png'))

        missing = [part for part, values in {**copilot_signature, **folder_signature}.items() if values is None]
        assert not missing, missing
        differences = {
            part: {name: (copilot_signature[part][name], value) for name, value in values.items()
                   if copilot_signature[part][name] != value}
            for part, values in folder_signature.items()
        }
        differences = {part: diff for part, diff in differences.items() if diff}
        if differences:
            pytest.fail('rail parts differ (Co-Pilot, folder settings):\n' + json.dumps(differences, indent=1))
        assert errors == []
    finally:
        context.close()
