"""lib/text_lang.py — Lightweight text-language helpers.

Pure-function port of policy decisions previously embedded in
``static/js/translation.js`` (specifically ``_isAlreadyChinese``).
Centralised so:

* The headless ``/api/v1/agents/translate`` flow and SDK callers
  building CI translation pipelines see the same skip-rule the UI
  applies (avoid translating already-Chinese text into Chinese, which
  wastes an LLM round and can hallucinate an English rewrite).
* The threshold can be tuned in one place.

Public API
----------

  cjk_ratio(text) -> float                 # 0.0 to 1.0
  is_predominantly_chinese(text)           # >= 30% CJK, ≥ 8 non-ws chars
  guess_language(text) -> str              # 'zh' | 'en' | 'mixed' | 'unknown'

Heuristic only — not a substitute for ``langdetect`` / ``cld3``. We
stay dependency-free here on purpose.
"""

from __future__ import annotations

import re

# Unified CJK ideographs + extension-A. Mirrors the JS regex used in
# `_isAlreadyChinese`.
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

# ASCII letters, common Latin-1 letters, and digits. Used for the
# "predominantly English" check.
_LATIN_RE = re.compile(r'[A-Za-z\u00c0-\u024f]')

_WHITESPACE_RE = re.compile(r'\s+')

# Same threshold the JS impl uses.
CHINESE_RATIO_THRESHOLD = 0.30
ENGLISH_RATIO_THRESHOLD = 0.55
MIN_CHARS_FOR_DETECTION = 8

# A translation produced from mid-stream PARTIAL content tends to be a tiny
# fraction of the (now-final) source length. When the persisted translation
# is shorter than this fraction of the source AND the source is non-trivial,
# we treat it as stale and re-translate. This is a data-quality policy, so it
# lives here (not hard-coded in static/js/translation.js, where it silently
# drifted). Served to the frontend via /api/v1/server-config → `translation`.
STALE_TRANSLATION_FRAC = 0.15
STALE_TRANSLATION_MIN_SOURCE_CHARS = 500


def cjk_ratio(text: str) -> float:
    """Return the fraction of non-whitespace characters that are CJK.

    Returns 0.0 for empty / non-string / too-short input.
    """
    if not isinstance(text, str) or not text:
        return 0.0
    stripped = _WHITESPACE_RE.sub('', text)
    if len(stripped) < MIN_CHARS_FOR_DETECTION:
        return 0.0
    cjk_chars = _CJK_RE.findall(stripped)
    if not cjk_chars:
        return 0.0
    return len(cjk_chars) / len(stripped)


def is_predominantly_chinese(text: str) -> bool:
    """True when ``cjk_ratio(text) >= CHINESE_RATIO_THRESHOLD``.

    Direct port of the JS ``_isAlreadyChinese`` policy.
    """
    return cjk_ratio(text) >= CHINESE_RATIO_THRESHOLD


def is_predominantly_english(text: str) -> bool:
    """True when ``latin_ratio(text) >= ENGLISH_RATIO_THRESHOLD``.

    Mirror of :func:`is_predominantly_chinese` for the reverse direction:
    used to decide "this text is already English, skip translating it to
    English" — the language-agnostic generalisation of the old
    Chinese-only ``has_chinese`` gate.
    """
    return latin_ratio(text) >= ENGLISH_RATIO_THRESHOLD


def is_stale_partial_translation(source: str, translated: str) -> bool:
    """True when ``translated`` looks like a stale mid-stream partial.

    A translation is stale when the source is non-trivial
    (>= ``STALE_TRANSLATION_MIN_SOURCE_CHARS``) yet the translation came out
    shorter than ``STALE_TRANSLATION_FRAC`` of it — the signature of a
    translation captured before the source finished streaming.
    """
    if not isinstance(source, str) or not isinstance(translated, str):
        return False
    if len(source) <= STALE_TRANSLATION_MIN_SOURCE_CHARS or not translated:
        return False
    return len(translated) < len(source) * STALE_TRANSLATION_FRAC


def stale_translation_policy() -> dict:
    """Frontend-facing policy blob for the stale-partial heuristic."""
    return {
        'stale_frac': STALE_TRANSLATION_FRAC,
        'min_source_chars': STALE_TRANSLATION_MIN_SOURCE_CHARS,
    }


def latin_ratio(text: str) -> float:
    if not isinstance(text, str) or not text:
        return 0.0
    stripped = _WHITESPACE_RE.sub('', text)
    if len(stripped) < MIN_CHARS_FOR_DETECTION:
        return 0.0
    latin_chars = _LATIN_RE.findall(stripped)
    if not latin_chars:
        return 0.0
    return len(latin_chars) / len(stripped)


def guess_language(text: str) -> str:
    """Coarse language guess for translation routing.

    Returns one of ``'zh'``, ``'en'``, ``'mixed'``, ``'unknown'``.

    * ``'zh'``    — predominantly Chinese.
    * ``'en'``    — predominantly Latin / English.
    * ``'mixed'`` — both ratios are non-trivial (e.g. bilingual reply).
    * ``'unknown'`` — too short or no signal (fewer than 8 non-ws chars).
    """
    cjk = cjk_ratio(text)
    latin = latin_ratio(text)
    if cjk == 0.0 and latin == 0.0:
        return 'unknown'
    if cjk >= CHINESE_RATIO_THRESHOLD and latin >= 0.30:
        return 'mixed'
    if cjk >= CHINESE_RATIO_THRESHOLD:
        return 'zh'
    if latin >= ENGLISH_RATIO_THRESHOLD:
        return 'en'
    return 'unknown'


__all__ = [
    'CHINESE_RATIO_THRESHOLD', 'ENGLISH_RATIO_THRESHOLD',
    'MIN_CHARS_FOR_DETECTION',
    'STALE_TRANSLATION_FRAC', 'STALE_TRANSLATION_MIN_SOURCE_CHARS',
    'cjk_ratio', 'latin_ratio', 'is_predominantly_chinese',
    'is_predominantly_english',
    'is_stale_partial_translation', 'stale_translation_policy',
    'guess_language',
]
