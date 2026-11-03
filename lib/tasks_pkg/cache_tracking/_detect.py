"""Coordinate cache diagnosis under one invocation-scoped state lock.

Entry point: detect_cache_break compares source and provider evidence, applies
_policy, advances the baseline/checkpoints through _advance, and emits exactly
one round record. _wire owns provider comparisons; _report owns confirmed-break
diagnostics. Existing policy imports remain explicit re-exports for callers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import time
from typing import Any

from lib.cost import normalize_usage, observed_cache_read, split_input_tokens
from lib.log import get_logger
from lib.model_info import is_claude
from lib.tasks_pkg.cache_tracking._state import (
    CacheState,
    _cache_lock,
    _cache_states,
    _state_key,
    _current_invocation,
    get_prev_turn_cache_read,
)
from lib.tasks_pkg.cache_tracking._hashing import (
    _diff_prefix_fields,
    _diff_tool_hashes,
    _first_changed_prefix_index,
    _hash_prefix_fields,
    _hash_system_prompt,
    _hash_tools,
    _hash_tools_per_tool,
    _is_current_prefix_field_snapshot,
)
from lib.tasks_pkg.cache_tracking._roi import _emit_l2_roi

from lib.tasks_pkg.cache_tracking._policy import (
    _MIN_CACHE_MISS_TOKENS as _MIN_CACHE_MISS_TOKENS,
    EDITABLE_TAIL_COUNT as EDITABLE_TAIL_COUNT,
    _MIN_NO_REUSE_TOKENS as _MIN_NO_REUSE_TOKENS,
    BUCKET_NAMESPACE as BUCKET_NAMESPACE,
    BUCKET_TURN_BOUNDARY as BUCKET_TURN_BOUNDARY,
    BUCKET_TTL_FLIP as BUCKET_TTL_FLIP,
    BUCKET_TTL_EXPIRY as BUCKET_TTL_EXPIRY,
    _TTL_EXPIRY_CAUSE as _TTL_EXPIRY_CAUSE,
    BUCKET_BREAKPOINT_LOST as BUCKET_BREAKPOINT_LOST,
    BUCKET_MID_WINDOW as BUCKET_MID_WINDOW,
    BUCKET_WRITE_UNSETTLED as BUCKET_WRITE_UNSETTLED,
    BUCKET_UPSTREAM as BUCKET_UPSTREAM,
    BUCKET_BODY_CHANGE as BUCKET_BODY_CHANGE,
    BUCKET_NO_BREAK as BUCKET_NO_BREAK,
    BUCKET_INDETERMINATE as BUCKET_INDETERMINATE,
    BUCKET_UNCACHED as BUCKET_UNCACHED,
    BUCKET_OTHER as BUCKET_OTHER,
    _classify_break as _classify_break,
    _resolve_break_cause as _resolve_break_cause,
    classify_verdict as classify_verdict,
    uncached_round,
)
from lib.tasks_pkg.cache_tracking._wire import (
    _wire_evidence_from_usage, _detect_namespace_switch,
    compare_wire_evidence,
)
from lib.tasks_pkg.cache_tracking import _health
from lib.tasks_pkg.cache_tracking._advance import advance_cache_observation
from lib.tasks_pkg.cache_tracking._report import BreakReport, report_confirmed_break

logger = get_logger(__name__)


@dataclass(frozen=True)
class _ClientChangeEvidence:
    """Fallback hashes and named client changes for one cache round."""

    system_hash: str
    message_count: int
    tools_hash: str
    per_tool_hashes: dict
    prefix_field_hashes: list
    client_changes: dict
    prefix_mutated: bool
    prefix_culprits: list
    first_changed_prefix_index: int


def _detect_client_changes(prev, messages, tools, model, wire_evidence, *,
                           conv_id):
    """Compute source-level change evidence once, outside break policy.

    Provider-bound fingerprints remain authoritative when available. These
    hashes are the diagnostic fallback and the next-round baseline; keeping
    their construction together prevents the classifier from re-walking a
    long history for each field.
    """
    system_hash = _hash_system_prompt(messages)
    message_count = len(messages)
    current_wire_tools_hash = _validated_wire_region_hash(
        wire_evidence.region, 'tools')
    previous_wire_tools_hash = _validated_wire_region_hash(
        prev.wire_region, 'tools')
    wire_tools_comparable = bool(
        current_wire_tools_hash and previous_wire_tools_hash)
    wire_tools_changed = (
        wire_tools_comparable
        and current_wire_tools_hash != previous_wire_tools_hash)
    if prev.call_count == 0 or not wire_tools_comparable or wire_tools_changed:
        tools_hash = _hash_tools(tools)
        per_tool_hashes = _hash_tools_per_tool(tools)
    else:
        tools_hash = prev.tools_hash
        per_tool_hashes = prev.per_tool_hashes

    previous_prefix_count = (
        prev.prefix_field_count if prev.call_count > 0 else 0)
    new_prefix_count = max(0, message_count - EDITABLE_TAIL_COUNT)
    hash_count = max(previous_prefix_count, new_prefix_count)
    all_prefix_hashes = _hash_prefix_fields(messages, hash_count)
    current_previous_range = (
        all_prefix_hashes if previous_prefix_count == hash_count
        else all_prefix_hashes[:previous_prefix_count])
    prefix_field_hashes = (
        all_prefix_hashes if new_prefix_count == hash_count
        else all_prefix_hashes[:new_prefix_count])

    client_changes = {}
    prefix_mutated = False
    prefix_culprits = []
    first_changed_prefix_index = -1
    if prev.call_count > 0:
        first_changed_prefix_index = _first_changed_prefix_index(
            prev.prefix_field_hashes, current_previous_range)
        if system_hash != prev.system_hash:
            client_changes['system_prompt'] = 'changed'
        if tools_hash != prev.tools_hash:
            tool_diffs = _diff_tool_hashes(
                prev.per_tool_hashes, per_tool_hashes)
            client_changes['tools'] = (
                f'changed: [{", ".join(tool_diffs)}]' if tool_diffs
                else 'changed (ordering or meta)')
        if model != prev.model:
            client_changes['model'] = f'{prev.model} → {model}'
        if message_count < prev.message_count:
            client_changes['message_count'] = (
                f'{prev.message_count} → {message_count} (compacted)')
        if (previous_prefix_count > 0
                and prev.prefix_field_hashes
                and _is_current_prefix_field_snapshot(
                    prev.prefix_field_hashes)
                and current_previous_range != prev.prefix_field_hashes
                and not prev.compaction_pending):
            prefix_mutated = True
            prefix_culprits = _diff_prefix_fields(
                prev.prefix_field_hashes, current_previous_range)
            try:
                _intended_idx = int(getattr(
                    prev, 'expected_rewrite_index', -1))
            except (TypeError, ValueError):
                _intended_idx = -1
            if (_intended_idx >= 0
                    and first_changed_prefix_index >= _intended_idx):
                logger.debug(
                    '[CacheTrack] conv=%s call=%d expected prefix rewrite '
                    'at api-form index %d (intended %d)',
                    conv_id[:8], prev.call_count + 1,
                    first_changed_prefix_index, _intended_idx)
            else:
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ PREFIX MUTATION DETECTED: '
                    'messages[0:%d] content hash changed without compaction. '
                    'This will cause a cache miss. changed=[%s]%s',
                    conv_id[:8], prev.call_count + 1, previous_prefix_count,
                    ', '.join(prefix_culprits) or '?',
                    _prefix_mutation_content_fingerprints(
                        messages, prefix_culprits))

    return _ClientChangeEvidence(
        system_hash=system_hash,
        message_count=message_count,
        tools_hash=tools_hash,
        per_tool_hashes=per_tool_hashes,
        prefix_field_hashes=prefix_field_hashes,
        client_changes=client_changes,
        prefix_mutated=prefix_mutated,
        prefix_culprits=prefix_culprits,
        first_changed_prefix_index=first_changed_prefix_index,
    )


def _prefix_mutation_content_fingerprints(
        messages: list, culprits: list, max_report: int = 3) -> str:
    """Bounded byte evidence for each mutated prefix field.

    A ``msg[i].field`` culprit names WHAT changed but not the differing bytes,
    so a recurring tool_result content mutation (2026-09-24: 776/~830 of
    prefix breaks) cannot be attributed to a writer from the log alone. This
    appends, per culprit, the current field's length + head/tail snippet so
    successive breaks on the same conv/index reveal whether the content is
    growing, being re-truncated, or re-serialized — the writer's signature.
    Bounded to ``max_report`` entries and short snippets; never raises.
    """
    try:
        parts = []
        for culprit in culprits:
            if len(parts) >= max_report:
                break
            match = re.match(r'msg\[(\d+)\]\.([A-Za-z_]+)', str(culprit))
            if not match:
                continue
            index = int(match.group(1))
            field = match.group(2)
            if index >= len(messages):
                continue
            value = messages[index].get(field) if isinstance(
                messages[index], dict) else None
            text = _prefix_field_content_text(value)
            if text is None:
                continue
            head = text[:40].replace('\n', '\\n')
            tail = text[-40:].replace('\n', '\\n') if len(text) > 40 else ''
            parts.append(
                f'msg[{index}].{field}(len={len(text)},'
                f'head={head!r},tail={tail!r})')
        return ' evidence=[' + '; '.join(parts) + ']' if parts else ''
    except Exception as _e:  # noqa: BLE001 - diagnostic must never break detection
        logger.debug('[CacheTrack] prefix-mutation fingerprint failed: %s', _e)
        return ''


def _prefix_field_content_text(value) -> str | None:
    """Best-effort plain-text view of one prefix field for fingerprinting."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for block in value:
            if isinstance(block, dict):
                chunks.append(str(block.get('text', block.get('content', ''))))
            else:
                chunks.append(str(block))
        return ''.join(chunks)
    return str(value)


