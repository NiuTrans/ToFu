"""Provider-bound cache evidence comparison, separate from token accounting.

compare_wire_evidence reads the previous snapshot while the detector owns the
cache lock. It reports canonical, raw-byte, layout and position evidence without
changing CacheState. Namespace comparison uses the same provider evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.cache_tracking._state import CacheState
from lib.tasks_pkg.cache_tracking._policy import (
    EDITABLE_TAIL_COUNT, _MIN_CACHE_MISS_TOKENS, cache_ttl_window_s,
)
from lib.tasks_pkg.wire_fingerprint import (
    diff_canonical, markers_regressed, markers_ttl_flipped, mid_anchor_out_of_window,
)

logger = get_logger(__name__)
_NON_MESSAGE_WIRE_CULPRITS = (
    '<hoisted>', '<bytes>system', '<bytes>tools', '<breakpoint-lost>',
    '<ttl-flip>', '<mid-out-of-window>',
)


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
    tool_count: int | None = None
    tool_name_bytes: int | None = None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _wire_evidence_from_usage(usage: dict | None) -> _WireEvidence:
    if not usage:
        return _WireEvidence()
    surface = usage.get('_wire_tool_surface')
    if not isinstance(surface, dict):
        surface = {}
    return _WireEvidence(
        fingerprint=usage.get('_wire_fp'),
        static_prefix=usage.get('_wire_static') or '',
        system=usage.get('_wire_system'),
        markers=usage.get('_wire_markers'),
        message_bytes=usage.get('_wire_bytes'),
        field_bytes=usage.get('_wire_field_bytes'),
        region=usage.get('_wire_region'),
        routing=usage.get('_wire_routing'),
        tool_count=_non_negative_int(surface.get('count')),
        tool_name_bytes=_non_negative_int(surface.get('name_bytes')),
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
        # Failover rotation changes the key on purpose, and the other key's
        # namespace is often still warm. The cache outcome below warns only
        # when the switch actually cost a miss (NAMESPACE SWITCH BREAK).
        logger.info(
            '[CacheTrack] conv=%s call=%d cache namespace switch: the request '
            'routing changed between turns; the cache outcome is classified '
            'below. changed=[%s] prev=%s cur=%s',
            conv_id[:8], previous_state.call_count + 1,
            ', '.join(namespace_switch), previous_state.wire_routing,
            current_routing,
        )
    return namespace_switch, namespace_verified_same


def _report_wire_prefix_change(conv_id, prev, wire_culprits, *,
                               advertised_rewrite, api_first_changed,
                               expected_rewrite_index, first_changed_idx,
                               prior_prefix_boundary, inside_prior_prefix,
                               msg_count, prev_wire_count) -> bool:
    """Log one observed wire prefix change; return whether an advertised
    bounded compaction rewrite still explains it."""
    advertised_rewrite = advertised_rewrite and all(
        not str(c).startswith(_NON_MESSAGE_WIRE_CULPRITS)
        for c in wire_culprits)
    if advertised_rewrite:
        # The compactor announced this bounded message rewrite and
        # paid for it through its economics gate; not a defect.
        logger.info(
            '[CacheTrack] conv=%s call=%d wire prefix rewritten '
            'by an advertised compaction (api-form change at %d '
            '>= advertised index %d). changed=[%s] '
            'first_changed_idx=%d prior_prefix_boundary=%d',
            conv_id[:8], prev.call_count + 1, api_first_changed,
            expected_rewrite_index,
            ', '.join(wire_culprits[:8]) or '?',
            first_changed_idx, prior_prefix_boundary)
    else:
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ WIRE PREFIX CHANGED: '
            'the ACTUAL sent bytes differ from last round — '
            'client-caused miss. changed=[%s] first_changed_idx=%d '
            'prior_prefix_boundary=%d prev_wire_msg_count=%d '
            'prev_msg_count=%d cur_msg_count=%d '
            'inside_prior_cached_prefix=%s',
            conv_id[:8], prev.call_count + 1,
            ', '.join(wire_culprits[:8]) or '?',
            first_changed_idx, prior_prefix_boundary, prev_wire_count,
            prev.message_count, msg_count, inside_prior_prefix)
    return advertised_rewrite


@dataclass(frozen=True)
class WireComparison:
    comparable: bool
    bytes_comparable: bool
    prefix_changed: bool
    culprits: list[str]
    first_changed_index: int
    mutation_inside_prior_prefix: bool
    advertised_rewrite: bool
    # Idle seconds the previous round's cache entries survive, from its markers.
    ttl_window_s: int


def compare_wire_evidence(
    prev: CacheState, evidence: _WireEvidence, *, conv_id: str,
    was_compaction: bool, expected_covers: bool, expected_rewrite_index: int,
    api_first_changed: int, client_changes: dict, message_count: int,
    cache_read: int,
) -> WireComparison:
    """Compare only the prior prefix; newly appended messages are not edits."""
    _wire_available = evidence.fingerprint is not None
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
    # An announced bounded rewrite explains this round's prefix change
    # unless system/tools/model also moved; wire culprits refine it below.
    _advertised_rewrite = expected_covers and not client_changes
    _wire_comparable = (_wire_available and prev.call_count > 0
                        and prev.wire_fp is not None and not was_compaction)
    _wire_bytes_comparable = (evidence.message_bytes is not None
                              and prev.wire_bytes is not None)
    if _wire_comparable:
        # Compare only the region that existed last round (its full length);
        # this round appends new tail messages we don't diff against.
        _shared = len(prev.wire_fp)
        _wire_culprits = diff_canonical(
            prev.wire_fp[:_shared], (evidence.fingerprint or [])[:_shared])
        # ── System/tools diff (the hoisted-prefix blind spot) ──
        # On the Anthropic path system+tools live OUTSIDE body['messages'],
        # so the diff above never sees them. A per-turn system change
        # (cross-conv digest / charter / board) would otherwise leave
        # _wire_culprits empty → a false "server-side PROVEN" verdict on a
        # genuinely client-caused miss. Fold the system fingerprint diff in
        # so a system-block mutation is NAMED and blocks that false verdict.
        if evidence.system is not None and prev.wire_system is not None:
            for _fld in ('system', 'tools'):
                if prev.wire_system.get(_fld) != evidence.system.get(_fld):
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
        if markers_regressed(prev.wire_markers, evidence.markers):
            _wire_culprits.append('<breakpoint-lost>')
        if markers_ttl_flipped(prev.wire_markers, evidence.markers):
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
        if (not _wire_culprits and evidence.message_bytes is not None
                and prev.wire_bytes is not None):
            from lib.tasks_pkg.wire_fingerprint import diff_byte_prefix
            _byte_shared = len(prev.wire_bytes)
            _byte_culprits = diff_byte_prefix(
                prev.wire_bytes[:_byte_shared],
                (evidence.message_bytes or [])[:_byte_shared])
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
                if (evidence.field_bytes is not None
                        and prev.wire_field_bytes is not None):
                    try:
                        from lib.tasks_pkg.wire_fingerprint import (
                            diff_byte_field_prefix)
                        _ff_shared = len(prev.wire_field_bytes)
                        _field_culprits = diff_byte_field_prefix(
                            prev.wire_field_bytes[:_ff_shared],
                            (evidence.field_bytes or [])[:_ff_shared])
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
        if (evidence.region is not None and prev.wire_region is not None):
            from lib.tasks_pkg.wire_fingerprint import diff_byte_region
            _region_culprits = diff_byte_region(
                prev.wire_region, evidence.region)
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
                and not was_compaction
                and mid_anchor_out_of_window(evidence.markers)
                and prev.last_cache_read_tokens > _MIN_CACHE_MISS_TOKENS
                and cache_read < prev.last_cache_read_tokens * 0.95
                and (prev.last_cache_read_tokens - cache_read) >= _MIN_CACHE_MISS_TOKENS):
            _wire_culprits.append('<mid-out-of-window>')
        _wire_prefix_changed = bool(_wire_culprits)

        if _wire_prefix_changed:
            # ── Position evidence (the "which part & was it already
            #    cached" ground truth) ──
            # Report the FIRST changed message index and whether it falls
            # inside the PRIOR round's cached-prefix boundary (the previous
            # round's WIRE message count - EDITABLE_TAIL_COUNT). Both sides
            # count wire messages: prev.message_count is the API-form count,
            # which on the Anthropic wire still includes the hoisted system
            # message and every tool result of a merged turn. A changed index
            # BELOW that boundary means an ALREADY-CACHED message was
            # rewritten in place this round (the monotonic-growth break);
            # a changed index only in the editable tail / fresh region is a
            # different, benign class. This is what distinguishes an
            # EDITABLE_TAIL leak, a lost-state (thread-key) full-prefix
            # rewrite, and a cold-compaction-of-cached-message from each
            # other in the live log, without a rerun.
            _position_space = _shared
            try:
                from lib.tasks_pkg.wire_fingerprint import (
                    first_changed_byte_index, first_changed_index)
                _fci = first_changed_index(prev.wire_fp[:_shared],
                                           (evidence.fingerprint or [])[:_shared])
                # The canonical index is BLIND to a <bytes>-only culprit
                # (canonical says "identical", raw bytes differ), returning
                # -1 → a meaningless inside_prior_cached_prefix=False for
                # exactly the class where position matters most. Fall back
                # to the true-byte index so a byte-only divergence gets an
                # honest position.
                if _fci < 0 and prev.wire_bytes is not None \
                        and evidence.message_bytes is not None:
                    _byte_shared = len(prev.wire_bytes)
                    _position_space = _byte_shared
                    _fci = first_changed_byte_index(
                        prev.wire_bytes[:_byte_shared],
                        (evidence.message_bytes or [])[:_byte_shared])
            except Exception as _fe:
                logger.debug('[CacheTrack] first_changed_index failed: %s', _fe)
                _fci = -2
            _prior_prefix_boundary = max(0, _position_space
                                         - EDITABLE_TAIL_COUNT)
            _inside_prior_prefix = (0 <= _fci < _prior_prefix_boundary)
            # Publish to the break classifier (see prefix_mutation_break).
            _first_changed_idx = _fci
            _mutation_inside_prior_prefix = _inside_prior_prefix
            _advertised_rewrite = _report_wire_prefix_change(
                conv_id, prev, _wire_culprits,
                advertised_rewrite=_advertised_rewrite,
                api_first_changed=api_first_changed,
                expected_rewrite_index=expected_rewrite_index,
                first_changed_idx=_fci,
                prior_prefix_boundary=_prior_prefix_boundary,
                inside_prior_prefix=_inside_prior_prefix,
                msg_count=message_count, prev_wire_count=_position_space)

    return WireComparison(
        comparable=_wire_comparable, bytes_comparable=_wire_bytes_comparable,
        prefix_changed=_wire_prefix_changed, culprits=_wire_culprits,
        first_changed_index=_first_changed_idx,
        mutation_inside_prior_prefix=_mutation_inside_prior_prefix,
        advertised_rewrite=_advertised_rewrite,
        ttl_window_s=cache_ttl_window_s(prev.wire_markers),
    )
