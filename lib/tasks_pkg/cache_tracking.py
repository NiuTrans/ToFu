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
  3. **Concurrent conversation tracking**: counts active conversations on
     the same model (for diagnostics only — A/B tested 2026-04-10: cache
     contention between different conversations does NOT exist).
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

from lib.log import audit_log, get_logger
from lib.tasks_pkg.wire_fingerprint import diff_canonical

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
        'history_rewrite_pending',
        # v2: detailed diagnostics
        'per_tool_hashes',
        'prefix_content_hash',
        'prefix_content_count',
        'prefix_field_hashes',
        # Authoritative post-translation wire fingerprint (see wire_fingerprint.py)
        'wire_fp', 'wire_static',
        'total_cache_read', 'total_cache_write',
        'total_breaks', 'total_input_tokens',
        'first_call_time',
        'pending_l2_roi',
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
        # Set by notify_history_rewrite() when a backend reconcile /
        # committed-dict projection edited committed messages. Unlike
        # compaction_pending it feeds NO break gate and does NOT skip the wire
        # diff — it only NAMES the cause (see detect_cache_break).
        self.history_rewrite_pending: bool = False
        # Phase-C L2 ROI: the 'saved' half of one force-summary event, stashed
        # at compaction time and completed with the FOLLOWING round's re-billed
        # cache_write in detect_cache_break. None when no L2 event is pending.
        self.pending_l2_roi: dict | None = None
        # v2 fields
        self.per_tool_hashes: dict[str, str] = {}  # tool_name → hash
        self.prefix_content_hash: str = ''
        self.prefix_content_count: int = 0
        # Per-message, per-field prefix hashes (precise culprit attribution).
        self.prefix_field_hashes: list[dict] = []
        # Authoritative post-translation wire fingerprint from the PREVIOUS
        # round (list of per-msg canonical entries) + the static-floor hash.
        # When present, these are the ground truth for prefix-mutation
        # attribution — they reflect the actual bytes sent, not a client-side
        # reconstruction. See lib/tasks_pkg/wire_fingerprint.py.
        self.wire_fp: list | None = None
        self.wire_static: str = ''
        self.total_cache_read: int = 0
        self.total_cache_write: int = 0
        self.total_breaks: int = 0
        self.total_input_tokens: int = 0
        self.first_call_time: float = 0.0


_cache_states: dict[tuple, CacheState] = {}
"""Cache state keyed by ``(conv_id, thread_id)`` — see ``_state_key``."""

_cache_lock = threading.Lock()


