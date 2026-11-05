"""UI language manifest for Python consumers.

Responsibility: expose ``frontend/src/i18n/languages.json`` -- the single list
of selectable UI languages -- to the server: the request language resolver
(``routes/common.py``), the Vite i18n catalog digest (``lib/vite_assets.py``)
and features that write model output in the interface language
(``lib/conversations/title_gen.py``).

Entry points: ``UI_LANGUAGE_MANIFEST_PATH``, ``UI_LANGUAGES``,
``DEFAULT_UI_LANGUAGE`` and ``UI_LANGUAGE_ENGLISH_NAMES``.
Dependencies: standard library only; the manifest is read once at import.
"""

from __future__ import annotations

import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_LANGUAGE_MANIFEST_PATH = os.path.join(
    BASE_DIR, 'frontend', 'src', 'i18n', 'languages.json')

with open(UI_LANGUAGE_MANIFEST_PATH, encoding='utf-8') as _manifest_file:
    _MANIFEST = json.load(_manifest_file)
_ENTRIES = tuple(
    entry for entry in _MANIFEST.get('languages') or () if isinstance(entry, dict))

UI_LANGUAGES = tuple(entry.get('code') for entry in _ENTRIES)
DEFAULT_UI_LANGUAGE = _MANIFEST.get('default')
UI_LANGUAGE_ENGLISH_NAMES = {
    entry.get('code'): entry.get('englishName') for entry in _ENTRIES}

if (not UI_LANGUAGES or len(set(UI_LANGUAGES)) != len(UI_LANGUAGES)
        or DEFAULT_UI_LANGUAGE not in UI_LANGUAGES
        or not all(isinstance(code, str) and code.isascii() and code.isalpha()
                   and code.islower() and 2 <= len(code) <= 3
                   for code in UI_LANGUAGES)
        or not all(isinstance(name, str) and name.strip()
                   for name in UI_LANGUAGE_ENGLISH_NAMES.values())):
    raise RuntimeError('frontend/src/i18n/languages.json has an invalid shape')
