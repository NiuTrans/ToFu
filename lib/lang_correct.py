"""lib/lang_correct.py — the LLM-correction (Tier-2) helper for language detection.

``lib/text_lang.detect_language`` is dependency-free by design: it takes an
optional ``llm_corrector`` callable so the module never imports the LLM
dispatch layer. This module supplies that callable — a "typeless"-style
corrector that adjudicates the AMBIGUOUS tail (short / low-confidence / thin
English-vs-Latin-margin inputs) the statistical Tier-1 detector gets wrong
(the Intercom Fin failure class: ``"buenas dias"`` → pt@0.90, ``"App store?"``
→ no@0.40).

Two things live here, kept separate on purpose:

* :func:`llm_language_corrector` — the actual cheap-tier LLM call. It returns a
  lowercase language code (``'en'`` / ``'es'`` / …) or ``None`` when the model
  is unsure / errors. It is deliberately cheap + bounded (tiny max_tokens, a
  single non-streaming call) so escalation stays microsecond-cheap in aggregate
  because it only fires on the ~few % ambiguous tail.

* :func:`resolve_lang_correction_allowed` — the ``personal_scope`` gate. The
  corrector CAN SILENTLY BILL an LLM call, so it is registered as an app-level
  capability (``langCorrectionEnabled``) that fails CLOSED on headless surfaces
  (``apply_headless_personal_defaults`` already forces it off there) unless the
  caller opts in. Callers pass the resolved boolean to ``detect_language`` as
  ``allow_llm``; this function is the ONE place that reads the flag.
"""

from __future__ import annotations

import re
from typing import Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['llm_language_corrector', 'resolve_lang_correction_allowed']

# A short, focused prompt: the model returns ONLY a code. Kept tiny so the
# call is cheap and the output is trivially parseable.
_CORRECTOR_SYSTEM = (
    'You are a language identifier. Given a short user message, reply with '
    'ONLY the ISO 639-1 language code (two lowercase letters, e.g. en, es, '
    'de, fr, pt, zh, ja) of the language the message is written in. If you '
    'genuinely cannot tell, reply exactly: unknown. Output nothing else — no '
    'punctuation, no explanation.'
)

# A well-formed reply is a SINGLE code token: a 2–3-letter primary subtag, with
# an optional region/script suffix (e.g. ``zh-cn`` / ``pt_br``). We match the
# WHOLE cleaned response — a chatty reply like "The language is German (de)."
# has multiple tokens and is REJECTED (→ None), never salvaged into a garbage
# code by grabbing the first word. Anchored so partial matches don't sneak in.
_CODE_RE = re.compile(r'^([a-z]{2,3})(?:[-_][a-z0-9]{2,4})?$')


def llm_language_corrector(text: str, tier1=None) -> Optional[str]:
    """Cheap-tier LLM corrector for :func:`lib.text_lang.detect_language`.

    Args:
        text:  The (short) input whose language is ambiguous.
        tier1: The Tier-1 ``DetectionResult`` (its ``code`` is offered to the
               model as the statistical guess, for context). Optional.

    Returns:
        A lowercase language code, or ``None`` when the model is unsure /
        the call fails (the caller then keeps the Tier-1 result).
    """
    if not text or not text.strip():
        return None
    guess = ''
    try:
        guess = (getattr(tier1, 'code', '') or '')
    except Exception as e:
        logger.debug('[LangCorrect] tier1 guess extraction failed: %s', e)
        guess = ''
    hint = f' A statistical detector guessed "{guess}" but may be wrong.' if guess else ''
    user = f'Message: {text.strip()[:400]}{hint}'
    try:
        from lib.llm_dispatch import smart_chat
        content, _usage = smart_chat(
            messages=[{'role': 'system', 'content': _CORRECTOR_SYSTEM},
                      {'role': 'user', 'content': user}],
            max_tokens=8,
            temperature=0,
            capability='cheap',
            log_prefix='[LangCorrect]',
            max_retries=2,
        )
    except Exception as e:
        logger.warning('[LangCorrect] cheap-tier correction failed: %s', e)
        return None
    if isinstance(content, list):
        content = ''.join(b.get('text', '') for b in content
                          if isinstance(b, dict) and b.get('type') == 'text')
    # Normalize: strip whitespace + a trailing period/quote/backtick a model
    # might append, then require the WHOLE thing to be one code token. A chatty
    # multi-token reply fails the anchored match and is rejected — we never
    # grab the first word (which would turn "The language is..." into 'the').
    cleaned = (content or '').strip().strip('."\'`').lower()
    if not cleaned or cleaned == 'unknown':
        return None
    m = _CODE_RE.match(cleaned)
    if not m:
        logger.debug('[LangCorrect] rejected non-code reply: %.80r', cleaned)
        return None
    code = m.group(1)
    logger.info('[LangCorrect] tier1=%s → llm=%s (%d chars)',
                getattr(tier1, 'code', '?'), code, len(text))
    return code


def resolve_lang_correction_allowed(config: Optional[dict]) -> bool:
    """Whether the LLM-correction tier may fire for this request.

    Reads the ``langCorrectionEnabled`` flag which ``personal_scope`` forces
    OFF on every headless surface (unless the caller opted in) and defaults ON
    in the interactive UI. Absent flag → OFF (fail-closed): a caller/config
    that never mentions it must not silently trigger a billed correction call.
    """
    if not isinstance(config, dict):
        return False
    return bool(config.get('langCorrectionEnabled'))
