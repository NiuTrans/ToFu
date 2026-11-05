"""The Settings language select switches every manifest language in the real shell."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'frontend/src/i18n/languages.json').read_text(encoding='utf-8'))
FIELD_SELECTS = ('#settingLanguage', '#settingDefaultThinkingDepth', '#settingInputSendMode')


def _message(code: str, key: str) -> str:
    path = ROOT / 'frontend/src/i18n/locales' / f'{code}.json'
    return json.loads(path.read_text(encoding='utf-8'))[key]


def _chevron(page, selector: str) -> dict:
    return page.locator(selector).evaluate("""(select) => {
      const style = getComputedStyle(select);
      return { image: style.backgroundImage, gutter: parseFloat(style.paddingRight) };
    }""")


def test_settings_language_select_switches_every_manifest_language(page, live_server, screenshot_dir):
    page.add_init_script(
        "localStorage.setItem('tofu_onboarding_v1_done', '1');"
        "if (!localStorage.getItem('tofu_ui_lang')) localStorage.setItem('tofu_ui_lang', 'zh');")
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(live_server, wait_until='networkidle')
    page.wait_for_function("document.documentElement.lang === 'zh-CN'")
    page.wait_for_function("typeof window.openSettings === 'function' && typeof window.switchSettingsTab === 'function'")
    page.evaluate("() => { window.openSettings(); window.switchSettingsTab('general'); }")
    select = page.locator('#settingLanguage')
    select.wait_for(state='visible')

    # Manifest order; each option leads with the language's own name.
    options = select.locator('option')
    assert [options.nth(i).get_attribute('value') for i in range(options.count())] == [
        entry['code'] for entry in MANIFEST['languages']]
    texts = [options.nth(i).inner_text() for i in range(options.count())]
    for entry, text in zip(MANIFEST['languages'], texts):
        assert text == entry['name'] if entry['code'] == 'zh' else text.startswith(entry['name'] + ' — ')
    assert page.get_by_role('combobox', name=_message('zh', 'settings.language')).count() == 1

    # Every field select in the section reads as a dropdown, also while focused.
    for selector in FIELD_SELECTS:
        chevron = _chevron(page, selector)
        assert chevron['image'].startswith('url('), (selector, chevron)
        assert chevron['gutter'] >= 24, (selector, chevron)
    select.focus()
    assert _chevron(page, '#settingLanguage')['image'].startswith('url(')
    page.locator('#settingsTab_general').screenshot(path=str(Path(screenshot_dir) / 'language-select-zh.png'))

    title = page.locator('#settingLanguageLabel')
    for entry in MANIFEST['languages']:
        code, tag = entry['code'], entry['tag']
        select.select_option(code)
        page.wait_for_function(f'document.documentElement.lang === {json.dumps(tag)}')
        assert page.evaluate("localStorage.getItem('tofu_ui_lang')") == code
        assert f'tofu_ui_lang={code}' in page.evaluate('document.cookie')
        assert select.input_value() == code
        assert title.inner_text().strip() == _message(code, 'settings.language')
        # The selected option shows the bare endonym; the others are relabelled.
        assert select.locator('option:checked').inner_text() == entry['name']
    page.locator('#settingsTab_general').screenshot(path=str(Path(screenshot_dir) / 'language-select-ru.png'))

    # The choice persists across a reload and the server hint follows the cookie.
    page.reload(wait_until='networkidle')
    last = MANIFEST['languages'][-1]
    page.wait_for_function(f"document.documentElement.lang === {json.dumps(last['tag'])}")
    hint = page.evaluate(
        "JSON.parse(document.getElementById('tofu-boot-config').textContent).uiLanguageHint")
    assert hint == last['code']

    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate("() => { window.openSettings(); window.switchSettingsTab('general'); }")
    select.wait_for(state='visible')
    assert not page.locator('#settingsTab_general').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    page.locator('#settingsTab_general').screenshot(path=str(Path(screenshot_dir) / 'language-select-mobile.png'))
