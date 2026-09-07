"""Create durable compaction archives and emit their single live marker.

Schema lifecycle and conversation-delete cascading belong to the sidecar.
This module only assembles archive facts and emits the corresponding task
event.
"""

from lib.log import get_logger

logger = get_logger(__name__)

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

    archive_id = get_conversation_store().archive_transcript(
        conv_id, messages,
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
                conv_id, keep, user_id=user_id)
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
                reason=(reason or '')[:300],
                snapshotKind='pre_compaction_transcript',
                ts=int(time.time()),
            ))
        except Exception as e_ev:
            logger.debug('[Compact] compaction SSE emit failed: %s', e_ev)
    return archive_id
