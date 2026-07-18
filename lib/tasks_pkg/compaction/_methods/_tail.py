# HOT_PATH
"""OpenCode-inspired transform step — ``adaptive_hot_tail``.

Replace the fixed MICRO_HOT_TAIL count with a token-budget boundary that
walks back over tool-pairs, so the hot window self-tunes to the model's
context size.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._methods._shared import (
    _already_compacted,
    _content_str,
    _log_id,
)

logger = get_logger(__name__)


_ADAPTIVE_TAIL_BUDGET_DEFAULT = 24_000


@register_step('adaptive_hot_tail')
def adaptive_hot_tail(ctx: CompactionContext) -> int:
    """Token-budget hot tail instead of a fixed MICRO_HOT_TAIL count.

    Walks backward over ALL messages accumulating estimated tokens until
    ``ADAPTIVE_TAIL_BUDGET`` is covered; every cold tool result before
    that boundary (and outside the cache prefix) is compacted with the
    same placeholder style as the generic compactor.

    This is a drop-in alternative to ``compact_tool_results`` for arms
    that want the hot window to scale with content size rather than a
    fixed message count. Use ONE of them in a given arm, not both.

    Tunable: ``ADAPTIVE_TAIL_BUDGET`` (default 24000) via ``constant_overrides``.
    """
    _c = ctx.constants
    messages = ctx.messages
    budget = int(getattr(_c, 'ADAPTIVE_TAIL_BUDGET', _ADAPTIVE_TAIL_BUDGET_DEFAULT))

    # Find the boundary index: everything at boundary..end is "hot".
    acc = 0
    boundary = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        if acc >= budget:
            boundary = idx + 1
            break
        t = _content_str(messages[idx])
        acc += (len(t) // 4) if t else 0
        boundary = idx

    compacted = 0
    tokens_saved = 0
    for idx in range(boundary):
        if ctx.is_in_cache_prefix(idx):
            continue
        msg = messages[idx]
        if msg.get('role') != 'tool':
            continue
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            continue
        if len(text) <= _c.MICRO_COMPACT_THRESHOLD:
            continue
        tool_name = msg.get('name', 'tool')
        old_len = len(text)
        placeholder = (f'[{tool_name} result compacted — was {old_len:,} chars '
                       f'— re-call tool if full content needed]')
        msg['content'] = placeholder
        tokens_saved += (old_len - len(placeholder)) // 4
        compacted += 1
        ctx.stamp(msg, old_len, len(placeholder))

    if compacted > 0:
        logger.info('[OC-adaptive] conv=%s  compacted %d cold tool results '
                    'outside %d-token hot tail (~%d tokens saved)',
                    _log_id(ctx.conv_id), compacted, budget, tokens_saved)
    return tokens_saved