def _validated_wire_region_hash(region: Any, field: str) -> str:
    """Return one trusted 16-hex true-byte region digest, or empty."""
    if not isinstance(region, dict):
        return ''
    value = region.get(field)
    if (not isinstance(value, str) or len(value) != 16
            or any(character not in '0123456789abcdef'
                   for character in value)):
        return ''
    return value


def _emit_round_record(conv_id, call_num, verdict, *, ns_switch, ttl_flip,
                       breakpoint_lost, body_identical, namespace_verified,
                       cache_read, cache_write, elapsed, culprits=None,
                       model='', wire_available=False, wire_changed=False,
                       read_collapsed=False, lane='', tool_count=None,
                       wire_tool_count=None, wire_tool_name_bytes=None,
                       cold_group='', affinity=''):
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
        # 'conversation' (root agent), 'isolated' (child/transient invocation)
        # or 'unscoped'. One route can serve both lanes very differently, so
        # cache health is only comparable within a lane.
        if lane:
            rec['lane'] = lane
        # Tool definitions on the request. A gateway may serve one route from
        # caches that behave differently by request shape, so read share is
        # also compared across tool-surface sizes.
        if tool_count is not None:
            rec['tools'] = int(tool_count)
        # The provider-visible surface actually sent, after Tool Search,
        # budgets and protocol translation; ``tools`` above is the task's
        # logical catalog. On the Sankuai Claude route reuse followed the
        # joined sent names, not the catalog (docs/LLM_COST_OPTIMIZATION.md,
        # "Gateway tool-name budget").
        if wire_tool_count is not None:
            rec['wire_tools'] = int(wire_tool_count)
        if wire_tool_name_bytes is not None:
            rec['wire_tool_name_bytes'] = int(wire_tool_name_bytes)
        if cold_group:
            rec['cold_group'] = str(cold_group)
        # Wire slots that carried the lineage's cache-affinity key
        # (lib/llm/cache_affinity.py), e.g. 'Mt-Session-Id'. Reports split
        # read-back by carrier: a lineage that still reads an OLDER round's
        # entry while carrying a key shows that the hop ignored the key.
        if affinity:
            rec['affinity'] = str(affinity)
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
        _health.read_share_monitor.observe(rec)
    except Exception as _rre:
        logger.debug('[CacheTrack] round-record emit failed: %s', _rre)


