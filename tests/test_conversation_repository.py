"""Atomic contract for the centralized conversation repository."""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def repo_env(tmp_path, monkeypatch):
    from lib.database import _core as core

    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'conversation-repo.db'))
    db = core._new_sqlite_connection()
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    try:
        db.execute(
            'INSERT INTO conversations '
            '(id, user_id, title, messages, created_at, updated_at, settings, '
            ' msg_count, search_text) VALUES (?, 1, ?, ?, 1, 1, ?, 0, ?)',
            ('repo-conv', 'before', '[]', '{}', ''))
        db.commit()
        yield db
    finally:
        db.close()
        core.restore_db_state(snapshot)


def _blob(db):
    row = db.execute(
        'SELECT title, messages, rev FROM conversations '
        'WHERE id=? AND user_id=1', ('repo-conv',)).fetchone()
    return row['title'], json.loads(row['messages']), int(row['rev'])


def _rows(db):
    from lib.database.messages_rows import row_to_message
    rows = db.execute(
        'SELECT * FROM conversation_messages WHERE conv_id=? ORDER BY seq',
        ('repo-conv',)).fetchall()
    return [row_to_message(row) for row in rows]


def test_replace_messages_advances_blob_rows_and_revision_together(repo_env):
    from lib.database.conversation_repository import replace_messages

    db = repo_env
    messages = [{'role': 'user', 'content': 'hello', '_msgId': 'repo-m0'}]
    result = replace_messages(
        db, 'repo-conv', messages, expected_rev=0,
        metadata={'title': 'after', 'updated_at': 2, 'search_text': 'hello'},
        full=True)

    assert result.applied is True
    assert result.rev == 1
    assert _blob(db) == ('after', messages, 1)
    assert _rows(db) == messages


def test_transient_replace_can_defer_search_projection(repo_env):
    from lib.database.conversation_repository import replace_messages

    db = repo_env
    db.execute(
        "UPDATE conversations SET search_text='settled index' "
        "WHERE id='repo-conv'")
    db.commit()
    messages = [{
        'role': 'assistant', 'content': 'unsettled streaming delta',
        '_msgId': 'repo-partial-m0',
    }]

    result = replace_messages(
        db, 'repo-conv', messages, expected_rev=0,
        metadata={'updated_at': 2}, full=True, refresh_search=False)

    row = db.execute(
        'SELECT search_text FROM conversations WHERE id=?',
        ('repo-conv',)).fetchone()
    assert result.applied is True
    assert row['search_text'] == 'settled index'
    assert _rows(db) == messages


def test_revision_cas_miss_changes_neither_representation(repo_env):
    from lib.database.conversation_repository import replace_messages

    db = repo_env
    stable = [{'role': 'assistant', 'content': 'stable', '_msgId': 'repo-m1'}]
    first = replace_messages(db, 'repo-conv', stable, expected_rev=0, full=True)
    assert first.applied

    stale = [{'role': 'assistant', 'content': 'stale', '_msgId': 'repo-m1'}]
    missed = replace_messages(
        db, 'repo-conv', stale, expected_rev=0,
        metadata={'title': 'must-not-land'})
    assert missed == type(missed)(applied=False, rev=None)
    assert _blob(db) == ('before', stable, first.rev)
    assert _rows(db) == stable


def test_row_failure_rolls_back_blob_and_metadata(repo_env, monkeypatch):
    from lib.database import messages_rows as mr
    from lib.database.conversation_repository import replace_messages

    db = repo_env
    stable = [{'role': 'user', 'content': 'stable', '_msgId': 'repo-m2'}]
    first = replace_messages(db, 'repo-conv', stable, expected_rev=0, full=True)

    def _fail(*args, **kwargs):
        raise RuntimeError('injected row-store failure')

    monkeypatch.setattr(mr, '_mirror_conv_rows', _fail)
    edited = [{'role': 'user', 'content': 'partial', '_msgId': 'repo-m2'}]
    with pytest.raises(RuntimeError, match='row-store failure'):
        replace_messages(
            db, 'repo-conv', edited, expected_rev=first.rev,
            metadata={'title': 'partial-title'}, changed_seqs=[0])

    assert _blob(db) == ('before', stable, first.rev)
    assert _rows(db) == stable


def test_metadata_identifiers_are_whitelisted(repo_env):
    from lib.database.conversation_repository import replace_messages

    with pytest.raises(ValueError, match='unsupported.*drop_table'):
        replace_messages(
            repo_env, 'repo-conv', [], metadata={'drop_table': 'nope'})


