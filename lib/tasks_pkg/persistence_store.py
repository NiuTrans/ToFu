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

__all__ = ['DefaultConversationStore', 'ConcurrentWriteConflict']


def _row_fields(row, *names):
    """Pull ``names`` off a DB row that may be mapping-like OR sequence-like.

    The two backends hand back different row types (psycopg ``DictRow``,
    ``sqlite3.Row``, and plain tuples from test doubles). Indexing by position
    as a fallback is wrong for mapping types: on a dict, ``row[1]`` looks up the
    KEY ``1`` and raises ``KeyError`` rather than returning the second column.
    Decide by capability once, here, instead of at each call site.
    """
    if hasattr(row, 'keys'):
        keys = set(row.keys())
        if all(n in keys for n in names):
            return tuple(row[n] for n in names)
        # Mapping row missing a requested column: report None for it rather
        # than falling through to positional access, where `row[1]` on a dict
        # is a KEY lookup that raises KeyError.
        return tuple(row[n] if n in keys else None for n in names)
    # Sequence row. A row shorter than the request means the caller asked for a
    # column this query did not select (e.g. a legacy `SELECT messages` reached
    # by a test double); yield None for the missing tail instead of raising
    # IndexError, so a widened SELECT degrades to "no CAS token" rather than
    # killing the write path.
    return tuple(row[i] if i < len(row) else None for i in range(len(names)))


class ConcurrentWriteConflict(RuntimeError):
    """A rev-guarded transcript write lost its race and was NOT applied.

    Raised instead of silently overwriting, so a caller that read-modify-wrote
    the whole blob learns it must re-read rather than destroying the rows a
    concurrent writer appended in the meantime.
    """