def _emit_pending_l2_roi(prev, conv_id, cache_write, cache_read, usage, *,
                         user_id, model, now):
    """Pair the stashed L2 'saved' half with this round's OBSERVED re-billed
    half and emit ONE structured ROI metric, then clear the stash.

    THIS round's prompt was rebuilt from the summarized prefix, so its
    cache_write is the OBSERVED re-billed half (outcome='paired'). On
    OpenAI-compat routes cache_write is never metered — the rebuild is billed
    as uncached input instead, so record what this round actually paid at the
    base rate (split_input_tokens) as the honest rebuild-cost proxy. A
    call_count of 0 means the pairing round is this task thread's R1: its
    cold read also carries the per-turn recomposition break, so flag the
    boundary for the analysis.
    """
    _roi = prev.pending_l2_roi
    prev.pending_l2_roi = None
    _uncached_next = None
    _input_next = None
    if usage:
        try:
            from lib.cost import split_input_tokens as _split_in
            _uncached_next, _input_next = _split_in(usage)
        except Exception as _e:
            logger.debug('[CacheTrack] L2 ROI cost split failed: %s', _e)
    _emit_l2_roi(conv_id, _roi, user_id=user_id,
                 cache_write_rebilled=int(cache_write),
                 cache_read_next=int(cache_read),
                 cache_uncached_next=_uncached_next,
                 input_next=_input_next,
                 turn_boundary=(prev.call_count == 0),
                 model=model or '', now=now)

