"""tests/i18n/test_language_picker_action_registration.py — language picker stays clickable.

Regression pin (2026-09-18): the settings language picker
(``static/settings_panels/general.html``) switched from inline
``onclick="_onLanguageChange('en')"`` to declarative
``data-tofu-action="_onLanguageChange('en')"`` in the Vite runtime migration.
The restricted action interpreter resolves receiver names ONLY through the
eager ``registerAction`` map, feature bindings, or the generated retained
runtime action table — and that table (``scripts/update_runtime_actions.mjs``)
only carries section-owned top-level function declarations. ``_onLanguageChange``
is an export of the i18n module, so it never entered any resolution layer:
every click was refused as ``Unknown action`` and the picker looked frozen.

The fix registers the i18n owner eagerly: a top-level ``registerAction`` in
``frontend/src/application.ts`` (moved there from ``main.ts`` by fc64bc4e2),
the module the boot entry awaits before it starts the application
(``frontend/src/main.test.ts`` pins that order). These pins fail if any side
of the name contract drifts:

  1. the panel's language select keeps naming ``_onLanguageChange``;
  2. application.ts registers that exact receiver name at module evaluation
     and imports it from the i18n module;
  3. the i18n module keeps exporting the owner function.
"""

from __future__ import annotations

from html.parser import HTMLParser
import os
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GENERAL_PANEL = os.path.join(ROOT, 'static', 'settings_panels', 'general.html')
APPLICATION_TS = os.path.join(ROOT, 'frontend', 'src', 'application.ts')
I18N_INDEX = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'index.ts')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class _PanelControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes: dict[str, dict[str, str]] = {}
        self.options_in_language_select = 0
        self._in_language_select = False

    def handle_starttag(self, tag, attrs):
        values = {name: value or '' for name, value in attrs}
        if values.get('id'):
            self.attributes[values['id']] = {'tag': tag, **values}
        if tag == 'select' and values.get('id') == 'settingLanguage':
            self._in_language_select = True
        elif tag == 'option' and self._in_language_select:
            self.options_in_language_select += 1

    def handle_endtag(self, tag):
        if tag == 'select':
            self._in_language_select = False


def test_language_controls_reference_the_named_action():
    panel = _read(GENERAL_PANEL)
    controls = _PanelControls()
    controls.feed(panel)
    select = controls.attributes.get('settingLanguage')
    assert select and select['tag'] == 'select', 'the language select is gone'
    # One visible native select switches language through the named receiver.
    assert select.get('data-tofu-action-change') == '_onLanguageChange(this.value)'
    assert 'display:none' not in select.get('style', '').replace(' ', '')
    # Its accessible name is the visible "UI language" title.
    label = controls.attributes.get(select.get('aria-labelledby', ''))
    assert label and label.get('data-i18n') == 'settings.language'
    # Options come from languages.json at runtime; the panel keeps no copy.
    assert controls.options_in_language_select == 0
    assert 'lang-option' not in panel


def test_application_registers_the_receiver_eagerly():
    application = _read(APPLICATION_TS)
    # A column-0 statement runs at module evaluation; a registration inside a
    # function body would wait for a caller that a declarative click never is.
    assert len(re.findall(
        r"^registerAction\(\s*['\"]_onLanguageChange['\"]", application, re.M)) == 1, (
        '_onLanguageChange is not registered exactly once at the top level of '
        'frontend/src/application.ts; without it declarative clicks refuse as '
        'Unknown action and the language picker dies.')
    assert re.search(
        r"import\s*\{[^}]*\b_onLanguageChange\b[^}]*\}\s*from\s*['\"]\./i18n['\"]",
        application,
    ), 'application.ts registers the receiver but no longer imports the i18n owner'


def test_i18n_module_keeps_exporting_the_owner():
    source = _read(I18N_INDEX)
    assert re.search(r'export\s+function\s+_onLanguageChange\b', source), (
        'i18n/index.ts stopped exporting _onLanguageChange')
