"""System-message manipulation primitives — reminders + append helpers.

Extracted from ``lib.tasks_pkg.system_context`` (facade-preserving split).

Holds the low-level, dependency-free helpers used by the injection
orchestrator: the ``<system-reminder>`` wrapper, the first-system-message
appender, the plain-text system extractor, and the old-timestamp stripper.
"""

from lib.log import get_logger

logger = get_logger(__name__)

_TIMESTAMP_PREFIX = 'Current date and time: '


def _strip_old_timestamp(text: str) -> str:
    """Remove a previously injected timestamp line from user message text."""
    lines = text.split('\n')
    cleaned = [ln for ln in lines if not ln.strip().startswith(_TIMESTAMP_PREFIX)]
    # Also strip trailing blank lines left behind
    result = '\n'.join(cleaned).rstrip()
    return result


def _wrap_system_reminder(text: str) -> str:
    """Wrap text in <system-reminder> tags.

    Claude Code wraps all mid-conversation system-level injections in these
    tags to distinguish them from user-authored content.  The model is trained
    to treat <system-reminder> content as authoritative system instructions.

    We use the same convention for dynamic injected context (project, memory,
    search addendum, swarm) so that:
      1. The model clearly distinguishes system instructions from user text.
      2. Compaction can identify and preserve system-reminder blocks.
      3. Context is consistent with Claude Code's convention.
    """
    return f'<system-reminder>\n{text}\n</system-reminder>'


def _append_to_system_message(messages, text, *, as_separate_block=False):
    """Append text to the first system message, or create one if absent.

    Helper used by _inject_system_contexts to avoid repeating the
    str-vs-list content detection pattern.

    Args:
        messages: The messages list (mutated in-place).
        text: The text to append.
        as_separate_block: If True and content is already a list,
            append as a separate text block (for cache segmentation).
            If content is a string, convert to list-of-blocks first.
    """
    if messages and messages[0].get('role') == 'system':
        sc = messages[0].get('content', '')
        if as_separate_block:
            # Force list-of-blocks format for cache segmentation
            if isinstance(sc, str):
                messages[0]['content'] = [
                    {'type': 'text', 'text': sc},
                    {'type': 'text', 'text': text},
                ]
            elif isinstance(sc, list):
                messages[0]['content'].append({'type': 'text', 'text': text})
            else:
                messages[0]['content'] = [{'type': 'text', 'text': text}]
        else:
            if isinstance(sc, str):
                messages[0]['content'] = sc + '\n\n' + text
            elif isinstance(sc, list):
                # Merge into last text block to avoid block proliferation
                if sc and isinstance(sc[-1], dict) and sc[-1].get('type') == 'text':
                    sc[-1] = {**sc[-1], 'text': sc[-1]['text'] + '\n\n' + text}
                else:
                    messages[0]['content'].append({'type': 'text', 'text': text})
    else:
        # No system message yet — create one.
        # Respect as_separate_block so callers that want downstream cache
        # segmentation don't get stuck with a string content.
        if as_separate_block:
            messages.insert(0, {'role': 'system',
                                'content': [{'type': 'text', 'text': text}]})
        else:
            messages.insert(0, {'role': 'system', 'content': text.strip()})


def _refresh_tail_block(messages, block: str | None, marker: str) -> str:
    """Place/refresh a volatile ``<system-reminder>`` block on the TRUE tail.

    The cache-safe seam for AMBIENT-but-VOLATILE project context (the
    cross-conversation digest, the charter, the board) — the SAME seam
    ``_refresh_detail_block`` uses for the relevance-gated preference tier.

    Why NOT the system message (the previous home, bug: prefix-cache 29298-pin):
    these blocks change round-over-round as sibling conversations evolve (the
    digest re-orders, the board's epics move, the charter grows on a commit).
    On the Anthropic path ``system`` is hoisted to the top-level ``system``
    field and is the CACHED FLOOR — rewriting it every turn re-bills the WHOLE
    body uncached (``cache_read`` pinned at the static prompt, ~29298 tokens).
    Riding the true tail instead keeps the static system prefix + conversation
    history byte-stable across turns (so ``cache_read`` can grow), and confines
    the volatile bytes to the already-volatile tail region.

    Idempotency / per-turn refresh: within ONE task (endpoint Planner / Worker
    / Critic reuse the same message list) the last user message may be visited
    more than once, so this STRIPS any existing block carrying ``marker`` from
    the last user message and re-appends this turn's ``block`` (or, when
    ``block`` is None, just removes it). A PRIOR turn's block, frozen on a
    now-historical user message, is left untouched — the same tradeoff
    ``<relevant_memories>`` / the detail tier already make.

    Args:
        messages: message list (mutated in place).
        block: the ``<system-reminder>``-wrapped text to place, or None to only
            strip a stale one.
        marker: an idempotency substring the block carries (e.g.
            ``'[PROJECT CHARTER]'``) — used to find + strip the stale copy.

    Returns one of ``'replaced'`` / ``'added'`` / ``'removed'`` / ``'noop'``.
    """
    target_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            target_idx = i
            break
    if target_idx is None:
        return 'noop'

    content = messages[target_idx].get('content', '')
    if isinstance(content, str):
        # Never fabricate a phantom empty text block (content == '') — strict
        # providers hard-400 the request on it (Kimi: "text content is empty").
        blocks = [{'type': 'text', 'text': content}] if content.strip() else []
    elif isinstance(content, list):
        blocks = list(content)
    else:
        blocks = []

    def _has_marker(blk) -> bool:
        return (isinstance(blk, dict) and blk.get('type') == 'text'
                and marker in blk.get('text', ''))

    had_old = any(_has_marker(b) for b in blocks)
    if not had_old and block is None:
        return 'noop'

    new_blocks = [b for b in blocks if not _has_marker(b)]
    if block is not None:
        new_blocks.append({'type': 'text', 'text': block})

    messages[target_idx]['content'] = new_blocks
    if block is not None:
        return 'replaced' if had_old else 'added'
    return 'removed'


def _system_text(messages) -> str:
    """Return the plain-text concatenation of the first system message.

    Used for idempotency checks in ``_inject_system_contexts`` — callers
    can look for a known marker substring (e.g. ``[PROJECT CO-PILOT MODE]``,
    ``Function Result Clearing``) to detect whether a context block has
    already been injected.  Returns empty string when there is no system
    message.
    """
    if not messages or messages[0].get('role') != 'system':
        return ''
    sc = messages[0].get('content', '')
    if isinstance(sc, str):
        return sc
    if isinstance(sc, list):
        parts = []
        for b in sc:
            if isinstance(b, dict) and b.get('type') == 'text':
                parts.append(b.get('text', '') or '')
        return '\n\n'.join(parts)
    return ''
