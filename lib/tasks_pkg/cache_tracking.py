# HOT_PATH — called every round in the orchestrator.
"""Prompt Cache Break Detection & Cache-Aware Microcompact.

Inspired by Claude Code's ``promptCacheBreakDetection.ts`` (727 lines).

Features:
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
  3. **Concurrent conversation tracking**: detects when multiple active
     conversations share the same model, which can cause mutual cache
     eviction on the server side.
  4. **Session-stable TTL latch**: latches the CACHE_EXTENDED_TTL decision
     once per task to prevent mid-session cache key changes from shifting
     the beta header.
  5. **Cache-aware tool result ordering**: sorts tool results by tool_call_id
     to ensure deterministic prefix for automatic prefix caching providers.

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
import time
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

    Extended in v2:
      - per_tool_hashes: per-tool hash for diffing which tool changed
      - prefix_content_hash: hash of messages in the cache prefix
        (only used for mutation detection, NOT for break detection)
      - session-level aggregate stats (total reads/writes/breaks)
    """
    __slots__ = (
        'system_hash', 'tools_hash', 'model',
        'message_count', 'last_cache_read_tokens',
        'last_cache_write_tokens',
        'last_update_time', 'call_count',
        'compaction_pending',
        # v2: detailed diagnostics
        'per_tool_hashes',
        'prefix_content_hash',
        'prefix_content_count',
        'total_cache_read', 'total_cache_write',
        'total_breaks', 'total_input_tokens',
        'first_call_time',
    )

    def __init__(self):
        self.system_hash: str = ''
        self.tools_hash: str = ''
        self.model: str = ''
        self.message_count: int = 0
        self.last_cache_read_tokens: int = 0
        self.last_cache_write_tokens: int = 0
        self.last_update_time: float = 0.0
        self.call_count: int = 0
        self.compaction_pending: bool = False
        # v2 fields
        self.per_tool_hashes: dict[str, str] = {}  # tool_name → hash
        self.prefix_content_hash: str = ''
        self.prefix_content_count: int = 0
        self.total_cache_read: int = 0
        self.total_cache_write: int = 0
        self.total_breaks: int = 0
        self.total_input_tokens: int = 0
        self.first_call_time: float = 0.0


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
    """Hash the tool definitions (aggregate)."""
    if not tools:
        return ''
    try:
        return _md5(json.dumps(tools, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        logger.debug('[CacheTracking] Tool definitions not JSON-serializable, using str: %s', e)
        return _md5(str(tools))


def _hash_tools_per_tool(tools: list | None) -> dict[str, str]:
    """Hash each tool individually for per-tool diff reporting.

    Returns dict of {tool_name: hash} so we can report WHICH tool(s)
    changed when a tools hash mismatch is detected.
    """
    if not tools:
        return {}
    result = {}
    for tool in tools:
        fn = tool.get('function', {})
        name = fn.get('name', 'unknown')
        try:
            h = _md5(json.dumps(tool, sort_keys=True, ensure_ascii=False))
        except (TypeError, ValueError):
            h = _md5(str(tool))
        result[name] = h
    return result


def _diff_tool_hashes(
    old_hashes: dict[str, str],
    new_hashes: dict[str, str],
) -> list[str]:
    """Return list of tool names that changed, were added, or removed."""
    changes = []
    all_names = set(old_hashes) | set(new_hashes)
    for name in sorted(all_names):
        old_h = old_hashes.get(name)
        new_h = new_hashes.get(name)
        if old_h is None:
            changes.append(f'+{name}')
        elif new_h is None:
            changes.append(f'-{name}')
        elif old_h != new_h:
            changes.append(f'~{name}')
    return changes


def _hash_prefix_content(messages: list, prefix_count: int) -> str:
    """Hash the content of messages in the cache prefix.

    This is NOT used for cache break detection (to avoid false positives
    from micro-compact). It's used for diagnostic mutation detection:
    if this hash changes between rounds without a compaction event,
    something is silently mutating messages in the cached prefix.
    """
    if prefix_count <= 0 or not messages:
        return ''
    parts = []
    for msg in messages[:prefix_count]:
        content = msg.get('content', '')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get('text', ''))
        elif isinstance(content, str):
            parts.append(content)
        # Also include role for structural changes
        parts.append(msg.get('role', ''))
    return _md5(''.join(parts))


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

        # Per-tool hash diffing for detailed diagnostics
        per_tool_hashes = _hash_tools_per_tool(tools)

        # Prefix content mutation detection (diagnostic only)
        # ★ FIX: Use the PREVIOUS call's prefix count for mutation comparison,
        #   then compute a NEW prefix hash for saving.
        #   Bug was: _prefix_count grew each round (prev.message_count - 2),
        #   so the hash covered MORE messages than prev.prefix_content_hash,
        #   causing false positives every round (942 in one log window!).
        #   Fix: compare hash(messages[0:prev_prefix]) against saved hash,
        #   then save hash(messages[0:new_prefix]) for next round.
        _prev_prefix_count = prev.prefix_content_count if prev.call_count > 0 else 0
        _new_prefix_count = max(0, msg_count - 2)
        _prev_prefix_hash = _hash_prefix_content(messages, _prev_prefix_count)
        prefix_hash = _hash_prefix_content(messages, _new_prefix_count)

        client_changes = {}
        if prev.call_count > 0:
            if sys_hash != prev.system_hash:
                client_changes['system_prompt'] = 'changed'
            if tools_hash != prev.tools_hash:
                # Identify exactly which tools changed
                tool_diffs = _diff_tool_hashes(
                    prev.per_tool_hashes, per_tool_hashes)
                if tool_diffs:
                    client_changes['tools'] = (
                        f'changed: [{", ".join(tool_diffs)}]')
                else:
                    client_changes['tools'] = 'changed (ordering or meta)'
            if model != prev.model:
                client_changes['model'] = f'{prev.model} → {model}'
            # Message count going DOWN indicates compaction/truncation
            if msg_count < prev.message_count:
                client_changes['message_count'] = (
                    f'{prev.message_count} → {msg_count} (compacted)')

            # ★ Diagnostic: prefix content mutation detection
            # Compare hash of the SAME range (prev prefix count) to detect
            # if existing messages were silently mutated in-place.
            if (_prev_prefix_hash
                    and prev.prefix_content_hash
                    and _prev_prefix_hash != prev.prefix_content_hash
                    and not prev.compaction_pending):
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ PREFIX MUTATION DETECTED: '
                    'messages[0:%d] content hash changed without compaction. '
                    'This will cause a cache miss. prev_hash=%s new_hash=%s',
                    conv_id[:8], prev.call_count + 1, _prev_prefix_count,
                    prev.prefix_content_hash[:8], _prev_prefix_hash[:8])

        # ── Phase 2: Check API-reported cache stats ──
        cache_read = 0
        cache_write = 0
        if usage:
            cache_read = (usage.get('cache_read_tokens')
                          or usage.get('cache_read_input_tokens')
                          or 0)
            cache_write = (usage.get('cache_write_tokens')
                           or usage.get('cache_creation_input_tokens')
                           or 0)

        prev_cache_read = prev.last_cache_read_tokens

        # ★ FIX: compute elapsed BEFORE updating state so TTL detection works.
        # Previously, elapsed was computed AFTER setting last_update_time = now,
        # which meant it was always 0, making the >5min TTL check dead code.
        elapsed = now - prev.last_update_time if prev.last_update_time else 0

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

        # ── Update state (AFTER elapsed computation) ──
        prev.system_hash = sys_hash
        prev.tools_hash = tools_hash
        prev.per_tool_hashes = per_tool_hashes
        prev.prefix_content_hash = prefix_hash
        prev.model = model
        prev.message_count = msg_count
        prev.last_cache_read_tokens = cache_read
        prev.last_cache_write_tokens = cache_write
        prev.last_update_time = now
        prev.call_count += 1
        if not prev.first_call_time:
            prev.first_call_time = now
        # Accumulate session-level stats
        prev.total_cache_read += cache_read
        prev.total_cache_write += cache_write
        prompt_tokens = 0
        if usage:
            prompt_tokens = (usage.get('prompt_tokens')
                             or usage.get('input_tokens') or 0)
        prev.total_input_tokens += prompt_tokens + cache_write + cache_read
        if api_break:
            prev.total_breaks += 1

        # ── Report ──
        # Only warn when the API confirms a cache break (token drop) OR
        # when client-side changes are detected that WOULD break the cache.
        if client_changes and api_break:
            # Confirmed cache break with known cause
            logger.warning(
                '[CacheBreak] conv=%s call=%d CONFIRMED cache break: %s. '
                'cache_read: %d → %d tokens (gap=%.1fs)',
                conv_id[:8], prev.call_count,
                ', '.join(f'{k}={v}' for k, v in client_changes.items()),
                prev_cache_read, cache_read, elapsed,
            )
            return client_changes
        elif api_break and not client_changes:
            # Cache tokens dropped but we can't explain why — likely
            # server-side TTL expiry or routing change
            if elapsed > 300:  # >5min gap
                reason = 'possible TTL expiry (>5min gap, prompt unchanged)'
            else:
                # Check for concurrent conversations that might cause eviction
                _concurrent = _count_active_on_model(model, exclude_conv=conv_id)
                if _concurrent > 0:
                    reason = (f'likely cache contention ({_concurrent} other '
                              f'active conv(s) on {model}, <5min gap)')
                else:
                    reason = 'likely server-side (prompt unchanged, <5min gap)'
            logger.info(
                '[CacheTrack] conv=%s call=%d cache_read dropped: %d → %d '
                '(gap=%.1fs, %s)',
                conv_id[:8], prev.call_count,
                prev_cache_read, cache_read, elapsed, reason,
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


def get_session_cache_stats(conv_id: str) -> dict[str, Any] | None:
    """Get aggregate session-level cache stats for a conversation.

    Returns a dict with cumulative cache read/write tokens, break count,
    overall hit percentage, and session duration. Returns None if no
    state exists for this conversation.

    Use this for end-of-task diagnostics to understand overall cache
    effectiveness across the entire conversation session.
    """
    with _cache_lock:
        state = _cache_states.get(conv_id)
        if not state or state.call_count == 0:
            return None
        total_input = state.total_input_tokens
        return {
            'calls': state.call_count,
            'total_cache_read': state.total_cache_read,
            'total_cache_write': state.total_cache_write,
            'total_input_tokens': total_input,
            'overall_hit_pct': round(
                state.total_cache_read / max(total_input, 1) * 100),
            'total_breaks': state.total_breaks,
            'session_duration_s': round(
                state.last_update_time - state.first_call_time, 1)
                if state.first_call_time else 0,
            'model': state.model,
        }


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
#  Concurrent conversation tracking
# ═══════════════════════════════════════════════════════════════════════════════

def _count_active_on_model(model: str, exclude_conv: str = '') -> int:
    """Count conversations active on the same model within the last 60s.

    When multiple conversations hit the same model simultaneously,
    their different prefixes can compete for server-side cache capacity,
    causing mutual evictions.  This is most relevant for Anthropic where
    cache is keyed on the exact prefix bytes.

    Args:
        model: Model name to check.
        exclude_conv: Conv ID to exclude (the current conversation).

    Returns:
        Number of other active conversations on the same model.
    """
    cutoff = time.time() - 60  # consider "active" if called within last 60s
    count = 0
    for cid, state in _cache_states.items():
        if cid == exclude_conv:
            continue
        if (state.model == model
                and state.last_update_time > cutoff
                and state.call_count > 0):
            count += 1
    return count


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-round cache stats logging
# ═══════════════════════════════════════════════════════════════════════════════

def log_round_cache_stats(
    conv_id: str,
    round_num: int,
    usage: dict | None,
    model: str,
    tid: str = '',
) -> None:
    """Log per-round cache stats at INFO level for visibility.

    Previously cache stats were only logged at DEBUG in stream_chat.
    This gives us production-visible per-round data for diagnosing
    cache behavior without enabling DEBUG logging.

    Args:
        conv_id: Conversation ID.
        round_num: Current round number (0-based).
        usage: API usage dict from the LLM response.
        model: Model name.
        tid: Task ID for log correlation.
    """
    if not usage:
        return

    cache_write = (usage.get('cache_write_tokens')
                   or usage.get('cache_creation_input_tokens')
                   or 0)
    cache_read = (usage.get('cache_read_tokens')
                  or usage.get('cache_read_input_tokens')
                  or 0)
    prompt_tokens = (usage.get('prompt_tokens')
                     or usage.get('input_tokens')
                     or 0)

    # Only log if there's meaningful cache activity
    if not cache_write and not cache_read:
        return

    total_input = prompt_tokens + cache_write + cache_read
    hit_pct = round(cache_read / max(total_input, 1) * 100)

    logger.info(
        '[CacheStats] %s conv=%s R%d model=%s '
        'input=%d cache_w=%d cache_r=%d hit=%d%%',
        tid[:8] if tid else '???',
        conv_id[:8] if conv_id else '???',
        round_num + 1, model,
        prompt_tokens, cache_write, cache_read, hit_pct,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Session-stable TTL latch
# ═══════════════════════════════════════════════════════════════════════════════

_ttl_latch: dict[str, bool] = {}
"""Per-task_id TTL latch. Once set, the TTL decision is fixed for the task."""

_ttl_latch_lock = threading.Lock()


def latch_extended_ttl(task_id: str) -> bool:
    """Latch the CACHE_EXTENDED_TTL decision for a task's lifetime.

    Inspired by Claude Code's session-stable TTL decision: once a task
    starts with extended TTL on/off, it stays that way for the entire
    session.  This prevents mid-session settings changes from shifting
    the beta header, which would change the cache key and evict everything.

    Args:
        task_id: The task ID to latch for.

    Returns:
        The latched TTL decision (True = use 1h for stable prefix).
    """
    with _ttl_latch_lock:
        if task_id in _ttl_latch:
            return _ttl_latch[task_id]

        import lib as _lib
        decision = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
        _ttl_latch[task_id] = decision
        return decision


def release_ttl_latch(task_id: str) -> None:
    """Release the TTL latch when a task completes.

    Call from orchestrator._finalize_and_emit_done to prevent memory leak.
    """
    with _ttl_latch_lock:
        _ttl_latch.pop(task_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache-aware tool result ordering
# ═══════════════════════════════════════════════════════════════════════════════

def sort_tool_results(messages: list) -> None:
    """Sort consecutive tool-result messages by tool_call_id for cache stability.

    When multiple tool results come back from parallel tool execution, their
    order in the messages array may vary between rounds if tools complete in
    different orders.  This causes the prefix to differ even though the
    content is identical, breaking automatic prefix caching (OpenAI/Qwen).

    For Anthropic explicit breakpoints, this is less critical since the
    breakpoints mark exact positions.  But it doesn't hurt and improves
    determinism.

    This function finds consecutive runs of tool-role messages and sorts
    them by tool_call_id.  It's called before build_body to ensure
    deterministic ordering.

    Args:
        messages: The messages list (mutated in place).
    """
    if not messages or len(messages) < 2:
        return

    i = 0
    n = len(messages)
    while i < n:
        # Find start of a tool-result run
        if messages[i].get('role') == 'tool':
            run_start = i
            while i < n and messages[i].get('role') == 'tool':
                i += 1
            run_end = i
            # Only sort if there are 2+ consecutive tool results
            if run_end - run_start >= 2:
                # Sort by tool_call_id for deterministic ordering
                tool_run = messages[run_start:run_end]
                tool_run.sort(key=lambda m: m.get('tool_call_id', ''))
                messages[run_start:run_end] = tool_run
        else:
            i += 1


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


def cleanup_cache_state(conv_id: str) -> None:
    """Remove cache state for a conversation that's no longer active.

    Call when a conversation is explicitly deleted or after extended
    inactivity to prevent unbounded memory growth.
    """
    with _cache_lock:
        removed = _cache_states.pop(conv_id, None)
        if removed:
            logger.debug('[CacheTrack] Cleaned up state for conv=%s '
                         '(calls=%d, total_breaks=%d)',
                         conv_id[:8], removed.call_count,
                         removed.total_breaks)


def cleanup_stale_cache_states(max_age_s: float = 3600) -> int:
    """Remove cache states for conversations inactive longer than max_age_s.

    Call periodically (e.g., every 10 minutes) to prevent unbounded
    memory growth from long-lived server processes.

    Args:
        max_age_s: Max seconds since last update before eviction.
                   Default 3600 (1 hour).

    Returns:
        Number of stale entries removed.
    """
    cutoff = time.time() - max_age_s
    removed = 0
    with _cache_lock:
        stale_ids = [
            cid for cid, state in _cache_states.items()
            if state.last_update_time < cutoff
        ]
        for cid in stale_ids:
            del _cache_states[cid]
            removed += 1
    if removed:
        logger.info('[CacheTrack] Cleaned up %d stale cache states '
                    '(older than %ds, %d remaining)',
                    removed, int(max_age_s), len(_cache_states))
    return removed


def get_cache_diagnostics() -> dict[str, Any]:
    """Return a diagnostic snapshot of all active cache states.

    Useful for admin endpoints, debugging, or periodic health checks.

    Returns:
        Dict with overall stats and per-conversation summaries.
    """
    now = time.time()
    with _cache_lock:
        convs = []
        total_breaks = 0
        total_reads = 0
        total_writes = 0
        for cid, state in _cache_states.items():
            age = now - state.last_update_time if state.last_update_time else 0
            convs.append({
                'conv_id': cid[:8],
                'model': state.model,
                'calls': state.call_count,
                'last_cache_read': state.last_cache_read_tokens,
                'last_cache_write': state.last_cache_write_tokens,
                'total_breaks': state.total_breaks,
                'age_s': round(age, 1),
                'compaction_pending': state.compaction_pending,
            })
            total_breaks += state.total_breaks
            total_reads += state.total_cache_read
            total_writes += state.total_cache_write
        return {
            'active_conversations': len(convs),
            'total_breaks': total_breaks,
            'total_cache_read_tokens': total_reads,
            'total_cache_write_tokens': total_writes,
            'ttl_latches_active': len(_ttl_latch),
            'conversations': sorted(
                convs, key=lambda c: c['age_s']),
        }
