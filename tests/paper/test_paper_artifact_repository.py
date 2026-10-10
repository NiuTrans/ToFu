"""Typed, owner-scoped paper-artifact repository contracts."""

from __future__ import annotations

import sqlite3

import pytest

from lib.paper.artifact_repository import (
    PaperArtifactRepository,
    PaperNote,
    PaperPodcast,
    PaperReport,
    PaperReportReopen,
    PaperTranslation,
)


pytestmark = pytest.mark.unit


class _Client:
    def __init__(self) -> None:
        self.calls = []

    def query(self, operation, payload):
        self.calls.append(('query', operation, payload))
        if operation == 'paper.report.get':
            return {**payload, 'report': 'body', 'model': 'm',
                    'meta': {'kind': 'report'}, 'created_at': 10}
        if operation == 'paper.report.resolve':
            return {
                'paper_hash': payload['paper_hash'],
                'lang': payload.get('fallback_lang') or payload['preferred_lang'],
                'report': 'resolved', 'model': 'm', 'meta': {}, 'created_at': 10,
            }
        if operation == 'paper.report.reopen':
            return {
                'report': {
                    'paper_hash': payload['paper_hash'],
                    'lang': payload.get('fallback_lang') or payload['preferred_lang'],
                    'report': 'reopened', 'model': 'm', 'meta': {},
                    'created_at': 10,
                },
                'siblings': [{
                    'paper_hash': payload['paper_hash'], 'lang': sibling_lang,
                    'report': f'sibling-{sibling_lang}', 'model': '', 'meta': {},
                    'created_at': 11,
                } for sibling_lang in payload[
                    'sibling_langs_by_base'
                ].get(payload.get('fallback_lang') or payload['preferred_lang'], [])],
            }
        if operation == 'paper.report.excerpts':
            return [{
                'paper_hash': paper_hash, 'lang': payload['lang'],
                'report': f'excerpt-{paper_hash}', 'created_at': 10,
            } for paper_hash in payload['paper_hashes']]
        if operation == 'paper.report.latest':
            return {**payload, 'lang': 'en', 'report': 'latest', 'model': 'm',
                    'meta': {}, 'created_at': 11}
        if operation == 'paper.translation.get':
            return {**payload, 'text': '译文', 'model': 'm', 'created_at': 12}
        if operation == 'paper.note.list':
            return [{
                'id': 'n1', 'paper_hash': payload['paper_hash'],
                'lang': payload['lang'], 'anchor': {'quote': 'q'},
                'note': 'note', 'created_at': 1, 'updated_at': 2,
            }]
        if operation == 'paper.podcast.get':
            return {**payload, 'status': 'done', 'script_json': {'segments': []},
                    'meta': {}, 'created_at': 1, 'updated_at': 2}
        raise AssertionError(operation)

    def command(self, operation, payload, command_id):
        self.calls.append(('command', operation, payload, command_id))
        if operation.endswith('.upsert') or operation == 'paper.note.create':
            return {'saved': True}
        if operation == 'paper.note.update':
            return {'updated': True}
        if operation == 'paper.note.delete':
            return {'deleted': True}
        if operation == 'paper.report.second_pass.merge':
            return {'found': True, 'meta': {'merged': True}}
        if operation == 'paper.report.second_pass.accumulate':
            return {'found': True, 'meta': {'calls': 2}}
        raise AssertionError(operation)


def test_repository_injects_owner_into_every_operation():
    client = _Client()
    repo = PaperArtifactRepository(
        17, client_factory=lambda *, write=False: client)

    assert repo.get_report('hash', 'en').report == 'body'
    assert repo.get_report('hash', 'en', max_chars=6_000).report == 'body'
    resolved = repo.resolve_report('hash', 'zh', 'en')
    assert resolved.report == 'resolved' and resolved.lang == 'en'
    reopened = repo.reopen_report(
        'hash', 'zh', 'en',
        sibling_langs_by_base={
            'zh': ('insight:zh',),
            'en': ('insight:en', 'termfill:en'),
        })
    assert isinstance(reopened, PaperReportReopen)
    assert reopened.report.report == 'reopened' and reopened.report.lang == 'en'
    assert list(reopened.siblings) == ['insight:en', 'termfill:en']
    excerpts = repo.report_excerpts(
        ['hash-1', 'hash-1', 'hash-2'], 'en', max_chars=6_000)
    assert list(excerpts) == ['hash-1', 'hash-2']
    assert excerpts['hash-1'].report == 'excerpt-hash-1'
    assert repo.latest_report('hash').report == 'latest'
    assert repo.get_translation('hash', 'zh').text == '译文'
    assert repo.list_notes('hash', 'en')[0].note == 'note'
    assert repo.get_podcast('hash', 'short', 'en', 'voice').status == 'done'
    assert repo.put_report(
        PaperReport('hash', 'en', 'body', created_at=1), command_id='report')
    assert repo.put_translation(
        PaperTranslation('hash', 'zh', '译文', created_at=1),
        command_id='translation')
    note = PaperNote('n1', 'hash', 'en', {}, 'note', 1, 1)
    assert repo.create_note(note, command_id='note-create')
    assert repo.update_note('n1', 'changed', 2, command_id='note-update')
    assert repo.delete_note('n1', command_id='note-delete')
    assert repo.put_podcast(
        PaperPodcast('hash', 'short', 'en', 'voice', 'done'),
        command_id='podcast')
    assert repo.merge_report_second_pass(
        'hash', 'en', 'insight', {}, command_id='merge') == {'merged': True}
    assert repo.accumulate_report_second_pass(
        'hash', 'en', 'deepen', {}, command_id='accumulate') == {'calls': 2}

    assert client.calls
    for call in client.calls:
        assert call[2]['user_id'] == 17
    excerpt_call = next(
        call for call in client.calls
        if call[1] == 'paper.report.get'
        and call[2].get('max_report_chars') is not None)
    assert excerpt_call[2]['max_report_chars'] == 6_000
    batch_call = next(
        call for call in client.calls if call[1] == 'paper.report.excerpts')
    assert batch_call[2]['paper_hashes'] == ['hash-1', 'hash-2']
    assert batch_call[2]['max_report_chars'] == 6_000
    resolve_call = next(
        call for call in client.calls if call[1] == 'paper.report.resolve')
    assert resolve_call[2] == {
        'user_id': 17, 'paper_hash': 'hash',
        'preferred_lang': 'zh', 'fallback_lang': 'en',
    }
    reopen_call = next(
        call for call in client.calls if call[1] == 'paper.report.reopen')
    assert reopen_call[2] == {
        'user_id': 17, 'paper_hash': 'hash', 'preferred_lang': 'zh',
        'fallback_lang': 'en',
        'sibling_langs_by_base': {
            'zh': ['insight:zh'],
            'en': ['insight:en', 'termfill:en'],
        },
    }