class DefaultConversationStore:
    """chatui's PostgreSQL/SQLite-backed :class:`~lib.protocols.ConversationStore`.

    Stateless — every method acquires the connection it needs from the
    thread-local pool, so a single shared instance is safe across all worker
    threads.  Satisfies the protocol structurally (no inheritance).
    """

    # ── The CAS token is ``rev``, and it travels WITH the data ──
    # Every whole-blob write takes an ``expected_rev`` the caller got from its
    # own read. Two properties make ``rev`` the only sound token here:
    #
    #   * it is issued by the DB, not by the writer — a ``BEFORE UPDATE OF
    #     messages`` trigger advances it inside the same statement, so a writer
    #     can neither forge it nor skip it. ``updated_at`` is the opposite: the
    #     writer supplies its own value, so a clock that does not advance (same
    #     millisecond, NTP step, container clock skew) yields a predicate that
    #     PASSES while the data underneath has changed. That is a structural
    #     defect, not a precision one — raising the resolution would not fix it;
    #   * the trigger is guarded by ``messages IS DISTINCT FROM``, so a
    #     settings-only or title-only write does not bump it and cannot cause a
    #     spurious conflict.
    #
    # The token is a PARAMETER rather than remembered state on the store. An
    # earlier revision kept it in a ``threading.local`` keyed by conversation;
    # under the ASGI worker pool threads are REUSED, so a token left behind by
    # one request would silently guard an unrelated write in the next — and it
    # would fail in the "looks like it worked" direction.

    def release_connection(self) -> None:
        """Release this thread's pooled DB connection(s) back to the pool.

        Delegates to :func:`lib.database.close_thread_db` (no-arg → all
        domains).  Imported lazily so this module stays import-light and the
        DB layer is only touched when the method actually runs.
        """
        from lib.database import close_thread_db
        close_thread_db()

    def next_task_event_id(self, task_id, *, floor=0):
        """Allocate after the durable and local write-behind event tails."""
        from lib.database import DOMAIN_CHAT, pooled_db
        from lib.tasks_pkg.event_log import pending_event_rows

        with pooled_db(DOMAIN_CHAT) as db:
            row = db.execute(
                'SELECT MAX(event_id) AS max_event_id FROM task_events '
                'WHERE task_id=?', (task_id,)).fetchone()
        highest = -1
        if row is not None:
            value = row['max_event_id']
            if value is not None:
                highest = int(value)
        for pending in pending_event_rows(task_id):
            highest = max(highest, int(pending['event_id']))
        return max(int(floor), highest + 1)

    def load_conversation_messages(self, conv_id):
        """Read ``(messages, updated_at, rev)`` for a conversation, or ``None``.

        ``user_id=1`` is the single-user chatui convention (hidden behind the
        seam).  Returns ``None`` if the row is absent; an empty list with
        whatever ``updated_at``/``rev`` exists on a parse failure.

        ``rev`` is the CAS token for any subsequent whole-blob write: it rides
        the return value so it travels WITH the data the caller is about to
        modify.  All three fields come from ONE statement — reading them
        separately opens the exact window this module exists to close, because a
        writer that appends between the two reads leaves the caller holding the
        OLD messages with the NEW rev, and the CAS then passes while destroying
        the append.  A single-row read is atomic on both backends.
        """
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        return self._load_with_rev(db, conv_id)

    @staticmethod
    def _load_with_rev(db, conv_id):
        """``(messages, updated_at, rev)`` from one repository snapshot.

        Shared by the public read and by the internal retry loops so there is a
        single place that decides how a row is turned into a message list.
        """
        from lib.database.conversation_repository import load_conversation
        snapshot = load_conversation(
            db, conv_id, metadata_columns=('updated_at',))
        if snapshot is None:
            return None
        return (
            snapshot.messages,
            int(snapshot.get('updated_at') or 0),
            int(snapshot.get('rev') or 0),
        )

    def save_conversation_messages(self, conv_id, messages, *, expected_rev):
        """Persist ``messages``, but only while the row is still at ``expected_rev``.

        The blob holds the WHOLE transcript, so every caller is doing a
        read-modify-write. An unconditional ``UPDATE`` therefore erases any row
        a concurrent thread appended between the caller's read and this write —
        silently, with no error and no failing test. Measured incident (conv
        ms3sfyrmn31omb, 2026-07-28): 13 autopilot ``Appended VU msg`` log lines
        vs 8 surviving rows, because the per-round snapshot daemon kept writing
        back a transcript copy it had read before the append landed.

        ``expected_rev`` is REQUIRED and comes from the caller's own
        :meth:`load_conversation_messages` — there is no ``None`` escape hatch,
        so "read-modify-write that loses someone else's row" cannot be spelled.
        A caller that must genuinely win unconditionally has to say so out loud
        via :meth:`overwrite_conversation_messages_unconditional`.

        Raises ``ConcurrentWriteConflict`` when the row moved instead of
        overwriting. Callers setting FIELDS on one message should use
        :meth:`patch_message_fields_by_task`, which replays the mutation onto a
        re-read copy rather than making the caller retry.

        Returns the new ``updated_at`` timestamp (epoch-ms).
        """
        import time
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import replace_messages
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        result = replace_messages(
            db, conv_id, messages, expected_rev=int(expected_rev),
            metadata={'updated_at': now_ms}, full=True)
        if not result.applied:
            raise ConcurrentWriteConflict(
                f'conv={conv_id} changed since it was loaded at '
                f'rev={expected_rev} — refusing to clobber the concurrent '
                f'writer. Re-load and re-apply your change.')
        return now_ms

    def overwrite_conversation_messages_unconditional(self, conv_id, messages):
        """DANGEROUS: overwrite the transcript blob, ignoring concurrent writers.

        Any row another thread appended after this caller read the blob is LOST.
        Only correct when the caller is provably the sole writer for the
        conversation at that instant — e.g. boot-time crash recovery, which runs
        before any task or client can be attached to the row. The name is
        deliberately long and explicit so that choosing it is a visible decision
        at the call site rather than the accidental default; the ratchet
        ``tests/test_conv_messages_cas_writes.py`` treats it as the ONLY
        sanctioned unconditional whole-blob writer.
        """
        import time
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import replace_messages
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        replace_messages(
            db, conv_id, messages,
            metadata={'updated_at': now_ms}, full=True)
        return now_ms

    def patch_message_fields_by_task(self, conv_id, task_id, fields,
                                     *, max_attempts=5):
        """Set ``fields`` on the assistant message tagged ``_taskId == task_id``.

        The narrow-write path for post-settle enrichment (snapshot id, learned
        preferences): it re-reads the transcript, applies the field mutation to
        the ONE message it owns, and commits under a rev-CAS, retrying on a lost
        race. Concurrent appends therefore survive — the retry replays this
        patch against the newer transcript instead of overwriting it.

        There is deliberately NO "fall back to the last assistant message" path.
        The daemons that call this run after the turn settled, so a missing
        ``_taskId`` means the message was compacted away or the row was rebuilt
        — and positional guessing under concurrency stamps one task's snapshot
        onto ANOTHER task's turn (the same class of bug as the blob clobber,
        just quieter). Returns True when the patch landed, False when the target
        message no longer exists or the race could not be won.
        """
        if not (conv_id and task_id and fields):
            return False
        import time
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import replace_messages
        db = get_thread_db(DOMAIN_CHAT)
        for attempt in range(1, int(max_attempts) + 1):
            loaded = self._load_with_rev(db, conv_id)
            if loaded is None:
                return False
            messages, _updated_at, expected_rev = loaded
            if not isinstance(messages, list) or not messages:
                return False
            target_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                if (isinstance(m, dict) and m.get('role') == 'assistant'
                        and m.get('_taskId') == task_id):
                    target_idx = i
                    break
            if target_idx < 0:
                # WARNING, not info: with the positional fallback deliberately
                # gone, this line is the ONLY signal that a snapshot / learned
                # preference never reached the transcript. At info level a
                # systematic loss would look like ordinary chatter.
                logger.warning('[Store] patch_message_fields_by_task: conv=%s no '
                               'assistant message tagged task=%s — fields %s were '
                               'NOT persisted', conv_id[:8], task_id[:8],
                               sorted(fields))
                return False
            messages[target_idx].update(fields)
            now_ms = int(time.time() * 1000)
            result = replace_messages(
                db, conv_id, messages, expected_rev=expected_rev,
                metadata={'updated_at': now_ms},
                changed_seqs=[target_idx])
            if result.applied:
                return True
            logger.debug('[Store] patch_message_fields_by_task conv=%s lost the '
                         'rev=%s race (attempt %d/%d) — re-reading',
                         conv_id[:8], expected_rev, attempt, max_attempts)
        logger.warning('[Store] patch_message_fields_by_task conv=%s task=%s gave '
                       'up after %d CAS attempts', conv_id[:8], task_id[:8],
                       max_attempts)
        return False

    def cas_update_conversation_messages(self, conv_id, messages, expected_rev):
        """Compare-and-swap overwrite guarded by the row's ``rev``.

        Returns affected-row count (0 = a concurrent writer won the race).

        The token is ``rev`` and not ``updated_at`` for a structural reason, not
        a precision one: ``updated_at`` is a value the WRITER supplies, so a
        clock that fails to advance between two writes (same millisecond, an NTP
        step, container clock skew) produces a predicate that PASSES while the
        data underneath has already changed. A guard whose token is signed by
        the party being guarded is not a guard. ``rev`` is advanced by a DB
        trigger inside the same statement, so it cannot be forged or skipped —
        switching to a finer timestamp would NOT have fixed this.
        """
        import time
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import replace_messages
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        result = replace_messages(
            db, conv_id, messages, expected_rev=int(expected_rev),
            metadata={'updated_at': now_ms}, full=True)
        return 1 if result.applied else 0

    def cas_sync_conversation_with_search(self, conv_id, messages, expected_rev):
        """CAS overwrite that ALSO refreshes msg_count + search_text + FTS.

        The CAS-guarded sibling of :meth:`sync_conversation_with_search`: writes
        only if the row's ``rev`` still equals ``expected_rev`` (0 rows affected
        → a concurrent writer won → caller treats as a skip), and updates the
        full-text-search index ONLY on a landed write so a losing race never
        repoints FTS at content it didn't commit.

        Use this (not the plain ``cas_update_conversation_messages``) whenever a
        write CHANGES THE MESSAGE SET — e.g. manual /compact removes whole
        messages, so msg_count and the searchable text genuinely change; a plain
        CAS would leave the sidebar count stale and let search still match
        compacted-away text.  Returns affected-row count (0 = skipped).

        ORTHOGONAL to a caller's own content check: this proves only that the
        ROW did not move. It cannot prove that WHAT moved was outside the region
        the caller folded, so a caller that summarises part of the transcript
        (manual compaction) must ALSO verify its folded prefix is byte-unchanged.
        Dropping that check because "the CAS covers it" turns every legitimate
        tail append into a spurious conflict.
        """
        import time
        from lib.conversations import build_search_text
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import replace_messages
        db = get_thread_db(DOMAIN_CHAT)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        result = replace_messages(
            db, conv_id, messages, expected_rev=int(expected_rev),
            metadata={
                'updated_at': now_ms,
                'msg_count': len(messages),
                'search_text': search_text,
            },
            full=True)
        return 1 if result.applied else 0

    def ensure_compaction_schema(self):
        """Verify central schema bootstrap installed the compaction store."""
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db.execute(
            'SELECT id, conv_id, messages_json, summary, trigger, task_id, '
            'round_num, model, tokens_before, tokens_after, msgs_before, '
            'msgs_after, reason FROM transcript_archive LIMIT 0')

    def archive_transcript(self, conv_id, messages, *, trigger='force',
                           task_id='', round_num=0, model='',
                           tokens_before=0, tokens_after=0,
                           msgs_before=0, msgs_after=0, reason=''):
        """Insert a transcript-archive row; recover and return its id.

        Cross-backend-safe write-then-select to avoid vendor-specific
        RETURNING / lastrowid semantics.  Returns the new id, or None on
        failure.  The store owns serialisation of ``messages``.
        """
        from lib.database import (
            DOMAIN_CHAT, get_thread_db, json_dumps_pg, write_transaction)
        archive_id = None
        try:
            db = get_thread_db(DOMAIN_CHAT)
            messages_json = json_dumps_pg(messages, default=str)
            with write_transaction(db, label='transcript-archive-insert'):
                row = db.execute(
                    'INSERT INTO transcript_archive '
                    '(conv_id, messages_json, summary, trigger, task_id, round_num, '
                    ' model, tokens_before, tokens_after, msgs_before, msgs_after, reason) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id',
                    (conv_id, messages_json, '', trigger, task_id,
                     int(round_num or 0), model,
                     int(tokens_before or 0), int(tokens_after or 0),
                     int(msgs_before or 0), int(msgs_after or 0),
                     (reason or '')[:500]),
                ).fetchone()
                if row is not None:
                    archive_id = int(row[0] if not isinstance(row, dict) else row.get('id'))
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

    def sync_conversation_with_search(self, conv_id, messages, *, expected_rev,
                                      rebuild=None, max_attempts=5):
        """Rev-guarded overwrite of messages + msg_count + search_text + FTS.

        Endpoint multi-turn sync: the engine owns the ENGINE-PRODUCED tail and
        rewrites it wholesale, so this is a whole-blob write rather than a
        field patch. It is still guarded — an unconditional overwrite here
        erases anything another writer appended after ``expected_rev`` (the
        autopilot VU append fires on exactly this boundary), which is the same
        silent data loss the snapshot daemon caused.

        ``rebuild(messages, rev)`` is an OPTIONAL callback used to replay the
        caller's transform onto a freshly-read transcript after a lost race; it
        must return the new full message list. Without it a conflict raises
        ``ConcurrentWriteConflict`` rather than being retried, because blindly
        re-writing the SAME stale list is precisely the clobber being prevented.

        Returns the new ``updated_at`` (epoch-ms).
        """
        import time
        from lib.conversations import build_search_text
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import replace_messages
        db = get_thread_db(DOMAIN_CHAT)
        cur_messages, cur_rev = list(messages), int(expected_rev)
        for attempt in range(1, int(max_attempts) + 1):
            search_text = build_search_text(cur_messages)
            now_ms = int(time.time() * 1000)
            result = replace_messages(
                db, conv_id, cur_messages, expected_rev=cur_rev,
                metadata={
                    'updated_at': now_ms,
                    'msg_count': len(cur_messages),
                    'search_text': search_text,
                },
                full=True)
            if result.applied:
                return now_ms
            if rebuild is None:
                raise ConcurrentWriteConflict(
                    f'conv={conv_id} changed since it was loaded at '
                    f'rev={cur_rev} and no rebuild callback was supplied \u2014 '
                    f'refusing to overwrite the concurrent writer.')
            reloaded = self._load_with_rev(db, conv_id)
            if reloaded is None:
                return 0
            fresh_messages, _fresh_updated, cur_rev = reloaded
            cur_messages = rebuild(fresh_messages, cur_rev)
            logger.debug('[Store] sync_conversation_with_search conv=%s lost the '
                         'rev race (attempt %d/%d) \u2014 replayed onto rev=%s',
                         conv_id[:8], attempt, max_attempts, cur_rev)
        raise ConcurrentWriteConflict(
            f'conv={conv_id} sync_conversation_with_search gave up after '
            f'{max_attempts} CAS attempts')

    def notify_conversation_changed(self, conv_id):
        """Look up the conversation's current rev and fire the conv-changed push.

        The rev (bumped by the messages-change trigger on the preceding write)
        rides the notification so the client's rev-gate refetches this conv's
        body exactly once.  Best-effort: a failure here must never break the
        caller's write path.
        """
        from lib.conversations import notify_conv_changed
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        rev_row = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        notify_conv_changed(conv_id, rev=(rev_row[0] if rev_row else None))
