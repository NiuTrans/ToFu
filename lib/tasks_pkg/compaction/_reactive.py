"""Layer 3 — emergency reactive compaction on API context-length rejection.

Triggered when the upstream API returns either:

  * HTTP 400 "prompt is too long" — token count exceeds the model's
    advertised context window.
  * HTTP 413 "Request Entity Too Large" — raw body bytes exceed the
    gateway's ``client_max_body_size`` regardless of token count
    (almost always large base64 image_url blocks).

Public surface:
  * ``reactive_compact``         — main entry point called from
    ``llm_fallback._llm_call_with_fallback`` on those errors.
  * ``_estimate_wire_bytes``     — independent wire-byte safety metric.
  * ``_strip_images_aggressive`` — Phase 0 OOM protection (memory:
    ``micro-compact-image-strip-bug-fix``).
  * ``_head_truncate``           — last-resort truncate by tokens or bytes.

Critical ordering invariant (memory: ``compaction-viewer-architecture``):

  1. Early ``_archive_transcript(trigger='reactive')`` snapshot.
  2. Phase 0 image-strip via ``_strip_images_aggressive``.
  3. Phase 1 aggressive ``micro_compact``.
  4. Phase 2 cooldown reset.
  5. Phase 3 ``force_compact_if_needed(_compaction_skip_archive=True)``.
  6. Phase 4 wire-byte head truncate (defence-in-depth).

Steps 1+2 must come BEFORE step 5, and step 5 MUST carry the skip flag.
Otherwise the viewer gets two 'reactive' archive rows on the same 413.
"""

import json

from lib.log import get_logger
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _cooldown_lock,
    _summary_cooldowns,
    _WIRE_BYTE_SOFT_LIMIT,
    _WIRE_IMAGE_KEEP_TAIL,
)
from lib.tasks_pkg.compaction._layer1 import micro_compact
from lib.tasks_pkg.compaction._layer2 import force_compact_if_needed
from lib.tasks_pkg.compaction._tokens import (
    _estimate_total_tokens,
    _get_context_limit,
    _parse_reported_token_count,
    _usable_context,
)

logger = get_logger(__name__)


def _estimate_wire_bytes(messages: list) -> int:
    """Rough estimate of the serialized JSON body size (UTF-8 bytes).

    Used as an independent safety metric orthogonal to upstream token
    count, because the gateway's HTTP 413 cap measures bytes, not tokens.
    """
    try:
        return len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
    except Exception as e:
        logger.debug('[WireSize] json.dumps failed (%s) — falling back to char estimate', e)
        total = 0
        for m in messages:
            try:
                total += len(str(m))
            except Exception as e:
                logger.debug('[WireSize] str(message) failed: %s', e)
        return total


def _strip_images_aggressive(messages: list,
                             keep_tail: int = _WIRE_IMAGE_KEEP_TAIL,
                             ) -> tuple[int, int]:
    """Strip all ``image_url`` blocks except the most-recent ``keep_tail``.

    Used by reactive_compact when a 413 has already fired — at that point
    the normal hot-tail protection is overridden because the gateway has
    proven the payload is too big. Each stripped image is replaced with a
    short textual placeholder so the model knows something was there.

    Returns (stripped_count, bytes_freed_estimate).
    """
    image_positions: list[tuple[int, int]] = []
    for mi, msg in enumerate(messages):
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for bi, blk in enumerate(content):
            if isinstance(blk, dict) and blk.get('type') == 'image_url':
                image_positions.append((mi, bi))

    if len(image_positions) <= keep_tail:
        return 0, 0

    to_strip = image_positions[:-keep_tail] if keep_tail > 0 else image_positions
    stripped = 0
    bytes_freed = 0

    by_msg: dict[int, list[int]] = {}
    for mi, bi in to_strip:
        by_msg.setdefault(mi, []).append(bi)

    for mi, bi_list in by_msg.items():
        content = messages[mi].get('content')
        if not isinstance(content, list):
            continue
        for bi in sorted(bi_list, reverse=True):
            if bi >= len(content):
                continue
            blk = content[bi]
            if not (isinstance(blk, dict) and blk.get('type') == 'image_url'):
                continue
            url = blk.get('image_url', {}).get('url', '')
            bytes_freed += len(url)
            content[bi] = {
                'type': 'text',
                'text': '[image removed during emergency compaction — ask again if needed]',
            }
            stripped += 1

    return stripped, bytes_freed


