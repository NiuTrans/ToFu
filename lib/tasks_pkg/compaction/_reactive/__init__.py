"""Layer 3 — emergency compaction for a request that cannot be sent safely.

Triggered when the upstream API returns either:

  * HTTP 400 "prompt is too long" — token count exceeds the model's
    advertised context window.
  * HTTP 413 "Request Entity Too Large" — raw body bytes exceed the
    gateway's ``client_max_body_size`` regardless of token count
    (almost always large base64 image_url blocks).
  * Local request-memory admission — the body is valid, but its temporary
    serialisation copies do not fit the currently available cgroup headroom.

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

Live/transient tasks archive the original request, then attempt atomic L2 only.
They never strip images, clip messages, micro-compact, or head-truncate here.
A failed/declined summary closes the pre-archive without a summary or post
snapshot. Local-memory rejection refuses without allocating a summary request.
The explicit non-live utility path retains deterministic reduction below.

Utility ordering invariant (memory: ``compaction-viewer-architecture``):

  1. Early ``_archive_transcript(trigger='reactive')`` snapshot.
  2. Phase 0 image-strip via ``_strip_images_aggressive``.
  3. Phase 0.5 in-place truncate of the largest text message
     (``_truncate_largest_message``) — handles the single-fat-message
     overflow that whole-message dropping cannot.
  4. Phase 1 aggressive ``micro_compact``.
  5. Phase 2 cooldown reset.
  6. Phase 3 ``force_compact_if_needed(_compaction_skip_archive=True)`` for
     provider size rejections; local-memory recovery skips the summary LLM.
  7. Phase 4 wire-byte head truncate (defence-in-depth).

Steps 1+2 must come BEFORE step 5, and step 5 MUST carry the skip flag.
Otherwise the viewer gets two 'reactive' archive rows on the same 413.

Back-fill contract (fix for the "→ 0" viewer artifact): the step-1
pre-snapshot row is written BEFORE any compaction runs, so its
``tokens_after`` / ``msgs_after`` are 0 placeholders.  Step 6 adopts that
row via ``_compaction_archive_id`` so the inner post-summary UPDATE
(``update_archive_summary`` + ``compaction_done``) fills it in; when the
summary path declines to compact, step 7's fallback back-fill at the end
of ``reactive_compact`` writes the final counts itself. Further Phase 4
trimming refreshes the accepted row using metadata-only repository access,
preserving its summary/receipt and saving a detached final projection.
"""

from copy import deepcopy

from lib.log import get_logger
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _cooldown_lock,
    _SINGLE_RESULT_HARD_CEILING_CHARS,
    _summary_cooldowns,
    _WIRE_BYTE_SOFT_LIMIT,
    _WIRE_IMAGE_KEEP_TAIL,
)


def _task_owner(task):
    from lib.tasks_pkg.manager import task_user_id

    return task_user_id(task)
