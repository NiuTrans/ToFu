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

The fix registers the i18n owner eagerly in ``frontend/src/main.ts``. These
pins fail if any side of the name contract drifts:

  1. the panel keeps naming ``_onLanguageChange`` for its language controls;
  2. ``main.ts`` eagerly registers that exact receiver name;
  3. the i18n module keeps exporting the owner function.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GENERAL_PANEL = os.path.join(ROOT, 'static', 'settings_panels', 'general.html')
MAIN_TS = os.path.join(ROOT, 'frontend', 'src', 'main.ts')
I18N_INDEX = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'index.ts')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def test_language_controls_reference_the_named_action():
    panel = _read(GENERAL_PANEL)
    for language in ('zh', 'en'):
        assert f'data-lang="{language}"' in panel, (
            f'lang picker lost its {language} option')
        assert f"_onLanguageChange('{language}')" in panel, (
            f'lang picker {language} option no longer names _onLanguageChange')
    # Hidden compat select switches language through the same receiver.
    assert 'data-tofu-action-change="_onLanguageChange(this.value)"' in panel


def test_main_registers_the_receiver_eagerly():
    main = _read(MAIN_TS)
    assert re.search(r"registerAction\(\s*['\"]_onLanguageChange['\"]", main), (
        '_onLanguageChange is not in the eager action registry — declarative '
        'clicks refuse as Unknown action and the language picker dies. '
        'Register the i18n owner in frontend/src/main.ts.')
    assert re.search(
        r"import\s*\{[^}]*\b_onLanguageChange\b[^}]*\}\s*from\s*['\"]\./i18n['\"]",
        main,
    ), 'main.ts no longer imports _onLanguageChange from the i18n module'


def test_i18n_module_keeps_exporting_the_owner():
    source = _read(I18N_INDEX)
    assert re.search(r'export\s+function\s+_onLanguageChange\b', source), (
        'i18n/index.ts stopped exporting _onLanguageChange')