def _cache_observation_elapsed(prev, now, scope, key, user_id, conv_id):
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
            if (scope is None and _sk[0] == user_id
                    and _sk[1] == conv_id and _sk != key
                    and _sst.last_update_time > _latest_sib):
                _latest_sib = _sst.last_update_time
        elapsed = now - _latest_sib if _latest_sib else 0

    return elapsed


def _round_groups(scope, tools, model, call_count, wire_evidence) -> dict:
    """Record fields naming this round's request groups, keyed as the finisher's.

    ``cold_group`` is read before the round is observed, so a round is judged
    against the split its predecessors established.
    """
    lane = ('unscoped' if scope is None
            else 'conversation' if scope.lane == 'conversation'
            else 'isolated')
    tool_count = len(tools) if isinstance(tools, (list, tuple)) else None
    cold_group = _health.read_share_monitor.cold_group({
        'call': call_count, 'model': model, 'lane': lane, 'tools': tool_count,
        'wire_tool_name_bytes': wire_evidence.tool_name_bytes})
    routing = wire_evidence.routing if isinstance(wire_evidence.routing, dict) else {}
    return {'lane': lane, 'tool_count': tool_count,
            'wire_tool_count': wire_evidence.tool_count,
            'wire_tool_name_bytes': wire_evidence.tool_name_bytes,
            'cold_group': cold_group,
            'affinity': str(routing.get('affinity') or '').split('@', 1)[0]}