def test_upsert_conversation_owns_blob_rows_and_fts(repo_env):
    from lib.database.conversation_repository import upsert_conversation

    db = repo_env
    messages = [{'role': 'user', 'content': 'indexed phrase',
                 '_msgId': 'repo-upsert-m0'}]
    result = upsert_conversation(
        db, 'repo-upsert', messages, title='created',
        created_at=10, updated_at=11, settings='{"source":"test"}',
        search_text='indexed phrase')
    assert result.applied
    blob = db.execute(
        'SELECT messages, settings, rev FROM conversations WHERE id=?',
        ('repo-upsert',)).fetchone()
    assert json.loads(blob['messages']) == messages
    assert json.loads(blob['settings']) == {'source': 'test'}
    assert _rows_for(db, 'repo-upsert') == messages


def test_upsert_requires_cas_for_existing_and_creates_rows_only_in_authority(
        repo_env, monkeypatch):
    from lib.database.conversation_repository import (
        ConversationIntegrityError,
        load_conversation,
        upsert_conversation,
    )

    with pytest.raises(ConversationIntegrityError,
                       match='existing conversation requires expected_rev'):
        upsert_conversation(
            repo_env, 'repo-conv', [], title='unsafe',
            created_at=1, updated_at=1)

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    messages = [
        {'role': 'assistant', 'content': 'created canonical',
         '_msgId': 'authority-create-m0'}]
    created = upsert_conversation(
        repo_env, 'authority-create', messages, title='row create',
        created_at=20, updated_at=21, full=True)
    from lib.database._access_policy import allow_transcript_archive_access
    with allow_transcript_archive_access():
        archived = repo_env.execute(
            'SELECT messages FROM conversations WHERE id=?',
            ('authority-create',)).fetchone()['messages']
    assert json.loads(archived) == []
    assert created.applied
    snapshot = load_conversation(repo_env, 'authority-create')
    assert snapshot.messages == messages and snapshot.source == 'rows'


def test_load_conversation_prefers_verified_rows(repo_env):
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
    )

    messages = [{'role': 'user', 'content': 'canonical', '_msgId': 'load-m0'}]
    replace_messages(repo_env, 'repo-conv', messages, expected_rev=0, full=True)

    snapshot = load_conversation(
        repo_env, 'repo-conv', metadata_columns=('title', 'settings'))
    assert snapshot is not None
    assert snapshot.messages == messages
    assert snapshot.source == 'rows'
    assert snapshot['title'] == 'before'


def test_snapshot_iterator_and_materialized_list_share_authority_path(repo_env):
    from lib.database.conversation_repository import (
        iter_conversation_snapshots,
        list_conversation_snapshots,
        upsert_conversation,
    )

    upsert_conversation(
        repo_env, 'repo-conv-2',
        [{'role': 'user', 'content': 'second', '_msgId': 'iter-m0'}],
        title='second', created_at=2, updated_at=2, full=True)

    stream = iter_conversation_snapshots(
        repo_env, user_id=1, order_by='id_asc')
    assert iter(stream) is stream
    streamed = list(stream)
    materialized = list_conversation_snapshots(
        repo_env, user_id=1, order_by='id_asc')
    assert [row['id'] for row in streamed] == [
        'repo-conv', 'repo-conv-2']
    assert [row['id'] for row in materialized] == [
        row['id'] for row in streamed]
    assert materialized[-1].messages[0]['content'] == 'second'


def test_load_conversation_transition_falls_back_but_authority_fails_loud(
        repo_env, monkeypatch):
    from lib.database.conversation_repository import (
        ConversationIntegrityError,
        load_conversation,
    )

    transitional = load_conversation(repo_env, 'repo-conv')
    assert transitional is not None
    assert transitional.messages == []
    assert transitional.source == 'legacy_blob'

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    with pytest.raises(ConversationIntegrityError,
                       match='canonical message rows are not current'):
        load_conversation(repo_env, 'repo-conv')


def test_preserve_rev_is_atomic_and_requires_a_cas(repo_env):
    from lib.database.conversation_repository import replace_messages

    messages = [{'role': 'assistant', 'content': 'enriched', '_msgId': 'rev-m0'}]
    result = replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0,
        preserve_rev=True, full=True)
    assert result.applied and result.rev == 0
    assert _blob(repo_env) == ('before', messages, 0)
    assert _rows(repo_env) == messages

    with pytest.raises(ValueError, match='preserve_rev requires expected_rev'):
        replace_messages(
            repo_env, 'repo-conv', messages, preserve_rev=True)


