"""Layer 3 — emergency reactive compaction on API context-length rejection.

Triggered when the upstream API returns either:

  * HTTP 400 "prompt is too long" — token count exceeds the model's
    advertised context window.
  * HTTP 413 "Request Entity Too Large" — raw body bytes exceed the
    gateway's ``client_max_body_size`` regardless of token count
    (almost always large base64 image_url blocks).

This package is a FACADE: the import path
``lib.tasks_pkg.compaction._reactive`` is unchanged and every symbol that
used to live in the old flat ``_reactive.py`` module is re-exported here so
all existing importers (and the parent ``compaction`` facade) resolve
byte-identically. Implementations live in the cohesive sub-modules:

  * ``_measure``   — ``_estimate_wire_bytes`` (independent wire-byte metric).
  * ``_strip``     — ``_strip_images_aggressive`` (Phase 0) +
    ``_truncate_largest_message`` (Phase 0.5).
  * ``_headtrunc`` — ``_head_truncate`` (Phase 4, last-resort).
  * this ``__init__`` — ``reactive_compact`` (the orchestrating entry point).

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
  3. Phase 0.5 in-place truncate of the largest text message
     (``_truncate_largest_message``) — handles the single-fat-message
     overflow that whole-message dropping cannot.
  4. Phase 1 aggressive ``micro_compact``.
  5. Phase 2 cooldown reset.
  6. Phase 3 ``force_compact_if_needed(_compaction_skip_archive=True)``.
  7. Phase 4 wire-byte head truncate (defence-in-depth).

Steps 1+2 must come BEFORE step 5, and step 5 MUST carry the skip flag.
Otherwise the viewer gets two 'reactive' archive rows on the same 413.
"""

from lib.log import get_logger
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _cooldown_lock,
    _SINGLE_RESULT_HARD_CEILING_CHARS,
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

from lib.tasks_pkg.compaction._reactive._measure import (  # noqa: F401
    _estimate_wire_bytes,
)
from lib.tasks_pkg.compaction._reactive._strip import (  # noqa: F401
    _strip_images_aggressive,
    _truncate_largest_message,
)
from lib.tasks_pkg.compaction._reactive._headtrunc import (  # noqa: F401
    _head_truncate,
)

logger = get_logger(__name__)


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
        logger.warning('%s [ReactiveCompact] Pre-snapshot archive failed: %s',
                       pfx, _ar_e, exc_info=True)

    # Phase 0: aggressive image strip if wire OR token budget over limit.
    # messages is unchanged since the snapshot above (archive is read-only),
    # so reuse it rather than re-walking the whole list a second time.
    tokens_before = tokens_before_snap
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

    # Phase 0.5: in-place truncate the single largest text message.
    # When ONE tool result carries the whole overflow (e.g. a binary file
    # decoded as ~1.7MB of text — conv mqgfkmxy, 2026-06), dropping whole
    # messages can't help: the offending message sits in the protected tail
    # and the byte target is hit before it's reached. Shrinking WITHIN the
    # worst message is the only thing that removes the overflow. Runs every
    # reactive pass (not just over_wire) since token-overflow has the same
    # single-fat-message shape.
    _t_idx, _t_freed = _truncate_largest_message(
        messages, ceiling_chars=_SINGLE_RESULT_HARD_CEILING_CHARS)
    if _t_idx >= 0:
        logger.warning('%s [ReactiveCompact] Phase 0.5 in-place truncate freed '
                       '~%d chars from message idx=%d', pfx, _t_freed, _t_idx)

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


__all__ = [
    'reactive_compact',
    '_estimate_wire_bytes',
    '_strip_images_aggressive',
    '_truncate_largest_message',
    '_head_truncate',
]