def _cache_verdict_finisher(
    conv_id, prev, _ns_switch, _ttl_flip,
    _breakpoint_lost, _wire_proven_identical, _ns_verified_same, cache_read,
    cache_write, elapsed, _wire_culprits, model,
    _wire_available, _wire_prefix_changed, _read_collapsed, client_changes,
    prefix_mutation_break, _prefix_culprits, _wire_tools_verified, _wire_system_verified,
    turn_boundary_break, _cross_turn_prev_read, prev_cache_read, lane='',
    tool_count=None, wire_tool_count=None, wire_tool_name_bytes=None,
    cold_group='', affinity='',
):
    """Freeze this round's evidence for consistent telemetry and returned verdicts."""
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
            read_collapsed=_read_collapsed, lane=lane, tool_count=tool_count,
            wire_tool_count=wire_tool_count,
            wire_tool_name_bytes=wire_tool_name_bytes, cold_group=cold_group,
            affinity=affinity)
        if v:
            # Keep legacy keys and the primary ledger bucket, but never
            # discard independent evidence merely because another reason
            # won the classifier's display precedence.
            v = dict(v)
            for code, detail in client_changes.items():
                v.setdefault(code, detail)
            if prefix_mutation_break:
                culprits = _wire_culprits if _wire_available else _prefix_culprits
                v.setdefault('prefix_mutation',
                             'prefix changed: ' + ', '.join(culprits or []))
            if _ns_switch:
                v.setdefault('cache_namespace_switch',
                             'Observed routing fields changed: ' + ', '.join(_ns_switch)
                             + '; this does not establish server cache causality.')
            if _ttl_flip:
                v.setdefault('cache_ttl_flip', 'Provider-bound cache marker TTL changed.')
            if _breakpoint_lost:
                v.setdefault('cache_breakpoint_lost', 'Provider-bound cache marker regressed.')
            reasons = []
            for code, detail in v.items():
                if code in ('prev_read', 'read', 'gap_s', 'cold_write_gap_s'):
                    continue
                evidence = 'inferred'
                if code in ('tools', 'system_prompt', 'model', 'message_count'):
                    evidence = 'source_only'
                if ((code == 'tools' and _wire_tools_verified)
                        or (code == 'system_prompt' and _wire_system_verified)
                        or (code == 'prefix_mutation' and _wire_prefix_changed)
                        or code in ('cache_namespace_switch', 'cache_ttl_flip',
                                    'cache_breakpoint_lost')):
                    evidence = 'wire_verified'
                elif code == 'prefix_mutation':
                    evidence = 'source_only'
                elif code == 'turn_boundary_rebill':
                    evidence = 'provider_usage'
                reasons.append({'code': code, 'detail': detail,
                                'evidence': evidence})
            baseline = (_cross_turn_prev_read if turn_boundary_break
                        else prev_cache_read)
            if baseline > cache_read:
                reasons.append({
                    'code': 'cache_read_drop',
                    'detail': 'Provider-reported cache read decreased; '
                              'this alone does not establish the cause.',
                    'evidence': 'provider_usage',
                    'prevRead': int(baseline), 'read': int(cache_read),
                    'readDrop': int(baseline - cache_read),
                })
            v['reasons'] = reasons
        return v
    return _finish


def _report_indeterminate_round(conv_id, prev, *, cache_write, elapsed,
                                was_compaction, advertised_rewrite,
                                expected_rewrite_index) -> dict:
    """Log a zero-read substantial write that no break gate explained and
    return its ``indeterminate`` ledger record."""
    if advertised_rewrite:
        logger.info(
            '[CacheTrack] conv=%s call=%d zero-read write after an '
            'advertised compaction rewrite: cache_write=%d '
            '(gap=%.1fs, advertised api-form index %d) — counted as '
            'indeterminate.',
            conv_id[:8], prev.call_count, cache_write, elapsed,
            expected_rewrite_index)
    else:
        logger.warning(
            '[CacheTrack] conv=%s call=%d INDETERMINATE: cache_write=%d '
            'read=0 (gap=%.1fs, compaction=%s) but no break gate fired '
            '— the spend is real but the cause is unresolved. Counted '
            'as indeterminate, NOT as no_break: a zero-read rebuild '
            'must not be filed alongside genuine cache hits.',
            conv_id[:8], prev.call_count, cache_write, elapsed,
            was_compaction)
    return {
        'indeterminate': (
            'zero read-back on a substantial write, but no break gate '
            'could fire'
            + (' because this round followed a compaction, which is '
               'structurally exempt from every break predicate'
               if was_compaction else '')
            + (' because an advertised bounded compaction rewrite '
               'covered the first prefix change'
               if advertised_rewrite else '')
            + '. The spend is real and counted here; the CAUSE is '
            'unresolved — this round is NOT evidence of a healthy '
            'cache.')}


def _report_uncached_round(conv_id, prev, *, prompt_tokens, markers) -> dict:
    """Log a cacheable Claude round with no cache activity and return its
    ``uncached`` ledger record, naming which side the wire markers implicate."""
    count = markers.get('count') if isinstance(markers, dict) else None
    if count == 0:
        evidence = ('no cache_control marker reached the wire, so the client '
                    'never asked for caching (a client-side breakpoint '
                    'regression)')
    elif isinstance(count, int) and count > 0:
        evidence = (f'{count} cache_control marker(s) were on the wire, so the '
                    'gateway dropped them, does not cache this route, or does '
                    'not report cache usage')
    else:
        evidence = ('wire markers were not captured, so the client and '
                    'provider sides cannot be told apart')
    logger.warning(
        '[CacheTrack] conv=%s call=%d UNCACHED: input=%d cache_read=0 '
        'cache_write=0 — %s. Counted as uncached, NOT as no_break.',
        conv_id[:8], prev.call_count, prompt_tokens, evidence)
    return {'uncached': (
        'the provider reported no cache read and no cache write on a '
        f'cacheable prompt ({prompt_tokens} input tokens), so the whole prompt '
        f'was billed at the uncached input rate: {evidence}.')}


