"""Authoritative conversation read/write operations.

Business code supplies message values and optional metadata, never its own
SQL or commit choreography. During migration this repository advances the
legacy ``conversations.messages`` blob and ``conversation_messages`` in one
transaction. In row-authority mode only normalized rows advance; the blob is a
frozen rollback archive and runtime access policy prevents business reads.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json

from lib.database import json_dumps_pg, write_transaction
from lib.log import get_logger


logger = get_logger(__name__)

_ALLOWED_METADATA_COLUMNS = frozenset({
    'created_at',
    'msg_count',
    'search_text',
    'settings',
    'title',
    'updated_at',
})


@dataclass(frozen=True)
class ConversationWriteResult:
    applied: bool
    rev: int | None


@dataclass(frozen=True)
class ConversationMutation:
    """A business mutation decision consumed by :func:`mutate_conversation`."""

    changed: bool = True
    value: object = None
    metadata: dict | None = None
    changed_seqs: object = None
    full: bool = False


@dataclass(frozen=True)
class ConversationMutationResult:
    applied: bool
    rev: int | None
    value: object = None
    attempts: int = 0
    missing: bool = False


@dataclass(frozen=True)
class ConversationDeleteResult:
    conversation_rows: int
    message_rows: int
    task_rows: int
    archive_rows: int


class ConversationIntegrityError(RuntimeError):
    """The canonical row transcript is missing, stale, or structurally invalid."""


@dataclass(frozen=True)
class ConversationSnapshot:
    metadata: dict
    messages: list
    source: str

    def __getitem__(self, key):
        if key == 'messages':
            return self.messages
        return self.metadata[key]

    def get(self, key, default=None):
        if key == 'messages':
            return self.messages
        return self.metadata.get(key, default)

    def keys(self):
        return tuple(self.metadata) + ('messages',)


_READABLE_METADATA_COLUMNS = frozenset({
    'id', 'user_id', 'title', 'created_at', 'updated_at', 'settings',
    'rev', 'msg_count', 'search_text', 'messages_rows_rev',
})


def conversation_rows_authoritative() -> bool:
    """Whether a stale/missing row transcript must fail instead of fallback.

    This switch is intentionally explicit during rollout. Once enabled, the
    legacy JSON blob is an archive only and can never silently become truth
    again after row corruption.
    """
    from lib.database.messages_rows import rows_authority_enabled
    return rows_authority_enabled()


def load_conversation(
    db,
    conv_id: str,
    *,
    user_id=1,
    metadata_columns=(),
) -> ConversationSnapshot | None:
    """Load one transcript through the data layer's authority decision.

    During migration an exact revision/count marker selects rows and an
    incomplete historical conversation falls back to the blob. In authority
    mode the same incomplete state raises: stale archive data must never
    resurrect as current truth.
    """
    if not conv_id:
        raise ValueError('conv_id is required')
    requested = set(metadata_columns) | {
        'id', 'user_id', 'rev', 'msg_count', 'messages_rows_rev'}
    unknown = requested - _READABLE_METADATA_COLUMNS
    if unknown:
        raise ValueError(
            f'unsupported conversation metadata columns: {sorted(unknown)}')
    columns = sorted(requested)
    authority = conversation_rows_authoritative()

    metadata_defaults = {
        'id': conv_id,
        'user_id': user_id,
        'title': '',
        'created_at': 0,
        'updated_at': 0,
        'settings': '{}',
        'rev': 0,
        'msg_count': 0,
        'search_text': '',
        'messages_rows_rev': None,
    }

    def _keys(row):
        try:
            return set(row.keys())
        except (AttributeError, TypeError) as exc:
            logger.debug('conversation row has no mapping keys: %s', exc)
            return set()

    def _legacy_snapshot():
        """Compatibility/archive read through one exact parent snapshot.

        ``messages`` intentionally leads the projection. Besides making the
        retired archive obvious in SQL review, this preserves compatibility
        with narrow DB adapters/plugins that historically recognized
        ``SELECT messages ...`` and returned only ``(messages, rev)``. Real
        wrappers return every projected column; short adapter rows receive
        conservative metadata defaults and can never satisfy row authority.
        """
        # The mirror marker is irrelevant once this compatibility lane has
        # deliberately chosen the archive. Omitting it also lets narrow legacy
        # adapters distinguish this query from the row-integrity gate.
        legacy_columns = [
            column for column in columns
            if column != 'messages_rows_rev']
        projection = ['messages'] + legacy_columns
        row = db.execute(
            f'SELECT {", ".join(projection)} FROM conversations '
            'WHERE id=? AND user_id=?', (conv_id, user_id)).fetchone()
        if row is None:
            return None
        row_keys = _keys(row)
        if row_keys:
            raw_value = row['messages']
            meta = {
                column: (row[column] if column in row_keys
                         else metadata_defaults[column])
                for column in columns
            }
        else:
            values = list(row)
            if not values:
                return None
            raw_value = values[0]
            if len(values) >= len(legacy_columns) + 1:
                meta = {column: metadata_defaults[column]
                        for column in columns}
                meta.update({
                    column: values[index + 1]
                    for index, column in enumerate(legacy_columns)
                })
            else:
                meta = {
                    column: metadata_defaults[column]
                    for column in columns
                }
                # Historical adapters commonly returned (messages, rev) or
                # (messages, updated_at, rev). Preserve the CAS token without
                # guessing any other column positions.
                if len(values) > 1:
                    try:
                        meta['rev'] = int(values[-1] or 0)
                    except (TypeError, ValueError) as exc:
                        logger.debug(
                            'legacy conversation revision is malformed: %s',
                            exc)
        try:
            parsed = (raw_value if isinstance(raw_value, list)
                      else json.loads(raw_value or '[]'))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ConversationIntegrityError(
                f'legacy transcript is invalid for conversation {conv_id}') from exc
        if not isinstance(parsed, list):
            raise ConversationIntegrityError(
                f'legacy transcript is not a list for conversation {conv_id}')
        if 'msg_count' not in row_keys:
            meta['msg_count'] = len(parsed)
        return ConversationSnapshot(
            metadata=meta, messages=parsed, source='legacy_blob')

    from lib.database.messages_rows import rows_read_enabled, rows_to_messages
    use_rows = rows_read_enabled()
    if authority and not use_rows:
        raise ConversationIntegrityError(
            'row authority requires TOFU_MESSAGES_ROWS and '
            'TOFU_MESSAGES_ROWS_READ to remain enabled')

    # A read-modify-write caller must receive messages and its CAS revision
    # from ONE database snapshot.  Separate parent/marker/children SELECTs can
    # interleave with a writer under both SQLite autocommit and PostgreSQL READ
    # COMMITTED, producing an old rev paired with new messages (or vice versa).
    # The joined result below is one statement, therefore one MVCC snapshot.
    if use_rows:
        parent_fields = [f'c.{column} AS {column}' for column in columns]
        if not authority:
            parent_fields.append('c.messages AS _legacy_messages')
        projection = parent_fields + [
            'cm.meta AS _message_meta',
            'cm.translation_state AS _translation_state',
            'cm.seq AS _message_seq',
            'COUNT(cm.seq) OVER () AS _row_count',
            'COUNT(cm.meta_light) OVER () AS _light_count',
        ]
        statement_tail = (
            ' FROM conversations c LEFT JOIN conversation_messages cm '
            'ON cm.conv_id=c.id '
            'WHERE c.id=? AND c.user_id=? ORDER BY cm.seq')
        try:
            result_rows = db.execute(
                f'SELECT {", ".join(projection)}{statement_tail}',
                (conv_id, user_id),
            ).fetchall()
        except Exception as exc:
            # Rolling upgrade / narrow test adapters can briefly expose the
            # v53 row shape. Transitional reads remain lossless because meta
            # still contains every translation field; authority mode fails
            # closed until startup has installed v54.
            if authority or 'translation_state' not in str(exc).lower():
                raise
            legacy_projection = [
                ('NULL AS _translation_state'
                 if field == 'cm.translation_state AS _translation_state'
                 else field)
                for field in projection
            ]
            result_rows = db.execute(
                f'SELECT {", ".join(legacy_projection)}{statement_tail}',
                (conv_id, user_id),
            ).fetchall()
        if not result_rows:
            return None
        first = result_rows[0]
        first_keys = _keys(first)
        required_projection = set(columns) | {
            '_message_meta', '_translation_state', '_message_seq',
            '_row_count', '_light_count'}
        if not authority:
            required_projection.add('_legacy_messages')
        projection_complete = (
            required_projection <= first_keys if first_keys
            else len(first) >= len(projection)
        )
        if not projection_complete:
            if authority:
                raise ConversationIntegrityError(
                    'canonical transcript query returned an incomplete '
                    f'projection for conversation {conv_id}')
            return _legacy_snapshot()
        metadata = {
            column: (first[column] if hasattr(first, 'keys')
                     else first[index])
            for index, column in enumerate(columns)
        }
        offset = len(columns) + (0 if authority else 1)
        get_first = lambda name, pos: (  # noqa: E731 - compact row adapter
            first[name] if hasattr(first, 'keys') else first[offset + pos])
        rev = int(metadata.get('rev') or 0)
        marker = metadata.get('messages_rows_rev')
        row_count = int(get_first('_row_count', 3) or 0)
        light_count = int(get_first('_light_count', 4) or 0)
        expected_count = int(metadata.get('msg_count') or 0)
        rows_current = (
            marker is not None and int(marker) == rev
            and row_count == expected_count
            and light_count == expected_count
        )
        if rows_current:
            message_rows = []
            for result_row in result_rows:
                seq = (result_row['_message_seq']
                       if hasattr(result_row, 'keys')
                       else result_row[offset + 2])
                if seq is None:
                    continue
                meta = (result_row['_message_meta']
                        if hasattr(result_row, 'keys')
                        else result_row[offset])
                translation_state = (
                    result_row['_translation_state']
                    if hasattr(result_row, 'keys')
                    else result_row[offset + 1])
                message_rows.append({
                    'meta': meta,
                    'translation_state': translation_state,
                })
            return ConversationSnapshot(
                metadata=metadata,
                messages=rows_to_messages(message_rows),
                source='rows')
        if authority:
            raise ConversationIntegrityError(
                f'canonical message rows are not current for conversation {conv_id}')
        raw = (first['_legacy_messages'] if hasattr(first, 'keys')
               else first[len(columns)])
    else:
        return _legacy_snapshot()

    try:
        messages = raw if isinstance(raw, list) else json.loads(raw or '[]')
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConversationIntegrityError(
            f'legacy transcript is invalid for conversation {conv_id}') from exc
    if not isinstance(messages, list):
        raise ConversationIntegrityError(
            f'legacy transcript is not a list for conversation {conv_id}')
    return ConversationSnapshot(
        metadata=metadata, messages=messages, source='legacy_blob')


def iter_conversation_snapshots(
    db,
    *,
    user_id=1,
    ids=None,
    updated_at_gte=None,
    updated_at_gt=None,
    created_at_lt=None,
    metadata_columns=(),
    order_by: str = 'updated_at_desc',
    limit: int | None = None,
    on_invalid=None,
):
    """Yield a filtered batch without exposing transcript storage SQL.

    Parent identifiers are selected cheaply, then each transcript goes through
    :func:`load_conversation` and its exact authority/integrity gate. This is
    intentionally the same correctness path as an online single read; recovery
    jobs and reports do not get a weaker blob-only exception. Yielding keeps
    one transcript live at a time for maintenance scans of a multi-gigabyte
    authority.
    """
    order_sql = {
        'updated_at_desc': 'updated_at DESC, id DESC',
        'id_asc': 'id ASC',
    }.get(order_by)
    if order_sql is None:
        raise ValueError(f'unsupported conversation order: {order_by}')
    where = []
    params = []
    if user_id is not None:
        where.append('user_id=?')
        params.append(user_id)
    normalized_ids = tuple(dict.fromkeys(str(v) for v in (ids or ()) if v))
    if ids is not None:
        if not normalized_ids:
            return
        where.append('id IN (%s)' % ','.join('?' for _ in normalized_ids))
        params.extend(normalized_ids)
    if updated_at_gte is not None:
        where.append('updated_at>=?')
        params.append(updated_at_gte)
    if updated_at_gt is not None:
        where.append('updated_at>?')
        params.append(updated_at_gt)
    if created_at_lt is not None:
        where.append('created_at<?')
        params.append(created_at_lt)
    sql = 'SELECT id, user_id FROM conversations'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += f' ORDER BY {order_sql}'
    if limit is not None:
        if int(limit) < 0:
            raise ValueError('limit cannot be negative')
        sql += ' LIMIT ?'
        params.append(int(limit))
    owners = db.execute(sql, tuple(params)).fetchall()
    for owner in owners:
        get = lambda key, pos: (  # noqa: E731 - portable DB row adapter
            owner[key] if hasattr(owner, 'keys') else owner[pos])
        owner_id = get('id', 0)
        try:
            owner_keys = set(owner.keys()) if hasattr(owner, 'keys') else set()
            owner_user_id = (
                owner['user_id'] if 'user_id' in owner_keys
                else (user_id if user_id is not None else 1))
            snapshot = load_conversation(
                db, owner_id, user_id=owner_user_id,
                metadata_columns=metadata_columns)
        except Exception as exc:
            if on_invalid is None:
                raise
            on_invalid(owner_id, exc)
            continue
        if snapshot is not None:
            yield snapshot


def list_conversation_snapshots(
    db,
    *,
    user_id=1,
    ids=None,
    updated_at_gte=None,
    updated_at_gt=None,
    created_at_lt=None,
    metadata_columns=(),
    order_by: str = 'updated_at_desc',
    limit: int | None = None,
    on_invalid=None,
) -> list[ConversationSnapshot]:
    """Materialize :func:`iter_conversation_snapshots` for bounded callers."""
    return list(iter_conversation_snapshots(
        db,
        user_id=user_id,
        ids=ids,
        updated_at_gte=updated_at_gte,
        updated_at_gt=updated_at_gt,
        created_at_lt=created_at_lt,
        metadata_columns=metadata_columns,
        order_by=order_by,
        limit=limit,
        on_invalid=on_invalid,
    ))


def upsert_conversation(
    db,
    conv_id: str,
    messages,
    *,
    title: str,
    created_at: int,
    updated_at: int,
    user_id=1,
    settings=None,
    search_text: str = '',
    changed_seqs=None,
    full: bool = True,
    expected_rev: int | None = None,
) -> ConversationWriteResult:
    """Atomically create/replace a conversation and its message rows."""
    if not conv_id:
        raise ValueError('conv_id is required')
    if not isinstance(messages, list):
        raise TypeError('messages must be a list')

    from lib.conversations import build_search_text
    derived_search_text = build_search_text(messages)

    if expected_rev is not None:
        metadata = {
            'title': title,
            'created_at': int(created_at),
            'updated_at': int(updated_at),
        }
        if settings is not None:
            metadata['settings'] = settings
        return replace_messages(
            db, conv_id, messages, user_id=user_id,
            expected_rev=expected_rev, metadata=metadata,
            changed_seqs=changed_seqs, full=full)

    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database.messages_rows import rows_write_enabled, write_conv_rows
    from lib.conversations import update_conversation_fts

    row = {
        'id': conv_id,
        'user_id': user_id,
        'title': title,
        # In row-authority mode the archive is initialized once and never
        # rewritten. Transitional mode keeps both representations atomic.
        'messages': ('[]' if conversation_rows_authoritative()
                     else json_dumps_pg(messages)),
        'created_at': int(created_at),
        'updated_at': int(updated_at),
        'msg_count': len(messages),
        'search_text': derived_search_text,
    }
    insert_cols = list(row)
    if settings is not None:
        row['settings'] = settings
        insert_cols.append('settings')

    authority = conversation_rows_authoritative()
    if authority and not rows_write_enabled():
        raise ConversationIntegrityError(
            'row authority cannot create while message rows are disabled')

    with write_transaction(db, label='upsert conversation'):
        existing = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conv_id, user_id)).fetchone()
        if existing is not None:
            raise ConversationIntegrityError(
                'upsert of an existing conversation requires expected_rev')
        # A new authoritative parent still initializes the retired archive to
        # ``[]`` for schema compatibility.  This is the only ordinary server
        # write that may mention that column; the explicit scope keeps the
        # runtime plugin guard fail-closed everywhere else.
        if authority:
            from lib.database._access_policy import (
                allow_transcript_archive_access,
            )
            with allow_transcript_archive_access():
                upsert(
                    db, CONVERSATIONS, row, insert_cols=insert_cols,
                    retry=False, commit=False)
        else:
            upsert(
                db, CONVERSATIONS, row, insert_cols=insert_cols,
                retry=False, commit=False)
        update_conversation_fts(db, conv_id, derived_search_text)
        if rows_write_enabled():
            write_conv_rows(
                db, conv_id, messages, now_ms=int(updated_at),
                changed_seqs=changed_seqs, full=full,
                row_authority=authority,
                user_id=user_id)
        rev_row = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conv_id, user_id)).fetchone()
        rev = (None if rev_row is None else int(
            rev_row['rev'] if hasattr(rev_row, 'keys') else rev_row[0]))
        return ConversationWriteResult(applied=True, rev=rev)


def update_message_translation_overlay(
    db,
    conv_id: str,
    seq: int,
    message: dict,
    messages: list,
    *,
    expected_rev: int,
    updated_at: int,
    user_id=1,
) -> ConversationWriteResult:
    """CAS-commit translation enrichment without rewriting message ``meta``.

    This narrow writer is available only after normalized rows are canonical.
    It advances the parent revision/marker and the compact child overlay in one
    transaction. Ordinary content edits still go through ``replace_messages``;
    callers cannot use this API to mutate the lossless base document.
    """
    if not conversation_rows_authoritative():
        raise ConversationIntegrityError(
            'translation overlays require canonical message-row authority')
    if not conv_id:
        raise ValueError('conv_id is required')
    if not isinstance(seq, int) or seq < 0 or seq >= len(messages):
        raise ValueError('seq must address messages')
    if not isinstance(message, dict) or messages[seq] is not message:
        raise ValueError('message must be the addressed messages entry')

    from lib.conversations import build_search_text, update_conversation_fts
    from lib.database.messages_rows import (
        rows_write_enabled,
        translation_state_for_message,
    )
    if not rows_write_enabled():
        raise ConversationIntegrityError(
            'row authority cannot write while message rows are disabled')

    translated = message.get('translatedContent', '')
    if not isinstance(translated, str):
        translated = ''
    translation_state = json_dumps_pg(
        translation_state_for_message(message))
    search_text = build_search_text(messages)

    with write_transaction(db, label='update message translation overlay'):
        # Both right-hand ``rev`` references are evaluated from the old row on
        # SQLite and PostgreSQL, atomically blessing the exact child revision.
        cursor = db.execute(
            'UPDATE conversations SET rev=rev+1, messages_rows_rev=rev+1, '
            'updated_at=?, search_text=? '
            'WHERE id=? AND user_id=? AND rev=? '
            'AND messages_rows_rev=rev AND msg_count=?',
            (int(updated_at), search_text, conv_id, user_id,
             int(expected_rev), len(messages)),
        )
        if getattr(cursor, 'rowcount', None) == 0:
            return ConversationWriteResult(applied=False, rev=None)

        child = db.execute(
            'UPDATE conversation_messages SET translated_content=?, '
            'translation_state=?, updated_at=? '
            'WHERE conv_id=? AND seq=?',
            (translated, translation_state, int(updated_at), conv_id, seq),
        )
        if getattr(child, 'rowcount', None) != 1:
            raise ConversationIntegrityError(
                'canonical translation target row is missing')
        update_conversation_fts(db, conv_id, search_text)
        return ConversationWriteResult(
            applied=True, rev=int(expected_rev) + 1)


def replace_messages(
    db,
    conv_id: str,
    messages,
    *,
    user_id=1,
    expected_rev: int | None = None,
    metadata: dict | None = None,
    changed_seqs=None,
    full: bool = False,
    preserve_rev: bool = False,
    refresh_search: bool = True,
) -> ConversationWriteResult:
    """Atomically replace one transcript and its canonical message rows.

    ``expected_rev`` enables optimistic CAS.  A miss returns
    ``applied=False`` without changing either representation.  Metadata keys
    are restricted to a fixed storage whitelist so callers cannot smuggle
    arbitrary SQL identifiers through this semantic API. ``refresh_search``
    may be disabled only for transient streaming checkpoints; terminal writes
    retain the default and converge the derived search/FTS projection.
    """
    if not conv_id:
        raise ValueError('conv_id is required')
    if not isinstance(messages, list):
        raise TypeError('messages must be a list')

    if preserve_rev and expected_rev is None:
        raise ValueError('preserve_rev requires expected_rev')

    metadata = dict(metadata or {})
    unknown = set(metadata) - _ALLOWED_METADATA_COLUMNS
    if unknown:
        raise ValueError(
            f'unsupported conversation metadata columns: {sorted(unknown)}')
    metadata['msg_count'] = len(messages)
    # Search metadata and FTS are derived state. Business callers cannot hand
    # the repository a stale value that no longer matches the transcript.
    # A partial-stream checkpoint deliberately leaves the settled index alone;
    # its terminal owner will refresh it once the answer is complete.
    if refresh_search:
        from lib.conversations import build_search_text
        metadata['search_text'] = build_search_text(messages)
    else:
        metadata.pop('search_text', None)

    authority = conversation_rows_authoritative()
    assignments = []
    params = []
    if authority:
        if not preserve_rev:
            assignments.append('rev=rev+1')
    else:
        assignments.append('messages=?')
        params.append(json_dumps_pg(messages))
    for column, value in metadata.items():
        assignments.append(f'{column}=?')
        params.append(value)
    where = ['id=?', 'user_id=?']
    params.extend((conv_id, user_id))
    if expected_rev is not None:
        where.append('rev=?')
        params.append(int(expected_rev))

    sql = (
        f'UPDATE conversations SET {", ".join(assignments)} '
        f'WHERE {" AND ".join(where)}')

    from lib.database.messages_rows import rows_write_enabled, write_conv_rows
    from lib.conversations import update_conversation_fts

    if authority and not rows_write_enabled():
        raise ConversationIntegrityError(
            'row authority cannot write while message rows are disabled')

    with write_transaction(db, label='replace conversation messages'):
        cursor = db.execute(sql, tuple(params))
        if getattr(cursor, 'rowcount', None) == 0:
            return ConversationWriteResult(applied=False, rev=None)

        if refresh_search:
            update_conversation_fts(db, conv_id, metadata['search_text'])
        if rows_write_enabled():
            write_conv_rows(
                db, conv_id, messages,
                now_ms=int(metadata.get('updated_at') or 0),
                changed_seqs=changed_seqs,
                full=full,
                row_authority=authority,
                user_id=user_id,
            )

        # Enrichment-only writes (for example background translations) should
        # not invalidate an already-open client's optimistic revision.  The
        # reset is safe only behind expected_rev CAS and remains inside the
        # same transaction as both transcript representations.
        if preserve_rev and not authority:
            db.execute(
                'UPDATE conversations SET rev=? WHERE id=? AND user_id=?',
                (int(expected_rev), conv_id, user_id))

        rev_row = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conv_id, user_id)).fetchone()
        rev = (None if rev_row is None else int(
            rev_row['rev'] if hasattr(rev_row, 'keys') else rev_row[0]))
        return ConversationWriteResult(applied=True, rev=rev)


def mutate_conversation(
    db,
    conv_id: str,
    mutator,
    *,
    user_id=1,
    max_attempts: int = 5,
) -> ConversationMutationResult:
    """Replay a semantic message mutation until its revision CAS succeeds.

    The callback receives a private message list plus its exact repository
    snapshot and returns :class:`ConversationMutation`. Centralizing this
    loop prevents business code from inventing unsafe read/modify/write retry
    choreography or falling back to an unconditional overwrite on contention.
    """
    if not callable(mutator):
        raise TypeError('mutator must be callable')
    if max_attempts < 1:
        raise ValueError('max_attempts must be at least 1')
    for attempt in range(1, max_attempts + 1):
        snapshot = load_conversation(db, conv_id, user_id=user_id)
        if snapshot is None:
            return ConversationMutationResult(
                applied=False, rev=None, attempts=attempt, missing=True)
        messages = copy.deepcopy(snapshot.messages)
        decision = mutator(messages, snapshot)
        if decision is None or decision is False:
            decision = ConversationMutation(changed=False)
        elif decision is True:
            decision = ConversationMutation()
        elif not isinstance(decision, ConversationMutation):
            raise TypeError(
                'mutator must return ConversationMutation, bool, or None')
        if not decision.changed:
            return ConversationMutationResult(
                applied=False, rev=int(snapshot['rev']),
                value=decision.value, attempts=attempt)
        result = replace_messages(
            db, conv_id, messages, user_id=user_id,
            expected_rev=int(snapshot['rev']),
            metadata=decision.metadata,
            changed_seqs=decision.changed_seqs,
            full=decision.full,
        )
        if result.applied:
            return ConversationMutationResult(
                applied=True, rev=result.rev, value=decision.value,
                attempts=attempt)
    return ConversationMutationResult(
        applied=False, rev=None, attempts=max_attempts)


def refresh_conversation_search(
    db,
    conv_id: str,
    *,
    user_id=1,
    max_attempts: int = 5,
) -> ConversationWriteResult:
    """Rebuild derived search state from one authoritative transcript CAS.

    Maintenance jobs must not read the retired archive or hand the data layer
    caller-computed search text.  A concurrent transcript change advances
    ``rev`` and makes this operation retry from a fresh canonical snapshot.
    Derived-index repair intentionally does not advance the conversation rev.
    """
    from lib.conversations import build_search_text, update_conversation_fts

    for _attempt in range(max(1, int(max_attempts))):
        snapshot = load_conversation(
            db, conv_id, user_id=user_id, metadata_columns=('search_text',))
        if snapshot is None:
            return ConversationWriteResult(applied=False, rev=None)
        rev = int(snapshot['rev'])
        search_text = build_search_text(snapshot.messages)
        if search_text == str(snapshot.get('search_text') or ''):
            return ConversationWriteResult(applied=False, rev=rev)
        with write_transaction(db, label='refresh conversation search index'):
            cursor = db.execute(
                'UPDATE conversations SET search_text=? '
                'WHERE id=? AND user_id=? AND rev=?',
                (search_text, conv_id, user_id, rev))
            if getattr(cursor, 'rowcount', None) == 0:
                continue
            update_conversation_fts(db, conv_id, search_text)
            return ConversationWriteResult(applied=True, rev=rev)
    return ConversationWriteResult(applied=False, rev=None)


def delete_conversation(db, conv_id: str, *, user_id=1) -> ConversationDeleteResult:
    """Atomically delete one conversation and every application-owned child."""
    if not conv_id:
        raise ValueError('conv_id is required')
    from lib.conversations import update_conversation_fts

    with write_transaction(db, label='delete conversation cascade'):
        # Contentless SQLite FTS has no FK/trigger; retract its row while the
        # parent rowid still exists. It is derived state, so repair remains
        # possible if the best-effort helper encounters an engine limitation.
        update_conversation_fts(db, conv_id, '')
        message_cur = db.execute(
            'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        cold_archive_cur = db.execute(
            'DELETE FROM conversation_message_archives '
            'WHERE conv_id=? AND user_id=?', (conv_id, user_id))
        conversation_cur = db.execute(
            'DELETE FROM conversations WHERE id=? AND user_id=?',
            (conv_id, user_id))
        task_cur = db.execute(
            'DELETE FROM task_results WHERE conv_id=?', (conv_id,))
        archive_cur = db.execute(
            'DELETE FROM transcript_archive WHERE conv_id=?', (conv_id,))
        return ConversationDeleteResult(
            conversation_rows=int(getattr(conversation_cur, 'rowcount', 0) or 0),
            message_rows=int(getattr(message_cur, 'rowcount', 0) or 0),
            task_rows=int(getattr(task_cur, 'rowcount', 0) or 0),
            archive_rows=(
                int(getattr(archive_cur, 'rowcount', 0) or 0)
                + int(getattr(cold_archive_cur, 'rowcount', 0) or 0)
            ),
        )


__all__ = [
    'ConversationDeleteResult', 'ConversationIntegrityError',
    'ConversationMutation',
    'ConversationMutationResult', 'ConversationSnapshot',
    'ConversationWriteResult', 'conversation_rows_authoritative',
    'delete_conversation', 'iter_conversation_snapshots',
    'list_conversation_snapshots', 'load_conversation',
    'mutate_conversation', 'refresh_conversation_search', 'replace_messages',
    'update_message_translation_overlay', 'upsert_conversation',
]
