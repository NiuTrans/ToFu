"""Typed, owner-scoped paper-library repository contract."""

from __future__ import annotations

import inspect

import pytest

from lib.paper.library_repository import (
    PaperLibraryEntry,
    PaperLibraryRepository,
)


pytestmark = pytest.mark.unit


class CapturingClient:
    def __init__(self) -> None:
        self.calls = []

    def query(self, operation, payload):
        self.calls.append(('query', operation, dict(payload)))
        if operation == 'paper.library.list':
            return [{
                'id': 'paper-1', 'title': 'A paper', 'paperHash': 'hash-1',
                'qaHistory': [{'q': 'why'}], 'images': [], 'babelCache': {},
                'createdAt': 10, 'updatedAt': 20, 'hasReport': True,
            }]
        if operation == 'paper.library.identity':
            return {'title': 'A paper', 'arxiv_id': '2608.00001',
                    'parsed_text': 'body'}
        if operation == 'paper.library.recent':
            return [{'title': 'Prior', 'arxiv_id': '2607.00001'}]
        return None

    def command(self, operation, payload, command_id):
        self.calls.append(('command', operation, dict(payload), command_id))
        if operation == 'paper.library.delete':
            return {'deleted': True}
        if operation == 'paper.library.title.backfill':
            return {'title': payload['title'], 'updated': 1}
        return {'saved': True}


def test_repository_scopes_every_operation_to_its_explicit_owner():
    client = CapturingClient()
    repository = PaperLibraryRepository(
        73, client_factory=lambda **_kwargs: client)

    entry = repository.get('paper-1')
    assert entry is not None and entry.has_report
    assert repository.identity('hash-1').parsed_text == 'body'
    assert repository.recent(exclude_paper_hash='hash-1') == [
        {'title': 'Prior', 'arxiv_id': '2607.00001'}]
    assert repository.put(entry, command_id='put-1')
    assert repository.backfill_title(
        'hash-1', 'Recovered', command_id='title-1')['updated'] == 1
    assert repository.delete('paper-1', command_id='delete-1')

    payloads = [call[2] for call in client.calls]
    assert payloads
    assert {payload['user_id'] for payload in payloads} == {73}


def test_entry_round_trip_centralizes_json_column_encoding():
    entry = PaperLibraryEntry(
        paper_id='paper-1', qa_history=[{'q': 'why'}],
        images=[{'path': 'figure.png'}], babel_cache={'zh': '标题'},
    )
    payload = entry.to_storage_payload(owner_user_id=9)

    assert payload['user_id'] == 9
    assert payload['qa_history'] == '[{"q": "why"}]'
    assert 'figure.png' in payload['images']
    assert '标题' in payload['babel_cache']


def test_repository_rejects_implicit_owner():
    with pytest.raises(ValueError, match='numeric user_id'):
        PaperLibraryRepository(None)
