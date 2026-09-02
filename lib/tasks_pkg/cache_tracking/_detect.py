"""Two-phase cache-break detection.

Owns the break-classification thresholds and the single-source
``EDITABLE_TAIL_COUNT`` immutable-prefix bound, the pure classifiers
(``_classify_break`` / ``_resolve_break_cause``), and the round-driving
``detect_cache_break`` which mutates the shared ``CacheState`` singleton.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from lib.cost import normalize_usage
from lib.log import get_logger
from lib.tasks_pkg.wire_fingerprint import (
    diff_canonical, markers_regressed, markers_ttl_flipped,
    mid_anchor_out_of_window,
)
from lib.tasks_pkg.cache_tracking._state import (
    COLD_ROUND_TOKEN_FLOOR,
    COLD_STREAK_GUARD_OPEN,
    CacheState,
    _cache_lock,
    _cache_states,
    _state_key,
    get_prev_turn_cache_read,
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


@dataclass(frozen=True)
class _WireEvidence:
    """Provider-bound evidence captured after final request translation."""

    fingerprint: Any = None
    static_prefix: str = ''
    system: Any = None
    markers: Any = None
    message_bytes: Any = None
    field_bytes: Any = None
    region: Any = None
    routing: Any = None


def _wire_evidence_from_usage(usage: dict | None) -> _WireEvidence:
    if not usage:
        return _WireEvidence()
    return _WireEvidence(
        fingerprint=usage.get('_wire_fp'),
        static_prefix=usage.get('_wire_static') or '',
        system=usage.get('_wire_system'),
        markers=usage.get('_wire_markers'),
        message_bytes=usage.get('_wire_bytes'),
        field_bytes=usage.get('_wire_field_bytes'),
        region=usage.get('_wire_region'),
        routing=usage.get('_wire_routing'),
    )


def _detect_namespace_switch(
    *,
    current_routing: Any,
    previous_state: CacheState,
    was_compaction: bool,
    conv_id: str,
) -> tuple[list[str], bool]:
    """Compare the provider cache namespace after body-byte comparison."""
    if (current_routing is None or previous_state.wire_routing is None
            or previous_state.call_count <= 0 or was_compaction):
        return [], False

    from lib.tasks_pkg.wire_fingerprint import diff_routing
    namespace_switch = diff_routing(
        previous_state.wire_routing, current_routing)
    namespace_verified_same = not namespace_switch
    if namespace_switch:
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ CACHE NAMESPACE SWITCH: the '
            'request routing changed between turns — a byte-identical prefix '
            'now lands on a DIFFERENT gateway cache namespace (client-caused '
            'cold miss, NOT server-side). changed=[%s] prev=%s cur=%s',
            conv_id[:8], previous_state.call_count + 1,
            ', '.join(namespace_switch), previous_state.wire_routing,
            current_routing,
        )
    return namespace_switch, namespace_verified_same


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
    namespace_switch: list | None = None,
    namespace_verified_same: bool = False,
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
    # ── Mid-anchor slipped past the ~20-block cache lookback window ──
    # The mid-history stepping-stone drifted FARTHER than Anthropic's ~20-block
    # lookback behind the rolling tail, so the tail could not extend the prior
    # cache entry and the whole prefix past the mid was re-written — on a
    # byte-identical body. This is a CLIENT-side breakpoint-LAYOUT miss (the
    # add_cache_breakpoints trail/step params), NOT a server miss — name it so
    # it is never laundered into "upstream cache miss".
    if prefix_culprits and '<mid-out-of-window>' in prefix_culprits:
        return ('mid-history cache anchor drifted past Anthropic\'s ~20-block '
                'cache lookback window behind the rolling tail — the tail could '
                'not extend the prior cache entry, so the whole prefix past the '
                'mid anchor was re-billed uncached even though the body bytes '
                'were identical. A client-side breakpoint-layout miss (the '
                'stepping-stone trail/step params), NOT a server-side or '
                'gateway fault.')
    # ── Provider-bound hoisted structure changed (canonical AND raw proof) ──
    # ``<hoisted>.tools`` / ``<hoisted>.system`` comes from the canonical
    # system_fingerprint and therefore proves the structure/content changed;
    # a co-occurring ``<bytes>tools`` / ``<bytes>system`` proves the exact
    # serialized cache-key bytes changed too.  Handle this BEFORE the
    # canonical-invisible byte branch below.  Calling this a lossy-fingerprint
    # match (the old generic wording) is factually wrong and hid a dynamic
    # execute_tools description mutation behind a server-side-looking verdict.
    _hoisted = [str(c) for c in (prefix_culprits or [])
                if str(c).startswith('<hoisted>.')]
    if _hoisted:
        _named = ', '.join(str(c) for c in prefix_culprits)
        return (
            'provider-bound system/tools structure changed between turns — '
            'the client changed cache-key bytes in the hoisted prefix, so the '
            'cached prefix was re-billed uncached'
            f' [changed: {_named}]')
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
    # ── Cache-NAMESPACE switch (byte-identical body, routing flipped) ──
    # The request BODY was byte-identical to last round, but the cache-namespace
    # routing changed — the upstream key, the anthropic-beta header (e.g.
    # extended-cache-ttl presence), or the endpoint. Anthropic caches per
    # (key + beta + endpoint), so the same prefix bytes hit a COLD namespace →
    # a guaranteed miss. This is a CLIENT-side cause (dispatch rebind on
    # cooldown/429, or a per-task TTL-latch flip), NOT a server/gateway fault —
    # name the exact attribute(s) that flipped so it is never laundered upstream.
    if namespace_switch:
        _ns_names = {'<ns>key': 'upstream API key', '<ns>beta': 'anthropic-beta '
                     'header (e.g. extended-cache-ttl)', '<ns>endpoint': 'endpoint'}
        _flipped = ', '.join(_ns_names.get(c, c) for c in namespace_switch)
        return ('same prefix bytes routed to a different cache namespace — the '
                f'{_flipped} changed between turns (a client-side dispatch '
                'rebind on cooldown/429, or a per-task TTL-latch flip), so the '
                'byte-identical prefix landed on a COLD gateway cache and was '
                're-billed uncached. NOT a server-side miss.')
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
        # When routing was ALSO verified identical this round, say so explicitly
        # — that upgrades the verdict from an elimination guess to an
        # evidence-grade statement: body bytes AND (key + beta + endpoint) all
        # matched last round, so the miss is genuinely upstream, not a client
        # cache-namespace switch.
        _ns_evidence = (' The routing was also identical (key + anthropic-beta '
                        '+ endpoint all match last round), so this is not a '
                        'client cache-namespace switch either.'
                        if namespace_verified_same else '')
        if cache_read > _MIN_CACHE_MISS_TOKENS:
            return ('prefix not read back though the wire bytes were '
                    'byte-identical to the previous round — so this round is '
                    'NOT a client-side prefix change. The cached prefix was not '
                    'reused upstream: an upstream cache miss (a per-request '
                    'gateway miss or a TTL boundary). Only the body past the '
                    'static prefix was not read back.' + _ns_evidence
                    + ' (Most misses in this '
                    'system are instead client-side and are named per-field '
                    'above; this is not that class.)')
        return ('prefix not read back though the wire bytes were byte-identical '
                'to the previous round — so this round is NOT a client-side '
                'prefix change. The whole cached prefix was not reused '
                'upstream: an upstream cache miss (a per-request gateway miss '
                'or a TTL boundary).' + _ns_evidence + ' (Most misses in this '
                'system are instead client-side and are named per-field above; '
                'this is not that class.)')
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


def _emit_round_record(conv_id, call_num, verdict, *, ns_switch, ttl_flip,
                       breakpoint_lost, body_identical, namespace_verified,
                       cache_read, cache_write, elapsed, culprits=None,
                       model='', wire_available=False, wire_changed=False,
                       read_collapsed=False):
    """Emit ONE machine-readable per-round cache-verdict record (ALWAYS, every
    round — not only on a break).

    THE ROOT FIX for "monitoring is insufficient": one ``[CacheRoundRecord]``
    INFO line per round carrying a JSON payload with the ``bucket`` (from the
    single-source ``classify_verdict``), the routing diff, the ttl-flip /
    breakpoint-lost flags, the cache tokens, and whether the body was
    byte-identical. After ONE clean deploy the real-traffic client-vs-upstream
    bucket count is a ``grep [CacheRoundRecord] | aggregate_round_records`` —
    no default-OFF probe, no manual replay, no restart-to-verify. Best-effort:
    a serialization failure never disturbs the detector's return value.
    """
    try:
        rec = {
            'conv': (conv_id or '')[:8],
            'call': call_num,
            'bucket': classify_verdict(verdict),
            'routing_diff': list(ns_switch or []),
            'ttl_flip': bool(ttl_flip),
            'breakpoint_lost': bool(breakpoint_lost),
            'body_identical': bool(body_identical),
            # ``body_identical=False`` used to conflate a proven mutation with
            # missing fingerprint capture.  These explicit evidence bits make
            # production records mechanically distinguish those cases and show
            # when a zero-cache-write provider was classified from read loss.
            'wire_available': bool(wire_available),
            'wire_changed': bool(wire_changed),
            'read_collapsed': bool(read_collapsed),
            'namespace_verified': bool(namespace_verified),
            'cache_read': int(cache_read or 0),
            'cache_write': int(cache_write or 0),
            'gap_s': round(float(elapsed or 0), 1),
        }
        # The model this round ran on. Without it EVERY cost aggregation over
        #   these records has to price the whole table at one rate, which is an
        #   approximation nobody can bound: the real fleet spans two orders of
        #   magnitude (Opus family 0.04525 vs gemini-3-flash 0.00109 CNY/1k
        #   cache-write), so a single-rate total forced a paragraph of caveat
        #   onto every outbound cost figure.
        #
        #   Recorded RAW — the wire/logical id exactly as handed to
        #   detect_cache_break. Deliberately NOT alias-normalised here: that is
        #   lib/llm_dispatch's job, and doing it at this seam would create a
        #   second alias table to drift against the first.
        #
        #   Historical rows will never have this field (records are stamped at
        #   write time), so every consumer MUST tolerate its absence and fall
        #   back rather than dropping the row.
        if model:
            rec['model'] = str(model)
        # The RAW wire-culprit tokens (e.g. '<ttl-flip>', '<mid-out-of-window>',
        # 'assistant.content', '<hoisted>.system') that drove _wire_prefix_changed
        # this round. Emitted so a post-fix live A/B can see the ACTUAL driver of
        # a break, not only the final bucket — disambiguating whether a
        # body_identical=False round was a genuine content mutation vs a
        # layout/ttl token. Best-effort: capped to keep the line small.
        if culprits:
            rec['culprits'] = [str(c) for c in list(culprits)[:8]]
        import json as _json
        logger.info('[CacheRoundRecord] %s', _json.dumps(rec, sort_keys=True))
    except Exception as _rre:
        logger.debug('[CacheTrack] round-record emit failed: %s', _rre)


# ── Verdict bucket taxonomy (SINGLE SOURCE — replay + live records share it) ──
BUCKET_NAMESPACE = 'cache_namespace_switch'
BUCKET_TURN_BOUNDARY = 'turn_boundary_rebill'
BUCKET_TTL_FLIP = 'ttl_flip'
# TTL EXPIRY is NOT waste and NOT a client change. ``_resolve_break_cause``
#   emits 'TTL expiry (>5min gap, prompt unchanged)' for any >300s gap, but
#   classify_verdict used to match only 'ttl marker flipped' / 'new cache key',
#   so every idle-expiry rebuild fell through to body_change ("the client
#   changed the bytes" — the exact mislabel this detector family exists to
#   prevent) or to the `other` catch-all. Measured on real traffic: 73 rounds /
#   1,516 CNY of ORDINARY cache rebuilds were being counted as recoverable
#   waste, which distorted where the money looked like it was going (removing
#   them moves the upstream-gateway share of avoidable spend 86.1% → 91.2%).
#   A cache entry that expired on its own schedule HAD to be re-created; the
#   honest bucket is what lets a cost view subtract it instead of chasing it.
BUCKET_TTL_EXPIRY = 'ttl_expiry'
# The EXACT wording _resolve_break_cause emits for a genuine >5min idle expiry,
# lowercased for matching. Deliberately the full phrase: the no-fingerprint
# fallback cause also contains the words "TTL expiry", but as one item in a
# hedge ("likely server-side miss or TTL expiry (UNPROVEN ...)"). Matching the
# bare substring mis-claimed those rounds as confirmed expiries and dropped
# genuine waste out of the recoverable total.
_TTL_EXPIRY_CAUSE = 'ttl expiry (>5min gap'
BUCKET_BREAKPOINT_LOST = 'breakpoint_lost'
BUCKET_MID_WINDOW = 'cache_mid_out_of_window'
BUCKET_WRITE_UNSETTLED = 'cache_write_unsettled'
BUCKET_UPSTREAM = 'upstream_identical'
BUCKET_BODY_CHANGE = 'body_change'
BUCKET_NO_BREAK = 'no_break'
# INDETERMINATE — the detector reached NO conclusion, which is NOT the same
#   as concluding the cache was fine. `no_break` used to mean both, so a round
#   that paid to rebuild a large prefix and read back nothing was filed
#   alongside genuine cache hits.
#
#   Measured mechanism: all three break predicates in _classify_break carry
#   `and not was_compaction`, so a COMPACTED round with a huge zero-read write
#   is structurally exempt from every gate and falls through. The exemption is
#   correct for the detector's RETURN value — a compaction legitimately
#   rebuilds its prefix and must not be blamed on the gateway — but wrong for
#   the LEDGER, where the tokens were really paid for.
#
#   NOT the mechanism (checked and disproved): a zero-read predecessor starving
#   the gates. `prev_prefix_tokens` is prev_read + prev_write, so a miss
#   predecessor with a large write still clears no_reuse's threshold — verified
#   directly against _classify_break, which returns no_reuse=True on exactly
#   that shape.
#
#   Deliberately NOT fixed by loosening a detection threshold: those gates stop
#   a genuinely cold or legitimately-rebuilt prefix being reported as a miss.
#   The defect is in how an unconcluded round is NAMED, so the fix is at the
#   landing site.
BUCKET_INDETERMINATE = 'indeterminate'
BUCKET_OTHER = 'other'


def classify_verdict(verdict: dict | None) -> str:
    """Map ONE ``detect_cache_break`` result dict to a bucket name.

    THE SINGLE SOURCE OF TRUTH for cache-miss bucketing — imported by BOTH the
    live per-round record emitter (below) AND the offline replay harness
    (``replay.py`` re-exports this exact function), so offline counts and live
    counts can never drift (the recurring bug class this whole effort fights).

    Keys off the RETURN KEY first (most authoritative), then the cause wording
    for the byte-identical sub-classes that share the ``server_side`` key.
    """
    if not verdict:
        return BUCKET_NO_BREAK
    if BUCKET_INDETERMINATE in verdict:
        return BUCKET_INDETERMINATE
    if BUCKET_NAMESPACE in verdict:
        return BUCKET_NAMESPACE
    if BUCKET_WRITE_UNSETTLED in verdict:
        return BUCKET_WRITE_UNSETTLED
    if BUCKET_MID_WINDOW in verdict:
        return BUCKET_MID_WINDOW
    if BUCKET_TURN_BOUNDARY in verdict:
        return BUCKET_TURN_BOUNDARY
    _cause = ' '.join(str(v) for v in verdict.values()).lower()
    if any(k in verdict for k in ('prefix_mutation', 'system_prompt', 'tools',
                                  'model', 'no_cache_reuse')):
        if 'ttl marker flipped' in _cause or 'new cache key' in _cause:
            return BUCKET_TTL_FLIP
        # A >5min idle gap expired the entry on its own schedule — an ordinary
        # rebuild, not a client body change. Checked BEFORE the generic
        # body_change fallthrough below, which would otherwise accuse our own
        # client of mutating a prefix the wire fingerprint proved identical.
        #
        # Anchored on the FULL genuine wording, not the bare substring 'ttl
        #   expiry'. The no-fingerprint fallback cause reads "likely
        #   server-side miss OR TTL expiry (UNPROVEN ...)" — a HEDGE listing
        #   TTL as one possibility, not a finding. Matching the bare substring
        #   claimed those unproven rounds as confirmed TTL and so REMOVED real
        #   waste from the recoverable total, which is the more dangerous
        #   direction: it makes the gateway problem look smaller than it is.
        if _TTL_EXPIRY_CAUSE in _cause:
            return BUCKET_TTL_EXPIRY
        if 'lookback window' in _cause:
            return BUCKET_MID_WINDOW
        if 'breakpoint lost' in _cause:
            return BUCKET_BREAKPOINT_LOST
        # ``no_cache_reuse`` is returned ONLY when client_changes was empty
        # (see the no_reuse / partial_no_reuse branches in detect_cache_break),
        # so a byte-identical one — whose cause names the whole/partial prefix
        # "an upstream cache miss" — is NOT a client body change. It must bucket
        # as upstream_identical, never body_change (which reads as a client
        # miss). Verified against real traffic: 8 rounds carrying this verdict
        # were all byte-identical by DB reconstruction.
        if 'upstream cache miss' in _cause:
            return BUCKET_UPSTREAM
        return BUCKET_BODY_CHANGE
    if 'ttl marker flipped' in _cause or 'new cache key' in _cause:
        return BUCKET_TTL_FLIP
    # Same reasoning as the client-keys branch above: an idle-expiry rebuild is
    # its own honest verdict, not the `other` catch-all. Anchored on the full
    # genuine wording so the UNPROVEN "server-side miss or TTL expiry" hedge is
    # NOT claimed as a confirmed expiry.
    if _TTL_EXPIRY_CAUSE in _cause:
        return BUCKET_TTL_EXPIRY
    if 'lookback window' in _cause:
        return BUCKET_MID_WINDOW
    if 'breakpoint lost' in _cause:
        return BUCKET_BREAKPOINT_LOST
    if 'upstream cache miss' in _cause:
        return BUCKET_UPSTREAM
    return BUCKET_OTHER


def detect_cache_break(
    conv_id: str,
    messages: list,
    tools: list | None,
    model: str,
    usage: dict | None = None,
    *,
    user_id: int,
    durable: bool = True,
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

    # Cross-turn cache_read baseline for a NEW turn's round-1. MUST be read
    # BEFORE acquiring _cache_lock below — get_prev_turn_cache_read takes the
    # SAME lock, so calling it inside the `with _cache_lock:` block would
    # deadlock. It EXCLUDES the current thread's own entry, so it returns the
    # PREVIOUS turn's final cached-prefix read (carried across the run_task
    # thread boundary), not this round's. 0 when there is no prior warm turn.
    _cross_turn_prev_read = get_prev_turn_cache_read(
        conv_id, user_id=user_id)

    _key = _state_key(conv_id, user_id=user_id)
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
        # FIX: Use the PREVIOUS call's prefix count for mutation comparison,
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
            # Message count going down is an explicit compaction/truncation.
            # Committed turns are immutable outside lifecycle operations, so
            # there is no second "history rewrite" cause to infer here.
            if msg_count < prev.message_count:
                client_changes['message_count'] = (
                    f'{prev.message_count} → {msg_count} (compacted)')

            # Diagnostic: prefix content mutation detection
            # Compare hash of the SAME range (prev prefix count) to detect
            # if existing messages were silently mutated in-place.
            if (_prev_prefix_hash
                    and prev.prefix_content_hash
                    and _prev_prefix_hash != prev.prefix_content_hash
                    and not prev.compaction_pending):
                _prefix_mutated = True
                _prefix_culprits = _diff_prefix_fields(
                    prev.prefix_field_hashes, _cur_field_hashes_prevrange)
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
            _nu = normalize_usage(usage)
            cache_read = _nu['cache_read']
            cache_write = _nu['cache_write']

        prev_cache_read = prev.last_cache_read_tokens

        # FIX: compute elapsed BEFORE updating state so TTL detection works.
        # Previously, elapsed was computed AFTER setting last_update_time = now,
        # which meant it was always 0, making the >5min TTL check dead code.
        if prev.last_update_time:
            elapsed = now - prev.last_update_time
        else:
            # Fresh per-thread state (a new task's round-1 on a fresh run_task
            # thread): the thread's own clock starts at 0, which made gap_s a
            # broken 0.0 on EXACTLY the records where the TTL-window question
            # matters (turn_boundary_rebill — measured 44/44 records 2026-08-01
            # reporting gap_s=0.0). Fall back to the NEWEST sibling thread's
            # timestamp for the same conv — the previous turn's true send time.
            # Runs under _cache_lock, so the sibling scan is race-free.
            _latest_sib = 0.0
            for _sk, _sst in _cache_states.items():
                if _sk[0] == user_id and _sk[1] == conv_id and _sk != _key \
                        and _sst.last_update_time > _latest_sib:
                    _latest_sib = _sst.last_update_time
            elapsed = now - _latest_sib if _latest_sib else 0

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
            _emit_l2_roi(conv_id, _roi, user_id=user_id,
                         cache_write_rebilled=int(cache_write),
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
        _wire_evidence = _wire_evidence_from_usage(usage)
        _cur_wire_fp = _wire_evidence.fingerprint
        _wire_static = _wire_evidence.static_prefix
        _cur_wire_system = _wire_evidence.system
        _cur_wire_markers = _wire_evidence.markers
        _cur_wire_bytes = _wire_evidence.message_bytes
        _cur_wire_field_bytes = _wire_evidence.field_bytes
        _cur_wire_region = _wire_evidence.region
        _cur_wire_routing = _wire_evidence.routing
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
            # A breakpoint being LOST (count/sys/tools drop) and a breakpoint's
            # ttl VALUE flipping (5m↔1h, count unchanged) are DISTINCT client
            # causes on a byte-identical body — check them INDEPENDENTLY. The
            # old code read the ttl only INSIDE the markers_regressed branch
            # and off a 'stable_ttls' key marker_signature never emitted, so a
            # pure ttl flip (the _task_id-drop latch bypass) was doubly invisible
            # on the live path and laundered into "byte-identical → server-side".
            if markers_regressed(prev.wire_markers, _cur_wire_markers):
                _wire_culprits.append('<breakpoint-lost>')
            if markers_ttl_flipped(prev.wire_markers, _cur_wire_markers):
                _wire_culprits.append('<ttl-flip>')
            # ── Mid-anchor slipped past the ~20-block lookback (the sawtooth
            #    whole-prefix rewrite) ──
            # The mid-history stepping-stone trails the rolling tail so the tail
            # can extend the prior cache entry, but if the trail/step params let
            # it slip FARTHER than Anthropic's ~20-block lookback the tail can
            # no longer reach it and the whole prefix past the mid is re-written
            # — on a BYTE-IDENTICAL body, which would otherwise launder into the
            # "upstream cache miss" verdict. Only meaningful when the read
            # actually collapsed this round (a same-layout warm round reads back
            # fine); gate it on _read_collapsed so a within-window round with a
            # benign write is not falsely named. This is the LAST body-invisible
            # CLIENT cause, mirroring <ttl-flip> / <breakpoint-lost>.
            #
            # NOTE: the ``<mid-out-of-window>`` layout token is appended LATER
            # (just before ``_wire_prefix_changed`` is computed), AFTER every
            # content-culprit detector (canonical diff, <bytes>, <hoisted>
            # region) has run — so a genuine prefix mutation ALWAYS blocks the
            # byte-identity gate and the layout token can never hijack the
            # verdict of a real body change. See the gated append below.
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
                    _canonical_region_changed = any(
                        str(c).startswith('<hoisted>.')
                        for c in _wire_culprits)
                    if _canonical_region_changed:
                        logger.warning(
                            '[CacheTrack] conv=%s call=%d ⚠ HOISTED REGION '
                            'CHANGED in both canonical and raw fingerprints — '
                            'the provider-bound system/tools cache-key bytes '
                            'changed. changed=[%s]',
                            conv_id[:8], prev.call_count + 1,
                            ', '.join(_wire_culprits[:8]) or '?')
                    else:
                        logger.warning(
                            '[CacheTrack] conv=%s call=%d ⚠ HOISTED REGION BYTES '
                            'DIVERGED while the lossy system fingerprint matched '
                            '— a canonical-invisible change (system block reorder '
                            '/ wrapping flip / per-turn re-serialization / '
                            'tool-param key reorder) altered the real cached-prefix '
                            'bytes. changed=[%s]',
                            conv_id[:8], prev.call_count + 1,
                            ', '.join(_region_culprits) or '?')
            # ── Mid-anchor slipped past the ~20-block lookback (LAYOUT-ONLY,
            #    byte-identity-gated) ──
            # Appended HERE, AFTER every content-culprit detector (canonical
            # diff + <bytes> + <hoisted> region), and ONLY when NONE of them
            # fired (``not _wire_culprits``): this is a LAYOUT-ONLY cause that
            # only explains a miss when the prefix BYTES were identical (the mid
            # marker moved but no content changed). Otherwise the miss is a
            # genuine PREFIX MUTATION and the layout token would merely CO-OCCUR
            # (the read collapse is a shared symptom) while its verdict branch
            # preempts ``prefix_mutation`` / ``body_change`` — MASKING the real,
            # actionable culprit. Live evidence: 128/128 real floor-collapses
            # previously labelled cache_mid_out_of_window had
            # body_identical=False, i.e. the body DID change and the layout
            # token was hijacking the verdict. Gated on _read_collapsed so a
            # within-window round with a benign write is not falsely named.
            if (not _wire_culprits
                    and not _was_compaction
                    and mid_anchor_out_of_window(_cur_wire_markers)
                    and prev_cache_read > _MIN_CACHE_MISS_TOKENS
                    and cache_read < prev_cache_read * 0.95
                    and (prev_cache_read - cache_read) >= _MIN_CACHE_MISS_TOKENS):
                _wire_culprits.append('<mid-out-of-window>')
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

        # ── Cache-NAMESPACE routing diff (the last body-invisible client var) ──
        # Anthropic prompt caching is namespaced by (upstream key + anthropic-beta
        # header + endpoint). The dispatch layer CAN flip the key mid-conversation
        # (cooldown/429/401/timeout → sticky key scored inf → rebind, which drags
        # the endpoint along) and the extended-cache-ttl beta is latched per-TASK,
        # so a new turn can re-latch a changed global. When any flips, a
        # BYTE-IDENTICAL prefix lands on a COLD namespace → a client-caused cold
        # miss the BODY fingerprints above are blind to. Diffing the routing
        # fingerprint BEFORE the break verdict lets the byte-identical branch
        # NAME this client switch instead of laundering it into "server-side".
        # A missing side (mid-deploy / non-Claude / capture failure) is inert.
        _ns_switch, _ns_verified_same = _detect_namespace_switch(
            current_routing=_cur_wire_routing,
            previous_state=prev,
            was_compaction=_was_compaction,
            conv_id=conv_id,
        )

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

        # ── Round-1 (new-turn) boundary re-bill — the statistics blind spot ──
        # _classify_break's three predicates ALL gate on call_count > 0, so the
        # FIRST round of every user turn (a fresh run_task thread → a fresh
        # CacheState with call_count == 0) is structurally exempt: the previous
        # turn's warm cached prefix that this round did NOT read back is never
        # counted. That is exactly the "stats too optimistic" gap. Feed the
        # CROSS-TURN baseline (the previous turn's final cached-prefix read,
        # recovered across the thread boundary) so a genuine round-1 read
        # collapse is classified and counted. Gated on call_count == 0 (round-1
        # only — later rounds go through _classify_break's within-turn baseline)
        # AND on a real prior warm prefix (> _MIN_CACHE_MISS_TOKENS), so a
        # genuine first-ever call (baseline 0, cold start) stays a benign
        # first-time cache write, never a false break. On round-1 no OTHER break
        # predicate can fire (they all require call_count > 0), so this branch
        # never conflicts with the client-side / namespace verdicts below.
        turn_boundary_break = (
            prev.call_count == 0
            and not _was_compaction
            and _cross_turn_prev_read > _MIN_CACHE_MISS_TOKENS
            and cache_read < _cross_turn_prev_read * 0.95
            and (_cross_turn_prev_read - cache_read) >= _MIN_CACHE_MISS_TOKENS
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
        # Routing is captured independently of the body fingerprints — store it
        # whenever present so a namespace flip is caught even on a round where
        # the body wire_fp handling differs (non-Claude / partial capture).
        if _cur_wire_routing is not None:
            prev.wire_routing = _cur_wire_routing
        prev.model = model
        prev.message_count = msg_count
        prev.last_cache_read_tokens = cache_read
        prev.last_cache_write_tokens = cache_write
        prev.last_update_time = now
        prev.call_count += 1
        # ── Compaction-guard hysteresis bookkeeping (2026-08-01) ──
        # A round counts as verifiably COLD only when neither direction moved
        # tokens; any read-back or fresh write means a server-side entry
        # exists and the just-sent prefix must stay frozen for next round.
        # Skipped on usage-less rounds (a failed call carries no cache signal
        # and must not open the guard). get_cache_prefix_count's _boundary
        # reads this streak — see COLD_STREAK_GUARD_OPEN in _state.py.
        if usage:
            if (cache_read > COLD_ROUND_TOKEN_FLOOR
                    or cache_write > COLD_ROUND_TOKEN_FLOOR):
                prev.cold_streak = 0
            elif prev.cold_streak < COLD_STREAK_GUARD_OPEN:
                prev.cold_streak += 1
        # ── Advance the DURABLE prefix boundary (survives restart / replica
        #    switch). When THIS round confirms real cache activity (read or
        #    write > 1000 — the same floor the cold-streak bookkeeping uses),
        #    the prefix [0, msg_count - EDITABLE_TAIL_COUNT) is a cached
        #    conversation fact. Persist it as a monotonic high-water mark so a
        #    future turn on a fresh thread/process/replica restores the guard
        #    floor instead of collapsing to 0 and rewriting the still-cached
        #    prefix. Best-effort, monotonic, DB-write only on genuine growth.
        if durable and (cache_read > 1000 or cache_write > 1000):
            _durable_boundary = max(0, msg_count - EDITABLE_TAIL_COUNT)
            if _durable_boundary > 0:
                try:
                    from lib.tasks_pkg.cache_tracking._persist import (
                        advance_persisted_boundary)
                    advance_persisted_boundary(
                        conv_id, _durable_boundary, user_id=user_id)
                except Exception as _pe:
                    logger.debug('[CacheTrack] advance_persisted_boundary '
                                 'failed conv=%s: %s', conv_id[:8], _pe)
        # ── Persist the DURABLE round-1 read baseline (survives restart /
        #    stale-eviction / replica switch). This round's cache_read becomes
        #    the NEXT turn's cross-turn baseline; when that next turn starts on a
        #    cold process (no live sibling), get_prev_turn_cache_read falls back
        #    to this persisted value so a collapsed round-1 buckets honestly as
        #    turn_boundary_rebill instead of being laundered into no_break. Only
        #    persist a real warm read (> the miss floor) — a floor-only / zero
        #    read is not a usable prior-warm baseline and writing it would just
        #    lower the durable value pointlessly. Best-effort, last-writer-wins.
        if durable and cache_read > _MIN_CACHE_MISS_TOKENS:
            try:
                from lib.tasks_pkg.cache_tracking._persist import (
                    write_last_turn_cache_read)
                write_last_turn_cache_read(
                    conv_id, cache_read, user_id=user_id)
            except Exception as _pe:
                logger.debug('[CacheTrack] write_last_turn_cache_read '
                             'failed conv=%s: %s', conv_id[:8], _pe)
        if not prev.first_call_time:
            prev.first_call_time = now
        # Accumulate session-level stats
        prev.total_cache_read += cache_read
        prev.total_cache_write += cache_write
        # total_input_tokens is the denominator of [CacheSession]'s
        #   overall_hit%. It must be the TOTAL prompt the provider processed,
        #   which is convention-dependent: on the OpenAI-compat wire
        #   prompt_tokens ALREADY INCLUDES the cached tokens, so the previous
        #   `prompt_tokens + cache_write + cache_read` counted them twice and
        #   halved the session hit rate. split_input_tokens is the same helper
        #   the cost engine uses (lib/cost.py), so session telemetry, per-round
        #   telemetry and billing all read the prompt size identically.
        from lib.cost import split_input_tokens as _split_input_tokens
        prev.total_input_tokens += _split_input_tokens(usage)[1]

        # When the authoritative wire fingerprint is available it REPLACES
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

        # A silent prefix-byte mutation counts as a CONFIRMED surfaced break
        # when either provider accounting reports a real cache_write OR the API
        # authoritative wire capture reports a mutation AND the API reports a
        # material collapse from the previous warm cache_read.  Some OpenAI-
        # compatible gateways (including the incident's Kimi route) always
        # report cache_write=0, so requiring write metering launders a wire-
        # proven client mutation into the generic server_side branch.  Without
        # wire capture, the legacy write threshold remains required.
        # A byte change without either cost signal remains diagnostic-only.
        #
        # POSITION GATE (the (A)-vs-benign-tail discriminator). A byte change
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
        _read_collapsed = api_break
        _position_known = _first_changed_idx >= 0
        _mutation_is_whole_prefix = (
            _mutation_inside_prior_prefix or _read_collapsed
            or not _position_known
        )
        prefix_mutation_break = (
            _prefix_mutated
            and not _was_compaction
            and (cache_write >= _MIN_CACHE_MISS_TOKENS
                 or (_wire_available and _read_collapsed))
            and _mutation_is_whole_prefix
        )

        if (api_break or no_reuse or partial_no_reuse or prefix_mutation_break
                or turn_boundary_break):
            prev.total_breaks += 1

        # ── ALWAYS-ON per-round record (single-exit seam) ──
        # `_finish(v)` emits ONE machine-readable [CacheRoundRecord] with the
        # bucket (from single-source classify_verdict) then returns v, so the
        # client-vs-upstream classification is a logged, greppable fact for
        # EVERY round (break AND no-break) — the root fix for "monitoring
        # insufficient". Defined BEFORE the break branch so the no-break path
        # below can call it too.
        _ttl_flip = '<ttl-flip>' in (_wire_culprits or [])
        _breakpoint_lost = '<breakpoint-lost>' in (_wire_culprits or [])

        def _finish(v, record_as=None):
            """Emit the per-round record, then return the DETECTOR's verdict.

            ``record_as`` decouples what is RECORDED from what is RETURNED. A
            round can be honestly bucketed as ``indeterminate`` in telemetry
            while the function still returns ``None`` to its callers — the
            return value is the confirmed-break signal that drives behaviour,
            and an unconcluded round must NOT start claiming a confirmed break.
            """
            _emit_round_record(
                conv_id, prev.call_count,
                record_as if record_as is not None else v,
                ns_switch=_ns_switch, ttl_flip=_ttl_flip,
                breakpoint_lost=_breakpoint_lost,
                body_identical=_wire_proven_identical,
                namespace_verified=_ns_verified_same,
                cache_read=cache_read, cache_write=cache_write,
                elapsed=elapsed, culprits=_wire_culprits, model=model,
                wire_available=_wire_available,
                wire_changed=_wire_prefix_changed,
                read_collapsed=_read_collapsed)
            return v

        # ── Report ──
        # A cache break is "confirmed" when the API shows: a DROP from a prior
        # high read (api_break), a large fresh write with zero read despite an
        # established prefix (no_reuse), OR a large write repeated round-over-
        # round while the read stays pinned (partial_no_reuse). All three mean
        # we paid to rebuild (part of) the cache instead of reading it back.
        if (api_break or no_reuse or partial_no_reuse or prefix_mutation_break
                or turn_boundary_break):
            # ── Round-1 boundary re-bill (round-1 only; no other break can
            #    co-fire since they all require call_count > 0). Named as its
            #    OWN bucket — NOT laundered into server_side — so the previous
            #    turn's warm prefix that fell out across the boundary is an
            #    honest, counted client-visible miss, not "first-cache warm-up".
            if turn_boundary_break:
                _boundary_cause = (
                    'new-turn round-1 boundary re-bill: the previous turn left '
                    f'a warm ~{_cross_turn_prev_read}-token cached prefix, but '
                    f'this turn read back only {cache_read} (collapsed toward '
                    'the static floor) — the cached prefix was not reused '
                    'across the turn boundary and was re-billed uncached. '
                    'Counted here so round-1 is no longer a stats blind spot '
                    '(likely a TTL-window boundary miss; see the tail-TTL ticket).')
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ TURN-BOUNDARY RE-BILL: '
                    'cache_write=%d cache_read=%d (prev-turn read=%d, gap=%.1fs) '
                    '— new-turn round-1 read collapsed; prefix not reused across '
                    'the turn boundary.',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    _cross_turn_prev_read, elapsed)
                return _finish({'turn_boundary_rebill': _boundary_cause})

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
                namespace_switch=_ns_switch,
                namespace_verified_same=_ns_verified_same,
            )

            # CACHE WRITE-VISIBILITY RACE (Anthropic SDK #1451) — a
            #   byte-identical, same-routing round whose read collapsed AND
            #   which arrived within the cold-write settle window since THIS
            #   conv's prior COLD write. A freshly-written cache entry is not
            #   readable for ~15–20s (reproduced live: a cold prefix re-sent
            #   every 4s missed at t=3s/t=10s, HIT at t≈16s), so a fast tool
            #   loop that fires the next round ~8s later misses and re-writes
            #   the whole prefix — NOT a server/gateway fault and NOT a client
            #   byte change. Named FIRST (it is the most specific timing cause)
            #   when the prior-round cold-write gap marker is present and inside
            #   the window; the cache_settle gate is the fix that prevents it.
            # The cold-write gap: prefer an explicit usage marker (test / future
            # plumbing), else DERIVE it from internal state — the prior round
            # was a COLD WRITE when its write dominated its read, and ``elapsed``
            # is precisely the gap since that round ended. This keeps the
            # detector self-contained (no dispatch plumbing) while staying
            # overridable.
            _cold_gap = None
            if usage is not None:
                _cold_gap = usage.get('_prev_cold_write_gap_s')
            if _cold_gap is None:
                _prev_was_cold = (prev_cache_write >= _MIN_CACHE_MISS_TOKENS
                                  and prev_cache_read < prev_cache_write)
                if _prev_was_cold and elapsed > 0:
                    _cold_gap = elapsed
            _within_settle_window = False
            if _cold_gap is not None:
                try:
                    from lib.llm_dispatch.cache_settle import settle_cold_window_ms
                    _within_settle_window = (
                        float(_cold_gap) < (settle_cold_window_ms() / 1000.0))
                except Exception as _cse:
                    logger.debug('[CacheTrack] cold-window lookup failed: %s', _cse)
            if (_within_settle_window and _wire_proven_identical
                    and not _ns_switch and not client_changes
                    and not prefix_mutation_break):
                _unsettled_cause = (
                    'prefix not read back though the wire bytes were '
                    'byte-identical and the routing was identical — but this '
                    f'round arrived only {float(_cold_gap):.1f}s after the '
                    'previous round COLD-WROTE this prefix, inside the '
                    '~15–20s Anthropic cache write-visibility window (SDK '
                    '#1451): the just-written entry was not yet visible '
                    'upstream, so the prefix was re-billed uncached. A '
                    'CLIENT-side timing race (fast tool loop firing before the '
                    'write settled), NOT a server/gateway miss — the '
                    'cache-settle gate is meant to hold the next send until '
                    'the write is visible.')
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ CACHE WRITE UNSETTLED: '
                    'cache_write=%d cache_read=%d (prev read=%d, cold-write gap='
                    '%.1fs, gap=%.1fs) — prior cold write not yet visible.',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    prev_cache_read, float(_cold_gap), elapsed)
                return _finish({'cache_write_unsettled': _unsettled_cause})

            # CACHE-NAMESPACE SWITCH — a body-identical round whose ROUTING
            #   flipped (upstream key / anthropic-beta / endpoint) is a
            #   CLIENT-caused cold miss: the same prefix bytes were routed to a
            #   different gateway cache namespace. It MUST NOT fall through to
            #   the server_side branch (the exact mislabel the owner flagged —
            #   "byte-identical → blame the gateway"). It wins over the generic
            #   byte-identical verdict but still defers to a named body change
            #   (client_changes / prefix_mutation_break), which is a distinct,
            #   more-specific client culprit. Surfaced under its own key so the
            #   cost popover names the actionable routing switch.
            if (_ns_switch and _wire_proven_identical
                    and not client_changes and not prefix_mutation_break):
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ CACHE NAMESPACE SWITCH BREAK: '
                    'cache_write=%d cache_read=%d (prev read=%d, gap=%.1fs) — '
                    'byte-identical prefix routed to a different cache namespace. '
                    'Cause: %s',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    prev_cache_read, elapsed, cause_str)
                return _finish({'cache_namespace_switch': cause_str})

            # MID-ANCHOR OUT-OF-WINDOW — a byte-identical round whose mid
            #   stepping-stone drifted past the ~20-block lookback so the tail
            #   could not extend it → the whole prefix past the mid was
            #   re-written. It is a CLIENT-side breakpoint-LAYOUT miss and MUST
            #   NOT fall through to server_side/upstream (the "blame the
            #   gateway" mislabel this fix targets). It wins over the generic
            #   byte-identical / prefix_mutation verdicts (it is the SPECIFIC
            #   named layout cause) but still defers to a concrete body change
            #   (client_changes) which is a distinct, differently-named client
            #   culprit. Surfaced under its own key so the record buckets it as
            #   cache_mid_out_of_window, never upstream_identical.
            if ('<mid-out-of-window>' in (_wire_culprits or [])
                    and not client_changes):
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ MID-ANCHOR OUT OF WINDOW: '
                    'cache_write=%d cache_read=%d (prev read=%d, gap=%.1fs) — '
                    'mid stepping-stone drifted past the ~20-block lookback; '
                    'tail could not extend the prior entry → whole prefix past '
                    'the mid re-billed on a byte-identical body. Cause: %s',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    prev_cache_read, elapsed, cause_str)
                return _finish({'cache_mid_out_of_window': cause_str})

            # Prefix mutation is the most ACTIONABLE and most CERTAIN cause —
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
                return _finish({'prefix_mutation': cause_str})

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
                    return _finish(client_changes)
                return _finish({'no_cache_reuse': cause_str})

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
                    return _finish(client_changes)
                return _finish({'no_cache_reuse': cause_str})

            # api_break path: cache_read dropped from a prior high value.
            if client_changes:
                logger.info(
                    '[CacheBreak] conv=%s call=%d CONFIRMED cache break: %s. '
                    'cache_read: %d → %d tokens (gap=%.1fs)',
                    conv_id[:8], prev.call_count, cause_str,
                    prev_cache_read, cache_read, elapsed,
                )
                return _finish(client_changes)
            logger.info(
                '[CacheTrack] conv=%s call=%d cache_read dropped: %d → %d '
                '(gap=%.1fs, %s)',
                conv_id[:8], prev.call_count,
                prev_cache_read, cache_read, elapsed, cause_str,
            )
            return _finish({'server_side': cause_str})
        # ── No confirmed break: still emit the per-round record so the ledger
        #    has EVERY round, not only misses. ──
        if client_changes:
            # Client-side changes detected but cache wasn't broken (or no
            # cache stats available) — log at debug level only.
            logger.debug(
                '[CacheTrack] conv=%s call=%d client changes: %s '
                '(cache_read: %d → %d, no confirmed break)',
                conv_id[:8], prev.call_count,
                ', '.join(f'{k}={v}' for k, v in client_changes.items()),
                prev_cache_read, cache_read,
            )

        # ── INDETERMINATE vs genuinely-no-break ──
        # This round paid to write a substantial prefix and read back NOTHING,
        # yet no break gate fired. That is not "the cache was reused fine" — it
        # is "we could not tell", and recording both under `no_break` is what
        # let real spend hide behind a healthy-looking bucket.
        #
        # THE ACTUAL SUPPRESSOR IS COMPACTION, measured — not gate starvation.
        # All three break predicates carry `and not was_compaction`, so a
        # compacted round with a huge zero-read write is structurally exempt
        # from every gate and lands here. (An earlier version of this fix
        # asserted the cause was a miss-predecessor starving the gates; that is
        # FALSE — `prev_prefix_tokens` is prev_read + prev_write, so a
        # zero-read predecessor with a large write still satisfies no_reuse's
        # threshold. Verified directly against _classify_break.)
        #
        # The exemption is right for the RETURN value: a compaction legitimately
        # rebuilds its prefix, so it is not a confirmed break and must not be
        # blamed on the gateway. But it is wrong for the LEDGER: the tokens were
        # really paid for, and filing them as `no_break` makes a rebuild look
        # like a cache hit. Recording them as indeterminate keeps the spend
        # visible and the cause honestly unresolved.
        #
        # Requires a genuine PREDECESSOR. ``prev.call_count`` has already been
        # incremented for THIS round by the time we get here, so round 1 sees
        # it as 1 — the guard must be > 1, not > 0, or every cold start is
        # mislabelled as an unknown. A first round had nothing to read back.
        _gates_starved = (
            prev.call_count > 1
            and cache_read == 0
            and cache_write >= _MIN_NO_REUSE_TOKENS
        )
        if _gates_starved:
            logger.warning(
                '[CacheTrack] conv=%s call=%d INDETERMINATE: cache_write=%d '
                'read=0 (gap=%.1fs, compaction=%s) but no break gate fired — '
                'the spend is real but the cause is unresolved. Counted as '
                'indeterminate, NOT as no_break: a zero-read rebuild must not '
                'be filed alongside genuine cache hits.',
                conv_id[:8], prev.call_count, cache_write, elapsed,
                _was_compaction)
            return _finish(None, record_as={
                'indeterminate': (
                    'zero read-back on a substantial write, but no break gate '
                    'could fire'
                    + (' because this round followed a compaction, which is '
                       'structurally exempt from every break predicate'
                       if _was_compaction else '')
                    + '. The spend is real and counted here; the CAUSE is '
                    'unresolved — this round is NOT evidence of a healthy '
                    'cache.')})
        _finish(None)

    return None
