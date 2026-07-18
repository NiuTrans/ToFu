"""lib/text_lang/_policy.py — cascade thresholds + frontend policy blobs.

Backend-owned single source of truth for the Tier-1 (fastText) → Tier-2 (LLM
correction) escalation decision. The constants here are served to the frontend
/ SDK callers via ``/api/v1/server-config`` (``langDetect`` / ``translation``)
so no consumer re-derives a magic number.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.text_lang._ratios import (
    STALE_TRANSLATION_FRAC,
    STALE_TRANSLATION_MIN_SOURCE_CHARS,
)
from lib.text_lang._fasttext import _fasttext_available

logger = get_logger(__name__)

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


def stale_translation_policy() -> dict:
    """Frontend-facing policy blob for the stale-partial heuristic."""
    return {
        'stale_frac': STALE_TRANSLATION_FRAC,
        'min_source_chars': STALE_TRANSLATION_MIN_SOURCE_CHARS,
    }
