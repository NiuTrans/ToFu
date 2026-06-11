# HOT_PATH
"""Per-conversation accumulator for LLM usage spent *inside* compaction.

Why this exists
---------------
Compaction can itself call the LLM:
  * Layer 2 ``smart_summary_compact`` → ``_generate_query_aware_summary``
  * the advanced host's ``ctx.summarize`` (OpenCode/Hermes/OpenClaw arms)

Historically both did ``content, usage = dispatch_chat(...)`` and threw
``usage`` away. The orchestrator's ``task['usage']`` only sums the main
agent-loop rounds, so the summary calls' tokens were invisible — making
summary-based strategies look cheaper than they really are. For a
cost-comparison experiment that is a real bias (it under-reports exactly
the arms that summarize).

This module is the single place those calls report their token usage.
The orchestrator drains it at task finalize and folds it into
``task['usage']`` so the reported cost is complete, and also exposes it
separately as ``task['compactionUsage']`` for a per-arm "compaction
overhead" breakdown.

Thread-safety: a task runs in one worker thread, but the dispatch path
may touch this from helper threads; a lock keeps the per-conv dict
consistent under the concurrent SWE-bench harness.
"""

from __future__ import annotations

import threading

from lib.log import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
# conv_id → accumulated usage dict (prompt_tokens / completion_tokens /
# total_tokens / cache_read_tokens / cache_write_tokens / n_calls)
_usage: dict[str, dict] = {}

# The keys we accumulate. Mirrors the usage dict shape dispatch_chat returns.
_NUMERIC_KEYS = (
    'prompt_tokens', 'completion_tokens', 'total_tokens',
    'input_tokens', 'output_tokens',
    'cache_read_tokens', 'cache_write_tokens', 'cached_tokens',
)


def record_compaction_usage(conv_id: str, usage: dict | None,
                            kind: str = 'summary') -> None:
    """Add one compaction LLM call's ``usage`` to the conversation's tally.

    Args:
        conv_id: Conversation ID (no-op when empty — stateless calls).
        usage:   The usage dict returned by ``dispatch_chat`` (may be None).
        kind:    Free-form label for logging (e.g. 'L2', 'advanced').
    """
    if not conv_id or not isinstance(usage, dict):
        return
    with _lock:
        acc = _usage.setdefault(conv_id, {'n_calls': 0})
        acc['n_calls'] += 1
        for k in _NUMERIC_KEYS:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                acc[k] = acc.get(k, 0) + v
    logger.info('[CompactUsage] conv=%s +%s call: prompt=%s completion=%s',
                conv_id[:8] if conv_id else '?', kind,
                usage.get('prompt_tokens') or usage.get('input_tokens'),
                usage.get('completion_tokens') or usage.get('output_tokens'))


def get_compaction_usage(conv_id: str) -> dict:
    """Return a copy of the accumulated compaction usage for ``conv_id``
    (empty dict if none)."""
    if not conv_id:
        return {}
    with _lock:
        acc = _usage.get(conv_id)
        return dict(acc) if acc else {}


def pop_compaction_usage(conv_id: str) -> dict:
    """Return AND clear the accumulated compaction usage for ``conv_id``.
    Called by the orchestrator at task finalize so a reused conv_id does
    not double-count across turns."""
    if not conv_id:
        return {}
    with _lock:
        return _usage.pop(conv_id, {}) or {}


def reset_compaction_usage(conv_id: str) -> None:
    """Drop a conversation's compaction-usage tally (call on conv reset)."""
    if not conv_id:
        return
    with _lock:
        _usage.pop(conv_id, None)
