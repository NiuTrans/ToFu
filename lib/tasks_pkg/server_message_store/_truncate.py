"""Old-turn tool-result truncation — constants + ``_truncate_old_tool_results``.

Truncates the bulky content of tool results in OLD turns (not the most recent
completed turn) to control context growth, while remaining cache-safe: never
mutates bytes inside the previous round's cached prefix, and is idempotent so
an already-truncated result stays byte-identical on later turns.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ── Truncation constants for old-turn tool results ──
_OLD_RESULT_MAX_CHARS = 2000
"""Max chars to keep from tool results in old turns (not the latest completed turn).
Keeps tool names + args fully visible, but truncates the bulky result content.
Set to 0 to strip all old results. Set to None to disable truncation."""

_TRUNCATION_MARKER = 'truncated —'
"""Stable substring of the trailing note this module stamps onto a truncated
old tool result. Used as the idempotency probe so a result truncated on a
PREVIOUS turn is recognised and left byte-identical on later turns (re-running
the truncation would drift the embedded char count and mutate the cache
prefix). MUST stay a substring of the f-string note emitted below."""


def _truncate_old_tool_results(
    messages: list[dict[str, Any]],
    max_chars: int | None = _OLD_RESULT_MAX_CHARS,
    conv_id: str = '',
) -> int:
    """Truncate tool result content in older turns, keeping recent turn intact.

    Strategy: find the last user message (which starts the current turn).
    The turn before that is the "most recent completed turn" — keep its tool
    results intact. Everything older gets truncated.

    ★ CACHE-CRITICAL prefix gate (2026-06-23). The turn boundary alone is NOT
    enough. A large tool result enters the message list at FULL size in the
    round it ran, becoming part of the Anthropic-cached prefix. On the NEXT
    round a new user message advances ``old_boundary`` PAST that result, so
    this function would truncate it FOR THE FIRST TIME — but it is STILL inside
    the cached prefix (``messages[0:prefix_n]``). That first-time truncation
    rewrites already-cached bytes → ``PREFIX MUTATION DETECTED`` → the whole
    conversation body is re-billed uncached (cache_read pinned at the static
    system+tools floor, cache_write climbing monotonically as the conv grows —
    exactly one more result crosses the boundary inside the prefix each round).
    The idempotency marker only stops RE-truncation; it does nothing for this
    first-time-inside-prefix mutation. So we additionally skip any message
    whose index is still within the previous round's cache prefix. Such a
    result is left byte-identical now and gets truncated on a LATER round once
    it has scrolled OUT of the prefix — by then it is no longer cached, so the
    edit costs nothing. See .tofu/memories/cache-bp4-moving-tail-rebills-body and
    prefix-mutation-cache-miss-truncate-old-tool-results.

    Returns the number of tool results truncated.
    """
    if max_chars is None:
        return 0

    # Find user message indices to identify turn boundaries
    user_indices = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    if len(user_indices) < 2:
        return 0  # Only one turn, nothing to truncate

    # The last user msg is the current (new) question.
    # The second-to-last user msg starts the most recent completed turn.
    # Everything BEFORE that turn boundary is "old".
    old_boundary = user_indices[-2]

    # ★ Cache-prefix floor: messages[0:prefix_n] were cached last round and
    #   MUST stay byte-identical. Truncating inside this window is the bug
    #   above. 0 when cache wasn't active (fresh conv / non-cached provider) —
    #   then only the turn boundary gates, preserving prior behaviour.
    #
    # ★ COLD-THREAD RELIANCE — this runs at TURN-START on a FRESH run_task
    #   thread, BEFORE detect_cache_break has populated this thread's in-memory
    #   CacheState. So ``prefix_n`` here depends on get_cache_prefix_count's
    #   warm-sibling / DURABLE-HWM fallback (settings.cachePrefixHWM). If that
    #   floor is missing (the pre-HWM-persist regime), prefix_n collapses to 0
    #   and this loop truncates already-cached tool results → whole-prefix
    #   re-bill (observed on conv mrni76b8 call=34: 7 old results truncated at
    #   turn-start, then cache_r fell to the static floor, 298k re-billed). The
    #   durable HWM (lib/tasks_pkg/cache_tracking/_persist.py) is what keeps the
    #   floor alive across the new-thread turn boundary.
    #
    # ★ DELIBERATELY NO current_msg_count. get_cache_prefix_count's clamp only
    #   ever LOWERS the boundary (for micro_compact, a lower boundary = safe, it
    #   compacts MORE cold tail). Here the danger is the OPPOSITE: a boundary
    #   that is too SMALL truncates cached prefix bytes. A larger prefix_n only
    #   OVER-protects (defers truncation to a later round once the result has
    #   scrolled out of the prefix — costless then). So we pass NO clamp: the
    #   monotonic HWM floor is exactly the protection we want, and clamping it
    #   down would re-open the leak. Do NOT "add current_msg_count for symmetry".
    prefix_n = 0
    if conv_id:
        try:
            from lib.tasks_pkg.cache_tracking import get_cache_prefix_count
            prefix_n = get_cache_prefix_count(conv_id)
        except Exception as e:
            logger.debug('[MsgStore] conv=%s prefix-count lookup failed, '
                         'truncating by turn boundary only: %s', conv_id[:8], e)
            prefix_n = 0

    truncated = 0
    for i, msg in enumerate(messages):
        if i >= old_boundary:
            break  # Don't touch the latest completed turn or current turn
        if i < prefix_n:
            continue  # Still inside the cached prefix — truncating mutates it
        if msg.get('role') != 'tool':
            continue
        content = msg.get('content', '')
        if not isinstance(content, str):
            continue
        if len(content) <= max_chars:
            continue
        # ★ CACHE-CRITICAL idempotency guard. This runs at the START of
        #   EVERY turn against the server-stored messages (live dict refs —
        #   save_messages keeps mutable references, get_messages hands them
        #   back), so a message already truncated on a previous turn must NOT
        #   be re-truncated. The old guard only recognised the micro-compact
        #   '[...compacted...]' marker, NOT this function's OWN trailing
        #   '[... truncated — was N chars ...]' marker. A 2080-char already-
        #   truncated result therefore got re-truncated every turn, and the
        #   embedded "was N chars" count drifted (5,000 → 2,080 → …),
        #   silently rewriting bytes inside the cached prompt prefix
        #   (messages[0:N]). That is exactly the "PREFIX MUTATION DETECTED"
        #   every-turn cache miss: each new turn re-billed the whole body
        #   uncached (full cache_write, cache_read pinned at the static
        #   system+tools prefix) with no error surfaced. Recognise BOTH
        #   markers so truncation is a fixpoint.
        _head = content[:80]
        if content.startswith('[') and 'compacted' in _head:
            continue
        if _TRUNCATION_MARKER in content[-200:]:
            continue

        tool_name = msg.get('name', 'tool')
        original_len = len(content)
        preview = content[:max_chars]
        # Truncate at last newline for cleanliness
        last_nl = preview.rfind('\n', max_chars // 2)
        if last_nl > 0:
            preview = preview[:last_nl]
        msg['content'] = (
            f'{preview}\n\n'
            f'[... {_TRUNCATION_MARKER} was {original_len:,} chars, '
            f'showing first {len(preview):,}. '
            f'Re-call {tool_name} if full content needed.]'
        )
        truncated += 1

    return truncated
