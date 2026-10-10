"""The registry owns the intervention reason presentation vocabulary.

Every ``InjectionSpec.presentation.label_key`` is a frontend i18n contract key.
This is the executable spec pinning the single-source-of-truth invariant: the
registry cannot reference a label the locales do not define (the chip would
render its raw fallback instead of a translated string), and the tone must be
a value the stylesheet actually implements. The generated frontend contract
(``system-note-presentation.generated.ts``) and the JS runtime embed are
derived from this same registry — see ``scripts/gen_system_note_presentation.mjs``
and ``scripts/check_note_presentation_parity.mjs``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.tasks_pkg.context_composer.registry import (
    NOTE_TONES, note_presentations,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_LOCALE_DIR = _ROOT / 'frontend' / 'src' / 'i18n' / 'locales'


def _locale_keys(language: str) -> set[str]:
    return set(json.loads((_LOCALE_DIR / f'{language}.json').read_text()))


def test_every_presentation_label_key_exists_in_both_locales():
    presentations = note_presentations()
    assert presentations, 'expected at least one declared presentation'
    en, zh = _locale_keys('en'), _locale_keys('zh')
    missing = {
        reason: presentation.label_key
        for reason, presentation in presentations.items()
        if presentation.label_key not in en or presentation.label_key not in zh
    }
    assert not missing, (
        'registry presentation label_key(s) absent from a locale: '
        f'{missing}. Add the key to frontend/src/i18n/locales/{{en,zh}}.json.'
    )


def test_presentation_tones_are_stylesheet_backed():
    """A tone with no `.sw-note-reason-{tone}` rule renders unstyled."""
    css = (_ROOT / 'frontend' / 'src' / 'styles' / 'application'
           / '05-paper-report-media.css').read_text()
    for tone in NOTE_TONES:
        assert f'.sw-note-reason-{tone}' in css, (
            f'tone {tone!r} declared in the registry has no stylesheet rule')
    for reason, presentation in note_presentations().items():
        assert presentation.tone in NOTE_TONES, (reason, presentation.tone)
