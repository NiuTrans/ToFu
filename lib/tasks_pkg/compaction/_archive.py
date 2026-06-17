"""DB persistence + the single SSE-emit boundary for compaction events.

Three responsibilities:

  * Lazy, idempotent ``_ensure_compaction_tables`` / ``_init_tables``
    safety net for installs whose DDL migration hasn't run yet.
  * ``_archive_transcript`` — writes a row to ``transcript_archive`` AND
    emits the ``'compaction'`` SSE event so the frontend can render an
    inline marker.  This is the **only** module that should fire that
    event; the post-summary ``'compaction_done'`` follow-up is fired
    from ``execute_compact_tool`` in ``_layer2.py`` which then
    UPDATE-s the row this module wrote.
  * ``cleanup_compaction_data`` — drops archive rows + persisted
    tool-result files when a conversation is deleted.

Imports nothing from sibling sub-modules except ``_constants``.  This
keeps the SSE-emit boundary self-contained and easy to audit.
"""

import os

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import _PERSIST_DIR_BASE

logger = get_logger(__name__)


def _ensure_compaction_tables():
    """Create compaction-related tables if they don't already exist.

    NOTE: the canonical schema (including all metadata columns) lives in
    ``lib/database/_schema_sqlite.py`` and ``_schema_pg.py``. This helper
    only ensures the base table exists as a safety net for older installs
    that haven't run the DDL migration yet — the metadata columns are
    added by the init_db migration block, not here.
    """
    from lib.agent_core.store import get_conversation_store
    get_conversation_store().ensure_compaction_schema()


def _init_tables():
    """Lazy, one-time table creation.  Thread-safe via double-checked lock.

    The latch (``_tables_initialized`` + ``_tables_lock``) lives in
    ``_constants.py`` so the package and its sub-modules share one
    state.  We mirror the True flip back onto the package namespace
    so the existing hot-reload contract
    (``lib.tasks_pkg.compaction._tables_initialized``) keeps working.
    """
    import sys

    from lib.tasks_pkg.compaction import _constants as _c
    if _c._tables_initialized:
        return
    with _c._tables_lock:
        if _c._tables_initialized:
            return
        try:
            _ensure_compaction_tables()
            _c._tables_initialized = True
            pkg = sys.modules.get('lib.tasks_pkg.compaction')
            if pkg is not None:
                pkg._tables_initialized = True
            logger.debug('[Compaction] DB tables initialized')
        except Exception as e:
            logger.error('[Compaction] Failed to initialize DB tables: %s',
                         e, exc_info=True)


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
                        trigger: str = 'force',
                        task: dict | None = None,
                        round_num: int = 0,
                        tokens_before: int = 0,
                        tokens_after: int = 0,
                        msgs_before: int = 0,
                        msgs_after: int = 0,
                        reason: str = '',
                        emit_event: bool = True) -> int | None:
    """Archive the full message list to DB before compaction and optionally
    emit a ``compaction`` SSE event so the frontend can surface an inline
    marker the user can click to inspect the pre-compaction context.

    Args:
        conv_id: Conversation id — used as archive key and in the SSE event.
        messages: Full pre-compaction message list (deep-copyable).
        summary: Human-readable summary string (may be empty at write time).
        trigger: What fired this archival — one of
            ``'force'`` (L3 force-compact threshold),
            ``'reactive'`` (emergency after API 400/413), or
            ``'manual'`` (caller-injected).
        task: Live task dict — used to extract task_id and model for the row.
        round_num: Zero-based round number (for cross-reference with tool rounds).
        tokens_before / tokens_after: Heuristic token counts around the compaction.
        msgs_before / msgs_after: Message-count pair.
        reason: Short diagnostic string shown in the UI badge
            (e.g. "prompt too long: 1,310,784 tokens").
        emit_event: Whether to append a ``compaction`` event to task['events'].

    Returns:
        The row id of the newly-inserted archive, or ``None`` on failure.
    """
    import time

    _init_tables()
    from lib.agent_core.store import get_conversation_store
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
        trigger=trigger, task_id=task_id, round_num=int(round_num or 0),
        model=model,
        tokens_before=int(tokens_before or 0), tokens_after=int(tokens_after or 0),
        msgs_before=int(msgs_before or 0), msgs_after=int(msgs_after or 0),
        reason=reason or '',
    )
    if archive_id is None:
        return None
    logger.info('[Compact] Transcript archived conv=%s  id=%s  trigger=%s  '
                'messages=%d  tokens=%d→%d',
                conv_id[:8] if conv_id else '?',
                archive_id, trigger,
                len(messages),
                int(tokens_before or 0), int(tokens_after or 0))

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
                msgsBefore=int(msgs_before or 0),
                msgsAfter=int(msgs_after or 0),
                model=model,
                reason=(reason or '')[:300],
                ts=int(time.time()),
            ))
        except Exception as e_ev:
            logger.debug('[Compact] compaction SSE emit failed: %s', e_ev)
    return archive_id


def cleanup_compaction_data(conv_id: str):
    """Delete all compaction artifacts for a conversation.

    Cleans up both database records and persisted tool-result files on disk.
    """
    import shutil

    from lib.agent_core.store import get_conversation_store
    try:
        get_conversation_store().delete_archives(conv_id)
        logger.debug('[Compaction] Cleaned up DB artifacts for conv=%s',
                     conv_id[:8] if conv_id else '?')
    except Exception as e:
        logger.debug('[Compaction] Cleanup DB artifacts failed for conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e, exc_info=True)

    # Clean up persisted tool-result files for this conversation
    if conv_id:
        dir_name = conv_id[:12]
        persist_dir = os.path.join(_PERSIST_DIR_BASE, dir_name)
        if os.path.isdir(persist_dir):
            try:
                shutil.rmtree(persist_dir)
                logger.debug('[Compaction] Cleaned up persisted files: %s', persist_dir)
            except Exception as e:
                logger.debug('[Compaction] Failed to clean persisted files %s: %s',
                             persist_dir, e)
