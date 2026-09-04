"""Paper insight readers use named library operations, never connections."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


class _Client:
    def __init__(self):
        self.calls = []

    def query(self, operation, payload):
        self.calls.append((operation, payload))
        if operation == 'paper.library.recent':
            return [
                {'title': 'Prior paper', 'arxiv_id': '2608.00001'},
                {'title': 'Another paper', 'arxiv_id': ''},
            ]
        if operation == 'paper.library.identity':
            return {
                'title': 'Current paper', 'arxiv_id': '2608.00002',
                'parsed_text': 'body',
            }
        raise AssertionError(operation)


def test_reader_context_queries_sidecar(monkeypatch):
    import lib.paper.insight_engine._context as _context

    client = _Client()
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: client)
    monkeypatch.setattr(
        'lib.memory.relevance.score_items',
        lambda _query, _titles: [(1, 1.0), (0, 0.5)],
    )

    assert _context._library_context(
        'current-hash', 'query', user_id=7) == [
        {'title': 'Another paper', 'arxiv_id': ''},
        {'title': 'Prior paper', 'arxiv_id': '2608.00001'},
    ]
    assert client.calls == [('paper.library.recent', {
        'user_id': 7, 'exclude_paper_hash': 'current-hash', 'limit': 40,
    })]


def test_self_identity_queries_sidecar(monkeypatch):
    import lib.paper.insight_engine._grounding as _grounding

    client = _Client()
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: client)

    assert _grounding._self_identity(
        'current-hash', '', user_id=7) == (
        '2608.00002', 'Current paper')
    assert client.calls == [
        ('paper.library.identity', {
            'user_id': 7, 'paper_hash': 'current-hash',
            'max_text_chars': 0}),
    ]


def test_podcast_fallback_requests_only_its_prompt_source_ceiling(monkeypatch):
    import lib.paper.artifact_repository as artifacts
    import lib.paper.podcast_engine.worker as worker

    class _Artifacts:
        def __init__(self, owner):
            assert owner == 7

        def get_report(self, *_args):
            return None

        def get_translation(self, *_args):
            return None

    client = _Client()
    monkeypatch.setattr(artifacts, 'PaperArtifactRepository', _Artifacts)
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: client)

    text, source_kind = worker.load_source_text(
        'current-hash', 'en', user_id=7)

    assert (text, source_kind) == ('body', 'parsed_text')
    assert client.calls == [
        ('paper.library.identity', {
            'user_id': 7, 'paper_hash': 'current-hash',
            'max_text_chars': 40_000,
        }),
    ]


def test_existence_projection_does_not_scan_or_select_paper_text():
    from lib.storage_sidecar.operations_pkg._papers import (
        _paper_library_identity,
    )

    class _Session:
        def __init__(self):
            self.sql = ''
            self.args = ()

        def fetch_one(self, sql, args):
            self.sql = sql
            self.args = args
            return {
                'title': 'Paper',
                'arxiv_id': '',
                'parsed_text': '',
                'parsed_text_length': 0,
            }

    session = _Session()
    result = _paper_library_identity(session, {
        'user_id': 7,
        'paper_hash': 'a' * 32,
        'max_text_chars': 0,
        'include_text_length': False,
    })

    assert result['parsed_text'] == ''
    assert result['parsed_text_length'] == 0
    assert 'substr(' not in session.sql.lower()
    assert 'length(' not in session.sql.lower()
    assert session.args == (7, 'a' * 32)
