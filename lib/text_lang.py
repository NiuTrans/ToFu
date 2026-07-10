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
import threading
from typing import NamedTuple, Optional

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)

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

# ══════════════════════════════════════════════════════════════════════
#  Language-detection cascade policy (backend-owned single source of truth)
#
#  These constants govern the Tier-1 (fastText) → Tier-2 (LLM correction)
#  escalation decision.  They are served to the frontend / SDK callers via
#  ``/api/v1/server-config`` → ``langDetect`` (see
#  :func:`detect_language_policy`) so no consumer re-derives a magic number.
#
#  Values are grounded in the Intercom Fin production study
#  (https://fin.ai/research/building-a-better-language-detection-model-for-fin/)
#  and reproduced locally on the bundled lid.176 lite model: short casual
#  chat input ("buenas dias" → pt@0.90, "App store?" → no@0.40/it@0.38) is
#  exactly where a statistical detector is unreliable, so we escalate the
#  ambiguous tail to an LLM.
# ══════════════════════════════════════════════════════════════════════

#: Top-1 fastText confidence below which the result is "uncertain" and the
#: LLM-correction tier may be consulted (when enabled).
LANG_CONFIDENCE_THRESHOLD = 0.70

#: Confidence at/above which a Tier-1 result is trusted OUTRIGHT — never
#: escalated, regardless of length or margin. This caps the LLM-correction
#: cost: without it, the short-text trigger fires on confident-correct short
#: inputs too (measured: "Ciao"@1.00, "Olá"@1.00 escalating needlessly). The
#: residual confident-but-wrong short case (Fin: "im julia"→de@1.00) is an
#: accepted, documented limit — it cannot be caught cheaply.
LANG_HIGH_CONFIDENCE = 0.90

#: Inputs shorter than this (non-whitespace chars) are treated as
#: low-signal for a statistical n-gram model regardless of confidence —
#: fastText's own docs warn accuracy drops below ~80 chars, and the Fin
#: study shows single-word / greeting inputs are the dominant failure class.
LANG_SHORT_TEXT_CHARS = 20

#: "Thin Latin margin": when the top candidate is a Latin-script language and
#: the gap between the top-1 and top-2 scores is below this, the English-vs-
#: other-Latin-language call is unreliable (the exact German/Spanish gotcha
#: our own memory flagged). Escalate even if top-1 cleared the threshold.
LANG_THIN_LATIN_MARGIN = 0.15

#: Languages whose SCRIPT alone identifies them (Tier-0 fast path). A run of
#: characters in these Unicode blocks is decisive with ~100% precision and
#: never needs a model or an LLM. Ordered most-specific first.
_SCRIPT_RANGES: tuple[tuple[str, str], ...] = (
    ('ja', r'\u3040-\u309f\u30a0-\u30ff'),          # Hiragana + Katakana
    ('ko', r'\uac00-\ud7a3\u1100-\u11ff'),          # Hangul
    ('zh', r'\u4e00-\u9fff\u3400-\u4dbf'),          # CJK ideographs
    ('ar', r'\u0600-\u06ff\u0750-\u077f'),          # Arabic
    ('he', r'\u0590-\u05ff'),                        # Hebrew
    ('el', r'\u0370-\u03ff'),                        # Greek
    ('ru', r'\u0400-\u04ff'),                        # Cyrillic (reported as ru)
    ('th', r'\u0e00-\u0e7f'),                        # Thai
    ('hi', r'\u0900-\u097f'),                        # Devanagari (reported as hi)
)
_SCRIPT_RES: tuple[tuple[str, "re.Pattern"], ...] = tuple(
    (code, re.compile(f'[{rng}]')) for code, rng in _SCRIPT_RANGES)

#: Minimum fraction of non-whitespace chars in one script for the Tier-0
#: fast path to fire decisively (a stray ideograph in an English sentence
#: must NOT trigger a 'zh' verdict — that is the ``mixed`` case).
_SCRIPT_FASTPATH_RATIO = 0.50


