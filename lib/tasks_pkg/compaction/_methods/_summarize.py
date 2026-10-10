# HOT_PATH
"""Advanced-host LLM example — ``summarize_oldest_turn``.

Replaces the single oldest evictable turn with a terse cheap-model summary
injected as one assistant message.  Demonstrates the ``needs=('llm',)``
capability (``ctx.summarize``) combined with structural surgery
(``ctx.edit``).
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    CompactionContext,
    register_step,
)
from lib.tasks_pkg.compaction._methods._shared import (
    _already_compacted,
    _content_str,
    _log_id,
)

logger = get_logger(__name__)


@register_step('summarize_oldest_turn', kind=STEP_KIND_STRUCTURAL,
               needs=('llm',))
def summarize_oldest_turn(ctx: CompactionContext) -> int:
    """LLM example: replace the single oldest evictable turn with a terse
    cheap-model summary injected as one assistant message.

    Demonstrates the ``needs=('llm',)`` capability (``ctx.summarize``)
    combined with structural surgery (``ctx.edit``): the turn's prose is
    summarized, the turn is dropped, and a one-line summary message is
    spliced in at the turn's former position.  A real recursive/rolling
    summarizer would maintain a running note across rounds; this keeps
    the example minimal but exercises both granted capabilities.
    """
    editor = ctx.edit
    if editor is None:
        return 0
    evictable = editor.evictable_turns()
    if not evictable:
        return 0

    oldest = evictable[0]
    msgs = ctx.messages

    # Gather the turn's natural-language text for summarization.
    chunks = []
    for i in oldest.indices:
        t = _content_str(msgs[i])
        if t and not _already_compacted(t):
            role = msgs[i].get('role', '?')
            chunks.append(f'[{role}] {t}')
    blob = '\n\n'.join(chunks).strip()
    if len(blob) < 400:  # not worth an LLM round-trip
        return 0

    summary = ctx.summarize(
        blob, instruction='Summarize this earlier turn of a coding session '
        'in 2-3 sentences, preserving file paths, decisions, and outcomes.',
        max_tokens=256)
    if not summary:
        return 0

    saved = editor.drop_turns([oldest])
    if saved <= 0:
        return 0

    # Splice the summary in at the (now-shifted) position of the dropped
    # turn's start.  After drop_turns, the message that was at
    # oldest.end is now at index oldest.start.
    summary_msg = {
        'role': 'assistant',
        'content': f'[Earlier turn summarized] {summary}',
    }
    insert_at = min(oldest.start, len(msgs))
    msgs.insert(insert_at, summary_msg)
    saved -= len(summary_msg['content']) // 4
    logger.info('[Adv-summary] conv=%s  summarized + dropped oldest turn '
                '(~%d net tokens saved)',
                _log_id(ctx.conv_id), max(0, saved))
    return max(0, saved)