def reactive_compact(messages: list, task: dict | None = None,
                     *, error_text: str | None = None) -> bool:
    """Emergency compaction triggered when the API rejects a request as too long.

    Handles two orthogonal failure modes:

      1. Upstream "prompt too long" (HTTP 400, tokens > context window).
      2. Gateway HTTP 413 "Request Entity Too Large" — raw body bytes
         exceed openresty's ``client_max_body_size`` regardless of token
         count.  Almost always caused by large base64 image_url blocks.

    Returns True if compaction was performed, False otherwise.
    """
    conv_id = task.get('convId', '') if task else ''
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    reported_tokens = _parse_reported_token_count(error_text or '')

    if conv_id:
        try:
            from lib.token_counter import invalidate as _uc_invalidate
            _uc_invalidate(conv_id)
        except Exception as e:
            logger.debug('[Compact] usage_cache invalidate failed: %s', e)

    wire_before = _estimate_wire_bytes(messages)
    tokens_before_snap = _estimate_total_tokens(messages)
    msgs_before_snap = len(messages)
    logger.warning('%s [ReactiveCompact] Emergency compaction triggered for conv=%s '
                   '(API rejected request as too long; '
                   'reported_tokens=%s wire_bytes=%.1fMB)',
                   pfx, conv_id[:8] if conv_id else '?',
                   f'{reported_tokens:,}' if reported_tokens else '?',
                   wire_before / 1048576)

    # ── Proactive archival of the RAW pre-reactive context ──
    if reported_tokens:
        _pre_reason = f'prompt too long: {reported_tokens:,} tokens'
    elif wire_before > _WIRE_BYTE_SOFT_LIMIT:
        _pre_reason = f'request body too large: {wire_before / 1048576:.1f} MB'
    else:
        _pre_reason = 'API rejected request as too long'
    try:
        _archive_transcript(
            conv_id, messages,
            trigger='reactive',
            task=task,
            round_num=int((task.get('round_num') if task else 0) or 0),
            tokens_before=tokens_before_snap,
            msgs_before=msgs_before_snap,
            reason=_pre_reason,
            emit_event=True,
        )
    except Exception as _ar_e:
        logger.debug('%s [ReactiveCompact] Pre-snapshot archive failed: %s',
                     pfx, _ar_e)

    # Phase 0: aggressive image strip if wire OR token budget over limit.
    tokens_before = _estimate_total_tokens(messages)
    context_limit_hint = _get_context_limit(task)
    token_over = tokens_before > int(context_limit_hint * 0.95)
    over_wire = wire_before > _WIRE_BYTE_SOFT_LIMIT
    if over_wire or token_over:
        trigger = 'wire+tokens' if (over_wire and token_over) else (
            'wire' if over_wire else 'tokens')
        stripped, freed = _strip_images_aggressive(messages,
                                                   keep_tail=_WIRE_IMAGE_KEEP_TAIL)
        if stripped > 0:
            logger.warning('%s [ReactiveCompact] Stripped %d old images '
                           '(~%d bytes freed) trigger=%s tokens=%d/%d '
                           'wire_bytes=%.1fMB (target %.1fMB)',
                           pfx, stripped, freed, trigger,
                           tokens_before, context_limit_hint,
                           _estimate_wire_bytes(messages) / 1048576,
                           _WIRE_BYTE_SOFT_LIMIT / 1048576)

    # Phase 1: aggressive micro-compact.
    micro_compact(
        messages, conv_id=conv_id, task=task,
        enable_assistant_compact=True,
        enable_paired_assistant_compact=True,
    )

    # Phase 2: clear cooldown so force_compact can fire.
    with _cooldown_lock:
        _summary_cooldowns.pop(conv_id, None)

    # Phase 3: force compact with a tighter preservation budget.
    context_limit = _get_context_limit(task)
    usable = _usable_context(context_limit)
    tight_budget = max(1, int(usable * 0.10))
    if reported_tokens:
        _r_reason = f'prompt too long: {reported_tokens:,} tokens'
    elif wire_before > _WIRE_BYTE_SOFT_LIMIT:
        _r_reason = f'request body too large: {wire_before / 1048576:.1f} MB'
    else:
        _r_reason = 'API rejected request as too long'
    compacted = force_compact_if_needed(
        messages, task=task,
        preserve_budget_tokens=tight_budget,
        keep_recent_pairs=2,
        force=True,
        _compaction_trigger='reactive',
        _compaction_reason=_r_reason,
        _compaction_skip_archive=True,  # already archived above
    )

    # Phase 4: wire-byte guard.
    wire_after_phases = _estimate_wire_bytes(messages)
    need_byte_trim = (over_wire and wire_after_phases > _WIRE_BYTE_SOFT_LIMIT)

    if not compacted and not need_byte_trim:
        logger.warning('%s [ReactiveCompact] Force compact did not trigger — '
                       'attempting head truncation (reported=%s)',
                       pfx, f'{reported_tokens:,}' if reported_tokens else '?')
        _head_truncate(messages, task, reported_token_count=reported_tokens)
        compacted = True

    if need_byte_trim:
        logger.warning('%s [ReactiveCompact] Wire bytes still over limit '
                       '(%.1fMB > %.1fMB) — running byte-aware head truncate',
                       pfx, wire_after_phases / 1048576,
                       _WIRE_BYTE_SOFT_LIMIT / 1048576)
        _head_truncate(messages, task, byte_target=_WIRE_BYTE_SOFT_LIMIT)
        compacted = True

    tokens_after = _estimate_total_tokens(messages)
    wire_after = _estimate_wire_bytes(messages)
    logger.info('%s [ReactiveCompact] Complete — %d messages, ~%d tokens, '
                '%.1fMB wire (was %.1fMB)',
                pfx, len(messages), tokens_after,
                wire_after / 1048576, wire_before / 1048576)

    return compacted