class DetectionResult(NamedTuple):
    """The outcome of :func:`detect_language`.

    Attributes:
        code:       BCP-47-ish language code (``en`` / ``zh`` / ``de`` / …)
                    or ``'unknown'`` when no reliable signal.
        confidence: 0.0–1.0 confidence in ``code``. Script fast-path returns
                    ~1.0; heuristic fallback returns a calibrated proxy.
        source:     Which tier decided — ``'script'`` / ``'fasttext'`` /
                    ``'llm'`` / ``'heuristic'`` / ``'unknown'``.
    """

    code: str
    confidence: float
    source: str


def detect_language_policy() -> dict:
    """Frontend-facing policy blob for the detection cascade.

    Served via ``/api/v1/server-config`` under ``langDetect`` so the UI and
    SDK callers read the SAME thresholds the backend acts on (no drift).
    """
    return {
        'confidence_threshold': LANG_CONFIDENCE_THRESHOLD,
        'high_confidence': LANG_HIGH_CONFIDENCE,
        'short_text_chars': LANG_SHORT_TEXT_CHARS,
        'thin_latin_margin': LANG_THIN_LATIN_MARGIN,
        'fasttext_available': _fasttext_available(),
    }


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


# ══════════════════════════════════════════════════════════════════════
#  Tier-1: fastText lid.176 — guarded-optional backend.
#
#  Mirrors lib/rate_limit_store.py's pluggable-backend contract: a memoized
#  loader built lazily on first use, env-gated, fail-open (a vanilla box
#  without the package degrades to the script + heuristic path and is
#  byte-identical). DEFAULT OFF: the loader is only consulted when
#  TOFU_LANGDETECT_BACKEND=fasttext.
# ══════════════════════════════════════════════════════════════════════

_ft_lock = threading.Lock()
_ft_detect = None            # the fast_langdetect.detect callable, or False if unavailable
_ft_backend: str = ''


def _fasttext_available() -> bool:
    """True when the fastText backend is enabled AND importable."""
    return _get_ft_detector() is not None


def _get_ft_detector():
    """Return the memoized ``fast_langdetect.detect`` callable, or None.

    Backend is chosen from ``TOFU_LANGDETECT_BACKEND`` (default ``script``):
      * ``script``   → no statistical model; Tier-1 disabled (returns None).
      * ``fasttext`` → lazily import ``fast_langdetect`` (bundled lite .ftz,
                       917 KB, offline). Import failure logs once and pins
                       None so a missing optional dep degrades gracefully.
    """
    global _ft_detect, _ft_backend
    desired = (getenv_compat('TOFU_LANGDETECT_BACKEND') or 'script').strip().lower()
    with _ft_lock:
        if _ft_detect is not None and _ft_backend == desired:
            return _ft_detect or None
        _ft_backend = desired
        if desired != 'fasttext':
            _ft_detect = False
            return None
        try:
            from fast_langdetect import detect as _detect
            _ft_detect = _detect
            logger.info('[LangDetect] fastText backend active (lid.176 lite)')
        except Exception as e:
            _ft_detect = False
            logger.warning('[LangDetect] fastText backend requested but '
                           'fast_langdetect unavailable (%s) — falling back '
                           'to script+heuristic path', e)
    return _ft_detect or None


def reset_for_test():
    """Force the next detector lookup to rebuild — test-only helper."""
    global _ft_detect, _ft_backend
    with _ft_lock:
        _ft_detect = None
        _ft_backend = ''


def detect_script(text: str) -> Optional[str]:
    """Tier-0: decide language by SCRIPT alone when one script dominates.

    Returns a language code when ≥ ``_SCRIPT_FASTPATH_RATIO`` of the
    non-whitespace characters fall in a single decisive script block
    (CJK / Kana / Hangul / Arabic / Hebrew / Greek / Cyrillic / Thai /
    Devanagari); otherwise ``None`` (defer to Tier-1/heuristic). Returns
    ``None`` for too-short input so we never over-commit on a stray glyph.
    """
    if not isinstance(text, str) or not text:
        return None
    stripped = _WHITESPACE_RE.sub('', text)
    if len(stripped) < MIN_CHARS_FOR_DETECTION:
        return None
    total = len(stripped)
    for code, pat in _SCRIPT_RES:
        n = len(pat.findall(stripped))
        if n and n / total >= _SCRIPT_FASTPATH_RATIO:
            return code
    return None


