"""Two-phase cache-break detection.

Owns the break-classification thresholds and the single-source
``EDITABLE_TAIL_COUNT`` immutable-prefix bound, the pure classifiers
(``_classify_break`` / ``_resolve_break_cause``), and the round-driving
``detect_cache_break`` which mutates the shared ``CacheState`` singleton.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.wire_fingerprint import diff_canonical, markers_regressed
from lib.tasks_pkg.cache_tracking._state import (
    CacheState,
    _cache_lock,
    _cache_states,
    _state_key,
)
from lib.tasks_pkg.cache_tracking._hashing import (
    _diff_prefix_fields,
    _diff_tool_hashes,
    _hash_prefix_content,
    _hash_prefix_fields,
    _hash_system_prompt,
    _hash_tools,
    _hash_tools_per_tool,
)
from lib.tasks_pkg.cache_tracking._roi import _emit_l2_roi

logger = get_logger(__name__)


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
    changed ``key.field`` list when known) → TTL expiry → upstream eviction.

    The eviction verdict is GATED on evidence, not reached by elimination.
    ``wire_proven_identical`` is True only when the authoritative
    post-translation wire fingerprint (see ``wire_fingerprint.py``) showed the
    actual sent bytes were byte-for-byte identical to the previous round.
    CRUCIALLY, byte-identity proves only that the miss is NOT a client-side
    prefix change THIS ROUND — it says nothing about the cause. A byte-identical
    prefix that is not read back was not reused upstream; that is an ordinary
    upstream cache miss (a per-request gateway miss or a TTL boundary). The
    gateway forwards requests and does not evict, and there is no per-key cache
    capacity limit, so this is NOT shared-pool contention. So the verdict names
    an "upstream cache miss" WITHOUT asserting a single confident cause. NOTE the systemic picture: most cache
    misses in this system are CLIENT-side (already-cached bytes re-serialized
    differently across turns) — those take the ``<bytes>`` / ``prefix_mutation``
    branches above and are named there. This byte-identical branch is the
    comparatively RARE residual, so it must not over-claim.

    ⚠ The eviction verdict is DOUBLE-gated. ``wire_proven_identical`` requires
    BOTH the lossy canonical fingerprint AND the TRUE per-message serialized
    bytes (``wire_byte_prefix``) to match. The canonical fingerprint alone is
    NOT enough: it drops ``cache_control``, collapses ``str`` ↔ block, and
    skips ``reasoning_details`` — so a round can rebuild ``reasoning_details``,
    merge same-role turns, or switch protocol while canonical says "identical".
    When the raw bytes diverged, the caller has already appended a ``<bytes>``
    culprit (so this function takes the byte-divergence branch above), meaning
    we NEVER reach the eviction branch claiming "byte-identical" when the bytes
    were not. When no wire fingerprint was captured (non-Claude / capture
    failure) we fall back to the honestly-hedged "likely server/TTL OR silent
    byte change" wording — the elimination guess, explicitly marked unproven.
    """
    _culprits = ', '.join(prefix_culprits) if prefix_culprits else ''
    if client_changes:
        return ', '.join(f'{k}={v}' for k, v in client_changes.items())
    # ── Stable-block cache-TTL flipped between turns (the _task_id-drop bug) ──
    # The system/tools cache_control ttl value changed (e.g. "1h" ↔ absent)
    # while content bytes were identical. That value is part of the gateway
    # cache key, so the flip creates a DISTINCT entry → full prefix miss. It is
    # a CLIENT-side bug (a body rebuild dropped ``_task_id`` and fell back to the
    # live global CACHE_EXTENDED_TTL instead of the per-task latch), NOT a server
    # miss — name it precisely so it is never laundered into "server-side PROVEN".
    if prefix_culprits and '<ttl-flip>' in prefix_culprits:
        return ('cache TTL marker flipped between turns (the stable system/tools '
                'cache_control ttl changed, e.g. "1h" ↔ default — a body rebuild '
                'lost the per-task TTL latch and read the live global) — the '
                'whole prefix was re-billed under a new cache key')
    # ── Breakpoint LOST in translation (the tool_result/assistant-tail bug) ──
    # A cache_control marker the client placed vanished before the wire (or the
    # system/tools marker count changed) while the CONTENT bytes were identical.
    # This is a CLIENT-side bug (a dropped breakpoint), not a server miss — name
    # it precisely so it is never laundered into "server-side PROVEN".
    if prefix_culprits and '<breakpoint-lost>' in prefix_culprits:
        return ('cache breakpoint lost between turns (a cache_control marker '
                'the client placed did not survive to the wire — e.g. dropped '
                'in the tool_result translation) — the body past the last '
                'surviving marker was re-billed uncached')
    # ── TRUE-byte divergence with an IDENTICAL lossy-canonical fingerprint ──
    # canonical_messages matched (same tokenized content) yet the RAW serialized
    # bytes of a prefix message changed. The lossy canonicaliser is blind to
    # this class: reasoning_details rebuilt by build_body, consecutive same-role
    # turns merged, JSON field order changed, or an OpenAI↔Anthropic envelope /
    # endpoint switch. Any of these changes the exact bytes the gateway keys its
    # cache on → a full/partial miss. This is NOT proof of a client HISTORY
    # edit (the envelope-switch case is routing, not content), so name the
    # honest SET of causes rather than asserting a specific one — and never let
    # it be called "byte-identical eviction", because the bytes were NOT
    # identical.
    if prefix_culprits and any(str(c).startswith('<bytes>') or
                               str(c).startswith('byte-len')
                               for c in prefix_culprits):
        _named = ', '.join(c for c in prefix_culprits
                           if str(c).startswith('<bytes>'))
        _suffix = f' [changed: {_named}]' if _named else ''
        # If the HOISTED system/tools region is among the byte-diverged parts,
        # name it as the prime suspect — on the Anthropic path that region is
        # where the per-turn context (charter / board / peer-status /
        # relevant_memories) is injected fresh, and it rides on a LOSSY system
        # fingerprint, so it is the most likely place a real context-mechanism
        # corruption hides.
        _region_hit = any(c in ('<bytes>system', '<bytes>tools')
                          for c in prefix_culprits)
        if _region_hit:
            return ('hoisted system/tools bytes changed between turns while the '
                    'lossy system fingerprint matched — a canonical-invisible '
                    'change in the per-turn-injected system prefix (block '
                    'reorder, wrapping flip, re-serialization, or tool-param '
                    'key reorder) altered the exact bytes the gateway caches '
                    f'on → the cached prefix was re-billed uncached{_suffix}')
        return ('wire bytes changed between turns while the lossy content '
                'fingerprint matched — a canonical-invisible change '
                '(reasoning_details rebuild, consecutive same-role merge, JSON '
                'field reorder, or an OpenAI↔Anthropic envelope/endpoint '
                'switch) altered the exact bytes the gateway caches on → the '
                f'affected prefix was re-billed uncached{_suffix}')
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
    # ── Upstream cache miss — byte-identical, so NOT a client byte change THIS
    #    round ──
    # The wire fingerprint confirmed our sent bytes were IDENTICAL to last
    # round, so THIS round's miss is not a client-side prefix mutation. But
    # byte-identity does not tell us the cause: an identical prefix that comes
    # back with a dropped cache_read simply was not reused upstream. That is an
    # ordinary upstream cache miss (a per-request gateway miss or a TTL
    # boundary) — the gateway forwards and does not evict, and there is no
    # per-key cache capacity limit, so it is NOT shared-pool contention. We do
    # NOT assert it is or is not a server fault. IMPORTANT systemic note: the DOMINANT cache-miss cause
    # in this system is CLIENT-side (already-cached bytes re-serialized
    # differently across turns), and those are caught+named on the <bytes> /
    # prefix_mutation branches ABOVE — a stable client (byte-identical prefixes
    # every round) has been observed to drive misses to ~zero. This
    # byte-identical branch is therefore the comparatively RARE residual; word
    # it as a possibility, not a confident verdict, and never over-claim a
    # single mechanism.
    if wire_proven_identical:
        if cache_read > _MIN_CACHE_MISS_TOKENS:
            return ('prefix not read back though the wire bytes were '
                    'byte-identical to the previous round — so this round is '
                    'NOT a client-side prefix change. The cached prefix was not '
                    'reused upstream: an upstream cache miss (a per-request '
                    'gateway miss or a TTL boundary). Only the body past the '
                    'static prefix was not read back. (Most misses in this '
                    'system are instead client-side and are named per-field '
                    'above; this is not that class.)')
        return ('prefix not read back though the wire bytes were byte-identical '
                'to the previous round — so this round is NOT a client-side '
                'prefix change. The whole cached prefix was not reused '
                'upstream: an upstream cache miss (a per-request gateway miss '
                'or a TTL boundary). (Most misses in this system are instead '
                'client-side and are named per-field above; this is not that '
                'class.)')
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
        _cur_wire_system = None
        _cur_wire_markers = None
        _cur_wire_bytes = None
        _cur_wire_field_bytes = None
        _cur_wire_region = None
        if usage:
            _cur_wire_fp = usage.get('_wire_fp')
            _wire_static = usage.get('_wire_static') or ''
            _cur_wire_system = usage.get('_wire_system')
            _cur_wire_markers = usage.get('_wire_markers')
            _cur_wire_bytes = usage.get('_wire_bytes')
            _cur_wire_field_bytes = usage.get('_wire_field_bytes')
            _cur_wire_region = usage.get('_wire_region')
        _wire_available = _cur_wire_fp is not None
        _wire_prefix_changed = False
        _wire_culprits: list = []
        # Position of the first byte-diverged prefix message, and whether it
        # falls INSIDE the prior round's cached-prefix boundary. Hoisted here
        # (default: no position known) so the break classifier below can use
        # it to tell a genuine already-cached-message rewrite (a real
        # whole-prefix break) from a benign change confined to the fresh /
        # editable tail (read still hits ~fully). Set in the wire block.
        _first_changed_idx = -1
        _mutation_inside_prior_prefix = False
        if (_wire_available and prev.call_count > 0
                and prev.wire_fp is not None and not _was_compaction):
            # Compare only the region that existed last round (its full length);
            # this round appends new tail messages we don't diff against.
            _shared = len(prev.wire_fp)
            _wire_culprits = diff_canonical(
                prev.wire_fp[:_shared], (_cur_wire_fp or [])[:_shared])
            # ── System/tools diff (the hoisted-prefix blind spot) ──
            # On the Anthropic path system+tools live OUTSIDE body['messages'],
            # so the diff above never sees them. A per-turn system change
            # (cross-conv digest / charter / board) would otherwise leave
            # _wire_culprits empty → a false "server-side PROVEN" verdict on a
            # genuinely client-caused miss. Fold the system fingerprint diff in
            # so a system-block mutation is NAMED and blocks that false verdict.
            if _cur_wire_system is not None and prev.wire_system is not None:
                for _fld in ('system', 'tools'):
                    if prev.wire_system.get(_fld) != _cur_wire_system.get(_fld):
                        _wire_culprits.append(f'<hoisted>.{_fld}')
            # ── Marker-layout regression (the breakpoint-lost blind spot) ──
            # canonical_messages STRIPS cache_control, so a miss caused purely
            # by a breakpoint being LOST between rounds (byte-identical content,
            # e.g. the tail marker dropped in the anthropic tool_result
            # translation) leaves _wire_culprits empty → false "server-side
            # PROVEN". markers_regressed fires ONLY on a marker COUNT DROP /
            # system-tools marker change (NOT on the rolling tail's normal
            # forward move), so folding it in names the dropped breakpoint and
            # blocks the false verdict without crying wolf every round.
            if markers_regressed(prev.wire_markers, _cur_wire_markers):
                # Distinguish the two marker-regression sub-causes so the cost
                # popover names the ACTIONABLE one. A stable-block ttl VALUE
                # flip (1h ↔ absent) is the _task_id-drop latch bypass — a
                # different, more fixable cause than a dropped breakpoint.
                _prev_ttls = (prev.wire_markers or {}).get('stable_ttls', [])
                _cur_ttls = (_cur_wire_markers or {}).get('stable_ttls', [])
                if _prev_ttls != _cur_ttls:
                    _wire_culprits.append('<ttl-flip>')
                else:
                    _wire_culprits.append('<breakpoint-lost>')
            # ── TRUE-byte divergence (the lossy-canonical blind spot) ──
            # canonical_messages is deliberately lossy: it drops cache_control,
            # collapses str↔block, canonicalises tool-arg key order, and DOES
            # NOT hash reasoning_details. So "canonical identical" does NOT
            # prove the SERIALIZED bytes were identical. If canonical + system +
            # markers all say "unchanged" but the raw per-message bytes DID
            # diverge (reasoning_details rebuild, consecutive same-role merge,
            # field reorder, or an envelope/endpoint switch), the round is NOT
            # eligible for the "bytes were byte-identical" eviction verdict.
            # Fold a <bytes> culprit in so the verdict names the byte divergence
            # instead of laundering a real content/serialization change into an
            # upstream eviction. Only meaningful when canonical found NOTHING
            # (if canonical already named a culprit, that's the more specific
            # cause and we don't pile on).
            if (not _wire_culprits and _cur_wire_bytes is not None
                    and prev.wire_bytes is not None):
                from lib.tasks_pkg.wire_fingerprint import diff_byte_prefix
                _byte_shared = len(prev.wire_bytes)
                _byte_culprits = diff_byte_prefix(
                    prev.wire_bytes[:_byte_shared],
                    (_cur_wire_bytes or [])[:_byte_shared])
                if _byte_culprits:
                    _wire_culprits.extend(_byte_culprits)
                    # FIELD-GRANULAR attribution: name the EXACT top-level
                    # field that flipped (reasoning_details / tool_calls /
                    # content / __order__) instead of only the message, so the
                    # dominant canonical-invisible miss is logged as a PROVEN
                    # field, not a guessed category. Best-effort: falls back to
                    # the message-level culprits if the field capture is absent
                    # (non-Claude / pre-restart state / capture failure).
                    _field_culprits: list = []
                    if (_cur_wire_field_bytes is not None
                            and prev.wire_field_bytes is not None):
                        try:
                            from lib.tasks_pkg.wire_fingerprint import (
                                diff_byte_field_prefix)
                            _ff_shared = len(prev.wire_field_bytes)
                            _field_culprits = diff_byte_field_prefix(
                                prev.wire_field_bytes[:_ff_shared],
                                (_cur_wire_field_bytes or [])[:_ff_shared])
                        except Exception as _ffe:
                            logger.debug('[CacheTrack] field-byte diff failed: '
                                         '%s', _ffe)
                    logger.warning(
                        '[CacheTrack] conv=%s call=%d ⚠ WIRE BYTES DIVERGED '
                        'while canonical fingerprint matched — a '
                        'canonical-invisible change (reasoning_details rebuild '
                        '/ same-role merge / field reorder / protocol switch) '
                        'altered the real sent bytes. changed=[%s] field=[%s]',
                        conv_id[:8], prev.call_count + 1,
                        ', '.join(_byte_culprits[:8]) or '?',
                        ', '.join(_field_culprits[:8]) or '?')
            # ── TRUE-byte divergence in the HOISTED system/tools region ──
            # system_fingerprint (wire_system) is itself LOSSY (_text_of over
            # system blocks + sort_keys over tool params), so a system BLOCK
            # REORDER / wrapping flip / per-turn re-serialization / tool-param
            # KEY REORDER — the highest-probability suspect on the Anthropic
            # path, where charter/board/peer/relevant_memories are injected
            # fresh each turn — leaves the <hoisted> diff empty and would be
            # laundered into "eviction". wire_byte_region hashes the REAL bytes
            # of that region so the divergence is named and the eviction verdict
            # is refused. Checked even if messages already flagged, so a system
            # byte flip is always surfaced.
            if (_cur_wire_region is not None and prev.wire_region is not None):
                from lib.tasks_pkg.wire_fingerprint import diff_byte_region
                _region_culprits = diff_byte_region(
                    prev.wire_region, _cur_wire_region)
                if _region_culprits:
                    _wire_culprits.extend(_region_culprits)
                    logger.warning(
                        '[CacheTrack] conv=%s call=%d ⚠ HOISTED REGION BYTES '
                        'DIVERGED while the lossy system fingerprint matched — '
                        'a canonical-invisible change (system block reorder / '
                        'wrapping flip / per-turn re-serialization / tool-param '
                        'key reorder) altered the real cached-prefix bytes. '
                        'changed=[%s]',
                        conv_id[:8], prev.call_count + 1,
                        ', '.join(_region_culprits) or '?')
            _wire_prefix_changed = bool(_wire_culprits)
            if _wire_prefix_changed:
                # ── Position evidence (the "which part & was it already
                #    cached" ground truth) ──
                # Report the FIRST changed message index and whether it falls
                # inside the PRIOR round's cached-prefix boundary
                # (prev.message_count - EDITABLE_TAIL_COUNT). A changed index
                # BELOW that boundary means an ALREADY-CACHED message was
                # rewritten in place this round (the monotonic-growth break);
                # a changed index only in the editable tail / fresh region is a
                # different, benign class. This is what distinguishes an
                # EDITABLE_TAIL leak, a lost-state (thread-key) full-prefix
                # rewrite, and a cold-compaction-of-cached-message from each
                # other in the live log, without a rerun.
                try:
                    from lib.tasks_pkg.wire_fingerprint import (
                        first_changed_byte_index, first_changed_index)
                    _fci = first_changed_index(prev.wire_fp[:_shared],
                                               (_cur_wire_fp or [])[:_shared])
                    # The canonical index is BLIND to a <bytes>-only culprit
                    # (canonical says "identical", raw bytes differ), returning
                    # -1 → a meaningless inside_prior_cached_prefix=False for
                    # exactly the class where position matters most. Fall back
                    # to the true-byte index so a byte-only divergence gets an
                    # honest position.
                    if _fci < 0 and prev.wire_bytes is not None \
                            and _cur_wire_bytes is not None:
                        _byte_shared = len(prev.wire_bytes)
                        _fci = first_changed_byte_index(
                            prev.wire_bytes[:_byte_shared],
                            (_cur_wire_bytes or [])[:_byte_shared])
                except Exception as _fe:
                    logger.debug('[CacheTrack] first_changed_index failed: %s', _fe)
                    _fci = -2
                _prior_prefix_boundary = max(0, prev.message_count
                                             - EDITABLE_TAIL_COUNT)
                _inside_prior_prefix = (0 <= _fci < _prior_prefix_boundary)
                # Publish to the break classifier (see prefix_mutation_break).
                _first_changed_idx = _fci
                _mutation_inside_prior_prefix = _inside_prior_prefix
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ WIRE PREFIX CHANGED: the '
                    'ACTUAL sent bytes differ from last round — client-caused '
                    'miss. changed=[%s] first_changed_idx=%d '
                    'prior_prefix_boundary=%d prev_msg_count=%d cur_msg_count=%d '
                    'inside_prior_cached_prefix=%s',
                    conv_id[:8], prev.call_count + 1,
                    ', '.join(_wire_culprits[:8]) or '?',
                    _fci, _prior_prefix_boundary, prev.message_count, msg_count,
                    _inside_prior_prefix)

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
            prev.wire_system = _cur_wire_system
            prev.wire_markers = _cur_wire_markers
            prev.wire_bytes = _cur_wire_bytes
            prev.wire_field_bytes = _cur_wire_field_bytes
            prev.wire_region = _cur_wire_region
        prev.model = model
        prev.message_count = msg_count
        prev.last_cache_read_tokens = cache_read
        prev.last_cache_write_tokens = cache_write
        prev.last_update_time = now
        prev.call_count += 1
        # ── Advance the DURABLE prefix boundary (survives restart / replica
        #    switch). When THIS round confirms a warm cached prefix (read or
        #    write > 1000, mirroring get_cache_prefix_count's _boundary gate),
        #    the prefix [0, msg_count - EDITABLE_TAIL_COUNT) is a cached
        #    conversation fact. Persist it as a monotonic high-water mark so a
        #    future turn on a fresh thread/process/replica restores the guard
        #    floor instead of collapsing to 0 and rewriting the still-cached
        #    prefix. Best-effort, monotonic, DB-write only on genuine growth.
        if cache_read > 1000 or cache_write > 1000:
            _durable_boundary = max(0, msg_count - EDITABLE_TAIL_COUNT)
            if _durable_boundary > 0:
                try:
                    from lib.tasks_pkg.cache_tracking._persist import (
                        advance_persisted_boundary)
                    advance_persisted_boundary(conv_id, _durable_boundary)
                except Exception as _pe:
                    logger.debug('[CacheTrack] advance_persisted_boundary '
                                 'failed conv=%s: %s', conv_id[:8], _pe)
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
        #
        # ★ POSITION GATE (the (A)-vs-benign-tail discriminator). A byte change
        #   is a genuine WHOLE-PREFIX break (an already-cached message rewritten
        #   in place → the illegal freeze-guard leak) ONLY when it lands INSIDE
        #   the prior cached prefix, OR the read actually COLLAPSED this round
        #   (cache_read fell materially below the prior read — the floor-miss
        #   signature). A <bytes> change confined to the fresh / editable tail
        #   while cache_read still reads back ~fully (observed on mrnnfvs6:
        #   cache_w≈15–31k but cache_r stayed 206–273k / 88–99% hit) is a benign
        #   tail re-bill, NOT a whole-body PREFIX MUTATION BREAK — flagging it as
        #   one over-reports a miss that did not happen. When the wire fingerprint
        #   is UNAVAILABLE (_first_changed_idx stays -1, e.g. non-Claude / capture
        #   failure) we cannot place the mutation, so we keep the legacy
        #   write-threshold behaviour rather than silently under-reporting.
        _read_collapsed = (
            prev_cache_read > _MIN_CACHE_MISS_TOKENS
            and cache_read < prev_cache_read * 0.95
            and (prev_cache_read - cache_read) >= _MIN_CACHE_MISS_TOKENS
        )
        _position_known = _first_changed_idx >= 0
        _mutation_is_whole_prefix = (
            _mutation_inside_prior_prefix or _read_collapsed
            or not _position_known
        )
        prefix_mutation_break = (
            _prefix_mutated
            and not _was_compaction
            and cache_write >= _MIN_CACHE_MISS_TOKENS
            and _mutation_is_whole_prefix
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
