"""Runtime boundary for the frozen conversations.messages archive."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_archive_guard_is_exact_and_has_an_explicit_admin_escape(monkeypatch):
    from lib.database._access_policy import (
        TranscriptArchiveAccessError,
        allow_transcript_archive_access,
        enforce_sql_access,
    )

    monkeypatch.setenv('TOFU_SERVER_PROCESS', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')

    with pytest.raises(TranscriptArchiveAccessError):
        enforce_sql_access(
            'SELECT json_array_length(c.messages) FROM conversations c')
    with pytest.raises(TranscriptArchiveAccessError):
        enforce_sql_access(
            'UPDATE conversations SET messages=? WHERE id=?')
    with pytest.raises(TranscriptArchiveAccessError):
        enforce_sql_access(
            'INSERT INTO "conversations" (id, "messages") VALUES (?, ?)')
    with pytest.raises(TranscriptArchiveAccessError):
        enforce_sql_access(
            'WITH candidate AS (SELECT 1) '
            'UPDATE conversations SET messages=? WHERE id=?')
    with pytest.raises(TranscriptArchiveAccessError):
        enforce_sql_access(
            'DELETE FROM conversations WHERE json_array_length(messages)=0')

    # The normalized child table and a string literal are not archive reads.
    enforce_sql_access(
        'SELECT cm.meta FROM conversations c '
        'JOIN conversation_messages cm ON cm.conv_id=c.id')
    enforce_sql_access(
        "SELECT 'messages' AS label FROM conversations")
    enforce_sql_access(
        'UPDATE conversations SET messages_rows_rev=? WHERE id=?')
    # Defining the historical trigger mentions UPDATE/messages in its body,
    # but CREATE itself does not touch archive data.
    enforce_sql_access(
        'CREATE TRIGGER t AFTER UPDATE OF messages ON conversations BEGIN '
        'SELECT 1; END')

    with allow_transcript_archive_access():
        enforce_sql_access('SELECT messages FROM conversations')
        enforce_sql_access(
            'UPDATE conversations SET messages=? WHERE id=?')


def test_archive_guard_is_authority_wide_and_inert_when_disabled(monkeypatch):
    from lib.database._access_policy import (
        TranscriptArchiveAccessError,
        enforce_sql_access,
    )

    monkeypatch.delenv('TOFU_SERVER_PROCESS', raising=False)
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    with pytest.raises(TranscriptArchiveAccessError):
        enforce_sql_access('SELECT messages FROM conversations')

    monkeypatch.setenv('TOFU_SERVER_PROCESS', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '0')
    enforce_sql_access('SELECT messages FROM conversations')
