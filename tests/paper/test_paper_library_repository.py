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
        if operation == 'paper.library.summaries':
            return [{
                'id': 'paper-1', 'title': 'A paper', 'paperHash': 'hash-1',
                'createdAt': 10, 'updatedAt': 20, 'hasReport': True,
            }]
        if operation == 'paper.library.list':
            return [{
                'id': 'paper-1', 'title': 'A paper', 'paperHash': 'hash-1',
                'parsedText': 'full body', 'qaHistory': [{'q': 'why'}],
                'images': [], 'babelCache': {}, 'createdAt': 10,
                'updatedAt': 20, 'hasReport': True,
            }]
        if operation == 'paper.library.get':
            return {
                'id': 'paper-1', 'title': 'A paper', 'paperHash': 'hash-1',
                'parsedText': 'full body', 'qaHistory': [{'q': 'why'}],
                'images': [], 'babelCache': {}, 'createdAt': 10,
                'updatedAt': 20, 'hasReport': True,
            }
        if operation == 'paper.library.reader':
            return {
                'id': 'paper-1', 'title': 'A paper', 'paperHash': 'hash-1',
                'parsedText': 'reader body', 'qaHistory': [{'q': 'why'}],
                'images': [], 'createdAt': 10, 'updatedAt': 20,
                'hasReport': True,
            }
        if operation == 'paper.library.inputs':
            return [{
                'id': 'paper-1', 'title': 'A paper',
                'arxivId': '2608.00001', 'paperHash': 'hash-1',
                'parsedText': 'body', 'parserVersion': 'parser-v1',
                'parsedTextLength': 100_000,
                'updatedAt': 20,
            }]
        if operation == 'paper.library.identity':
            max_chars = payload.get('max_text_chars')
            text = 'body' if max_chars is None else 'body'[:max_chars]
            return {'title': 'A paper', 'arxiv_id': '2608.00001',
                    'parsed_text': text,
                    'parsed_text_length': (
                        100_000 if payload.get('include_text_length', True)
                        else 0)}
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

    summaries = repository.list_summaries()
    assert len(summaries) == 1 and not summaries[0].parsed_text
    compatibility_rows = repository.list_entries()
    assert compatibility_rows[0].parsed_text == 'full body'
    entry = repository.get('paper-1')
    assert entry is not None and entry.has_report
    assert entry.parsed_text == 'full body'
    reader = repository.reader_detail('paper-1')
    assert reader is not None and reader.parsed_text == 'reader body'
    assert reader.babel_cache == {}
    inputs = repository.by_arxiv_ids(['2608.00001', '2608.00001'])
    assert len(inputs) == 1 and inputs[0].parsed_text == 'body'
    assert inputs[0].parsed_text_length == 100_000
    assert repository.identity('hash-1').parsed_text == 'body'
    metadata = repository.identity('hash-1', max_text_chars=0)
    assert metadata is not None and metadata.parsed_text == ''
    assert metadata.parsed_text_length == 100_000
    existence = repository.identity(
        'hash-1', max_text_chars=0, include_text_length=False)
    assert existence is not None and existence.parsed_text_length == 0
    assert repository.recent(exclude_paper_hash='hash-1') == [
        {'title': 'Prior', 'arxiv_id': '2607.00001'}]
    assert repository.put(entry, command_id='put-1')
    assert repository.backfill_title(
        'hash-1', 'Recovered', command_id='title-1')['updated'] == 1
    assert repository.delete('paper-1', command_id='delete-1')

    payloads = [call[2] for call in client.calls]
    assert payloads
    assert {payload['user_id'] for payload in payloads} == {73}
    input_call = next(call for call in client.calls
                      if call[1] == 'paper.library.inputs')
    assert input_call[2]['arxiv_ids'] == ['2608.00001']
    assert input_call[2]['max_text_chars'] == 0
    assert next(call for call in client.calls
                if call[1] == 'paper.library.get')[2]['id'] == 'paper-1'
    assert next(call for call in client.calls
                if call[1] == 'paper.library.reader')[2]['id'] == 'paper-1'
    identity_calls = [call for call in client.calls
                      if call[1] == 'paper.library.identity']
    assert 'max_text_chars' not in identity_calls[0][2]
    assert identity_calls[1][2]['max_text_chars'] == 0
    assert identity_calls[2][2] == {
        'user_id': 73,
        'paper_hash': 'hash-1',
        'max_text_chars': 0,
        'include_text_length': False,
    }


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

    summary = entry.to_summary_projection()
    assert not {'parsedText', 'qaHistory', 'images', 'babelCache'} & summary.keys()


def test_repository_rejects_implicit_owner():
    with pytest.raises(ValueError, match='numeric user_id'):
        PaperLibraryRepository(None)


def test_repository_rejects_more_than_40_target_papers():
    repository = PaperLibraryRepository(
        73, client_factory=lambda **_kwargs: CapturingClient())
    with pytest.raises(ValueError, match='at most 40'):
        repository.by_arxiv_ids(
            [f'2608.{index:05d}' for index in range(41)])


def test_repository_rejects_non_string_target_paper_ids():
    repository = PaperLibraryRepository(
        73, client_factory=lambda **_kwargs: CapturingClient())
    with pytest.raises(ValueError, match='must be strings'):
        repository.by_arxiv_ids(['2608.00001', {'id': '2608.00002'}])


def test_repository_rejects_unbounded_target_text_projection():
    repository = PaperLibraryRepository(
        73, client_factory=lambda **_kwargs: CapturingClient())
    with pytest.raises(ValueError, match='0..6000'):
        repository.by_arxiv_ids(['2608.00001'], max_text_chars=6_001)


def test_repository_rejects_unbounded_identity_text_projection():
    repository = PaperLibraryRepository(
        73, client_factory=lambda **_kwargs: CapturingClient())
    with pytest.raises(ValueError, match='0..1000000'):
        repository.identity('hash-1', max_text_chars=1_000_001)


def test_repository_only_omits_length_for_zero_text_projection():
    repository = PaperLibraryRepository(
        73, client_factory=lambda **_kwargs: CapturingClient())
    with pytest.raises(ValueError, match='zero-text'):
        repository.identity(
            'hash-1', max_text_chars=1, include_text_length=False)
