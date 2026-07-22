"""lib/transcription/_zh.py — normalize the Chinese variant of a transcript.

Whisper-family and omni-chat ASR models transcribe Mandarin into **Traditional**
Chinese by default (no language/prompt hint reliably steers this). For a
Simplified-Chinese user that reads as garbled variant text. This module is the
last-gate fix requested in approach "B": after a successful transcription the
raw text is run through a pure-Python variant converter so the injected text is
always in the target variant regardless of which model produced it.

Design (mirrors :mod:`lib.transcription._correct`)
--------------------------------------------------
* Env-gated + env-tunable: :func:`zh_variant_target` reads
  ``TOFU_ASR_ZH_VARIANT`` (default ``zh-cn`` = Simplified). Set it to an empty
  value / ``off`` / ``none`` to disable conversion entirely.
* Fail-safe: :func:`normalize_zh_variant` NEVER raises and NEVER returns a
  worse result than its input — a missing ``zhconv`` package, a non-Chinese
  transcript, or a converter error all fall through to the original text.
* Cheap short-circuit: text with no CJK characters (e.g. an English utterance)
  skips the converter — a no-op both semantically and for cost.

``zhconv`` is a pure-Python single-module converter (MediaWiki conversion
tables, MIT) — no native build step, so it fits a vanilla self-hosted machine
and the ``bootstrap.py`` auto-install model (CLAUDE.md §3.5).
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

# Variants zhconv understands. We validate the env value against this so a typo
# disables conversion (fail-safe) rather than raising deep in the transcribe path.
_ZHCONV_LOCALES = frozenset({
    'zh-cn', 'zh-hans', 'zh-sg', 'zh-my',
    'zh-tw', 'zh-hant', 'zh-hk', 'zh-mo',
})

# Values that explicitly turn conversion OFF.
_DISABLED_TOKENS = frozenset({'', 'off', 'no', 'none', '0', 'false', 'raw'})


def zh_variant_target() -> str | None:
    """Return the target Chinese variant locale, or ``None`` when disabled.

    Reads ``TOFU_ASR_ZH_VARIANT`` (default ``zh-cn`` — convert to Simplified).
    An empty value / ``off`` / ``none`` disables conversion. An unrecognized
    locale is treated as disabled (with a warning) so a typo never breaks the
    transcribe path.
    """
    raw = os.environ.get('TOFU_ASR_ZH_VARIANT')
    if raw is None:
        return 'zh-cn'  # default: normalize to Simplified Chinese
    val = raw.strip().lower()
    if val in _DISABLED_TOKENS:
        return None
    if val not in _ZHCONV_LOCALES:
        logger.warning('[STT] TOFU_ASR_ZH_VARIANT=%r is not a recognized '
                       'zhconv locale (%s) — variant conversion disabled',
                       raw, ', '.join(sorted(_ZHCONV_LOCALES)))
        return None
    return val


def _has_cjk(text: str) -> bool:
    """True when the text contains at least one CJK ideograph."""
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


def normalize_zh_variant(text: str) -> str:
    """Convert a transcript to the configured Chinese variant (fail-safe).

    Returns ``text`` unchanged when conversion is disabled, the text has no CJK
    characters, ``zhconv`` is unavailable, or conversion fails — this gate must
    never degrade or drop a transcript.
    """
    if not text:
        return text
    target = zh_variant_target()
    if not target:
        return text
    if not _has_cjk(text):
        return text
    try:
        import zhconv
    except ImportError as e:
        logger.debug('[STT] zhconv not installed — skipping variant '
                     'normalization: %s', e)
        return text
    try:
        converted = zhconv.convert(text, target)
    except Exception as e:
        logger.warning('[STT] zh variant conversion failed, keeping raw '
                       'transcript: %s', e)
        return text
    return converted or text
