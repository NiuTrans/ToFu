# HOT_PATH
"""M2 — observation folding (structured one-line facts).

Replace a cold ``grep_search`` / ``find_files`` / ``list_dir`` result with
a one-line *structured fact* extracted cheaply (no LLM): e.g.
``grep_search → 3 match line(s)``.  Preserves the *answer* the tool
produced rather than just "it was compacted", so the model rarely needs to
re-call it.
"""

from __future__ import annotations

import re

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._methods._shared import (
    _already_compacted,
    _content_str,
    _log_id,
)

logger = get_logger(__name__)


# Tools whose output can be losslessly summarised to a one-line fact.
_FOLDABLE_TOOLS = frozenset({'grep_search', 'find_files', 'list_dir'})


def _fold_fact(tool_name: str, text: str) -> str | None:
    """Produce a one-line structured fact for a foldable tool result, or
    None if we can't summarise it confidently (then leave it to the
    generic compactor)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if tool_name == 'grep_search':
        # Result lines look like "path:line: match" — count them.
        match_lines = [ln for ln in lines
                       if re.match(r'.+:\d+:', ln) or re.search(r':\d+:', ln)]
        n = len(match_lines) if match_lines else max(0, len(lines) - 1)
        sample = match_lines[0][:120] if match_lines else ''
        fact = f'[grep_search folded — {n} match line(s)]'
        if sample:
            fact += f' first: {sample}'
        return fact
    if tool_name == 'find_files':
        n = len(lines)
        return f'[find_files folded — {n} path(s) matched]'
    if tool_name == 'list_dir':
        return f'[list_dir folded — {len(lines)} entr(y/ies) listed]'
    return None


@register_step('fold_observations')
def fold_observations(ctx: CompactionContext) -> int:
    """Fold cold search/list tool results to a one-line structured fact (M2).

    Preserves the *answer* (how many hits / paths / entries, plus the
    first hit) instead of a generic "re-call tool" placeholder, so the
    model keeps the useful signal at a fraction of the tokens.
    """
    _c = ctx.constants
    messages = ctx.messages

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if len(tool_indices) <= _c.MICRO_HOT_TAIL:
        return 0
    cold_indices = tool_indices[:-_c.MICRO_HOT_TAIL]

    folded = 0
    tokens_saved = 0

    for idx in cold_indices:
        if ctx.is_in_cache_prefix(idx):
            continue
        msg = messages[idx]
        tool_name = msg.get('name', '')
        if tool_name not in _FOLDABLE_TOOLS:
            continue
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            continue
        if len(text) <= _c.MICRO_COMPACT_THRESHOLD:
            continue
        fact = _fold_fact(tool_name, text)
        if not fact:
            continue
        old_len = len(text)
        msg['content'] = fact
        tokens_saved += (old_len - len(fact)) // 4
        folded += 1
        ctx.stamp(msg, old_len, len(fact))

    if folded > 0:
        logger.info('[M2-fold] conv=%s  folded %d cold observation results '
                    '(~%d tokens saved; structured fact preserved)',
                    _log_id(ctx.conv_id), folded, tokens_saved)
    return tokens_saved
