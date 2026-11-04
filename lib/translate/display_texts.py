"""Translate short, mutually independent display strings in one bounded pass.

Responsibility: give model-authored interface strings (for example the
Coordinator task board's summary, titles and notes) the same display
translation as conversation content, without a second engine or new limits.
Each string is one synthetic ``text`` segment of the settled-turn segment core,
so the translation cache, protected spans, batching, the enrichment wall
budget, the upstream-429 budget and polite shared-contention admission are
exactly those of segment enrichment.

Entry point: ``translate_display_texts(texts, target, *, log_tag, abort_check=None)``.
Dependencies, imported on first call so importing this module keeps the engine
dormant: ``runtime._segments``, ``skip_policy``, ``prompt``, ``policy`` and
``lib.conv_config.target_lang_code``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

# Output-side display translation starts from the model's working language;
# whole-turn and incremental translation pin the same source.
_SOURCE = 'English'


def translate_display_texts(
    texts: Iterable[str],
    target: str,
    *,
    log_tag: str,
    abort_check: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """Return ``{text: translation}`` for the texts that gained a translation.

    A text the automatic-translation skip gate finds already in ``target``
    maps to itself without a provider call. Provider failures and unfunded
    budget are logged by the segment core and leave that text out; only an
    abort raises.
    """
    from lib.conv_config import target_lang_code

    from .policy import translation_max_429_attempts
    from .prompt import _build_translate_prompt
    from .runtime._segments import (
        _segment_enrichment_deadline_seconds,
        _translate_segments_to_map,
    )
    from .skip_policy import should_skip_automatic_translation

    target_code = target_lang_code(target)
    result: dict[str, str] = {}
    pending = []
    for text in dict.fromkeys(texts):
        if should_skip_automatic_translation(text, target, target_code):
            result[text] = text
        else:
            pending.append(text)
    if not pending:
        return result
    # Block ids must be truthy strings: the segment core falls back to a text
    # segment's llmRound when its blockId is empty, which would drop index 0.
    segments = [{'type': 'text', 'blockId': str(index), 'text': text}
                for index, text in enumerate(pending)]
    translated = _translate_segments_to_map(
        segments,
        _build_translate_prompt(target, _SOURCE),
        _SOURCE,
        target,
        log_tag=log_tag,
        abort_check=abort_check,
        overall_deadline=_segment_enrichment_deadline_seconds(
            sum(len(text) for text in pending)),
        max_429_attempts=translation_max_429_attempts(),
        defer_on_shared_contention=True,
    )
    for segment in segments:
        value = translated.get(segment['blockId'])
        if value:
            result[segment['text']] = value
    return result


__all__ = ['translate_display_texts']