from lib.tasks_pkg.compaction._layer1 import micro_compact
from lib.tasks_pkg.compaction._layer2 import force_compact_if_needed
from lib.tasks_pkg.compaction._tokens import (
    _estimate_total_tokens,
    _get_context_limit,
    _parse_reported_token_count,
    _usable_context,
)
from lib.tasks_pkg.compaction._receipt import build_compaction_receipt

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
                     *, error_text: str | None = None,
                     byte_target: int | None = None) -> bool:
    """Emergency compaction when a request cannot be sent safely.

    Handles two orthogonal failure modes:

      1. Upstream "prompt too long" (HTTP 400, tokens > context window).
      2. Gateway HTTP 413 "Request Entity Too Large" — raw body bytes
         exceed openresty's ``client_max_body_size`` regardless of token
         count.  Almost always caused by large base64 image_url blocks.
      3. Local memory headroom — live/transient tasks refuse unchanged;
         explicit utility callers may request LLM-free byte reduction.

    Returns True if compaction was performed, False otherwise.
    """
    from lib.tasks_pkg.compaction._handoff import has_open_tool_batch
    if (task and task.get('aborted')) or has_open_tool_batch(messages):
        return False
    from lib.tasks_pkg.compaction._summary_budget import bounded_summary_task
    preserve_source = bounded_summary_task(task)
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    conv_id = compaction_owner_key(task)
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
    stripped_images = 0
    truncated_chars = 0
    dropped_messages = 0
    logger.warning('%s [ReactiveCompact] Emergency compaction triggered for conv=%s '
                   '(request rejected or locally unsafe; '
                   'reported_tokens=%s wire_bytes=%.1fMB)',
                   pfx, conv_id[:8] if conv_id else '?',
                   f'{reported_tokens:,}' if reported_tokens else '?',
                   wire_before / 1048576)

    # ── Proactive archival of the RAW pre-reactive context ──
    if byte_target is not None:
        _pre_reason = (
            f'local memory pressure: target {byte_target / 1048576:.1f} MB')
    elif reported_tokens:
        _pre_reason = f'prompt too long: {reported_tokens:,} tokens'
    elif wire_before > _WIRE_BYTE_SOFT_LIMIT:
        _pre_reason = f'request body too large: {wire_before / 1048576:.1f} MB'
    else:
        _pre_reason = 'API rejected request as too long'
    round_num = int((task.get('round_num') if task else 0) or 0)
    archive_id = None
    summary_meta: dict = {}

    def cancelled(*, adopted: bool = False) -> bool:
        if not (task and task.get('aborted')):
            return False
        if not adopted:
            from lib.tasks_pkg.compaction._archive import cancel_compaction_archive
            cancel_compaction_archive(
                archive_id, messages, task, trigger='reactive', round_num=round_num,
                tokens_before=tokens_before_snap, msgs_before=msgs_before_snap,
                summary_usage=summary_meta.get('summary_usage'),
                summary_duration_ms=summary_meta.get('summary_duration_ms', 0))
        return True

    try:
        archive_id = _archive_transcript(
            conv_id, messages,
            user_id=_task_owner(task),
            trigger='reactive',
            task=task,
            round_num=round_num,
            tokens_before=tokens_before_snap,
            msgs_before=msgs_before_snap,
            reason=_pre_reason,
            emit_event=True,
        )
    except Exception as _ar_e:
        logger.warning('%s [ReactiveCompact] Pre-snapshot archive failed: %s',
                       pfx, _ar_e, exc_info=True)

    if cancelled():
        return False

    # Phase 0: aggressive image strip if wire OR token budget over limit.
    # messages is unchanged since the snapshot above (archive is read-only),
    # so reuse it rather than re-walking the whole list a second time.
    tokens_before = tokens_before_snap
    context_limit_hint = _get_context_limit(task)
    token_over = tokens_before > int(context_limit_hint * 0.95)
    over_wire = wire_before > _WIRE_BYTE_SOFT_LIMIT
    if not preserve_source and (over_wire or token_over):
        trigger = 'wire+tokens' if (over_wire and token_over) else (
            'wire' if over_wire else 'tokens')
        stripped, freed = _strip_images_aggressive(messages,
                                                   keep_tail=_WIRE_IMAGE_KEEP_TAIL)
        stripped_images += max(0, int(stripped or 0))
        if stripped > 0:
            logger.warning('%s [ReactiveCompact] Stripped %d old images '
                           '(~%d bytes freed) trigger=%s tokens=%d/%d '
                           'wire_bytes=%.1fMB (target %.1fMB)',
                           pfx, stripped, freed, trigger,
                           tokens_before, context_limit_hint,
                           _estimate_wire_bytes(messages) / 1048576,
                           _WIRE_BYTE_SOFT_LIMIT / 1048576)

    if cancelled():
        return False

    # Phase 0.5: in-place truncate the single largest text message.
    # When ONE tool result carries the whole overflow (e.g. a binary file
    # decoded as ~1.7MB of text — conv mqgfkmxy, 2026-06), dropping whole
    # messages can't help: the offending message sits in the protected tail
    # and the byte target is hit before it's reached. Shrinking WITHIN the
    # worst message is the only thing that removes the overflow. Runs every
    # reactive pass (not just over_wire) since token-overflow has the same
    # single-fat-message shape.
    if not preserve_source:
        _t_idx, _t_freed = _truncate_largest_message(
            messages, ceiling_chars=_SINGLE_RESULT_HARD_CEILING_CHARS)
        if _t_idx >= 0:
            truncated_chars += max(0, int(_t_freed or 0))
            logger.warning('%s [ReactiveCompact] Phase 0.5 in-place truncate freed '
                           '~%d chars from message idx=%d', pfx, _t_freed, _t_idx)

    if cancelled():
        return False

    # Phase 1: aggressive micro-compact.
    if not preserve_source:
        micro_compact(
            messages, conv_id=conv_id, task=task,
            enable_assistant_compact=True,
            enable_paired_assistant_compact=True,
        )

    if cancelled():
        return False

    # Phase 2: clear cooldown so force_compact can fire.
    with _cooldown_lock:
        _summary_cooldowns.pop(conv_id, None)

    # Phase 3: force compact with a tighter preservation budget. A local
    # memory recovery must not allocate another summary request under the same
    # pressure. Live/transient tasks refuse unchanged; utilities may trim below.
    context_limit = _get_context_limit(task)
    usable = _usable_context(context_limit)
    tight_budget = max(1, int(usable * 0.10))
    if byte_target is not None:
        _r_reason = (
            f'local memory pressure: target {byte_target / 1048576:.1f} MB')
    elif reported_tokens:
        _r_reason = f'prompt too long: {reported_tokens:,} tokens'
    elif wire_before > _WIRE_BYTE_SOFT_LIMIT:
        _r_reason = f'request body too large: {wire_before / 1048576:.1f} MB'
    else:
        _r_reason = 'API rejected request as too long'
    compacted = False
    if byte_target is None:
        compacted = force_compact_if_needed(
            messages, task=task,
            preserve_budget_tokens=tight_budget,
            keep_recent_pairs=2,
            force=True,
            _compaction_trigger='reactive',
            _compaction_reason=_r_reason,
            _compaction_skip_archive=True,  # already archived above
            # …so hand that pre-snapshot row's id down: the inner post-summary
            # UPDATE (update_archive_summary + compaction_done) back-fills THIS
            # row's tokens_after/msgs_after instead of leaving them at 0.
            _compaction_archive_id=archive_id,
            _compaction_round=round_num,
            _result_meta=summary_meta,
        )
    if cancelled(adopted=compacted):
        return False
    # Phase 3's success tail already back-filled + closed out the adopted
    # archive row when it compacted.  Only when it DECLINED (summary
    # empty/refused) does the fallback below need to write the final counts.
    phase3_updated_archive = compacted and archive_id is not None

    # Carry state before final size guards; maintenance must not create a new
    # displayed recovery note, nor hide its bytes from the recovery receipt.
    from lib.tasks_pkg.context_composer.checklist import carry_checklist_through_compaction
    if not preserve_source:
        # Atomic L2 carries accepted state on its candidate. A failed attempt
        # must not add even a checklist carrier to the untouched source.
        carry_checklist_through_compaction(messages, task)

    # Phase 4: wire-byte guard.
    wire_after_phases = _estimate_wire_bytes(messages)
    effective_byte_target = (
        min(_WIRE_BYTE_SOFT_LIMIT, byte_target)
        if byte_target is not None else _WIRE_BYTE_SOFT_LIMIT)
    need_byte_trim = (
        not preserve_source and (over_wire or byte_target is not None)
        and wire_after_phases > effective_byte_target)

    if (not preserve_source and byte_target is None
            and not compacted and not need_byte_trim):
        logger.warning('%s [ReactiveCompact] Force compact did not trigger — '
                       'attempting head truncation (reported=%s)',
                       pfx, f'{reported_tokens:,}' if reported_tokens else '?')
        dropped_messages += max(0, int(_head_truncate(
            messages, task, reported_token_count=reported_tokens) or 0))
        compacted = compacted or dropped_messages > 0

    if need_byte_trim:
        logger.warning('%s [ReactiveCompact] Wire bytes still over limit '
                       '(%.1fMB > %.1fMB) — running byte-aware head truncate',
                       pfx, wire_after_phases / 1048576,
                       effective_byte_target / 1048576)
        dropped_messages += max(0, int(_head_truncate(
            messages, task, byte_target=effective_byte_target) or 0))
        compacted = compacted or dropped_messages > 0

    tokens_after = _estimate_total_tokens(messages)
    wire_after = _estimate_wire_bytes(messages)
    if not preserve_source:
        compacted = compacted or wire_after < wire_before

    # ── Archive back-fill (final recovery projection) ──
    #   The pre-snapshot row was written with tokens_after=0 / msgs_after=0
    #   placeholders.  Phase 3's success tail fills them when it compacts;
    #   when it declined (source preserved or utility reduction), that UPDATE never
    #   fired and the row kept showing the misleading "→ 0" in the
    #   viewer.  Write the final counts here (summary stays '' — a
    #   head-truncate produces none) and close out the live marker with a
    #   compaction_done so the chip leaves its in_progress state.
    #   Successful Phase 3 may also need a final update after Phase 4. Read
    #   only its accepted metadata, never hydrate the source transcript.
    if archive_id is not None and (
            not phase3_updated_archive or dropped_messages > 0):
        receipt = build_compaction_receipt(
            trigger='reactive',
            status='completed' if compacted else 'failed',
            strategy=('preserve_source' if preserve_source
                      else 'deterministic_recovery'),
            implementation=('atomic_summary_recovery' if preserve_source
                            else 'bounded_recovery_pipeline'),
            mode=('local_memory' if byte_target is not None
                  else 'provider_rejection'),
            continuation_format='none',
            summary_generated=False,
            summary_usage=summary_meta.get('summary_usage'),
            summary_duration_ms=summary_meta.get('summary_duration_ms', 0),
            summary_rejected=bool(summary_meta.get('summaryRejected')),
            summary_rejection_reason=summary_meta.get('summaryRejectionReason', ''),
            stripped_images=stripped_images,
            truncated_chars=truncated_chars,
            dropped_messages=dropped_messages,
            wire_bytes_before=wire_before,
            wire_bytes_after=wire_after,
            outcome_reason=(
                ('local_memory_pressure' if byte_target is not None else
                 summary_meta.get('summaryFailureReason')
                 or summary_meta.get('reason') or 'summary_not_adopted')
                if preserve_source else _pre_reason),
        )
        summary = ''
        final_archive_updated = False
        try:
            from lib.agent_core.store import get_conversation_store
            store = get_conversation_store()
            if phase3_updated_archive:
                # Match the create/prior-receipt target: the invocation key is
                # working-state identity, not the scoped parent authorization link.
                archive_scope = (task or {}).get('_archiveScope', '')
                archive_conv_id = task.get('convId') if archive_scope else conv_id
                scoped = {'archive_scope': archive_scope} if archive_scope else {}
                accepted = store.get_compaction_archive(
                    archive_conv_id, archive_id, user_id=_task_owner(task),
                    include_messages=False, include_post_messages=False, **scoped)
                accepted = (accepted or {}).get('archive')
                if not isinstance(accepted, dict) or not isinstance(
                        accepted.get('receipt'), dict):
                    raise ValueError('accepted compaction metadata unavailable')
                summary = accepted.get('summary') or ''
                recovery = receipt.get('recovery', {})
                receipt = deepcopy(accepted['receipt'])
                receipt['recovery'] = recovery
            store.update_archive_summary(
                archive_id, summary, int(tokens_after), len(messages),
                user_id=_task_owner(task), receipt=receipt,
                archive_scope=(task or {}).get('_archiveScope', ''),
                post_messages=deepcopy(messages) if compacted else None)
        except Exception as _bf_e:
            logger.debug('%s [ReactiveCompact] archive back-fill failed: %s',
                         pfx, _bf_e)
        else:
            final_archive_updated = True
        if task is not None and final_archive_updated:
            try:
                from lib.agent_core.events import EventType, build_event
                from lib.tasks_pkg.manager import append_event
                _red_pct = (1 - tokens_after / max(1, tokens_before_snap)) * 100
                append_event(task, build_event(
                    EventType.COMPACTION_DONE,
                    archiveId=str(archive_id),
                    convId=conv_id,
                    trigger='reactive',
                    tokensBefore=int(tokens_before_snap),
                    tokensAfter=int(tokens_after),
                    tokenCountKind='estimated',
                    msgsBefore=int(msgs_before_snap),
                    msgsAfter=len(messages),
                    reductionPct=round(_red_pct, 1),
                    roundNum=round_num,
                    receipt=receipt,
                ))
            except Exception as _ev_e:
                logger.debug('%s [ReactiveCompact] compaction_done emit '
                             'failed: %s', pfx, _ev_e)

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
