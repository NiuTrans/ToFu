"""lib/tasks_pkg/persistence_store.py — Default ConversationStore adapter.

This is the **host side** of the agent base's persistence seam (see
``lib.protocols.ConversationStore`` and ``lib.agent_core.store``).  The
reusable agent base never imports ``lib.database`` / ``lib.conversations``
directly; instead it calls the store returned by
``lib.agent_core.store.get_conversation_store()``.  In a normal chatui
deployment that store is the :class:`DefaultConversationStore` defined here,
which is backed by the project's PostgreSQL/SQLite layer.

Because this adapter is the concrete, DB-bound implementation, it lives OUTSIDE
``lib/agent_core/`` (a CORE_MODULES location, which is forbidden from importing
``lib.database``) — exactly mirroring how ``lib.tasks_pkg.manager`` is the
DB-bound implementation behind the ``TaskEventSink`` protocol.

Methods are added here in lock-step with the core call sites migrated behind
the seam (see ``lib/agent_core_manifest.py`` boundary stages).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['DefaultConversationStore']


class DefaultConversationStore:
    """chatui's PostgreSQL/SQLite-backed :class:`~lib.protocols.ConversationStore`.

    Stateless — every method acquires the connection it needs from the
    thread-local pool, so a single shared instance is safe across all worker
    threads.  Satisfies the protocol structurally (no inheritance).
    """

    def release_connection(self) -> None:
        """Release this thread's pooled DB connection(s) back to the pool.

        Delegates to :func:`lib.database.close_thread_db` (no-arg → all
        domains).  Imported lazily so this module stays import-light and the
        DB layer is only touched when the method actually runs.
        """
        from lib.database import close_thread_db
        close_thread_db()

    def load_conversation_messages(self, conv_id):
        """Read a conversation's messages + updated_at from the chat DB.

        ``user_id=1`` is the single-user chatui convention (hidden behind the
        seam).  Returns ``None`` if the row is absent; an empty list with
        whatever ``updated_at`` exists on a parse failure.
        """
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1',
            (conv_id,),
        ).fetchone()
        if not row:
            return None
        raw = row['messages'] if 'messages' in row.keys() else row[0]
        updated_at = (row['updated_at'] if 'updated_at' in row.keys() else row[1]) or 0
        try:
            messages = json.loads(raw or '[]')
            if not isinstance(messages, list):
                messages = []
        except (ValueError, TypeError) as e:
            logger.warning('[Store] load_conversation_messages parse failed conv=%s: %s',
                           conv_id, e)
            messages = []
        return messages, int(updated_at or 0)

    def save_conversation_messages(self, conv_id, messages):
        """Overwrite a conversation's messages (non-CAS) and bump updated_at."""
        import time
        from lib.database import (DOMAIN_CHAT, db_execute_with_retry,
                                  get_thread_db, json_dumps_pg)
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(
            db,
            'UPDATE conversations SET messages=?, updated_at=? '
            'WHERE id=? AND user_id=1',
            (json_dumps_pg(messages), now_ms, conv_id),
        )
        return now_ms

    def cas_update_conversation_messages(self, conv_id, messages, expected_updated_at):
        """Compare-and-swap overwrite guarded by ``updated_at``.

        Returns affected-row count (0 = a concurrent writer won the race).
        """
        import time
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        cur = db.execute(
            'UPDATE conversations SET messages=?, updated_at=? '
            'WHERE id=? AND user_id=1 AND updated_at=?',
            (json_dumps_pg(messages), now_ms, conv_id, expected_updated_at),
        )
        db.commit()
        affected = getattr(cur, 'rowcount', None)
        return affected if affected is not None else 0

    def ensure_compaction_schema(self):
        """Create transcript_archive table + index if absent (safety net)."""
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS transcript_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_ta_conv ON transcript_archive(conv_id);
        ''')

    def archive_transcript(self, conv_id, messages, *, trigger='force',
                           task_id='', round_num=0, model='',
                           tokens_before=0, tokens_after=0,
                           msgs_before=0, msgs_after=0, reason=''):
        """Insert a transcript-archive row; recover and return its id.

        Cross-backend-safe write-then-select to avoid vendor-specific
        RETURNING / lastrowid semantics.  Returns the new id, or None on
        failure.  The store owns serialisation of ``messages``.
        """
        from lib.database import (DOMAIN_CHAT, db_execute_with_retry,
                                  get_thread_db, json_dumps_pg)
        archive_id = None
        try:
            db = get_thread_db(DOMAIN_CHAT)
            messages_json = json_dumps_pg(messages, default=str)
            db_execute_with_retry(db,
                'INSERT INTO transcript_archive '
                '(conv_id, messages_json, summary, trigger, task_id, round_num, '
                ' model, tokens_before, tokens_after, msgs_before, msgs_after, reason) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (conv_id, messages_json, '', trigger, task_id,
                 int(round_num or 0), model,
                 int(tokens_before or 0), int(tokens_after or 0),
                 int(msgs_before or 0), int(msgs_after or 0),
                 (reason or '')[:500]),
            )
            try:
                cur = db.execute(
                    'SELECT id FROM transcript_archive WHERE conv_id=? '
                    'ORDER BY id DESC LIMIT 1',
                    (conv_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    archive_id = int(row[0] if not isinstance(row, dict) else row.get('id'))
            except Exception as e_id:
                logger.debug('[Store] archive id lookup failed: %s', e_id)
        except Exception as e:
            logger.warning('[Store] archive_transcript failed conv=%s: %s',
                           conv_id[:8] if conv_id else '?', e, exc_info=True)
            return None
        return archive_id

    def update_archive_summary(self, archive_id, summary, tokens_after, msgs_after):
        """Fill summary + post-compaction counts on an archive row."""
        from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db,
            'UPDATE transcript_archive SET summary=?, '
            'tokens_after=?, msgs_after=? WHERE id=?',
            ((summary or '')[:200_000], int(tokens_after),
             int(msgs_after), int(archive_id)),
        )

    def delete_archives(self, conv_id):
        """Delete all transcript-archive rows for a conversation."""
        from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM transcript_archive WHERE conv_id=?',
                              (conv_id,))

    def prune_archives(self, conv_id, keep):
        """Ring-buffer retention: keep only the ``keep`` most-recent archive
        rows for ``conv_id`` (highest ``id`` = newest), delete the rest.

        Every compaction (force + reactive) inserts a full ``messages_json``
        row, so without retention the table grows unbounded on a long-lived
        conversation.  Called as a GC-on-insert from ``_archive_transcript``.
        ``keep <= 0`` is a no-op (unlimited).  Returns the number of rows
        deleted (0 when under the cap).  Cross-backend: a correlated subselect
        of the newest ``keep`` ids, deleting anything not in it.
        """
        if not conv_id or keep <= 0:
            return 0
        from lib.database import (DOMAIN_CHAT, db_execute_with_retry,
                                  get_thread_db)
        db = get_thread_db(DOMAIN_CHAT)
        try:
            cur = db.execute(
                'SELECT COUNT(*) FROM transcript_archive WHERE conv_id=?',
                (conv_id,),
            )
            row = cur.fetchone()
            total = int(row[0] if not isinstance(row, dict) else row.get('count', 0)) if row else 0
            if total <= keep:
                return 0
            # Delete every row for this conv whose id is NOT among the newest
            # ``keep`` ids.  Subselect avoids OFFSET dialect differences.
            db_execute_with_retry(db,
                'DELETE FROM transcript_archive WHERE conv_id=? AND id NOT IN '
                '(SELECT id FROM transcript_archive WHERE conv_id=? '
                ' ORDER BY id DESC LIMIT ?)',
                (conv_id, conv_id, int(keep)),
            )
            deleted = total - keep
            logger.info('[Store] pruned transcript_archive conv=%s: deleted %d '
                        'oldest row(s), kept %d',
                        conv_id[:8] if conv_id else '?', deleted, keep)
            return deleted
        except Exception as e:
            logger.warning('[Store] prune_archives failed conv=%s: %s',
                           conv_id[:8] if conv_id else '?', e)
            return 0

    def sync_conversation_with_search(self, conv_id, messages):
        """Overwrite messages + msg_count + search_text and refresh FTS.

        This is the NON-CAS variant: it overwrites unconditionally and always
        updates the FTS row.  Use it for callers that hold the only writer for
        the conversation at that moment (e.g. endpoint multi-turn sync).  The
        CAS-guarded sibling — which gates the FTS update on a successful
        compare-and-swap of ``updated_at`` so a losing write never repoints
        FTS at uncommitted content — lives inline in
        ``lib.tasks_pkg.manager._sync_partial_to_conversation`` /
        ``_sync_result_to_conversation``.  Keep the two separate: collapsing
        them would drop that guard.
        """
        import time
        from lib.conversations import build_search_text, update_conversation_fts
        from lib.database import (DOMAIN_CHAT, db_execute_with_retry,
                                  get_thread_db, json_dumps_pg)
        db = get_thread_db(DOMAIN_CHAT)
        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(db, '''UPDATE conversations
            SET messages=?, updated_at=?, msg_count=?, search_text=?
            WHERE id=? AND user_id=1''',
            (messages_json, now_ms, len(messages), search_text, conv_id))
        update_conversation_fts(db, conv_id, search_text)
        return now_ms
