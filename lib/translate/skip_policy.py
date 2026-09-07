"""Conservative identity policy for automatic translation.

Language detection answers which language dominates a document.  It does not
answer whether *every meaningful span* is already in the target language.
Automatic translation may skip only identity-invariant content after examining
the whole document; mixed-language prose must continue to the translator.
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


def identity_invariant_reason(text: str, target: str) -> tuple[bool, str]:
    """Return whether translating ``text`` may legitimately preserve it.

    The target-language branch is deliberately conservative.  A dominant
    language verdict is insufficient because long assistant answers commonly
    start in the UI language and contain foreign-language sections later.
    Ratios inspect the complete text, not the detector's bounded sample.
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
        latin_ratio,
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
        # Five percent permits incidental acronyms while ensuring a later
        # English paragraph cannot be hidden by a Chinese-majority opening.
        if is_predominantly_chinese(text) and latin_ratio(text) < 0.05:
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
    Japanese kanji from Chinese; both conditions must agree before skipping.
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

    return detect_language(text, force_fasttext=True).code == target_code


__all__ = [
    "identity_invariant_reason",
    "should_skip_automatic_translation",
]