def _head_truncate(messages: list, task: dict | None = None,
                   byte_target: int | None = None,
                   reported_token_count: int | None = None):
    """Last-resort head truncation: drop the oldest non-system messages."""
    system_end = 0
    for i, msg in enumerate(messages):
        if msg.get('role') == 'system':
            system_end = i + 1
        else:
            break

    if byte_target is not None:
        dropped = 0
        while (_estimate_wire_bytes(messages) > byte_target
               and len(messages) > system_end + 4):
            messages.pop(system_end)
            dropped += 1
        if dropped:
            logger.warning('[HeadTruncate] Dropped %d oldest messages by byte target '
                           '(wire now %.1fMB, target %.1fMB)',
                           dropped,
                           _estimate_wire_bytes(messages) / 1048576,
                           byte_target / 1048576)
        return

    context_limit = _get_context_limit(task)
    target = int(context_limit * 0.60)

    if reported_token_count and reported_token_count > target:
        est_before = max(1, _estimate_total_tokens(messages))
        frac_to_drop = (reported_token_count - target) / reported_token_count
        heuristic_target = int(est_before * (1 - frac_to_drop))
        logger.warning('[HeadTruncate] Using upstream-reported count: '
                       'reported=%d target=%d → shed %.0f%% '
                       '(heuristic %d → %d)',
                       reported_token_count, target, frac_to_drop * 100,
                       est_before, heuristic_target)
        target_measure = heuristic_target
    else:
        target_measure = target

    dropped = 0
    while _estimate_total_tokens(messages) > target_measure and len(messages) > system_end + 4:
        messages.pop(system_end)
        dropped += 1

    if dropped:
        logger.warning('[HeadTruncate] Dropped %d oldest messages to fit context '
                       '(tokens now ~%d, target ~%d, reported_api=%s)',
                       dropped, _estimate_total_tokens(messages), target_measure,
                       f'{reported_token_count:,}' if reported_token_count else 'n/a')
