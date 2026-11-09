"""Advance one cache observation and stage its existing durable checkpoints.

Called under the detector's cache lock after all previous-round comparisons.
The guarded sinks retain task settlement authority; only unscoped callers may
write through _persist. No new state, store, worker or durability boundary.
"""
from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.cache_tracking._policy import EDITABLE_TAIL_COUNT
from lib.tasks_pkg.cache_tracking._state import COLD_ROUND_TOKEN_FLOOR, COLD_STREAK_GUARD_OPEN

logger = get_logger(__name__)


def advance_cache_observation(
    prev, client_evidence, wire_evidence, *, scope, observed_read,
    conv_id, user_id, model, usage, now, cache_read, cache_write, durable,
    cache_prefix_hwm_sink, last_turn_cache_read_sink,
) -> None:
    """Update the baseline, stage checkpoints, then accumulate usage totals."""
    _wire_available = wire_evidence.fingerprint is not None
    _wire_static = wire_evidence.static_prefix
    _cur_wire_system = wire_evidence.system
    _cur_wire_markers = wire_evidence.markers
    _cur_wire_bytes = wire_evidence.message_bytes
    _cur_wire_field_bytes = wire_evidence.field_bytes
    _cur_wire_region = wire_evidence.region
    _cur_wire_routing = wire_evidence.routing
    if scope is not None and observed_read is not None:
        scope.rounds_seen += 1
    prev.system_hash = client_evidence.system_hash
    prev.tools_hash = client_evidence.tools_hash
    prev.per_tool_hashes = client_evidence.per_tool_hashes
    prev.prefix_field_count = max(0, client_evidence.message_count - EDITABLE_TAIL_COUNT)
    prev.prefix_field_hashes = client_evidence.prefix_field_hashes
    if _wire_available:
        prev.wire_fp = wire_evidence.fingerprint
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
    prev.message_count = client_evidence.message_count
    if observed_read is not None:
        prev.last_cache_read_tokens = observed_read
    prev.last_cache_write_tokens = cache_write
    prev.last_update_time = now
    prev.call_count += 1
    # ── Cold-streak bookkeeping (reporting only since 2026-09-15) ──
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
    #    the whole sent request [0, msg_count) is a cached conversation
    #    fact (the same count get_cache_prefix_count protects). Persist it
    #    as a monotonic high-water mark so a future turn on a fresh
    #    thread/process/replica restores the guard floor instead of
    #    collapsing to 0 and rewriting the still-cached prefix.
    #    Best-effort, monotonic, DB-write only on genuine growth.
    if durable and (cache_read > 1000 or cache_write > 1000):
        _durable_boundary = max(0, client_evidence.message_count)
        if _durable_boundary > 0:
            _boundary_staged = False
            if cache_prefix_hwm_sink is not None:
                try:
                    cache_prefix_hwm_sink(_durable_boundary)
                    _boundary_staged = True
                except Exception as _pe:
                    logger.debug(
                        '[CacheTrack] cache-prefix sink failed conv=%s: %s',
                        conv_id, _pe,
                    )
            if not _boundary_staged and scope is None:
                try:
                    from lib.tasks_pkg.cache_tracking._persist import (
                        advance_persisted_boundary)
                    advance_persisted_boundary(
                        conv_id, _durable_boundary, user_id=user_id)
                except Exception as _pe:
                    logger.debug('[CacheTrack] advance_persisted_boundary '
                                 'failed conv=%s: %s', conv_id, _pe)
    # ── Persist the DURABLE round-1 read baseline (survives restart /
    #    stale-eviction / replica switch). This round's cache_read becomes
    #    the NEXT turn's cross-turn baseline; when that next turn starts on a
    #    cold process (no live sibling), get_prev_turn_cache_read falls back
    #    to this persisted value so a collapsed round-1 buckets honestly as
    #    turn_boundary_rebill instead of being laundered into no_break.
    #    Zero/floor reads are observations too; omission is not a zero.
    #    Scoped tasks only stage into the guarded task-result checkpoint.
    if durable and observed_read is not None:
        _last_read_staged = False
        if last_turn_cache_read_sink is not None:
            try:
                last_turn_cache_read_sink(observed_read)
                _last_read_staged = True
            except Exception as _pe:
                logger.debug(
                    '[CacheTrack] last-read sink failed conv=%s: %s',
                    conv_id, _pe,
                )
        if not _last_read_staged and scope is None:
            try:
                from lib.tasks_pkg.cache_tracking._persist import (
                    write_last_turn_cache_read)
                write_last_turn_cache_read(
                    conv_id, observed_read, user_id=user_id)
            except Exception as _pe:
                logger.debug('[CacheTrack] write_last_turn_cache_read '
                             'failed conv=%s: %s', conv_id, _pe)
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