def _heuristic_result(text: str) -> DetectionResult:
    """Zero-dependency fallback used when no statistical model is available.

    Reuses the existing latin/cjk-ratio logic so a vanilla box behaves
    exactly as before this feature existed. English/Chinese only — anything
    else is reported ``unknown`` (the heuristic cannot tell Latin languages
    apart, which is precisely why Tier-1 exists).
    """
    g = guess_language(text)
    if g == 'zh':
        return DetectionResult('zh', min(1.0, cjk_ratio(text)), 'heuristic')
    if g == 'en':
        return DetectionResult('en', min(1.0, latin_ratio(text)), 'heuristic')
    # 'mixed' / 'unknown' → no confident single-language call.
    return DetectionResult('unknown', 0.0, 'heuristic')


def _fasttext_result(text: str) -> Optional[DetectionResult]:
    """Tier-1: run the fastText detector. Returns None when unavailable/failed."""
    detect = _get_ft_detector()
    if detect is None:
        return None
    # fastText chokes on newlines and gains nothing from huge inputs.
    sample = _WHITESPACE_RE.sub(' ', text).strip()[:200]
    if not sample:
        return None
    try:
        cands = detect(sample, model='lite', k=2)
    except Exception as e:
        logger.warning('[LangDetect] fastText detect failed: %s', e)
        return None
    if not cands:
        return None
    top = cands[0]
    code = (top.get('lang') or '').lower()
    score = float(top.get('score') or 0.0)
    return DetectionResult(code or 'unknown', score, 'fasttext')


def _heuristic_en_is_unreliable(text: str) -> bool:
    """Is a heuristic ``'en'`` verdict on this input inherently untrustworthy?

    The latin-ratio heuristic CANNOT tell English apart from other Latin-script
    languages (German/Spanish/French/Italian/Portuguese all read ~0.97 Latin),
    so ANY confident ``'en'`` it produces on Latin-script, multi-word input is a
    coin-flip on language — the exact English-vs-other-Latin gotcha. Such a
    verdict MUST be routed to the LLM corrector (when allowed).

    Bounded so genuine short/trivial input isn't needlessly escalated: requires
    at least two word-tokens of ≥2 letters (multi-word ⇒ enough signal for the
    corrector, and skips a bare single word below the short-text floor).
    """
    if not text:
        return False
    words = [w for w in re.findall(r'[^\W\d_]{2,}', text, flags=re.UNICODE)]
    return len(words) >= 2


def _needs_llm_correction(res: DetectionResult, text: str,
                          candidates: Optional[list] = None) -> bool:
    """Bounded escalation trigger: is a Tier-1 result too shaky to trust?

    Fires (→ escalate to the LLM tier) when ANY of:
      * top-1 confidence < ``LANG_CONFIDENCE_THRESHOLD``; OR
      * the input is very short (< ``LANG_SHORT_TEXT_CHARS`` non-ws chars); OR
      * the top candidate is a Latin-script language AND the top-1/top-2 score
        gap is below ``LANG_THIN_LATIN_MARGIN`` (the English-vs-other-Latin
        ambiguity — the exact gotcha this feature closes).
    """
    # A strongly-confident verdict is trusted outright — this caps escalation
    # cost so we don't pay the LLM for confident-correct short inputs.
    if res.confidence >= LANG_HIGH_CONFIDENCE:
        return False
    if res.confidence < LANG_CONFIDENCE_THRESHOLD:
        return True
    stripped = _WHITESPACE_RE.sub('', text or '')
    if len(stripped) < LANG_SHORT_TEXT_CHARS:
        return True
    # Thin-Latin-margin only matters for Latin-script (non-CJK/etc.) top-1s.
    if candidates and len(candidates) >= 2 and detect_script(text) is None:
        s0 = float(candidates[0].get('score') or 0.0)
        s1 = float(candidates[1].get('score') or 0.0)
        if (s0 - s1) < LANG_THIN_LATIN_MARGIN:
            return True
    return False


