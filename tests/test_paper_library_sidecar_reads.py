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
    from lib.paper.insight_engine import _context

    client = _Client()
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: client)
    monkeypatch.setattr(
        'lib.memory.relevance.score_items',
        lambda _query, _titles: [(1, 1.0), (0, 0.5)],
    )

    assert _context._library_context('current-hash', 'query') == [
        {'title': 'Another paper', 'arxiv_id': ''},
        {'title': 'Prior paper', 'arxiv_id': '2608.00001'},
    ]
    assert client.calls == [('paper.library.recent', {
        'exclude_paper_hash': 'current-hash', 'limit': 40,
    })]


def test_self_identity_queries_sidecar(monkeypatch):
    from lib.paper.insight_engine import _grounding

    client = _Client()
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: client)

    assert _grounding._self_identity('current-hash', '') == (
        '2608.00002', 'Current paper')
    assert client.calls == [
        ('paper.library.identity', {'paper_hash': 'current-hash'}),
    ]
