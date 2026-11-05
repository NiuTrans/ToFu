"""Pure cache-break thresholds, cause precedence and shared verdict buckets.

Live detection and offline replay use this policy. It owns no locks, storage,
provider calls or logging. EDITABLE_TAIL_COUNT is a diagnosis tolerance only;
prefix protection (``_prefix.get_cache_prefix_count``) protects the whole
previously sent count and does not read it.
"""
from __future__ import annotations

# Ignore small token fluctuations when surfacing a provider-reported read drop.
_MIN_CACHE_MISS_TOKENS = 2000
# Diagnosis tolerance: the newest two api-form messages of a round are left out
# of the prior-prefix hash comparison and of the "inside the prior cached
# prefix" position test. It absorbs provider-wire representation of the tail
# (the rolling tail marker, string/block wrapping), not caller-side mutation:
# build_body deep-copies api-form messages (_strip_non_api_fields), and
# add_cache_breakpoints/canonicalization rewrite only that body copy, so the
# transport never edits api-form messages in place (verified 2026-11-02).
# Prefix protection does not use this bound.
EDITABLE_TAIL_COUNT = 2
# Repeated large writes with no reuse are meaningful; small-prompt warmup is not.
_MIN_NO_REUSE_TOKENS = 20000
BUCKET_NAMESPACE = 'cache_namespace_switch'
BUCKET_TURN_BOUNDARY = 'turn_boundary_rebill'
BUCKET_TTL_FLIP = 'ttl_flip'
BUCKET_TTL_EXPIRY = 'ttl_expiry'
# Match the established cause phrase, never an unproven mention of TTL expiry.
# The '(>' anchor covers every window label ('5min', '1h') and the legacy
# '(>5min gap, prompt unchanged)' wording, but no hedge ('TTL expiry (UNPROVEN').
_TTL_EXPIRY_CAUSE = 'ttl expiry (>'
# Anthropic cache lifetime per cache_control ``ttl`` value; a bare marker is
# the 5-minute default. Unknown spellings keep the conservative default.
DEFAULT_CACHE_TTL_S = 300
_MARKER_TTL_S = {'': 300, '5m': 300, '1h': 3600}
BUCKET_BREAKPOINT_LOST = 'breakpoint_lost'
BUCKET_MID_WINDOW = 'cache_mid_out_of_window'
BUCKET_WRITE_UNSETTLED = 'cache_write_unsettled'
BUCKET_UPSTREAM = 'upstream_identical'
BUCKET_BODY_CHANGE = 'body_change'
BUCKET_NO_BREAK = 'no_break'
BUCKET_INDETERMINATE = 'indeterminate'
BUCKET_OTHER = 'other'


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



def cache_ttl_window_s(markers) -> int:
    """Seconds the previous round's cache entries outlive an idle gap.

    ``markers`` is that round's wire ``marker_signature``. Its shortest marker
    TTL bounds the window: past it, an entry the round wrote may have expired
    on its own schedule. With every marker at ``1h`` a 5-60 minute gap is
    therefore NOT an expected expiry. Without captured markers (non-Claude,
    capture failure, a fresh thread) the 5-minute default keeps the historical
    threshold.
    """
    ttls = markers.get('ttls') if isinstance(markers, dict) else None
    if not ttls:
        return DEFAULT_CACHE_TTL_S
    return min(_MARKER_TTL_S.get(str(pair[1]), DEFAULT_CACHE_TTL_S)
               for pair in ttls)


def _resolve_break_cause(
    *, client_changes: dict, prefix_mutation_break: bool,
    elapsed: float, cache_read: int, cache_write: int, prefix_mutated: bool,
    prefix_culprits: list | None = None,
    wire_proven_identical: bool = False,
    namespace_switch: list | None = None,
    namespace_verified_same: bool = False,
    ttl_window_s: int = DEFAULT_CACHE_TTL_S,
) -> str:
    """Build the single most-specific human cause string for a confirmed break.

    Precedence: explicit client changes → prefix-byte mutation (with the EXACT
    changed ``key.field`` list when known) → TTL expiry → upstream eviction.
    TTL expiry needs ``elapsed`` beyond ``ttl_window_s`` (see
    ``cache_ttl_window_s``), not a fixed five minutes. The prefix-mutation
    billing clause states this round's reported ``cache_read`` /
    ``cache_write``: a mutation re-bills only what follows the last cache
    point before it, never the whole prompt while reads still hit.

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
        if cache_read <= 0:
            _billed = 'the whole prompt was re-billed uncached'
        elif cache_write > 0:
            _billed = (f'{cache_read} tokens were still read from cache and '
                       f'{cache_write} tokens from the last cache point '
                       'before the change onward were written to cache')
        else:
            # OpenAI-compatible gateways meter reads but never report writes.
            _billed = (f'{cache_read} tokens were still read from cache; the '
                       'rest of the prompt was billed as uncached input (no '
                       'cache write reported)')
        _base = ('cached prefix bytes changed between turns '
                 f'(non-idempotent history edit) — {_billed}')
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
    if elapsed > ttl_window_s:
        _window = ('1h' if ttl_window_s == 3600
                   else f'{max(1, round(ttl_window_s / 60))}min')
        return (f'Possible TTL expiry (>{_window} gap); server cause not '
                'established')
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
        _ns_evidence = (' Captured routing hashes were identical '
                        '(key + anthropic-beta + endpoint).'
                        if namespace_verified_same else '')
        return ('Provider reported less prefix reuse (an upstream cache miss); '
                'captured wire-prefix hashes were identical. This is evidence '
                'about observed request fields, not proof of server causality, '
                'TTL expiry, or unobserved upstream transformations.' + _ns_evidence)
    # ── Wire fingerprint UNAVAILABLE → legacy elimination guess (unproven) ──
    if cache_read > _MIN_CACHE_MISS_TOKENS:
        return ('Cache read decreased with partial prefix reuse; local '
                'fingerprints cannot establish the upstream cause.')
    if not prefix_mutated:
        return ('Prefix not reused; matching local fingerprints alone '
                'cannot establish a server cause or TTL expiry.')
    # Prefix bytes DID change but the write was below the surfacing floor.
    # We still know exactly which field moved — name it instead of guessing.
    if _culprits:
        return ('prefix bytes changed between turns '
                f'[changed: {_culprits}] — likely cause of the miss')
    return ('Prefix not reused; insufficient provider-bound evidence to '
            'attribute a cause.')



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

