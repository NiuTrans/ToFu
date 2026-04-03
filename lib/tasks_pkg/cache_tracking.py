# HOT_PATH — called every round in the orchestrator.
"""Prompt Cache Break Detection & Cache-Aware Microcompact.

Inspired by Claude Code's ``promptCacheBreakDetection.ts`` (727 lines).

Two features:
  1. **Cache break detection**: two-phase approach (like Claude Code):
     - Phase 1 (pre-call): hash system prompt, tools, and message count
       to detect what WOULD cause a cache break.
     - Phase 2 (post-call): check API-reported cache_read_tokens to
       confirm whether a break actually occurred.
     Uses only system/tools/model/message-count changes (NOT message
     content hashes) to avoid false positives from micro-compact mutations.
  2. **Cache-aware microcompact**: when editing messages, skip those in the
     "cache prefix" (messages that were part of the last cache hit) to
     maintain byte-identical content for prompt cache stability.

Key insight (from investigating "cache_read_tokens stays unchanged"):
  The old code hashed message PREFIX content, which changed every round due
  to micro-compact mutating cold tool results → false positive warnings.
  The new approach separates "things that break server-side cache" (system
  prompt, tools, model) from "expected content changes" (tool result
  compaction, new messages appended).

  For Anthropic: cache breakpoints must advance with the conversation tail
  to cover the growing prefix (fixed in add_cache_breakpoints).

  For OpenAI/Qwen automatic prefix caching: micro-compact must NOT mutate
  messages inside the cached prefix (enforced by get_cache_prefix_count).
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache state tracking
# ═══════════════════════════════════════════════════════════════════════════════

class CacheState:
    """Tracks the state of the prompt cache for a conversation.

    Stores hashes of system prompt, tools, and message count so we can
    detect what changed between turns.  Does NOT hash message content
    because micro-compact legitimately mutates older messages — hashing
    content would produce false positives on every round.
    """
    __slots__ = (
        'system_hash', 'tools_hash', 'model',
        'message_count', 'last_cache_read_tokens',
        'last_update_time', 'call_count',
        'compaction_pending',
    )

    def __init__(self):
        self.system_hash: str = ''
        self.tools_hash: str = ''
        self.model: str = ''
        self.message_count: int = 0
        self.last_cache_read_tokens: int = 0
        self.last_update_time: float = 0.0
        self.call_count: int = 0
        self.compaction_pending: bool = False


_cache_states: dict[str, CacheState] = {}
"""Per-conv_id cache state."""

_cache_lock = threading.Lock()


def _md5(text: str) -> str:
    """Fast hash for comparison (not security)."""
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()[:16]


def _hash_system_prompt(messages: list) -> str:
    """Hash the system message content."""
    for msg in messages:
        if msg.get('role') == 'system':
            content = msg.get('content', '')
            if isinstance(content, list):
                parts = [
                    b.get('text', '') for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                ]
                return _md5(''.join(parts))
            return _md5(str(content))
    return ''


def _hash_tools(tools: list | None) -> str:
    """Hash the tool definitions."""
    if not tools:
        return ''
    try:
        return _md5(json.dumps(tools, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError):
        return _md5(str(tools))


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache break detection
# ═══════════════════════════════════════════════════════════════════════════════

# Minimum absolute token drop required to trigger a cache break warning.
# Small drops (e.g., a few thousand tokens) can happen due to normal
# variation and aren't worth alerting on.
_MIN_CACHE_MISS_TOKENS = 2000


def detect_cache_break(
    conv_id: str,
    messages: list,
    tools: list | None,
    model: str,
    usage: dict | None = None,
) -> dict[str, Any] | None:
    """Two-phase cache break detection (inspired by Claude Code).

    Phase 1: Compare system/tools/model hashes to detect WHAT changed.
    Phase 2: Check API-reported cache_read_tokens to confirm whether
             a break actually occurred.

    Returns a dict describing what changed, or None if no break detected.
    Logs warnings on significant cache breaks for cost diagnostics.

    Key change from previous implementation:
      - Does NOT hash message content (avoids false positives from
        micro-compact mutations)
      - Only tracks system prompt, tools, model, and message count
      - Uses API-reported cache tokens as the source of truth
      - Accounts for compaction events (expected token drops)
    """
    if not conv_id:
        return None

    import time
    now = time.time()

    with _cache_lock:
        prev = _cache_states.get(conv_id)
        if prev is None:
            prev = CacheState()
            _cache_states[conv_id] = prev

        # ── Phase 1: Detect WHAT changed (client-side hashes) ──
        sys_hash = _hash_system_prompt(messages)
        tools_hash = _hash_tools(tools)
        msg_count = len(messages)

        client_changes = {}
        if prev.call_count > 0:
            if sys_hash != prev.system_hash:
                client_changes['system_prompt'] = 'changed'
            if tools_hash != prev.tools_hash:
                client_changes['tools'] = 'changed'
            if model != prev.model:
                client_changes['model'] = f'{prev.model} → {model}'
            # Message count going DOWN indicates compaction/truncation
            if msg_count < prev.message_count:
                client_changes['message_count'] = (
                    f'{prev.message_count} → {msg_count} (compacted)')

        # ── Phase 2: Check API-reported cache stats ──
        cache_read = 0
        if usage:
            cache_read = (usage.get('cache_read_tokens')
                          or usage.get('cache_read_input_tokens')
                          or 0)

        prev_cache_read = prev.last_cache_read_tokens

        # Handle compaction: if compaction happened, a drop in cache_read
        # is expected — don't flag it as a break.
        if prev.compaction_pending:
            prev.compaction_pending = False
            if cache_read < prev_cache_read:
                logger.debug(
                    '[CacheTrack] conv=%s Expected cache drop after compaction: '
                    '%d → %d tokens',
                    conv_id[:8], prev_cache_read, cache_read)

        # Detect actual cache break from API response:
        # cache_read dropped >5% AND the absolute drop exceeds threshold
        api_break = False
        if (prev.call_count > 0
                and prev_cache_read > _MIN_CACHE_MISS_TOKENS
                and cache_read < prev_cache_read * 0.95
                and (prev_cache_read - cache_read) >= _MIN_CACHE_MISS_TOKENS
                and not prev.compaction_pending):
            api_break = True

        # ── Update state ──
        prev.system_hash = sys_hash
        prev.tools_hash = tools_hash
        prev.model = model
        prev.message_count = msg_count
        prev.last_cache_read_tokens = cache_read
        prev.last_update_time = now
        prev.call_count += 1

        # ── Report ──
        # Only warn when the API confirms a cache break (token drop) OR
        # when client-side changes are detected that WOULD break the cache.
        if client_changes and api_break:
            # Confirmed cache break with known cause
            logger.warning(
                '[CacheBreak] conv=%s call=%d CONFIRMED cache break: %s. '
                'cache_read: %d → %d tokens',
                conv_id[:8], prev.call_count,
                ', '.join(f'{k}={v}' for k, v in client_changes.items()),
                prev_cache_read, cache_read,
            )
            return client_changes
        elif api_break and not client_changes:
            # Cache tokens dropped but we can't explain why — likely
            # server-side TTL expiry or routing change
            import time as _time
            elapsed = now - prev.last_update_time if prev.last_update_time else 0
            if elapsed > 300:  # >5min gap
                reason = 'possible TTL expiry (>5min gap, prompt unchanged)'
            else:
                reason = 'likely server-side (prompt unchanged, <5min gap)'
            logger.info(
                '[CacheTrack] conv=%s call=%d cache_read dropped: %d → %d (%s)',
                conv_id[:8], prev.call_count,
                prev_cache_read, cache_read, reason,
            )
            return {'server_side': reason}
        elif client_changes and not api_break:
            # Client-side changes detected but cache wasn't broken (or no
            # cache stats available) — log at debug level only
            logger.debug(
                '[CacheTrack] conv=%s call=%d client changes: %s '
                '(cache_read: %d → %d, no confirmed break)',
                conv_id[:8], prev.call_count,
                ', '.join(f'{k}={v}' for k, v in client_changes.items()),
                prev_cache_read, cache_read,
            )

    return None


def notify_compaction(conv_id: str) -> None:
    """Notify that compaction occurred — the next cache_read drop is expected.

    Call this after micro-compact or smart_summary_compact modifies messages
    so that detect_cache_break doesn't false-positive on the resulting
    cache_read token drop.

    Inspired by Claude Code's notifyCompaction() which resets the baseline.
    """
    if not conv_id:
        return
    with _cache_lock:
        state = _cache_states.get(conv_id)
        if state:
            state.compaction_pending = True


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache-aware microcompact
# ═══════════════════════════════════════════════════════════════════════════════

def get_cache_prefix_count(conv_id: str) -> int:
    """Get the number of messages in the cache prefix for this conversation.

    Microcompact should skip editing messages[0:N] where N is this count,
    to keep cached content byte-identical for automatic prefix caching
    providers (OpenAI, Qwen, etc.).

    Returns the message count from the previous call if cache was active.
    For Anthropic (explicit breakpoints), this is less critical since
    add_cache_breakpoints places markers at the conversation tail.
    """
    with _cache_lock:
        state = _cache_states.get(conv_id)
        if state and state.last_cache_read_tokens > 1000:
            # Cache was active — protect the prefix
            # Use message_count - 2 (keep last 2 messages editable)
            return max(0, state.message_count - 2)
    return 0
