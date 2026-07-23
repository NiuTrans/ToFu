"""lib/text_lang/_fasttext.py — Tier-0 script fast-path + Tier-1 fastText.

This submodule is the SINGLE HOME of the lazily-built, memoized fastText
detector cache (``_ft_detect`` / ``_ft_backend`` guarded by ``_ft_lock``) and
of :func:`reset_for_test` which clears it. The package ``__init__`` re-exports
``reset_for_test`` by reference, so clearing it there clears the SAME global
that :func:`_get_ft_detector` / :func:`_fasttext_available` read.

It also owns the decisive-script Tier-0 fast path (:func:`detect_script`) and
the compiled ``_SCRIPT_RES`` table. Tests neuter ``_SCRIPT_RES`` on the package
facade, so :func:`detect_script` resolves it THROUGH the package module at call
time (facade pattern) — see the ``import lib.text_lang`` inside the function.
"""

from __future__ import annotations

import re
import threading
from typing import Optional

from lib.env_compat import getenv_compat
from lib.log import get_logger

from lib.text_lang._ratios import MIN_CHARS_FOR_DETECTION, _WHITESPACE_RE

logger = get_logger(__name__)

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

    Resolves ``_SCRIPT_RES`` through the package facade so a test that neuters
    ``lib.text_lang._SCRIPT_RES`` is honoured (facade parity).
    """
    if not isinstance(text, str) or not text:
        return None
    stripped = _WHITESPACE_RE.sub('', text)
    if len(stripped) < MIN_CHARS_FOR_DETECTION:
        return None
    import lib.text_lang as _pkg
    total = len(stripped)
    for code, pat in _pkg._SCRIPT_RES:
        n = len(pat.findall(stripped))
        if n and n / total >= _SCRIPT_FASTPATH_RATIO:
            return code
    return None


def _fasttext_result(text: str) -> Optional["DetectionResult"]:
    """Tier-1: run the fastText detector. Returns None when unavailable/failed."""
    from lib.text_lang._ratios import DetectionResult
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
