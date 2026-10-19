"""Create durable compaction archives and emit their single live marker.

Schema lifecycle and conversation-delete cascading belong to the sidecar.
This module only assembles archive facts and emits the corresponding task
event.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def summary_dispatch_calls(usage) -> list[dict]:
    """Bound observed call identities only; selection is not dispatch proof."""
    if not isinstance(usage, dict):
        return []
    calls = usage.get('_summaryCalls')
    calls = calls if isinstance(calls, list) else [{'usage': usage}]
    result = []
    for call in calls[:16]:
        observed = call.get('usage') if isinstance(call, dict) else None
        metadata = observed.get('_dispatch') if isinstance(observed, dict) else None
        metadata = metadata if isinstance(metadata, dict) else {}
        result.append({key: value[:160] for key, value in (
            ('model', metadata.get('model')),
            ('providerId', metadata.get('provider_id')),
        ) if isinstance(value, str) and value})
    return result


def cancel_compaction_archive(archive_id, messages: list, task: dict, *,
                              trigger: str, round_num: int,
                              tokens_before: int, msgs_before: int,
                              summary_usage: dict | None = None,
                              summary_duration_ms: int = 0) -> None:
    """Close an already-created audit row, never adopt a cancelled projection.

    Cancellation before archival needs no row. Cancellation during archive or
    later preparation must not leave the viewer pending forever. Counts cover
    the current working list (earlier recovery steps may have run); no accepted
    post snapshot or generated-summary claim is manufactured. Best effort like
    other archive telemetry; current task ownership still gates every write.
    """
    if archive_id is None:
        return
    from lib.agent_core.store import get_conversation_store
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    from lib.tasks_pkg.manager import task_user_id

    tokens_after = _estimate_total_tokens(messages)
    receipt = build_compaction_receipt(
        trigger=trigger, status='cancelled', strategy='cancelled',
        implementation='owner_cancellation', continuation_format='none',
        summary_usage=summary_usage, summary_duration_ms=summary_duration_ms,
        outcome_reason='owner_cancelled_before_adoption')
    try:
        get_conversation_store().update_archive_summary(
            archive_id, '', tokens_after, len(messages),
            user_id=task_user_id(task), archive_scope=task.get('_archiveScope', ''),
            receipt=receipt, post_messages=None)
    except Exception as exc:
        logger.debug('[Compact] cancellation archive finalization failed: %s', exc)
        return
    try:
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import append_event
        append_event(task, build_event(
            EventType.COMPACTION_DONE, archiveId=str(archive_id),
            convId=compaction_owner_key(task), trigger=trigger,
            tokensBefore=int(tokens_before), tokensAfter=tokens_after,
            tokenCountKind='estimated', msgsBefore=int(msgs_before),
            msgsAfter=len(messages), reductionPct=round(
                (1 - tokens_after / max(1, tokens_before)) * 100, 1),
            roundNum=int(round_num), receipt=receipt,
            summaryDispatchCalls=summary_dispatch_calls(summary_usage)))
    except Exception as exc:
        logger.debug('[Compact] cancellation archive event failed: %s', exc)

def fail_compaction_archive(task, archive_id, meta, cause=None):
    """Expose an uncommitted candidate and stop without adopting lossy context."""
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
    from lib.agent_core.storage_errors import StorageError
    receipt = build_compaction_receipt(
        trigger=meta.get('trigger') or 'window', status='failed',
        strategy='unchanged', implementation='archive_commit_failed',
        outcome_reason='compaction_archive_unavailable')
    append_event(task, build_event(EventType.COMPACTION_DONE,
        archiveId=str(archive_id or ''), convId=task.get('convId', ''),
        trigger=meta.get('trigger') or 'window',
        roundNum=int(meta.get('round_num') or 0),
        tokensBefore=int(meta.get('tokens_before') or 0),
        tokensAfter=int(meta.get('tokens_before') or 0), tokenCountKind='estimated',
        msgsBefore=int(meta.get('msgs_before') or 0),
        msgsAfter=int(meta.get('msgs_before') or 0), reductionPct=0.0, receipt=receipt))
    raise StorageError('database_unavailable',
        'Could not save compacted context; original context retained. Retry to continue.',
        retryable=True, operation_id='compaction_archive.commit') from cause


def _human_size(byte_count: int) -> str:
    """Format a byte/char count as a human-readable string.

    Local copy (also exported from _tokens) so _archive.py imports
    nothing else from the package.  Single-purpose 6-line helper —
    duplication cost is negligible compared to the import-graph
    benefit of keeping _archive a strict leaf-of-_constants.
    """
    if byte_count < 1024:
        return f'{byte_count}B'
    elif byte_count < 1024 * 1024:
        return f'{byte_count / 1024:.1f}KB'
    else:
        return f'{byte_count / (1024 * 1024):.1f}MB'


def _archive_transcript(conv_id: str, messages: list, summary: str = '',
                        *,
                        user_id,
                        trigger: str = 'force',
                        task: dict | None = None,
                        round_num: int = 0,
                        tokens_before: int = 0,
                        tokens_after: int = 0,
                        msgs_before: int = 0,
                        msgs_after: int = 0,
                        reason: str = '',
                        receipt: dict | None = None,
                        emit_event: bool = True) -> str | None:
    """Archive the full message list to DB before compaction and optionally
    emit a ``compaction`` SSE event so the frontend can surface an inline
    marker the user can click to inspect the pre-compaction context.

    Args:
        conv_id: Conversation id — used as archive key and in the SSE event.
        messages: Full pre-compaction message list (deep-copyable).
        summary: Human-readable summary string (may be empty at write time).
        trigger: What fired this archival — one of
            ``'working_set'`` (automatic economic threshold),
            ``'window'`` (automatic model-window safety threshold),
            ``'force'`` (explicit forced compaction),
            ``'reactive'`` (emergency after API 400/413), or
            ``'manual'`` (caller-injected).
        task: Live task dict — used to extract task_id and model for the row.
        round_num: Zero-based round number (for cross-reference with tool rounds).
        tokens_before / tokens_after: Heuristic token counts around the compaction.
        msgs_before / msgs_after: Message-count pair.
        reason: Short diagnostic string shown in the UI badge
            (e.g. "prompt too long: 1,310,784 tokens").
        receipt: Bounded structured result metadata. When omitted, a pending
            receipt is stored until the successful/fallback path finalizes it.
        emit_event: Whether to append a ``compaction`` event to task['events'].

    Returns:
        The row id of the newly-inserted archive, or ``None`` on failure.
    """
    import time

    from lib.agent_core.store import get_conversation_store
    from lib.tasks_pkg.compaction._receipt import pending_compaction_receipt
    task_id = (task.get('id', '') if task else '') or ''
    model = ''
    if task:
        try:
            model = (task.get('model')
                     or (task.get('config', {}) or {}).get('model')
                     or '')
        except Exception as _m_e:
            logger.debug('[Compact] model extract failed: %s', _m_e)
            model = ''

    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    conv_id = compaction_owner_key(task, conv_id)
    # Storage validates the parent authorization link AND child scope. Reuse
    # this exact target for retention, never prune a parent/sibling namespace.
    archive_scope = (task or {}).get('_archiveScope', '')
    archive_conv_id = task.get('convId') if archive_scope else conv_id
    scoped = {'archive_scope': archive_scope} if archive_scope else {}

    archive_id = get_conversation_store().archive_transcript(
        archive_conv_id, messages, **scoped,
        user_id=user_id, summary=summary,
        trigger=trigger, task_id=task_id, round_num=int(round_num or 0),
        model=model,
        tokens_before=int(tokens_before or 0), tokens_after=int(tokens_after or 0),
        msgs_before=int(msgs_before or 0), msgs_after=int(msgs_after or 0),
        reason=reason or '',
        receipt=(receipt if receipt is not None
                 else pending_compaction_receipt(trigger)),
    )
    if archive_id is None:
        if task and task.get('_turnId') and not task.get('aborted'):
            fail_compaction_archive(task, None, {'trigger': trigger, 'round_num': round_num})
        return None
    logger.info('[Compact] Transcript archived conv=%s  id=%s  trigger=%s  '
                'messages=%d  tokens=%d→%d',
                conv_id[:8] if conv_id else '?',
                archive_id, trigger,
                len(messages),
                int(tokens_before or 0), int(tokens_after or 0))

    # RETENTION (GC-on-insert) — every compaction inserts a full transcript
    #   row, so without pruning the table grows unbounded on a long-lived
    #   conversation.  Keep the newest N per conv (ring buffer).  Best-effort:
    #   a prune failure must never break the archival/compaction path.
    try:
        from lib.tasks_pkg.compaction._constants import archive_retention
        keep = archive_retention()
        if keep and conv_id:
            get_conversation_store().prune_archives(
                archive_conv_id, keep, user_id=user_id, **scoped)
    except Exception as e_gc:
        logger.debug('[Compact] archive prune skipped conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e_gc)

    # Emit SSE event so the frontend can render an inline marker.  We guard
    # against missing task / archive_id so the archival path never breaks
    # if the live task dict isn't wired through.
    if emit_event and task is not None and archive_id is not None:
        try:
            from lib.agent_core.events import EventType, build_event
            from lib.tasks_pkg.manager import append_event
            append_event(task, build_event(
                EventType.COMPACTION,
                archiveId=archive_id,
                convId=conv_id,
                trigger=trigger,
                roundNum=int(round_num or 0),
                tokensBefore=int(tokens_before or 0),
                tokensAfter=int(tokens_after or 0),
                tokenCountKind='estimated',
                msgsBefore=int(msgs_before or 0),
                msgsAfter=int(msgs_after or 0),
                model=model,

                taskModel=model,
                **({'receipt': receipt} if receipt is not None else {}),
                reason=(reason or '')[:300],
                snapshotKind='pre_compaction_transcript',
                ts=int(time.time()),
            ))
        except Exception as e_ev:
            logger.debug('[Compact] compaction SSE emit failed: %s', e_ev)
    return archive_id
