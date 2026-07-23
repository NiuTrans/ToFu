"""lib/text_lang/_detect.py — the cascade detector (Tiers 0→2 orchestration).

Owns the heuristic fallback, the bounded escalation trigger, and
:func:`detect_language` — the single source of truth.

FACADE PARITY: debug tooling and tests patch ``_get_ft_detector`` /
``_needs_llm_correction`` on the *package* (``lib.text_lang``). So
:func:`detect_language` resolves BOTH through the package module at call time
(``import lib.text_lang as _pkg``) — a patch on the facade therefore takes
effect here.
"""

from __future__ import annotations

import re
from typing import Optional

from lib.log import get_logger

from lib.text_lang._ratios import (
    DetectionResult,
    _WHITESPACE_RE,
    cjk_ratio,
    guess_language,
    latin_ratio,
)
from lib.text_lang._fasttext import _get_ft_detector, detect_script
from lib.text_lang._policy import (
    LANG_CONFIDENCE_THRESHOLD,
    LANG_HIGH_CONFIDENCE,
    LANG_SHORT_TEXT_CHARS,
    LANG_THIN_LATIN_MARGIN,
)

logger = get_logger(__name__)


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
    # Resolve the facade-patchable hooks THROUGH the package so tests/debug
    # that monkeypatch ``lib.text_lang._get_ft_detector`` /
    # ``lib.text_lang._needs_llm_correction`` are honoured.
    import lib.text_lang as _pkg

    if not isinstance(text, str) or not text.strip():
        return DetectionResult('unknown', 0.0, 'unknown')

    # Tier 0 — script fast path.
    script = detect_script(text)
    if script is not None:
        return DetectionResult(script, 1.0, 'script')

    # Tier 1 — fastText (or heuristic fallback when unavailable).
    detector = _pkg._get_ft_detector()
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
    if allow_llm and llm_corrector is not None and _pkg._needs_llm_correction(res, text, cands):
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