def detect_cache_break(
    conv_id: str,
    messages: list,
    tools: list | None,
    model: str,
    usage: dict | None = None,
    *,
    user_id: int,
    durable: bool = True,

    prev_turn_cache_read: int | None = None,
    cache_prefix_hwm_sink: Callable[[int], None] | None = None,
    last_turn_cache_read_sink: Callable[[int], None] | None = None,
) -> dict[str, Any] | None:
    """Two-phase cache break detection.

    Phase 1: Compare system/tools/model hashes to detect WHAT changed.
    Phase 2: Check API-reported cache_read_tokens to confirm whether
             a break actually occurred.

    Returns a dict describing what changed, or None if no break detected.
    Logs warnings on significant cache breaks for cost diagnostics.

    Provider-bound fingerprints override source-level fallbacks. Compaction
    and advertised bounded rewrites keep their existing exclusions; comparison
    always precedes baseline mutation and guarded checkpoint publication.
    """
    if not conv_id:
        return None

    now = time.time()

    _key = _state_key(conv_id, user_id=user_id)
    # Cross-turn cache_read is consumed ONLY by the new-thread round-1 verdict.
    # Avoid scanning every sibling state (and possibly the durable settings
    # store) on every steady-state provider round. Peek under the shared lock,
    # release it before get_prev_turn_cache_read (which takes the same lock),
    # and skip the lookup entirely once this thread has a completed call. A
    # pre-call compaction placeholder also needs no baseline here because its
    # expected drop is explicitly suppressed by compaction_pending.
    with _cache_lock:
        _current = _cache_states.get(_key)
        _needs_cross_turn_read = (
            _current is None
            or (_current.call_count == 0 and not _current.compaction_pending)
        )
    _scope = _current_invocation(conv_id, user_id)

    durable = durable and (_scope is None or _scope.lane == 'conversation')
    _observed_read = observed_cache_read(usage)
    _is_turn_start = _scope is not None and _scope.rounds_seen == 0
    _cross_turn_prev_read = (
        (prev_turn_cache_read if prev_turn_cache_read is not None
         else get_prev_turn_cache_read(conv_id, user_id=user_id))
        if _needs_cross_turn_read or _is_turn_start else 0
    )

    with _cache_lock:
        prev = _cache_states.get(_key)
        if prev is None:
            prev = CacheState()
            _cache_states[_key] = prev

        _wire_evidence = _wire_evidence_from_usage(usage)

        client_evidence = _detect_client_changes(
            prev, messages, tools, model, _wire_evidence, conv_id=conv_id)
        msg_count = client_evidence.message_count
        client_changes = client_evidence.client_changes
        _prefix_mutated = client_evidence.prefix_mutated
        _prefix_culprits = client_evidence.prefix_culprits
        _api_first_changed = client_evidence.first_changed_prefix_index

        # ── Phase 2: Check API-reported cache stats ──
        cache_read = 0
        cache_write = 0
        if usage:
            _nu = normalize_usage(usage)
            cache_read = _nu['cache_read']
            cache_write = _nu['cache_write']

        prev_cache_read = prev.last_cache_read_tokens

        elapsed = _cache_observation_elapsed(prev, now, _scope, _key, user_id, conv_id)

        # Handle compaction: if compaction happened, a drop in cache_read
        # is expected — don't flag it as a break. Capture the flag BEFORE
        # resetting it; the break/no-reuse guards below must see the value
        # this round had, not the already-cleared False.
        #
        # Bounded in-place rewrites (intended_rewrite_index) do NOT raise
        # compaction_pending. Capture and clear expected_rewrite_index here
        # so token-drop / prefix_mutation gates can treat a first api-form
        # change at or after that index as advertised, while leaving TTL,
        # namespace, and earlier-mutation detectors live.
        _was_compaction = prev.compaction_pending
        try:
            _expected_rewrite_index = int(
                getattr(prev, 'expected_rewrite_index', -1))
        except (TypeError, ValueError):
            _expected_rewrite_index = -1
        if prev.compaction_pending:
            prev.compaction_pending = False
            if cache_read < prev_cache_read:
                logger.debug(
                    '[CacheTrack] conv=%s Expected cache drop after compaction: '
                    '%d → %d tokens',
                    conv_id[:8], prev_cache_read, cache_read)
        if _expected_rewrite_index >= 0:
            prev.expected_rewrite_index = -1
        # Api-form index space (prefix-field hashes), NOT wire_fp positions.
        # Anthropic hoists system, merges same-role turns, and wraps tool
        # results, so a wire first_changed_index is not comparable to
        # notify_compaction(intended_rewrite_index=...).
        _expected_covers = (
            _expected_rewrite_index >= 0
            and _api_first_changed >= _expected_rewrite_index
        )
        # ── Phase-C: complete a pending L2 ROI record ──
        # An L2 force-summary event stashed its 'saved' half via
        # record_l2_compaction. THIS round is the one whose prompt was rebuilt
        # from the summarized prefix, so its cache_write is the tokens re-billed
        # BECAUSE the summary busted the prefix. Pair the two halves and emit
        # ONE structured metric with both sides populated, then clear it.
        if prev.pending_l2_roi is not None:
            _emit_pending_l2_roi(
                prev, conv_id, cache_write, cache_read, usage,
                user_id=user_id, model=model, now=now)

        # ── Authoritative wire-fingerprint prefix diff ──
        # `usage['_wire_fp']` is the post-translation, envelope-agnostic
        # fingerprint of the ACTUAL bytes sent this round (captured in
        # prepare_request — the only point after add_cache_breakpoints AND
        # openai_body_to_anthropic). Diffing it against the previous round's
        # stored fingerprint is GROUND TRUTH for prefix mutation, unlike the
        # fallback field reconstruction above which is blind to the
        # build_body / breakpoint / anthropic-translation transforms. When
        # present it OVERRIDES the reconstruction's verdict:
        #   * identical → no observed local prefix change, NOT proof of a
        #                 server cause or of unobserved upstream transformations.
        #   * differ → identifies a concrete provider-bound field change.
        wire_comparison = compare_wire_evidence(
            prev, _wire_evidence, conv_id=conv_id,
            was_compaction=_was_compaction, expected_covers=_expected_covers,
            expected_rewrite_index=_expected_rewrite_index,
            api_first_changed=_api_first_changed, client_changes=client_changes,
            message_count=msg_count, cache_read=cache_read,
        )
        _wire_available = _wire_evidence.fingerprint is not None
        _cur_wire_routing = _wire_evidence.routing
        _cur_wire_region = _wire_evidence.region
        _wire_comparable = wire_comparison.comparable
        _wire_bytes_comparable = wire_comparison.bytes_comparable
        _wire_prefix_changed = wire_comparison.prefix_changed
        _wire_culprits = wire_comparison.culprits
        _first_changed_idx = wire_comparison.first_changed_index
        _mutation_inside_prior_prefix = wire_comparison.mutation_inside_prior_prefix
        _advertised_rewrite = wire_comparison.advertised_rewrite

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
        _remaining_prefix_floor_miss = (
            _expected_covers
            and _expected_rewrite_index > 0
            and prev_cache_read > _MIN_CACHE_MISS_TOKENS
            and cache_read < _MIN_CACHE_MISS_TOKENS
        )
        if _expected_covers and not _remaining_prefix_floor_miss:
            api_break = False
            no_reuse = False
            partial_no_reuse = False

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
            (prev.call_count == 0 or _is_turn_start)
            and not _was_compaction
            and _cross_turn_prev_read > _MIN_CACHE_MISS_TOKENS
            and cache_read < _cross_turn_prev_read * 0.95
            and (_cross_turn_prev_read - cache_read) >= _MIN_CACHE_MISS_TOKENS
        )

        _wire_tools_verified = bool(
            _validated_wire_region_hash(_cur_wire_region, 'tools')
            and _validated_wire_region_hash(prev.wire_region, 'tools')
            and _validated_wire_region_hash(_cur_wire_region, 'tools')
            != _validated_wire_region_hash(prev.wire_region, 'tools'))
        _wire_system_verified = bool(
            _validated_wire_region_hash(_cur_wire_region, 'system')
            and _validated_wire_region_hash(prev.wire_region, 'system')
            and _validated_wire_region_hash(_cur_wire_region, 'system')
            != _validated_wire_region_hash(prev.wire_region, 'system'))
        # ── Update state (AFTER elapsed computation) ──

        advance_cache_observation(
            prev, client_evidence, _wire_evidence, scope=_scope,
            observed_read=_observed_read, conv_id=conv_id, user_id=user_id,
            model=model, usage=usage, now=now, cache_read=cache_read,
            cache_write=cache_write, durable=durable,
            cache_prefix_hwm_sink=cache_prefix_hwm_sink,
            last_turn_cache_read_sink=last_turn_cache_read_sink,
        )

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
        _wire_proven_identical = (
            _wire_comparable and _wire_bytes_comparable and not _wire_prefix_changed)

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
            and not _expected_covers
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
        _groups = _round_groups(_scope, tools, model, prev.call_count, _wire_evidence)

        _finish = _cache_verdict_finisher(
            conv_id, prev, _ns_switch, _ttl_flip,
            _breakpoint_lost, _wire_proven_identical, _ns_verified_same, cache_read,
            cache_write, elapsed, _wire_culprits, model,
            _wire_available, _wire_prefix_changed, _read_collapsed, client_changes,
            prefix_mutation_break, _prefix_culprits, _wire_tools_verified, _wire_system_verified,
            turn_boundary_break, _cross_turn_prev_read, prev_cache_read,
            **_groups,
        )

        # ── Report ──
        # A cache break is "confirmed" when the API shows: a DROP from a prior
        # high read (api_break), a large fresh write with zero read despite an
        # established prefix (no_reuse), OR a large write repeated round-over-
        # round while the read stays pinned (partial_no_reuse). All three mean
        # we paid to rebuild (part of) the cache instead of reading it back.
        if (api_break or no_reuse or partial_no_reuse or prefix_mutation_break
                or turn_boundary_break):
            report = BreakReport(
                call_count=prev.call_count,
                conv_id=conv_id,
                cache_read=cache_read,
                cache_write=cache_write,
                previous_read=prev_cache_read,
                previous_write=prev_cache_write,
                elapsed=elapsed, ttl_window_s=wire_comparison.ttl_window_s,
                usage=usage,
                client_changes=client_changes,
                prefix_mutation_break=prefix_mutation_break,
                turn_boundary_break=turn_boundary_break,
                previous_turn_read=_cross_turn_prev_read,
                prefix_mutated=_prefix_mutated,
                prefix_culprits=_prefix_culprits,
                wire_proven_identical=_wire_proven_identical,
                namespace_switch=_ns_switch,
                namespace_verified_same=_ns_verified_same,
                wire_culprits=_wire_culprits,
                no_reuse=no_reuse,
                api_break=api_break,
                partial_no_reuse=partial_no_reuse,
                previous_prefix_tokens=_prev_prefix_tokens,
                cold_group=_groups['cold_group'],
            )
            return _finish(report_confirmed_break(report))
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
            return _finish(None, record_as=_report_indeterminate_round(
                conv_id, prev, cache_write=cache_write, elapsed=elapsed,
                was_compaction=_was_compaction,
                advertised_rewrite=_advertised_rewrite,
                expected_rewrite_index=_expected_rewrite_index))
        # A cacheable Claude round with no cache activity at all paid the base
        # input rate for its whole prompt. Like the indeterminate case it is a
        # ledger fact, not a confirmed break: the return value stays None.
        _prompt_tokens = split_input_tokens(usage)[1]
        if uncached_round(claude=is_claude(model), call_count=prev.call_count,
                          cache_read=cache_read, cache_write=cache_write,
                          prompt_tokens=_prompt_tokens):
            return _finish(None, record_as=_report_uncached_round(
                conv_id, prev, prompt_tokens=_prompt_tokens,
                markers=_wire_evidence.markers))
        _finish(None)

    return None
