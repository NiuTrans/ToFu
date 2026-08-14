"""Safety contract for physically slimming row-authoritative conversations."""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def archive_env(tmp_path, monkeypatch):
    from lib.database import _core as core

    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'archive-offload.db'))
    db = core._new_sqlite_connection()
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    try:
        db.execute(
            'INSERT INTO conversations '
            '(id,user_id,title,messages,created_at,updated_at,settings,'
            'msg_count,search_text) VALUES (?,?,?,?,?,?,?,?,?)',
            ('archive-conv', 1, 'archive', '[]', 1, 1, '{}', 0, ''),
        )
        db.commit()
        yield db
    finally:
        db.close()
        core.restore_db_state(snapshot)


def _seed(db):
    from lib.database.conversation_repository import replace_messages

    messages = [{
        'role': 'assistant',
        'content': 'answer',
        '_msgId': 'archive-m0',
        'toolRounds': [{'output': 'x' * 262_144}],
    }]
    result = replace_messages(
        db, 'archive-conv', messages, expected_rev=0, full=True)
    assert result.applied
    return messages, result.rev


def test_offload_preserves_archive_and_canonical_header(
        archive_env, monkeypatch):
    from lib.database._access_policy import allow_transcript_archive_access
    from lib.database.conversation_repository import load_conversation
    from lib.database.message_archive_offload import (
        offload_frozen_message_archives,
    )

    messages, rev = _seed(archive_env)
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    result = offload_frozen_message_archives(archive_env, limit=1)

    with allow_transcript_archive_access():
        parent = archive_env.execute(
            'SELECT messages,rev,messages_rows_rev,msg_count '
            'FROM conversations WHERE id=?', ('archive-conv',)).fetchone()
    cold = archive_env.execute(
        'SELECT messages,source_rev,msg_count '
        'FROM conversation_message_archives WHERE conv_id=?',
        ('archive-conv',)).fetchone()
    snapshot = load_conversation(archive_env, 'archive-conv')
    assert result['archived'] == result['cleared'] == 1
    assert result['bytes_released'] > 262_144
    assert json.loads(parent['messages']) == []
    assert int(parent['rev']) == int(parent['messages_rows_rev']) == rev
    assert int(parent['msg_count']) == len(messages)
    assert json.loads(cold['messages']) == messages
    assert int(cold['source_rev']) == rev
    assert int(cold['msg_count']) == len(messages)
    assert snapshot.messages == messages

    # Idempotency: a committed archive marker removes the row from future
    # batches, so restart/retry never copies or clears it twice.
    again = offload_frozen_message_archives(archive_env, limit=1)
    assert again['candidates'] == again['archived'] == again['cleared'] == 0


def test_offload_fails_closed_on_stale_row_marker(archive_env, monkeypatch):
    from lib.database.message_archive_offload import (
        offload_frozen_message_archives,
    )

    _seed(archive_env)
    archive_env.execute(
        'UPDATE conversations SET messages_rows_rev=-1 WHERE id=?',
        ('archive-conv',))
    archive_env.commit()
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')

    with pytest.raises(RuntimeError, match='marker is stale'):
        offload_frozen_message_archives(archive_env, limit=1)
    assert archive_env.execute(
        'SELECT 1 FROM conversation_message_archives').fetchone() is None


def test_offload_rolls_back_archive_when_parent_clear_fails(
        archive_env, monkeypatch):
    from lib.database._access_policy import allow_transcript_archive_access
    from lib.database.message_archive_offload import (
        offload_frozen_message_archives,
    )

    messages, _rev = _seed(archive_env)
    archive_env.execute('''
        CREATE TRIGGER reject_archive_parent_clear
        BEFORE UPDATE OF messages ON conversations
        WHEN NEW.id = 'archive-conv'
        BEGIN
            SELECT RAISE(ABORT, 'injected archive clear failure');
        END
    ''')
    archive_env.commit()
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')

    with pytest.raises(Exception, match='injected archive clear failure'):
        offload_frozen_message_archives(archive_env, limit=1)

    assert archive_env.execute(
        'SELECT 1 FROM conversation_message_archives').fetchone() is None
    with allow_transcript_archive_access():
        parent = archive_env.execute(
            'SELECT messages FROM conversations WHERE id=?',
            ('archive-conv',)).fetchone()
    assert json.loads(parent['messages']) == messages
