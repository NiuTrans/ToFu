"""Render confirmed cache-break diagnostics from one captured round.

The detector owns state, persistence and the exactly-once round record. This
module only chooses the ordered diagnostic and returns its existing public
shape; it never mutates a task, cache baseline or provider request.
"""
from __future__ import annotations

from dataclasses import dataclass

from lib.log import get_logger
from lib.tasks_pkg.cache_tracking._policy import (
    DEFAULT_CACHE_TTL_S, _MIN_CACHE_MISS_TOKENS, _resolve_break_cause,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class BreakReport:
    call_count: int
    conv_id: str
    cache_read: int
    cache_write: int
    previous_read: int
    previous_write: int
    elapsed: float
    usage: dict | None
    client_changes: dict
    prefix_mutation_break: bool
    turn_boundary_break: bool
    previous_turn_read: int
    prefix_mutated: bool
    prefix_culprits: list
    wire_proven_identical: bool
    namespace_switch: list
    namespace_verified_same: bool
    wire_culprits: list
    no_reuse: bool
    api_break: bool
    partial_no_reuse: bool
    previous_prefix_tokens: int
    # Non-empty while this round's group is the cold side of a live
    # same-route read-share split (``_health.read_share_monitor``).
    cold_group: str = ''
    ttl_window_s: int = DEFAULT_CACHE_TTL_S


def _split_note(report: BreakReport) -> str:
    """Qualify an upstream/timing attribution for a round on the cold side of a split."""
    if not report.cold_group:
        return ''
    return (f' This round is on the cold side of a live same-route split '
            f'({report.cold_group}; see [CacheHealth]): compare what the cold '
            'and warm request groups send before attributing it upstream.')


def report_confirmed_break(report: BreakReport) -> dict:
    """Keep boundary, write visibility, routing and content cause precedence."""
    if report.turn_boundary_break:
        _boundary_cause = (
            'new-turn round-1 boundary re-bill: the previous turn left '
            f'a warm ~{report.previous_turn_read}-token cached prefix, but '
            f'this turn read back only {report.cache_read} (collapsed toward '
            'the static floor) — the cached prefix was not reused '
            'across the turn boundary and was re-billed uncached. '
            'Provider usage establishes the read drop, not its cause.')
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ TURN-BOUNDARY RE-BILL: '
            'cache_write=%d cache_read=%d (prev-turn read=%d, gap=%.1fs) '
            '— new-turn round-1 read collapsed; prefix not reused across '
            'the turn boundary.',
            report.conv_id[:8], report.call_count, report.cache_write, report.cache_read,
            report.previous_turn_read, report.elapsed)
        # Attach STRUCTURED fields alongside the free-form cause so the
        # frontend can render a localized, parameterized sentence
        # (t('finishInfo.cb.turnBoundaryRebill', {prev, read, gap}))
        # instead of substring-translating English prose with dynamic
        # numbers (which can never match a static phrase table). The
        # string cause is RETAINED for back-compat with any persisted
        # row / consumer that renders the value verbatim; no backend
        # consumer reads the string value (replay/aggregate key off the
        # dict KEY via classify_verdict), so the extra keys are inert
        # there.
        return {
            'turn_boundary_rebill': _boundary_cause,
            'prev_read': int(report.previous_turn_read),
            'read': int(report.cache_read),
            'gap_s': round(float(report.elapsed or 0), 1),
        }

    # Build the most specific cause we can (pure; see _resolve_break_cause).
    #
    # NOTE: "cache contention" between different conversations is NOT a
    # real phenomenon. A/B tested 2026-04-10: per-round cache_read is
    # identical between solo and interleaved modes (±0.0%). Anthropic
    # cache is keyed on exact prefix bytes — different conversations
    # have different keys and CANNOT evict each other.
    cause_str = _resolve_break_cause(
        client_changes=report.client_changes,
        prefix_mutation_break=report.prefix_mutation_break,
        elapsed=report.elapsed, cache_read=report.cache_read,
        cache_write=report.cache_write,
        prefix_mutated=report.prefix_mutated,
        prefix_culprits=report.prefix_culprits,
        wire_proven_identical=report.wire_proven_identical,
        namespace_switch=report.namespace_switch,
        namespace_verified_same=report.namespace_verified_same,
        ttl_window_s=report.ttl_window_s,
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
    if report.usage is not None:
        _cold_gap = report.usage.get('_prev_cold_write_gap_s')
    if _cold_gap is None:
        _prev_was_cold = (report.previous_write >= _MIN_CACHE_MISS_TOKENS
                          and report.previous_read < report.previous_write)
        if _prev_was_cold and report.elapsed > 0:
            _cold_gap = report.elapsed
    _within_settle_window = False
    if _cold_gap is not None:
        try:
            from lib.llm_dispatch.cache_settle import settle_cold_window_ms
            _within_settle_window = (
                float(_cold_gap) < (settle_cold_window_ms() / 1000.0))
        except Exception as _cse:
            logger.debug('[CacheTrack] cold-window lookup failed: %s', _cse)
    if (_within_settle_window and report.wire_proven_identical
            and not report.namespace_switch and not report.client_changes
            and not report.prefix_mutation_break):
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
            '%.1fs, gap=%.1fs) — prior cold write not yet visible.%s',
            report.conv_id[:8], report.call_count, report.cache_write, report.cache_read,
            report.previous_read, float(_cold_gap), report.elapsed,
            _split_note(report))
        # Structured sibling fields for the parameterized frontend
        # rendering (see the turn_boundary_rebill note above): the
        # string cause stays for back-compat, the numbers let the zh/en
        # UI interpolate instead of substring-matching dynamic digits.
        return {
            'cache_write_unsettled': _unsettled_cause,
            'prev_read': int(report.previous_read),
            'read': int(report.cache_read),
            'gap_s': round(float(report.elapsed or 0), 1),
            'cold_gap_s': round(float(_cold_gap), 1),
        }

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
    if (report.namespace_switch and report.wire_proven_identical
            and not report.client_changes and not report.prefix_mutation_break):
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ CACHE NAMESPACE SWITCH BREAK: '
            'cache_write=%d cache_read=%d (prev read=%d, gap=%.1fs) — '
            'byte-identical prefix routed to a different cache namespace. '
            'Cause: %s',
            report.conv_id[:8], report.call_count, report.cache_write, report.cache_read,
            report.previous_read, report.elapsed, cause_str)
        return {'cache_namespace_switch': cause_str}

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
    if ('<mid-out-of-window>' in (report.wire_culprits or [])
            and not report.client_changes):
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ MID-ANCHOR OUT OF WINDOW: '
            'cache_write=%d cache_read=%d (prev read=%d, gap=%.1fs) — '
            'mid stepping-stone drifted past the ~20-block lookback; '
            'tail could not extend the prior entry → whole prefix past '
            'the mid re-billed on a byte-identical body. Cause: %s',
            report.conv_id[:8], report.call_count, report.cache_write, report.cache_read,
            report.previous_read, report.elapsed, cause_str)
        return {'cache_mid_out_of_window': cause_str}

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
    if report.prefix_mutation_break and not report.client_changes:
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ PREFIX MUTATION BREAK: '
            'cache_write=%d cache_read=%d (prev read=%d, gap=%.1fs) — '
            'cached prefix bytes changed between turns. Cause: %s',
            report.conv_id[:8], report.call_count, report.cache_write, report.cache_read,
            report.previous_read, report.elapsed, cause_str,
        )
        return {'prefix_mutation': cause_str}

    if report.no_reuse and not report.api_break:
        # The expensive, previously-undetected pattern: wrote a fresh
        # large prefix and read nothing back. WARN so each occurrence
        # is greppable in error.log for debugging.
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ NO CACHE REUSE: '
            'cache_write=%d cache_read=0 (prev prefix=%d tokens, '
            'gap=%.1fs). Cause: %s%s',
            report.conv_id[:8], report.call_count, report.cache_write,
            report.previous_prefix_tokens, report.elapsed, cause_str,
            _split_note(report),
        )
        if report.client_changes:
            return report.client_changes
        return {'no_cache_reuse': cause_str}

    if report.partial_no_reuse and not report.api_break and not report.no_reuse:
        # Big write repeated while cache_read fell short of the prefix the
        # previous round left cached — the conversation body is being
        # re-billed uncached. WARN so each occurrence is greppable.
        logger.warning(
            '[CacheTrack] conv=%s call=%d ⚠ PREFIX RE-WRITTEN: '
            'cache_write=%d cache_read=%d of %d cached-prefix tokens '
            '(prev write=%d read=%d, gap=%.1fs) — body re-billed '
            'uncached. Cause: %s%s',
            report.conv_id[:8], report.call_count, report.cache_write,
            report.cache_read, report.previous_prefix_tokens,
            report.previous_write, report.previous_read, report.elapsed,
            cause_str, _split_note(report),
        )
        if report.client_changes:
            return report.client_changes
        return {'no_cache_reuse': cause_str}

    # api_break path: cache_read dropped from a prior high value.
    if report.client_changes:
        logger.info(
            '[CacheBreak] conv=%s call=%d CONFIRMED cache break: %s. '
            'cache_read: %d → %d tokens (gap=%.1fs)',
            report.conv_id[:8], report.call_count, cause_str,
            report.previous_read, report.cache_read, report.elapsed,
        )
        return report.client_changes
    logger.info(
        '[CacheTrack] conv=%s call=%d cache_read dropped: %d → %d '
        '(gap=%.1fs, %s)%s',
        report.conv_id[:8], report.call_count,
        report.previous_read, report.cache_read, report.elapsed, cause_str,
        _split_note(report),
    )
    return {'server_side': (
        'Provider-reported cache read decreased; server causality is '
        'not established. ' + cause_str)}