def test_row_authority_advances_rows_and_revision_without_rewriting_archive(
        repo_env, monkeypatch):
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
    )

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    canonical = [
        {'role': 'user', 'content': 'rows only', '_msgId': 'authority-m0'}]
    result = replace_messages(
        repo_env, 'repo-conv', canonical, expected_rev=0, full=True)

    from lib.database._access_policy import allow_transcript_archive_access
    with allow_transcript_archive_access():
        title, archived, rev = _blob(repo_env)
    marker = repo_env.execute(
        'SELECT messages_rows_rev FROM conversations WHERE id=?',
        ('repo-conv',)).fetchone()['messages_rows_rev']
    assert result.applied and result.rev == 1
    assert (title, archived, rev) == ('before', [], 1)
    assert int(marker) == 1
    assert _rows(repo_env) == canonical
    snapshot = load_conversation(repo_env, 'repo-conv')
    assert snapshot.messages == canonical
    assert snapshot.source == 'rows'


def test_translation_overlay_keeps_large_message_meta_byte_identical(
        repo_env, monkeypatch):
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
        update_message_translation_overlay,
    )

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    canonical = [{
        'role': 'assistant',
        'content': 'English answer',
        '_msgId': 'translation-overlay-m0',
        'toolRounds': [{'output': 'x' * 262_144}],
        'segments': [
            {'type': 'text', 'llmRound': 1, 'content': 'Narration'},
            {'type': 'tool', 'llmRound': 1, 'name': 'probe'},
        ],
    }]
    seeded = replace_messages(
        repo_env, 'repo-conv', canonical, expected_rev=0, full=True)
    before = repo_env.execute(
        'SELECT meta FROM conversation_messages WHERE conv_id=? AND seq=0',
        ('repo-conv',)).fetchone()['meta']

    snapshot = load_conversation(repo_env, 'repo-conv')
    message = snapshot.messages[0]
    message['translatedContent'] = '中文答案'
    message['_showingTranslation'] = True
    message['_translateDone'] = True
    message['_translateModel'] = 'translator-test'
    message['segments'][0]['translatedText'] = '中文旁白'
    result = update_message_translation_overlay(
        repo_env, 'repo-conv', 0, message, snapshot.messages,
        expected_rev=seeded.rev, updated_at=3)

    stored = repo_env.execute(
        'SELECT meta, translation_state, translated_content '
        'FROM conversation_messages WHERE conv_id=? AND seq=0',
        ('repo-conv',)).fetchone()
    parent = repo_env.execute(
        'SELECT rev, messages_rows_rev FROM conversations WHERE id=?',
        ('repo-conv',)).fetchone()
    hydrated = load_conversation(repo_env, 'repo-conv').messages[0]
    assert result.applied and result.rev == seeded.rev + 1
    assert stored['meta'] == before
    assert len(stored['translation_state']) < 1_024
    assert stored['translated_content'] == '中文答案'
    assert hydrated['translatedContent'] == '中文答案'
    assert hydrated['segments'][0]['translatedText'] == '中文旁白'
    assert int(parent['rev']) == int(parent['messages_rows_rev']) == result.rev


def test_translation_overlay_rolls_back_parent_when_child_is_missing(
        repo_env, monkeypatch):
    from lib.database.conversation_repository import (
        ConversationIntegrityError,
        load_conversation,
        replace_messages,
        update_message_translation_overlay,
    )

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    messages = [{
        'role': 'assistant', 'content': 'English', '_msgId': 'missing-child-m0'}]
    seeded = replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0, full=True)
    snapshot = load_conversation(repo_env, 'repo-conv')
    snapshot.messages[0]['translatedContent'] = '中文'

    # Simulate child-row loss after the canonical snapshot was read. The
    # parent CAS runs first, so only a real transaction rollback can keep the
    # revision/header unchanged when the child update discovers the loss.
    repo_env.execute(
        'DELETE FROM conversation_messages WHERE conv_id=? AND seq=0',
        ('repo-conv',))
    repo_env.commit()
    with pytest.raises(ConversationIntegrityError, match='target row is missing'):
        update_message_translation_overlay(
            repo_env, 'repo-conv', 0, snapshot.messages[0], snapshot.messages,
            expected_rev=seeded.rev, updated_at=99)

    parent = repo_env.execute(
        'SELECT rev,messages_rows_rev,updated_at FROM conversations WHERE id=?',
        ('repo-conv',)).fetchone()
    assert int(parent['rev']) == int(parent['messages_rows_rev']) == seeded.rev
    assert int(parent['updated_at']) != 99