def detect_language(text: str, *, allow_llm: bool = False,
                    llm_corrector=None) -> DetectionResult:
    """Cascade language detector — the single source of truth.

    Tiers, cheapest first:
      0. **Script fast-path** (:func:`detect_script`) — decisive scripts
         (CJK / Kana / Hangul / Cyrillic / …) resolve at ~100% with zero deps.
      1. **fastText lid.176** (guarded-optional; ``TOFU_LANGDETECT_BACKEND=
         fasttext``) — 176-language statistical model for Latin-script and the
         long tail. Degrades to the heuristic below when the package is absent.
      2. **LLM correction** — consulted ONLY when ``allow_llm`` is True AND the
         Tier-1 result trips :func:`_needs_llm_correction` (low confidence /
         very short / thin Latin margin). The caller supplies ``llm_corrector``
         (a ``fn(text, tier1_result) -> Optional[str]`` returning a language
         code) so this module stays free of any LLM-dispatch import. This is
         the "typeless"-style corrector for the ambiguous tail.

    ``allow_llm`` MUST be resolved by the caller through the app-vs-headless
    ``personal_scope`` gate — this function never enables the LLM tier on its
    own, keeping it fail-closed on headless surfaces.

    Returns a :class:`DetectionResult` — never raises.
    """
    if not isinstance(text, str) or not text.strip():
        return DetectionResult('unknown', 0.0, 'unknown')

    # Tier 0 — script fast path.
    script = detect_script(text)
    if script is not None:
        return DetectionResult(script, 1.0, 'script')

    # Tier 1 — fastText (or heuristic fallback when unavailable).
    detector = _get_ft_detector()
    if detector is None:
        # No statistical model: the heuristic is the terminal answer, EXCEPT
        # the LLM tier backstops the two cases it gets wrong (when allowed):
        #   * 'unknown' — no signal at all;
        #   * a confident 'en' on Latin-script MULTI-WORD input — the heuristic
        #     cannot tell English from German/Spanish/etc., so this verdict is
        #     unreliable and must be adjudicated (the default-box gotcha fix).
        # A corrected non-'en' code wins; a corrector that returns 'en'/None
        # leaves the heuristic verdict intact.
        res = _heuristic_result(text)
        if allow_llm and llm_corrector is not None and (
                res.code == 'unknown'
                or (res.code == 'en' and _heuristic_en_is_unreliable(text))):
            corrected = _run_llm_corrector(llm_corrector, text, res)
            if corrected and corrected != res.code:
                return DetectionResult(corrected, LANG_CONFIDENCE_THRESHOLD, 'llm')
        return res

    sample = _WHITESPACE_RE.sub(' ', text).strip()[:200]
    cands = []
    if sample:
        try:
            cands = detector(sample, model='lite', k=2) or []
        except Exception as e:
            logger.warning('[LangDetect] fastText detect failed: %s', e)
    if not cands:
        return _heuristic_result(text)
    top = cands[0]
    res = DetectionResult((top.get('lang') or 'unknown').lower(),
                          float(top.get('score') or 0.0), 'fasttext')

    # Tier 2 — LLM correction for the ambiguous tail.
    if allow_llm and llm_corrector is not None and _needs_llm_correction(res, text, cands):
        corrected = _run_llm_corrector(llm_corrector, text, res)
        if corrected:
            return DetectionResult(corrected, LANG_CONFIDENCE_THRESHOLD, 'llm')
    return res


def _run_llm_corrector(llm_corrector, text: str,
                       tier1: DetectionResult) -> Optional[str]:
    """Invoke the caller-supplied corrector defensively."""
    try:
        code = llm_corrector(text, tier1)
    except Exception as e:
        logger.warning('[LangDetect] LLM corrector failed: %s', e)
        return None
    if not code or not isinstance(code, str):
        return None
    return code.strip().lower() or None


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
    'LANG_CONFIDENCE_THRESHOLD', 'LANG_HIGH_CONFIDENCE',
    'LANG_SHORT_TEXT_CHARS', 'LANG_THIN_LATIN_MARGIN',
    'cjk_ratio', 'latin_ratio', 'is_predominantly_chinese',
    'is_predominantly_english',
    'is_stale_partial_translation', 'stale_translation_policy',
    'guess_language',
    'DetectionResult', 'detect_language', 'detect_script',
    'detect_language_policy', 'reset_for_test',
]
