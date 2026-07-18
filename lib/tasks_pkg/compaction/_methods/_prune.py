# HOT_PATH
"""OpenCode-inspired transform step — ``prune_with_hysteresis``.

Only prune cold tool output when there is a WORTHWHILE amount to reclaim
(PRUNE_MINIMUM), and always protect a token-budget tail (PRUNE_PROTECT).
The hysteresis avoids the compact→re-read→recompact churn loop a
per-result threshold causes.
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


# Defaults mirror opencode's PRUNE_PROTECT (40k) / PRUNE_MINIMUM (20k) in
# spirit but scaled to our smaller per-step budgets; overridable via
# constant_overrides for experiment arms.
_PRUNE_PROTECT_TOKENS_DEFAULT = 12_000
_PRUNE_MINIMUM_TOKENS_DEFAULT = 4_000


def _tool_text_len(msg: dict) -> int:
    t = _content_str(msg)
    return len(t) if t else 0


@register_step('prune_with_hysteresis')
def prune_with_hysteresis(ctx: CompactionContext) -> int:
    """Prune cold tool output, but only when worthwhile (OpenCode-style).

    Two-stage gate (the hysteresis):
      1. Protect a token-budget tail: walk backward over tool results
         accumulating their estimated tokens until ``PRUNE_PROTECT``
         tokens are covered — those are never pruned (cheaper + simpler
         than a fixed count, and adapts to result sizes).
      2. Only prune the remaining (older) tool results if the total
         reclaimable estimate exceeds ``PRUNE_MINIMUM``. If there's little
         to gain, do nothing — this is what prevents the
         compact→re-read→recompact churn loop that a per-result threshold
         causes (re-reading a file re-creates a big result that would be
         re-pruned next round for tiny gains).

    Tunables (via ``constant_overrides``):
      * ``PRUNE_PROTECT_TOKENS`` (default 12000)
      * ``PRUNE_MINIMUM_TOKENS``  (default 4000)
    """
    _c = ctx.constants
    messages = ctx.messages
    protect = int(getattr(_c, 'PRUNE_PROTECT_TOKENS', _PRUNE_PROTECT_TOKENS_DEFAULT))
    minimum = int(getattr(_c, 'PRUNE_MINIMUM_TOKENS', _PRUNE_MINIMUM_TOKENS_DEFAULT))

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if not tool_indices:
        return 0

    # Stage 1: protect a token-budget tail (walk newest→oldest).
    protected: set[int] = set()
    acc = 0
    for idx in reversed(tool_indices):
        if acc >= protect:
            break
        protected.add(idx)
        acc += _tool_text_len(messages[idx]) // 4

    prunable = [i for i in tool_indices if i not in protected
                and not ctx.is_in_cache_prefix(i)]

    # Estimate reclaimable tokens across prunable cold results.
    reclaimable = 0
    candidates = []
    for idx in prunable:
        msg = messages[idx]
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            continue
        if len(text) <= _c.MICRO_COMPACT_THRESHOLD:
            continue
        reclaimable += len(text) // 4
        candidates.append((idx, len(text)))

    # Stage 2: hysteresis — only act if the gain clears the minimum.
    if reclaimable < minimum:
        logger.debug('[OC-prune] conv=%s  reclaimable=%d < minimum=%d — '
                     'skipping (avoids churn)',
                     _log_id(ctx.conv_id), reclaimable, minimum)
        return 0

    pruned = 0
    tokens_saved = 0
    for idx, old_len in candidates:
        msg = messages[idx]
        tool_name = msg.get('name', 'tool')
        placeholder = (f'[{tool_name} output pruned — was {old_len:,} chars '
                       f'— re-call tool if needed]')
        msg['content'] = placeholder
        tokens_saved += (old_len - len(placeholder)) // 4
        pruned += 1
        ctx.stamp(msg, old_len, len(placeholder))

    if pruned > 0:
        logger.info('[OC-prune] conv=%s  pruned %d cold tool results past the '
                    '%d-token protected tail (~%d tokens saved; minimum=%d met)',
                    _log_id(ctx.conv_id), pruned, protect, tokens_saved, minimum)
    return tokens_saved