def test_real_translate_commit_selects_overlay_lane(repo_env, monkeypatch):
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
    )
    from lib.translate.commit import _commit_translation_to_db

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    messages = [{
        'role': 'assistant',
        'content': 'English answer',
        '_msgId': 'real-translate-overlay-m0',
        'toolRounds': [{'output': 'x' * 262_144}],
        'segments': [
            {'type': 'text', 'llmRound': 7, 'text': 'Narration'},
        ],
    }]
    assert replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0, full=True).applied
    before = repo_env.execute(
        'SELECT meta FROM conversation_messages WHERE conv_id=? AND seq=0',
        ('repo-conv',)).fetchone()['meta']
    monkeypatch.setattr('lib.database.get_thread_db', lambda *_args: repo_env)
    monkeypatch.setattr(
        'lib.conversations.notify_conv_changed', lambda *_args, **_kwargs: None)

    _commit_translation_to_db(
        'repo-conv', 0, 'translatedContent', '中文答案',
        model='translator-test', msg_id='real-translate-overlay-m0',
        segment_translations={7: '中文旁白'},
    )

    stored = repo_env.execute(
        'SELECT meta,translation_state FROM conversation_messages '
        'WHERE conv_id=? AND seq=0', ('repo-conv',)).fetchone()
    hydrated = load_conversation(repo_env, 'repo-conv').messages[0]
    assert stored['meta'] == before
    assert len(stored['translation_state']) < 1_024
    assert hydrated['translatedContent'] == '中文答案'
    assert hydrated['segments'][0]['translatedText'] == '中文旁白'


def test_authority_blocks_archive_sql_in_every_process_but_repository_reads(
        repo_env, monkeypatch):
    from lib.database._access_policy import TranscriptArchiveAccessError
    from lib.database.conversation_repository import (
        load_conversation,
        replace_messages,
        upsert_conversation,
    )

    messages = [
        {'role': 'user', 'content': 'guarded', '_msgId': 'guard-m0'}]
    assert replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0, full=True).applied

    monkeypatch.delenv('TOFU_SERVER_PROCESS', raising=False)
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    with pytest.raises(TranscriptArchiveAccessError,
                       match='conversation_repository'):
        repo_env.execute(
            'SELECT c.messages FROM conversations AS c WHERE c.id=?',
            ('repo-conv',)).fetchone()
    with pytest.raises(TranscriptArchiveAccessError,
                       match='conversation_repository'):
        repo_env.execute(
            'UPDATE conversations SET messages=? WHERE id=?',
            ('[]', 'repo-conv'))
    with pytest.raises(TranscriptArchiveAccessError,
                       match='conversation_repository'):
        repo_env.executescript(
            "UPDATE conversations SET messages='[]' "
            "WHERE id='repo-conv';")
    with pytest.raises(TranscriptArchiveAccessError,
                       match='conversation_repository'):
        repo_env.executemany(
            'UPDATE conversations SET messages=? WHERE id=?',
            [('[]', 'repo-conv')])

    snapshot = load_conversation(repo_env, 'repo-conv')
    assert snapshot.messages == messages
    assert snapshot.source == 'rows'

    created_messages = [{
        'role': 'assistant', 'content': 'repository create',
        '_msgId': 'guard-create-m0',
    }]
    created = upsert_conversation(
        repo_env, 'guard-create', created_messages,
        title='guarded create', created_at=10, updated_at=11, full=True)
    assert created.applied
    assert load_conversation(
        repo_env, 'guard-create').messages == created_messages


def test_sqlite_search_selfheal_uses_canonical_rows_in_authority(
        repo_env, monkeypatch):
    from lib.database.conversation_repository import replace_messages
    from lib.database._schema_sqlite._selfheal import _backfill_search_fts

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    messages = [{
        'role': 'user', 'content': 'canonical search phrase',
        '_msgId': 'search-heal-m0',
    }]
    assert replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0, full=True).applied
    repo_env.execute(
        "UPDATE conversations SET search_text='' WHERE id=?",
        ('repo-conv',))
    repo_env.commit()

    _backfill_search_fts(repo_env)

    row = repo_env.execute(
        'SELECT search_text FROM conversations WHERE id=?',
        ('repo-conv',)).fetchone()
    assert 'canonical search phrase' in row['search_text']


def test_authority_startup_preflight_rejects_stale_and_accepts_canonical(
        repo_env, monkeypatch):
    from lib.database.conversation_repository import replace_messages
    from lib.database.messages_rows import assert_rows_authority_ready

    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    with pytest.raises(RuntimeError, match='preflight failed'):
        assert_rows_authority_ready(repo_env)

    messages = [{
        'role': 'assistant', 'content': 'ready', '_msgId': 'preflight-m0'}]
    assert replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0, full=True).applied
    assert_rows_authority_ready(repo_env)


