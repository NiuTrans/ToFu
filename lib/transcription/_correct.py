"""lib/transcription/_correct.py — optional LLM correction pass (opt-in, NOT MVP).

The "cheap LLM fixes ASR errors" step, gated by :func:`correction_enabled`
(env ``TOFU_ASR_CORRECTION``, default OFF). Published generative-error-correction
results show a zero-shot correction pass can INCREASE word-error-rate, and
``gpt-4o-transcribe`` already corrects internally — so this is deliberately NOT
wired into the MVP transcribe path. It stays a measured, opt-in follow-up.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.transcription._config import correction_enabled

logger = get_logger(__name__)


def maybe_correct(text: str, *, context: str | None = None) -> str:
    """Optionally clean up an ASR transcript with a cheap LLM (no-op when off).

    Returns ``text`` unchanged when :func:`correction_enabled` is False (the
    default) so callers can invoke it unconditionally once wired. When enabled,
    routes a low-temperature ``cheap``-capability chat that fixes homophones,
    punctuation, and obvious proper-noun slips WITHOUT adding content. This is
    a follow-up increment — the MVP transcribe route does NOT call it.
    """
    if not text or not correction_enabled():
        return text
    try:
        from lib.llm_dispatch import dispatch_chat
        sys_prompt = (
            'You correct raw speech-to-text transcripts. Fix homophones, '
            'punctuation, casing, and obvious proper-noun mis-hearings. Do NOT '
            'add, remove, summarize, translate, or answer content. Return ONLY '
            'the corrected transcript text.')
        user = text if not context else f'Context: {context}\n\nTranscript:\n{text}'
        corrected, _usage = dispatch_chat(
            [{'role': 'system', 'content': sys_prompt},
             {'role': 'user', 'content': user}],
            capability='cheap', temperature=0, max_tokens=2048,
            log_prefix='[STT-correct]')
        corrected = (corrected or '').strip()
        return corrected or text
    except Exception as e:
        logger.warning('[STT] correction pass failed, returning raw text: %s', e)
        return text