def test_repository_rejects_implicit_owner_and_command_identity():
    with pytest.raises((TypeError, ValueError)):
        PaperArtifactRepository(0)

    repo = PaperArtifactRepository(
        1, client_factory=lambda *, write=False: _Client())
    with pytest.raises(ValueError, match='command_id'):
        repo.put_report(PaperReport('hash', 'en', 'body'), command_id='')
    with pytest.raises(ValueError, match='1..6000'):
        repo.get_report('hash', 'en', max_chars=6_001)
    with pytest.raises(ValueError, match='at most 40'):
        repo.report_excerpts(
            [f'hash-{index}' for index in range(41)],
            'en', max_chars=6_000)
    with pytest.raises(ValueError, match='at most 8'):
        repo.reopen_report(
            'hash', 'en', sibling_langs_by_base={
                'en': [f'sibling:{index}' for index in range(9)]})
    with pytest.raises(ValueError, match='unknown base'):
        repo.reopen_report(
            'hash', 'en', sibling_langs_by_base={'zh': ['insight:zh']})
    with pytest.raises(ValueError, match='contain sequences'):
        repo.reopen_report(
            'hash', 'en', sibling_langs_by_base={'en': 'insight:en'})


def test_application_boundary_uses_only_the_injected_storage_port(monkeypatch):
    """Repository behavior must use its injected storage port."""
    client = _Client()

    def _unexpected_sqlite_connection(*_args, **_kwargs):
        raise AssertionError('repository bypassed its injected storage port')

    monkeypatch.setattr(sqlite3, 'connect', _unexpected_sqlite_connection)
    repo = PaperArtifactRepository(
        23, client_factory=lambda *, write=False: client)

    assert repo.get_report('hash', 'en').report == 'body'
    assert repo.put_report(
        PaperReport('hash', 'en', 'body'), command_id='port-only')
    assert [call[1] for call in client.calls] == [
        'paper.report.get', 'paper.report.upsert',
    ]
    assert all(call[2]['user_id'] == 23 for call in client.calls)


def test_schema_33_repairs_version_32_ownerless_paper_tables(tmp_path):
    """A v32 version stamp must not hide ownerless legacy paper tables."""
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema

    connection = sqlite3.connect(tmp_path / 'ownerless-v32.db')
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        CREATE TABLE storage_meta(
            meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL);
        INSERT INTO storage_meta VALUES ('schema_version', '32');
        CREATE TABLE paper_reports(
            paper_hash TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en',
            report TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            meta TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
            PRIMARY KEY(paper_hash, lang));
        CREATE TABLE paper_translations(
            paper_hash TEXT NOT NULL, lang TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, PRIMARY KEY(paper_hash, lang));
        CREATE TABLE paper_podcasts(
            paper_hash TEXT NOT NULL, mode TEXT NOT NULL, lang TEXT NOT NULL,
            voice TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'generating',
            script_json TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '',
            duration_sec REAL NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '',
            tts_model TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            PRIMARY KEY(paper_hash, mode, lang, voice));
        CREATE TABLE paper_notes(
            id TEXT PRIMARY KEY, paper_hash TEXT NOT NULL DEFAULT '',
            lang TEXT NOT NULL DEFAULT '', anchor TEXT NOT NULL DEFAULT '{}',
            note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL);
        INSERT INTO paper_reports VALUES ('hash', 'en', 'body', 'm', '{}', 1);
        INSERT INTO paper_translations VALUES ('hash', 'zh', 'text', 'm', 2);
        INSERT INTO paper_podcasts VALUES (
            'hash', 'short', 'en', 'voice', 'done', '{}', '', 0,
            'm', 'tts', '{}', 3, 4);
        INSERT INTO paper_notes VALUES ('note', 'hash', 'en', '{}', 'n', 5, 6);
    ''')

    initialize_schema(SQLiteSession(connection))

    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0]
    for table_name in (
        'paper_reports', 'paper_translations', 'paper_podcasts', 'paper_notes',
    ):
        columns = {
            row['name']
            for row in connection.execute(f'PRAGMA table_info({table_name})')
        }
        assert 'user_id' in columns
        assert connection.execute(
            f'SELECT user_id FROM {table_name} LIMIT 1'
        ).fetchone()[0] == 1
    connection.close()

    assert int(version) == SCHEMA_VERSION