def _state_key(conv_id: str) -> tuple:
    """Key ``_cache_states`` per ``(conv_id, thread)``.

    N concurrent agent loops running under ONE conversation (swarm / flow /
    orchestration fan-out) previously shared a single ``conv_id``-keyed
    ``CacheState`` and clobbered each other's prefix baseline every round —
    the root cause of the incoherent ``PREFIX MUTATION DETECTED`` spam
    (call-counts / message-lengths jumping between threads) and the cache
    cost misattribution. A single task runs its whole round loop on one
    worker thread, so the thread id is a stable per-agent discriminator
    across rounds while distinct concurrent agents get distinct threads →
    distinct, non-colliding state. All same-thread callers
    (``detect_cache_break`` post-round, ``notify_compaction`` /
    ``get_cache_prefix_count`` in the pipeline) resolve to the same entry.
    """
    return (conv_id, threading.get_ident())


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
        except (TypeError, ValueError) as _e_audit:
            logger.debug('[cache_tracking] _hash_tools_per_tool caught %s: %s', type(_e_audit).__name__, _e_audit)
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

    ★ Covers the fields that ACTUALLY land on the wire and therefore affect
    the Anthropic prefix-byte match — not just ``content`` text. A turn's
    ``tool_calls`` (name + arguments + id), ``reasoning_content``,
    ``reasoning_details`` and ``thinking_signature`` are all serialized into
    the request body by ``build_body``; a per-round change in any of them is a
    real cache miss. The earlier text-only hash was BLIND to those, so a
    tool_call / argument / signature mutation produced a real miss with NO
    ``PREFIX MUTATION DETECTED`` log line (it got mislabeled ``server_side``).
    Block ORDER is preserved by appending in sequence, so a reorder also
    changes the hash.
    """
    if prefix_count <= 0 or not messages:
        return ''
    parts = []
    for msg in messages[:prefix_count]:
        parts.append(msg.get('role', ''))
        content = msg.get('content', '')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    # text blocks → text; non-text (image/tool_result) → type
                    parts.append(block.get('text', '') or block.get('type', ''))
        elif isinstance(content, str):
            parts.append(content)
        # Tool calls: name + arguments + id, in order (wire-affecting).
        for tc in msg.get('tool_calls') or ():
            if isinstance(tc, dict):
                parts.append(tc.get('id', ''))
                fn = tc.get('function') or {}
                if isinstance(fn, dict):
                    parts.append(fn.get('name', ''))
                    parts.append(fn.get('arguments', ''))
        if msg.get('tool_call_id'):
            parts.append(str(msg.get('tool_call_id')))
        # Replayed signed-thinking blocks (Claude) are part of the body.
        if msg.get('reasoning_content'):
            parts.append(str(msg.get('reasoning_content')))
        if msg.get('thinking_signature'):
            parts.append(str(msg.get('thinking_signature')))
        rd = msg.get('reasoning_details')
        if rd:
            try:
                parts.append(json.dumps(rd, sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append(str(rd))
    return _md5(''.join(parts))


def _hash_prefix_fields(messages: list, prefix_count: int) -> list[dict]:
    """Per-message, per-field hashes of the cache prefix.

    Companion to ``_hash_prefix_content`` (which rolls the WHOLE prefix into
    one hash). This returns a list — one dict per message in
    ``messages[:prefix_count]`` — mapping each wire-affecting FIELD
    (``role`` / ``content`` / ``tool_calls`` / ``tool_call_id`` /
    ``reasoning_content`` / ``thinking_signature`` / ``reasoning_details``)
    to its individual hash. ``_diff_prefix_fields`` then names the EXACT
    ``(message_index, field)`` that changed between two rounds — the same
    way ``_diff_tool_hashes`` names the exact tool. This turns the old
    terminal "silent prefix byte change (guess)" into a concrete culprit.
    """
    if prefix_count <= 0 or not messages:
        return []
    out: list[dict] = []
    for msg in messages[:prefix_count]:
        fh: dict[str, str] = {'role': _md5(msg.get('role', ''))}
        content = msg.get('content', '')
        if isinstance(content, list):
            _cp = []
            for block in content:
                if isinstance(block, dict):
                    _cp.append(block.get('text', '') or block.get('type', ''))
            fh['content'] = _md5('\x1f'.join(_cp))
        elif isinstance(content, str):
            fh['content'] = _md5(content)
        tcs = msg.get('tool_calls') or ()
        if tcs:
            _tp = []
            for tc in tcs:
                if isinstance(tc, dict):
                    fn = tc.get('function') or {}
                    _tp.append(tc.get('id', ''))
                    if isinstance(fn, dict):
                        _tp.append(fn.get('name', ''))
                        _tp.append(fn.get('arguments', ''))
            fh['tool_calls'] = _md5('\x1f'.join(_tp))
        if msg.get('tool_call_id'):
            fh['tool_call_id'] = _md5(str(msg.get('tool_call_id')))
        if msg.get('reasoning_content'):
            fh['reasoning_content'] = _md5(str(msg.get('reasoning_content')))
        if msg.get('thinking_signature'):
            fh['thinking_signature'] = _md5(str(msg.get('thinking_signature')))
        rd = msg.get('reasoning_details')
        if rd:
            try:
                fh['reasoning_details'] = _md5(
                    json.dumps(rd, sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError) as e:
                logger.debug('[CacheTrack] reasoning_details not JSON-serialisable '
                             '(%s) — hashing str() form', e)
                fh['reasoning_details'] = _md5(str(rd))
        out.append(fh)
    return out


def _diff_prefix_fields(old: list, new: list, max_report: int = 6) -> list:
    """Name the exact ``msg[i].field`` entries that differ between two
    per-message field-hash lists (from ``_hash_prefix_fields``).

    Only the overlapping index range is compared field-by-field; a length
    change of the compared prefix is reported as a separate ``len A->B``
    token. Capped at ``max_report`` culprits so the cause string stays
    readable (an extra ``…`` marks truncation).
    """
    changes: list[str] = []
    n = min(len(old), len(new))
    for i in range(n):
        o = old[i] or {}
        nw = new[i] or {}
        for field in sorted(set(o) | set(nw)):
            if o.get(field) != nw.get(field):
                changes.append(f'msg[{i}].{field}')
                if len(changes) >= max_report:
                    changes.append('…')
                    return changes
    if len(old) != len(new):
        changes.append(f'len {len(old)}\u2192{len(new)}')
    return changes


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache break detection
# ═══════════════════════════════════════════════════════════════════════════════

# Minimum absolute token drop required to trigger a cache break warning.
# Small drops (e.g., a few thousand tokens) can happen due to normal
# variation and aren't worth alerting on.
_MIN_CACHE_MISS_TOKENS = 2000

# ── Single-source editable-tail bound ──
# The number of trailing messages a backend writer (turn-end tail rewrite,
# reconcile) may mutate WITHOUT busting the cache. Equivalently: the cache
# prefix treated as immutable is messages[0 : len - EDITABLE_TAIL_COUNT].
# ``get_cache_prefix_count`` (what the cache assumes stable) and the
# reconcile prefix guard (what a writer is allowed to touch) BOTH read this
# ONE number, so "immutable prefix" and "mutable suffix" can never silently
# diverge. The value is 2 because the Anthropic tail breakpoint + the moving
# str↔block wrap keep the last user+assistant pair in flux each round.
EDITABLE_TAIL_COUNT = 2

# Minimum fresh cache_write (with zero cache_read) required to flag a
# "cache written but never read back" miss. Set well above the tiny-prompt
# alternating-WRITE/HIT noise floor (system+tools < 4096 tokens) so we only
# alert when re-writing the prefix actually cost real money. The motivating
# case: 2 rounds, both ~279k cache_write, zero cache_read.
_MIN_NO_REUSE_TOKENS = 20000


def _classify_break(
    *, call_count: int, was_compaction: bool,
    prev_cache_read: int, cache_read: int,
    prev_cache_write: int, cache_write: int,
    prev_prefix_tokens: int,
) -> tuple[bool, bool, bool]:
    """Phase-2 break classification (pure arithmetic on API cache tokens).

    Returns ``(api_break, no_reuse, partial_no_reuse)`` — the three
    mutually-narrowing break predicates.  Extracted verbatim from
    ``detect_cache_break``'s Phase 2 so the (subtle, much-commented)
    thresholds live in one testable place; the caller still owns the lock,
    the state mutation, and the cause/logging/return shaping.

      * api_break       — cache_read DROPPED >5% from a prior high read, with
                          the absolute drop over the miss threshold.
      * no_reuse        — a large fresh write with ZERO read despite an
                          established prefix last round.
      * partial_no_reuse— a large write repeated round-over-round while the
                          read stayed pinned (body re-billed uncached).
    """
    api_break = (
        call_count > 0
        and prev_cache_read > _MIN_CACHE_MISS_TOKENS
        and cache_read < prev_cache_read * 0.95
        and (prev_cache_read - cache_read) >= _MIN_CACHE_MISS_TOKENS
        and not was_compaction
    )

    no_reuse = (
        call_count > 0
        and cache_read == 0
        and cache_write >= _MIN_NO_REUSE_TOKENS
        and prev_prefix_tokens >= _MIN_NO_REUSE_TOKENS
        and not was_compaction
    )

    partial_no_reuse = (
        call_count > 0
        and not was_compaction
        and not api_break
        and not no_reuse
        and cache_write >= _MIN_NO_REUSE_TOKENS
        and prev_cache_write >= _MIN_NO_REUSE_TOKENS
        and cache_read <= prev_cache_read * 1.05
    )

    return bool(api_break), bool(no_reuse), bool(partial_no_reuse)


def _resolve_break_cause(
    *, client_changes: dict, prefix_mutation_break: bool,
    elapsed: float, cache_read: int, prefix_mutated: bool,
    prefix_culprits: list | None = None,
    wire_proven_identical: bool = False,
    history_rewrite: bool = False,
) -> str:
    """Build the single most-specific human cause string for a confirmed break.

    Precedence: explicit client changes → prefix-byte mutation (with the EXACT
    changed ``key.field`` list when known) → TTL expiry → server-side miss.

    The server-side verdict is now GATED on evidence, not reached by
    elimination. ``wire_proven_identical`` is True only when the authoritative
    post-translation wire fingerprint (see ``wire_fingerprint.py``) showed the
    actual sent bytes were byte-for-byte identical to the previous round. Only
    then may we state the miss is server-side as a PROVEN fact. When no wire
    fingerprint was captured (non-Claude / capture failure) we fall back to the
    legacy, honestly-hedged "stochastic server OR silent byte change" wording —
    the elimination guess, explicitly marked unproven.
    """
    _culprits = ', '.join(prefix_culprits) if prefix_culprits else ''
    if client_changes:
        return ', '.join(f'{k}={v}' for k, v in client_changes.items())
    if prefix_mutation_break:
        _base = ('cached prefix bytes changed between turns '
                 '(non-idempotent history edit) — the whole body '
                 'was re-billed uncached')
        return f'{_base} [changed: {_culprits}]' if _culprits else _base
    # ── Backend history rewrite ──
    # A reconcile / committed-dict projection edited or deleted a committed
    # message. This is a KNOWN backend cause, not an L2 compaction and not a
    # server-side miss — name it so it is not laundered into either.
    if history_rewrite:
        _base = ('backend history rewrite (reconcile / committed-dict '
                 'projection) edited or deleted a cached message — the '
                 'prefix was re-billed uncached')
        return f'{_base} [changed: {_culprits}]' if _culprits else _base
    if elapsed > 300:
        return 'TTL expiry (>5min gap, prompt unchanged)'
    # ── PROVEN server-side miss ──
    # The wire fingerprint confirmed our sent bytes were IDENTICAL to last
    # round, so the miss cannot be client-caused. This is the ONLY path allowed
    # to assert "server-side" as fact.
    if wire_proven_identical:
        if cache_read > _MIN_CACHE_MISS_TOKENS:
            return ('server-side cache miss — PROVEN: the wire bytes were '
                    'byte-identical to the previous round (only the body past '
                    'the static prefix was not read back)')
        return ('server-side cache miss — PROVEN: the wire bytes were '
                'byte-identical to the previous round (whole prefix not reused)')
    # ── Wire fingerprint UNAVAILABLE → legacy elimination guess (unproven) ──
    if cache_read > _MIN_CACHE_MISS_TOKENS:
        return ('likely server-side cache miss (UNPROVEN — no wire '
                'fingerprint; body re-billed, static prefix still cached)')
    if not prefix_mutated:
        return ('prefix not reused — likely server-side miss or TTL expiry '
                '(UNPROVEN — no wire fingerprint)')
    # Prefix bytes DID change but the write was below the surfacing floor.
    # We still know exactly which field moved — name it instead of guessing.
    if _culprits:
        return ('prefix bytes changed between turns '
                f'[changed: {_culprits}] — likely cause of the miss')
    return ('prefix not reused — likely server-side miss, TTL expiry, '
            'or a silent prefix byte change (UNPROVEN — no wire fingerprint)')


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

    _key = _state_key(conv_id)
    with _cache_lock:
        prev = _cache_states.get(_key)
        if prev is None:
            prev = CacheState()
            _cache_states[_key] = prev

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
        _new_prefix_count = max(0, msg_count - EDITABLE_TAIL_COUNT)
        _prev_prefix_hash = _hash_prefix_content(messages, _prev_prefix_count)
        prefix_hash = _hash_prefix_content(messages, _new_prefix_count)
        # Per-field hashes of the SAME (prev) range — lets us name the exact
        # message+field that changed, not just THAT the prefix changed.
        _cur_field_hashes_prevrange = _hash_prefix_fields(
            messages, _prev_prefix_count)
        prefix_field_hashes = _hash_prefix_fields(messages, _new_prefix_count)

        # Capture the history-rewrite signal (set by notify_history_rewrite
        # when the backend reconcile / committed-dict projection edited
        # committed messages). Read here for LABELING only; unlike compaction
        # it feeds NO break gate and does NOT skip the authoritative wire diff
        # below, so it can NEVER flip _wire_proven_identical to True. Cleared
        # in Phase 2 alongside compaction_pending.
        _was_history_rewrite = prev.history_rewrite_pending

        client_changes = {}
        _prefix_mutated = False
        _prefix_culprits: list = []
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
            # Message count going DOWN indicates compaction/truncation OR a
            # backend history rewrite (reconcile / committed-dict projection).
            # Distinguish them so a reconcile deletion is not mislabeled as an
            # L2 compaction it never was.
            if msg_count < prev.message_count:
                _lbl = 'history rewrite' if _was_history_rewrite else 'compacted'
                client_changes['message_count'] = (
                    f'{prev.message_count} → {msg_count} ({_lbl})')

            # ★ Diagnostic: prefix content mutation detection
            # Compare hash of the SAME range (prev prefix count) to detect
            # if existing messages were silently mutated in-place.
            if (_prev_prefix_hash
                    and prev.prefix_content_hash
                    and _prev_prefix_hash != prev.prefix_content_hash
                    and not prev.compaction_pending):
                _prefix_mutated = True
                _prefix_culprits = _diff_prefix_fields(
                    prev.prefix_field_hashes, _cur_field_hashes_prevrange)
                # The ANONYMOUS leading-indicator warning ("...without
                # compaction") is only useful when the mutation cause is
                # UNKNOWN. A history-rewrite signal (e.g. the per-turn
                # user-profile / detail splice via notify_history_rewrite)
                # NAMES the cause, so suppress the anonymous alarm here — but
                # keep _prefix_mutated=True so the CONFIRMED, NAMED break still
                # fires below and the re-bill is attributed (unlike
                # compaction_pending, which blanket-suppresses detection).
                if not _was_history_rewrite:
                    logger.warning(
                        '[CacheTrack] conv=%s call=%d ⚠ PREFIX MUTATION DETECTED: '
                        'messages[0:%d] content hash changed without compaction. '
                        'This will cause a cache miss. changed=[%s] '
                        'prev_hash=%s new_hash=%s',
                        conv_id[:8], prev.call_count + 1, _prev_prefix_count,
                        ', '.join(_prefix_culprits) or '?',
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
        # is expected — don't flag it as a break. Capture the flag BEFORE
        # resetting it; the break/no-reuse guards below must see the value
        # this round had, not the already-cleared False.
        _was_compaction = prev.compaction_pending
        if prev.compaction_pending:
            prev.compaction_pending = False
            if cache_read < prev_cache_read:
                logger.debug(
                    '[CacheTrack] conv=%s Expected cache drop after compaction: '
                    '%d → %d tokens',
                    conv_id[:8], prev_cache_read, cache_read)
        # Clear the history-rewrite signal (captured above). Deliberately does
        # NOT gate the wire diff or any break classifier — a backend edit that
        # changed prefix bytes MUST still be caught and named, not silenced.
        if prev.history_rewrite_pending:
            prev.history_rewrite_pending = False

        # ── Phase-C: complete a pending L2 ROI record ──
        # An L2 force-summary event stashed its 'saved' half via
        # record_l2_compaction. THIS round is the one whose prompt was rebuilt
        # from the summarized prefix, so its cache_write is the tokens re-billed
        # BECAUSE the summary busted the prefix. Pair the two halves and emit
        # ONE structured metric with both sides populated, then clear it.
        if prev.pending_l2_roi is not None:
            _roi = prev.pending_l2_roi
            prev.pending_l2_roi = None
            # THIS round's prompt was rebuilt from the summarized prefix, so its
            # cache_write is the OBSERVED re-billed half. outcome='paired'.
            _emit_l2_roi(conv_id, _roi, cache_write_rebilled=int(cache_write),
                         cache_read_next=int(cache_read), now=now)

        # ── Authoritative wire-fingerprint prefix diff ──
        # `usage['_wire_fp']` is the post-translation, envelope-agnostic
        # fingerprint of the ACTUAL bytes sent this round (captured in
        # prepare_request — the only point after add_cache_breakpoints AND
        # openai_body_to_anthropic). Diffing it against the previous round's
        # stored fingerprint is GROUND TRUTH for prefix mutation, unlike the
        # `_hash_prefix_content` reconstruction above which is blind to the
        # build_body / breakpoint / anthropic-translation transforms. When
        # present it OVERRIDES the reconstruction's verdict:
        #   * identical  → our bytes did NOT change → any miss is PROVABLY
        #                  server-side (the "stochastic" label is now earned,
        #                  not reached by elimination).
        #   * differ     → names the exact msg.field WE mutated → client-caused.
        _cur_wire_fp = None
        _wire_static = ''
        if usage:
            _cur_wire_fp = usage.get('_wire_fp')
            _wire_static = usage.get('_wire_static') or ''
        _wire_available = _cur_wire_fp is not None
        _wire_prefix_changed = False
        _wire_culprits: list = []
        if (_wire_available and prev.call_count > 0
                and prev.wire_fp is not None and not _was_compaction):
            # Compare only the region that existed last round (its full length);
            # this round appends new tail messages we don't diff against.
            _shared = len(prev.wire_fp)
            _wire_culprits = diff_canonical(
                prev.wire_fp[:_shared], (_cur_wire_fp or [])[:_shared])
            _wire_prefix_changed = bool(_wire_culprits)
            if _wire_prefix_changed:
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ WIRE PREFIX CHANGED: the '
                    'ACTUAL sent bytes differ from last round — client-caused '
                    'miss. changed=[%s]',
                    conv_id[:8], prev.call_count + 1,
                    ', '.join(_wire_culprits[:8]) or '?')

        # ── Phase-2 break classification (pure; see _classify_break) ──
        #   api_break        — cache_read dropped from a prior high read.
        #   no_reuse         — large fresh write, zero read, established prefix.
        #   partial_no_reuse — large write repeated while read stayed pinned
        #                      (conversation body re-billed uncached).
        _prev_prefix_tokens = (prev.last_cache_read_tokens
                               + prev.last_cache_write_tokens)
        prev_cache_write = prev.last_cache_write_tokens
        api_break, no_reuse, partial_no_reuse = _classify_break(
            call_count=prev.call_count, was_compaction=_was_compaction,
            prev_cache_read=prev_cache_read, cache_read=cache_read,
            prev_cache_write=prev_cache_write, cache_write=cache_write,
            prev_prefix_tokens=_prev_prefix_tokens,
        )

        # ── Update state (AFTER elapsed computation) ──
        prev.system_hash = sys_hash
        prev.tools_hash = tools_hash
        prev.per_tool_hashes = per_tool_hashes
        prev.prefix_content_hash = prefix_hash
        prev.prefix_content_count = _new_prefix_count
        prev.prefix_field_hashes = prefix_field_hashes
        if _wire_available:
            prev.wire_fp = _cur_wire_fp
            prev.wire_static = _wire_static
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

        # ★ When the authoritative wire fingerprint is available it REPLACES
        #   the client-side reconstruction as the prefix-mutation signal — it
        #   reflects the real sent bytes, so it neither misses a transform the
        #   reconstruction is blind to (downscale re-encode, anthropic re-dump)
        #   nor cries wolf on a benign wrapping the reconstruction over-counts.
        #   The reconstruction (_prefix_mutated) is used ONLY as a fallback when
        #   no wire fingerprint was captured (non-Claude / capture failure).
        if _wire_available:
            _prefix_mutated = _wire_prefix_changed
            _prefix_culprits = _wire_culprits
        _wire_proven_identical = _wire_available and not _wire_prefix_changed

        # ★ A silent prefix-byte mutation only counts as a CONFIRMED, surfaced
        #   break when it actually cost money this round (a real cache_write).
        #   On its own the hash change is a leading indicator; pairing it with
        #   a non-trivial write avoids crying wolf on rounds where the mutated
        #   prefix happened to still read back.
        prefix_mutation_break = (
            _prefix_mutated
            and not _was_compaction
            and cache_write >= _MIN_CACHE_MISS_TOKENS
        )

        if api_break or no_reuse or partial_no_reuse or prefix_mutation_break:
            prev.total_breaks += 1

        # ── Report ──
        # A cache break is "confirmed" when the API shows: a DROP from a prior
        # high read (api_break), a large fresh write with zero read despite an
        # established prefix (no_reuse), OR a large write repeated round-over-
        # round while the read stays pinned (partial_no_reuse). All three mean
        # we paid to rebuild (part of) the cache instead of reading it back.
        if api_break or no_reuse or partial_no_reuse or prefix_mutation_break:
            # Build the most specific cause we can (pure; see _resolve_break_cause).
            #
            # NOTE: "cache contention" between different conversations is NOT a
            # real phenomenon. A/B tested 2026-04-10: per-round cache_read is
            # identical between solo and interleaved modes (±0.0%). Anthropic
            # cache is keyed on exact prefix bytes — different conversations
            # have different keys and CANNOT evict each other.
            cause_str = _resolve_break_cause(
                client_changes=client_changes,
                prefix_mutation_break=prefix_mutation_break,
                elapsed=elapsed, cache_read=cache_read,
                prefix_mutated=_prefix_mutated,
                prefix_culprits=_prefix_culprits,
                wire_proven_identical=_wire_proven_identical,
                history_rewrite=_was_history_rewrite,
            )

            # ★ Prefix mutation is the most ACTIONABLE and most CERTAIN cause —
            #   it means our own code rewrote bytes inside the cached prefix,
            #   which GUARANTEES a miss regardless of any concurrent read drop.
            #   So it must win over `api_break` too: a round that both mutated
            #   the prefix AND shows a cache_read drop was previously falling
            #   through to the generic `server_side` "breakpoint advancement"
            #   label, hiding the real, fixable culprit (that exact mislabel is
            #   what the cost popover showed on memory-CRUD turns — the system
            #   prefix changed yet it read "服务端缓存失效（缓存断点前移…）").
            #   We still defer ONLY to `client_changes` (system/tools/model),
            #   which is a concrete, differently-named cause the popover labels
            #   on its own. Surfaced under the existing `prefix_mutation` key.
            if prefix_mutation_break and not client_changes:
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ PREFIX MUTATION BREAK: '
                    'cache_write=%d cache_read=%d (prev read=%d, gap=%.1fs) — '
                    'cached prefix bytes changed between turns. Cause: %s',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    prev_cache_read, elapsed, cause_str,
                )
                return {'prefix_mutation': cause_str}

            if no_reuse and not api_break:
                # The expensive, previously-undetected pattern: wrote a fresh
                # large prefix and read nothing back. WARN so each occurrence
                # is greppable in error.log for debugging.
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ NO CACHE REUSE: '
                    'cache_write=%d cache_read=0 (prev prefix=%d tokens, '
                    'gap=%.1fs). Cause: %s',
                    conv_id[:8], prev.call_count, cache_write,
                    _prev_prefix_tokens, elapsed, cause_str,
                )
                if client_changes:
                    return client_changes
                return {'no_cache_reuse': cause_str}

            if partial_no_reuse and not api_break and not no_reuse:
                # Big write repeated while cache_read stayed pinned at the
                # static prefix — the conversation body is being re-billed
                # uncached every round. WARN so each occurrence is greppable.
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ PREFIX RE-WRITTEN: '
                    'cache_write=%d cache_read=%d (prev write=%d read=%d, '
                    'gap=%.1fs) — read pinned, body re-billed uncached. '
                    'Cause: %s',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    prev_cache_write, prev_cache_read, elapsed, cause_str,
                )
                if client_changes:
                    return client_changes
                return {'no_cache_reuse': cause_str}

            # api_break path: cache_read dropped from a prior high value.
            if client_changes:
                logger.info(
                    '[CacheBreak] conv=%s call=%d CONFIRMED cache break: %s. '
                    'cache_read: %d → %d tokens (gap=%.1fs)',
                    conv_id[:8], prev.call_count, cause_str,
                    prev_cache_read, cache_read, elapsed,
                )
                return client_changes
            logger.info(
                '[CacheTrack] conv=%s call=%d cache_read dropped: %d → %d '
                '(gap=%.1fs, %s)',
                conv_id[:8], prev.call_count,
                prev_cache_read, cache_read, elapsed, cause_str,
            )
            return {'server_side': cause_str}
        elif client_changes:
            # Client-side changes detected but cache wasn't broken (or no
            # cache stats available) — log at debug level only.
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
        state = _cache_states.get(_state_key(conv_id))
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
        state = _cache_states.get(_state_key(conv_id))
        if state:
            state.compaction_pending = True


def notify_history_rewrite(conv_id: str) -> None:
    """Signal that the backend REWROTE committed history this round.

    Call this after a backend reconcile (``reconcile_conversation_messages``)
    or a committed-dict projection edited/deleted messages in
    ``conversations.messages`` that were part of the cached prefix.

    Deliberately the OPPOSITE of ``notify_compaction``: it does NOT suppress
    break detection and does NOT skip the authoritative wire diff. Its ONLY
    effect is to let ``detect_cache_break`` NAME the cause as a backend history
    rewrite instead of mislabeling it ``(compacted)`` or laundering it into a
    false ``server-side — PROVEN`` verdict. The real cache cost is still
    detected, attributed to the exact changed ``key.field``, and surfaced.
    """
    if not conv_id:
        return
    with _cache_lock:
        state = _cache_states.get(_state_key(conv_id))
        if state:
            state.history_rewrite_pending = True


def _emit_l2_roi(conv_id: str, roi: dict, *,
                 cache_write_rebilled: int | None,
                 cache_read_next: int | None = None,
                 now: float | None = None) -> None:
    """Emit ONE ``l2_cache_roi`` audit metric for a stashed L2 event.

    Two outcomes, BOTH signal (only a silent drop is bias):
      * ``cache_write_rebilled`` is an int → OUTCOME 'paired': we observed the
        following round's re-billed write. ``net_tokens`` = dropped − re-billed.
      * ``cache_write_rebilled`` is None → OUTCOME 'no_following_round': the
        session ended (cleanup / task teardown) OR a second L2 event superseded
        this one before a round paired it. The re-billed half is UNOBSERVED;
        ``net_tokens`` is None so the retune analysis can exclude it from the
        net distribution while still counting the fire. This is what keeps the
        dataset unbiased against late/last-round L2 events.

    Pure emit — the caller owns clearing ``pending_l2_roi``. Best-effort:
    instrumentation must never raise into a cache/cleanup path.
    """
    try:
        _t = now if now is not None else time.time()
        _dropped = int(roi.get('tokens_dropped', 0))
        _read_lost = int(roi.get('cache_read_at_event', 0))
        _observed = cache_write_rebilled is not None
        _net = (_dropped - int(cache_write_rebilled)) if _observed else None
        audit_log(
            'l2_cache_roi',
            conv_id=conv_id[:12],
            outcome='paired' if _observed else 'no_following_round',
            tokens_dropped=_dropped,
            tokens_before=int(roi.get('tokens_before', 0)),
            tokens_after=int(roi.get('tokens_after', 0)),
            msgs_before=int(roi.get('msgs_before', 0)),
            msgs_after=int(roi.get('msgs_after', 0)),
            cache_read_busted=_read_lost,
            cache_write_rebilled=(int(cache_write_rebilled) if _observed else None),
            cache_read_next=(int(cache_read_next) if cache_read_next is not None else None),
            net_tokens=_net,
            gap_s=round(_t - float(roi.get('event_time', _t)), 2),
        )
        logger.info(
            '[CacheTrack] conv=%s L2 ROI (%s): dropped=%d tokens, '
            're-billed=%s + busted read=%d → net=%s',
            conv_id[:8], 'paired' if _observed else 'no_following_round',
            _dropped,
            (str(int(cache_write_rebilled)) if _observed else 'UNOBSERVED'),
            _read_lost, (str(_net) if _observed else 'n/a'))
    except Exception as _roi_e:
        logger.debug('[CacheTrack] L2 ROI emit failed: %s', _roi_e)


def record_l2_compaction(conv_id: str, *, tokens_before: int, tokens_after: int,
                         msgs_before: int, msgs_after: int) -> None:
    """Record the 'saved' half of ONE L2 (force-summary) compaction event.

    Phase-C instrumentation (measure, don't tune). The ROI of an L2 event has
    two halves separated in time:
      * SAVED   — ``tokens_before - tokens_after`` (prefix tokens the summary
                  dropped) + the ``cache_read`` that was in flight when it fired
                  (the cached prefix the bust discarded); captured HERE.
      * REBILLED — the ``cache_write`` on the FOLLOWING round (the fresh prefix
                  the summary forced to be re-written); completed in
                  ``detect_cache_break`` when that round's usage arrives.

    Stashes the saved half on ``CacheState.pending_l2_roi``; the next
    ``detect_cache_break`` pairs it with the re-billed half and emits ONE
    ``audit_log('l2_cache_roi', ...)`` with BOTH sides populated. No-op when no
    cache state exists yet (cold conv — nothing was cached to bust, so ROI is
    trivially the saved tokens with zero re-bill).
    """
    if not conv_id:
        return
    with _cache_lock:
        state = _cache_states.get(_state_key(conv_id))
        if state is None:
            return
        # A second L2 event in the same round-gap would clobber the first's
        # unpaired record → the first event's ROI is silently lost. Flush it
        # first (re-billed half unobserved) so it is counted, not dropped.
        if state.pending_l2_roi is not None:
            _emit_l2_roi(conv_id, state.pending_l2_roi, cache_write_rebilled=None)
            state.pending_l2_roi = None
        state.pending_l2_roi = {
            'tokens_dropped': max(0, int(tokens_before) - int(tokens_after)),
            'tokens_before': int(tokens_before),
            'tokens_after': int(tokens_after),
            'msgs_before': int(msgs_before),
            'msgs_after': int(msgs_after),
            # The cached prefix that was in flight and is now busted by the
            # summary — this read will NOT recur next round.
            'cache_read_at_event': int(state.last_cache_read_tokens),
            'event_time': time.time(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  Concurrent conversation tracking
# ═══════════════════════════════════════════════════════════════════════════════

def _count_active_on_model(model: str, exclude_conv: str = '') -> int:
    """Count conversations active on the same model within the last 60s.

    NOTE (2026-04-10): A/B tested — cache contention between different
    conversations does NOT exist on Anthropic. Per-round cache_read is
    identical between solo and interleaved modes. The cache is keyed on
    exact prefix bytes, so different conversations have different keys
    and cannot evict each other.

    This function is retained for diagnostics/logging only (e.g., to
    report how many conversations are active on the same model), but
    should NOT be used to explain cache misses.

    Args:
        model: Model name to check.
        exclude_conv: Conv ID to exclude (the current conversation).

    Returns:
        Number of other active conversations on the same model.
    """
    cutoff = time.time() - 60  # consider "active" if called within last 60s
    count = 0
    for key, state in _cache_states.items():
        cid = key[0]
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

def sort_tool_results(messages: list, conv_id: str = '') -> None:
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

    ★ CACHE-CRITICAL: reordering messages inside the prompt-cache PREFIX
    rewrites the cached prefix bytes and forces a full re-cache — the exact
    silent cache-killer this module otherwise hunts. So the sort is gated to
    indices at/after ``get_cache_prefix_count(conv_id)``: a run that begins
    inside the prefix is left untouched (it was already cached in some order;
    re-sorting it now can only HURT). Newly-appended tool results (the tail,
    which is what actually varies round-over-round) are still sorted. Runs
    that straddle the boundary are skipped entirely rather than partially
    sorted, which would itself mutate prefix bytes.

    Args:
        messages: The messages list (mutated in place).
        conv_id:  Conversation ID — used to look up the cache-prefix boundary.
            When empty (no cache tracked), behaves as before (sort everywhere).
    """
    if not messages or len(messages) < 2:
        return

    _prefix_count = 0
    if conv_id:
        try:
            _prefix_count = get_cache_prefix_count(conv_id)
        except Exception as e:
            logger.debug('[CacheTrack] sort_tool_results prefix lookup failed: %s', e)

    i = 0
    n = len(messages)
    while i < n:
        # Find start of a tool-result run
        if messages[i].get('role') == 'tool':
            run_start = i
            while i < n and messages[i].get('role') == 'tool':
                i += 1
            run_end = i
            # Only sort if there are 2+ consecutive tool results AND the whole
            # run lies OUTSIDE the cached prefix (run_start >= prefix_count).
            # A run that begins inside the prefix \u2014 or straddles the boundary
            # \u2014 is skipped: re-ordering already-cached bytes guarantees a miss.
            if run_end - run_start >= 2 and run_start >= _prefix_count:
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
        state = _cache_states.get(_state_key(conv_id))
        # ★ Gate on WRITE as well as READ. The previous round may have only
        #   WRITTEN the prefix (cache_read=0, large cache_write) — e.g. round 1
        #   of a fresh conversation. That prefix is fully cached and reusable
        #   next round, so it must be protected from micro-compact mutation.
        #   Gating on read alone left round-2 unprotected after a round-1
        #   write, letting L1 mutate the just-written prefix → guaranteed miss.
        if state and (state.last_cache_read_tokens > 1000
                      or state.last_cache_write_tokens > 1000):
            # Cache was active — protect the prefix. Keep the last
            # EDITABLE_TAIL_COUNT messages editable (single-sourced bound).
            return max(0, state.message_count - EDITABLE_TAIL_COUNT)
    return 0


def _release_multiroot_sticky(conv_id: str) -> None:
    """Release the tools-registry latches (multi-root + tool-schema) on evict.

    Imported lazily so this low-level module doesn't pull in the tools
    package at import time (and tolerates the symbol being absent). Both
    latches key on conv_id and share the cache-state lifecycle, so they're
    released together here.
    """
    if not conv_id:
        return
    try:
        from lib.tools import clear_multiroot_sticky, clear_tool_list_latch
        clear_multiroot_sticky(conv_id)
        clear_tool_list_latch(conv_id)
    except Exception as e:
        logger.debug('[CacheTrack] tools-registry latch release unavailable: %s', e)


def cleanup_cache_state(conv_id: str) -> None:
    """Remove cache state for a conversation that's no longer active.

    Call when a conversation is explicitly deleted or after extended
    inactivity to prevent unbounded memory growth.
    """
    with _cache_lock:
        # State is keyed per (conv_id, thread) — a conversation may have
        # several entries when its agent loops ran on different worker
        # threads (swarm / flow fan-out). Drop them all.
        _keys = [k for k in _cache_states if k[0] == conv_id]
        removed = None
        for k in _keys:
            removed = _cache_states.pop(k, None)
            # Flush any L2 ROI event that fired but never got a following round
            # to observe its re-bill — otherwise late/last-round L2 events (the
            # MOST likely ones, since context grows monotonically) are silently
            # dropped, biasing the retune dataset. Marked 'no_following_round'.
            if removed is not None and removed.pending_l2_roi is not None:
                _emit_l2_roi(conv_id, removed.pending_l2_roi,
                             cache_write_rebilled=None)
                removed.pending_l2_roi = None
        if removed:
            logger.debug('[CacheTrack] Cleaned up %d state(s) for conv=%s '
                         '(last calls=%d, total_breaks=%d)',
                         len(_keys), conv_id[:8], removed.call_count,
                         removed.total_breaks)
    _release_multiroot_sticky(conv_id)


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
        stale_keys = [
            key for key, state in _cache_states.items()
            if state.last_update_time < cutoff
        ]
        for key in stale_keys:
            _stale = _cache_states.pop(key, None)
            # Same anti-bias flush as cleanup_cache_state: a stale-evicted conv
            # whose last cache-relevant act was an L2 fire must still emit its
            # ROI (re-bill unobserved), not drop it.
            if _stale is not None and _stale.pending_l2_roi is not None:
                _emit_l2_roi(key[0], _stale.pending_l2_roi,
                             cache_write_rebilled=None)
                _stale.pending_l2_roi = None
            removed += 1
    for key in stale_keys:
        _release_multiroot_sticky(key[0])
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
        for key, state in _cache_states.items():
            cid = key[0]
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