def test_mutate_conversation_replays_semantic_change_after_cas_loss(repo_env):
    from lib.database.conversation_repository import (
        ConversationMutation,
        mutate_conversation,
        replace_messages,
    )

    seed = [{'role': 'user', 'content': 'seed', '_msgId': 'mutate-m0'}]
    assert replace_messages(
        repo_env, 'repo-conv', seed, expected_rev=0, full=True).applied
    calls = 0

    def append_without_clobber(messages, snapshot):
        nonlocal calls
        calls += 1
        if calls == 1:
            concurrent = list(messages) + [
                {'role': 'assistant', 'content': 'concurrent',
                 '_msgId': 'mutate-concurrent'}]
            assert replace_messages(
                repo_env, 'repo-conv', concurrent,
                expected_rev=snapshot['rev'], full=True).applied
        messages.append(
            {'role': 'user', 'content': 'semantic append',
             '_msgId': 'mutate-owned'})
        return ConversationMutation(value='appended')

    result = mutate_conversation(
        repo_env, 'repo-conv', append_without_clobber, max_attempts=3)
    assert result.applied and result.attempts == 2 and result.value == 'appended'
    assert [m['_msgId'] for m in _rows(repo_env)] == [
        'mutate-m0', 'mutate-concurrent', 'mutate-owned']


def test_delete_conversation_cascades_canonical_rows_atomically(repo_env):
    from lib.database.conversation_repository import (
        delete_conversation,
        replace_messages,
    )

    messages = [{'role': 'user', 'content': 'delete me', '_msgId': 'delete-m0'}]
    assert replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0, full=True).applied
    repo_env.execute(
        'INSERT INTO conversation_message_archives '
        '(conv_id,user_id,messages,source_rev,msg_count,archived_at) '
        'VALUES (?,?,?,?,?,?)',
        ('repo-conv', 1, json.dumps(messages), 1, 1, 1))
    repo_env.commit()
    result = delete_conversation(repo_env, 'repo-conv')
    assert result.conversation_rows == 1
    assert result.message_rows == 1
    assert result.archive_rows == 1
    assert repo_env.execute(
        'SELECT 1 FROM conversations WHERE id=?', ('repo-conv',)).fetchone() is None
    assert repo_env.execute(
        'SELECT 1 FROM conversation_message_archives WHERE conv_id=?',
        ('repo-conv',)).fetchone() is None
    assert _rows(repo_env) == []


def test_refresh_search_derives_from_authoritative_rows_without_bumping_rev(
        repo_env):
    from lib.database.conversation_repository import (
        refresh_conversation_search,
        replace_messages,
    )

    messages = [{
        'role': 'user', 'content': 'translated',
        'originalContent': 'original needle', '_msgId': 'search-m0'}]
    seeded = replace_messages(
        repo_env, 'repo-conv', messages, expected_rev=0, full=True)
    repo_env.execute(
        "UPDATE conversations SET search_text='stale' WHERE id='repo-conv'")
    repo_env.commit()

    result = refresh_conversation_search(repo_env, 'repo-conv')
    row = repo_env.execute(
        'SELECT rev, search_text FROM conversations WHERE id=?',
        ('repo-conv',)).fetchone()
    assert result.applied is True
    assert int(row['rev']) == seeded.rev
    assert 'original needle' in row['search_text']


def test_refresh_search_rolls_back_parent_when_fts_refresh_fails(
        repo_env, monkeypatch):
    import lib.conversations as conversations
    from lib.database.conversation_repository import (
        refresh_conversation_search,
        replace_messages,
    )

    assert replace_messages(
        repo_env, 'repo-conv',
        [{'role': 'user', 'content': 'canonical search', '_msgId': 'search-m1'}],
        expected_rev=0, full=True).applied
    repo_env.execute(
        "UPDATE conversations SET search_text='stale' WHERE id='repo-conv'")
    repo_env.commit()

    def fail_fts(*_args, **_kwargs):
        raise RuntimeError('injected FTS failure')

    monkeypatch.setattr(conversations, 'update_conversation_fts', fail_fts)
    with pytest.raises(RuntimeError, match='injected FTS failure'):
        refresh_conversation_search(repo_env, 'repo-conv')
    row = repo_env.execute(
        'SELECT search_text FROM conversations WHERE id=?',
        ('repo-conv',)).fetchone()
    assert row['search_text'] == 'stale'


def _rows_for(db, conv_id):
    from lib.database.messages_rows import row_to_message
    rows = db.execute(
        'SELECT * FROM conversation_messages WHERE conv_id=? ORDER BY seq',
        (conv_id,)).fetchall()
    return [row_to_message(row) for row in rows]
