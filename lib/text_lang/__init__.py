"""lib/text_lang — Lightweight text-language helpers (facade package).

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

This is a pure re-export facade — all implementation lives in the sibling
sub-modules. Every public symbol AND every test-patched private
(``_get_ft_detector`` / ``_needs_llm_correction`` / ``_heuristic_en_is_unreliable``
/ ``_fasttext_available`` / ``_SCRIPT_RES``) is re-exported here so
``from lib.text_lang import X`` and ``from lib import text_lang as tl`` behave
byte-identically to the old single-module layout.

SHARED STATE: the memoized fastText detector cache and :func:`reset_for_test`
live together in :mod:`._fasttext`; re-exporting ``reset_for_test`` here clears
the SAME global the detector functions read (by reference).
"""

# No code lives in this file — it is a pure re-export facade.

# ── Ratios, core types, coarse router + threshold constants ──────────────
from lib.text_lang._ratios import (  # noqa: E402,F401
    CHINESE_RATIO_THRESHOLD,
    ENGLISH_RATIO_THRESHOLD,
    MIN_CHARS_FOR_DETECTION,
    STALE_TRANSLATION_FRAC,
    STALE_TRANSLATION_MIN_SOURCE_CHARS,
    DetectionResult,
    cjk_ratio,
    latin_ratio,
    is_predominantly_chinese,
    is_predominantly_english,
    is_stale_partial_translation,
    guess_language,
    _CJK_RE,
    _LATIN_RE,
    _WHITESPACE_RE,
)

# ── Tier-0 script fast-path + Tier-1 fastText cache (shared state home) ───
from lib.text_lang._fasttext import (  # noqa: E402,F401
    detect_script,
    reset_for_test,
    _fasttext_available,
    _get_ft_detector,
    _fasttext_result,
    _SCRIPT_RANGES,
    _SCRIPT_RES,
    _SCRIPT_FASTPATH_RATIO,
    _ft_lock,
)

# ── Cascade policy constants + frontend policy blobs ─────────────────────
from lib.text_lang._policy import (  # noqa: E402,F401
    LANG_CONFIDENCE_THRESHOLD,
    LANG_HIGH_CONFIDENCE,
    LANG_SHORT_TEXT_CHARS,
    LANG_THIN_LATIN_MARGIN,
    detect_language_policy,
    stale_translation_policy,
)

# ── Cascade detector (Tiers 0→2 orchestration) ───────────────────────────
from lib.text_lang._detect import (  # noqa: E402,F401
    detect_language,
    _heuristic_result,
    _heuristic_en_is_unreliable,
    _needs_llm_correction,
    _run_llm_corrector,
)


__all__ = [
    'CHINESE_RATIO_THRESHOLD', 'ENGLISH_RATIO_THRESHOLD',
    'MIN_CHARS_FOR_DETECTION',
    'STALE_TRANSLATION_FRAC', 'STALE_TRANSLATION_MIN_SOURCE_CHARS',
    'LANG_CONFIDENCE_THRESHOLD', 'LANG_HIGH_CONFIDENCE',
    'LANG_SHORT_TEXT_CHARS', 'LANG_THIN_LATIN_MARGIN',
    'cjk_ratio', 'latin_ratio', 'is_predominantly_chinese',
    'is_predominantly_english',
    'is_stale_partial_translation', 'stale_translation_policy',
    'guess_language',
    'DetectionResult', 'detect_language', 'detect_script',
    'detect_language_policy', 'reset_for_test',
]
