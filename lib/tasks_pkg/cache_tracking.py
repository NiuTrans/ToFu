# HOT_PATH — called every round in the orchestrator.
"""Prompt Cache Break Detection & Cache-Aware Microcompact.

Inspired by Claude Code's ``promptCacheBreakDetection.ts`` (727 lines).

Two features:
  1. **Cache break detection**: tracks what changed between turns to log
     cache misses and their causes — helps diagnose unexpected cost spikes.
  2. **Cache-aware microcompact**: when editing messages, skip those in the
     "cache prefix" (messages that were part of the last cache hit) to
     maintain byte-identical content for prompt cache stability.

Architectural limitation vs Claude Code:
  Claude Code computes per-field hashes (systemHash, toolsHash, etc.) and
  compares against API-reported ``cache_read_input_tokens``.  Our proxy layer
  doesn't always relay cache token counts, so we track hashes of the wire
  content and infer cache breaks from hash changes.  This is less precise
  but still useful for diagnostics and microcompact cache awareness.
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

    Stores hashes of system prompt, tools, and message prefix so we can
    detect what changed between turns.
    """
    __slots__ = (
        'system_hash', 'tools_hash', 'model', 'message_prefix_hash',
        'message_prefix_count', 'last_cache_read_tokens',
        'last_update_time', 'call_count',
    )

    def __init__(self):
        self.system_hash: str = ''
        self.tools_hash: str = ''
        self.model: str = ''
        self.message_prefix_hash: str = ''
        self.message_prefix_count: int = 0
        self.last_cache_read_tokens: int = 0
        self.last_update_time: float = 0.0
        self.call_count: int = 0


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


def _hash_message_prefix(messages: list, up_to: int) -> str:
    """Hash messages[0:up_to] for cache prefix comparison."""
    if up_to <= 0:
        return ''
    parts = []
    for msg in messages[:up_to]:
        role = msg.get('role', '')
        content = msg.get('content', '')
        if isinstance(content, list):
            content = ''.join(
                b.get('text', '') for b in content
                if isinstance(b, dict) and b.get('type') == 'text'
            )
        parts.append(f'{role}:{content}')
        for tc in msg.get('tool_calls', []):
            parts.append(tc.get('function', {}).get('name', ''))
            parts.append(tc.get('function', {}).get('arguments', ''))
    return _md5('||'.join(parts))


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache break detection
# ═══════════════════════════════════════════════════════════════════════════════

def detect_cache_break(
    conv_id: str,
    messages: list,
    tools: list | None,
    model: str,
    usage: dict | None = None,
) -> dict[str, Any] | None:
    """Compare current request state against previous to detect cache breaks.

    Returns a dict describing what changed, or None if no break detected.
    Logs warnings on significant cache breaks for cost diagnostics.
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

        # Compute current hashes
        sys_hash = _hash_system_prompt(messages)
        tools_hash = _hash_tools(tools)
        # Prefix = all messages except the last 2 (current user + potential assistant)
        prefix_count = max(0, len(messages) - 2)
        prefix_hash = _hash_message_prefix(messages, prefix_count)

        # Detect changes
        changes = {}
        if prev.call_count > 0:
            if sys_hash != prev.system_hash:
                changes['system_prompt'] = 'changed'
            if tools_hash != prev.tools_hash:
                changes['tools'] = 'changed'
            if model != prev.model:
                changes['model'] = f'{prev.model} → {model}'
            if prefix_hash != prev.message_prefix_hash:
                changes['message_prefix'] = f'{prev.message_prefix_count} → {prefix_count} msgs'

        # Check API-reported cache stats if available
        # ★ FIX: check BOTH OpenAI format (cache_read_tokens) and Anthropic
        #   native format (cache_read_input_tokens).  The Sankuai proxy
        #   returns the OpenAI format, so we were always reading 0.
        cache_read = 0
        if usage:
            cache_read = (usage.get('cache_read_tokens')
                          or usage.get('cache_read_input_tokens')
                          or 0)
            if (prev.last_cache_read_tokens > 2000
                    and cache_read < prev.last_cache_read_tokens * 0.5
                    and not changes):
                changes['cache_tokens_dropped'] = (
                    f'{prev.last_cache_read_tokens} → {cache_read}'
                )

        # ★ Capture previous value BEFORE updating state (for log message)
        prev_cache_read = prev.last_cache_read_tokens

        # Update state
        prev.system_hash = sys_hash
        prev.tools_hash = tools_hash
        prev.model = model
        prev.message_prefix_hash = prefix_hash
        prev.message_prefix_count = prefix_count
        prev.last_cache_read_tokens = cache_read
        prev.last_update_time = now
        prev.call_count += 1

        if changes:
            logger.warning(
                '[CacheBreak] conv=%s call=%d CACHE BREAK detected: %s. '
                'cache_read_tokens: %d → %d',
                conv_id[:8], prev.call_count,
                ', '.join(f'{k}={v}' for k, v in changes.items()),
                prev_cache_read, cache_read,
            )
            return changes

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache-aware microcompact
# ═══════════════════════════════════════════════════════════════════════════════

def get_cache_prefix_count(conv_id: str) -> int:
    """Get the number of messages in the cache prefix for this conversation.

    Microcompact should skip editing messages[0:N] where N is this count,
    to keep cached content byte-identical.
    """
    with _cache_lock:
        state = _cache_states.get(conv_id)
        if state and state.last_cache_read_tokens > 1000:
            # Cache was active — protect the prefix
            return state.message_prefix_count
    return 0

