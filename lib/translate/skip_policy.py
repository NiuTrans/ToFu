"""Conservative identity policy for automatic translation.

Language detection answers which language dominates a document.  It does not
answer whether *every meaningful span* is already in the target language.
Automatic translation may skip only identity-invariant content after examining
the whole document; mixed-language prose must continue to the translator.

Raw Latin-character ratio is the wrong probe for that decision.  Technical
Chinese commonly carries URLs, inline code, API identifiers, and product
names; those bytes are not hidden English paragraphs.  The already-target
branch therefore inspects *prose* — markup/code/URLs stripped — and only
treats leftover English function words as evidence that translation is still
required.
"""

from __future__ import annotations

import re


_URL_OR_PATH_RE = re.compile(
    r"^(?:"
    r"[A-Za-z][A-Za-z0-9+.\-]*://"
    r"|/"
    r"|~/"
    r"|\.{1,2}/"
    r"|[A-Za-z]:[\\/]"
    r")\S*$"
)
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*[^>]*>")
_DOTTED_IDENT_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*\b"
)
_SNAKE_IDENT_RE = re.compile(r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b")

# Articles, auxiliaries, and prepositions that almost never appear as
# borrowed technical tokens in already-Chinese writing, but always appear
# in a hidden English sentence.
_EN_FUNCTION_WORD_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    r"the|and|are|was|were|been|being|have|has|had|will|would|"
    r"could|should|please|because|about|into|over|also|just|"
    r"this|that|these|those|with|from|your|our|their|they|"
    r"what|which|when|where|who|how|why|there|here|"
    r"can|for|not|but|you|its|"
    r"a|an|of|to|is|in|on|as|at|by|or|if|so|do|no"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _prose_for_language_identity(text: str) -> str:
    """Strip spans that are not natural-language sentences."""
    prose = _CODE_FENCE_RE.sub(" ", text)
    prose = _INLINE_CODE_RE.sub(" ", prose)
    prose = _URL_RE.sub(" ", prose)
    prose = _HTML_TAG_RE.sub(" ", prose)
    prose = _DOTTED_IDENT_RE.sub(" ", prose)
    prose = _SNAKE_IDENT_RE.sub(" ", prose)
    return prose


def _has_english_prose(text: str) -> bool:
    """True when leftover text still contains English sentence glue."""
    return bool(_EN_FUNCTION_WORD_RE.search(text))


def identity_invariant_reason(text: str, target: str) -> tuple[bool, str]:
    """Return whether translating ``text`` may legitimately preserve it.

    The target-language branch is deliberately conservative about *prose*.
    A dominant-language verdict on the raw buffer is insufficient because
    long assistant answers commonly start in the UI language and contain
    foreign-language sections later.  Ratios inspect the complete text
    after markup/code/URL masking, not the detector's bounded sample.
    """
    if not text:
        return False, ""
    if not _HAS_LETTER_RE.search(text):
        return True, "no translatable letters (symbols/digits only)"
    if not any(ch.isspace() for ch in text) and _URL_OR_PATH_RE.match(text):
        return True, "path-or-URL token"

    from lib.text_lang import (
        cjk_ratio,
        is_predominantly_chinese,
        is_predominantly_english,
    )

    normalized_target = (target or "").lower()
    target_is_chinese = (
        normalized_target.startswith("chinese") or "zh" in normalized_target
    )
    target_is_english = (
        normalized_target == "en"
        or normalized_target.startswith("english")
        or normalized_target.startswith("en-")
        or normalized_target.startswith("en_")
    )
    if target_is_chinese:
        prose = _prose_for_language_identity(text)
        # Identifier-heavy Chinese is still Chinese: masking code/URLs/tags
        # recovers CJK dominance that raw latin_ratio < 0.05 used to miss.
        # English function words in the leftover prose are the signal that
        # a later English paragraph still needs translation.
        if is_predominantly_chinese(prose) and not _has_english_prose(prose):
            return True, "source already Chinese (target Chinese)"
    elif target_is_english:
        if is_predominantly_english(text) and cjk_ratio(text) < 0.05:
            return True, "source already English (target English)"
    return False, ""


def should_skip_automatic_translation(
    text: str,
    target: str,
    target_code: str,
) -> bool:
    """Return True only when whole-document auto-translation is unnecessary.

    Bare paths/symbols are language-independent.  For target-language prose we
    additionally retain the statistical language check used to distinguish
    Japanese kanji from Chinese.  An *unknown* detector must not override a
    positive identity verdict — identifier-heavy Chinese commonly classifies
    as unknown when fastText is absent.
    """
    invariant, reason = identity_invariant_reason(text.strip(), target)
    if not invariant:
        return False
    if reason in {
        "no translatable letters (symbols/digits only)",
        "path-or-URL token",
    }:
        return True

    from lib.text_lang import detect_language

    detected = detect_language(text, force_fasttext=True).code
    if not detected or detected == "unknown":
        return True
    return detected == target_code


__all__ = [
    "identity_invariant_reason",
    "should_skip_automatic_translation",
]
