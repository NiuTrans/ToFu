"""Atomicity, integrity, and bounded-read contracts for knowledge storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import threading
import time

import pytest

from lib.knowledge.repository import KnowledgeRepository
from lib.storage.errors import StorageError


pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)

TEST_OWNER_USER_ID = 1


def _document(
    identity: str,
    *,
    name: str = 'document.md',
    kind: str = '.md',
    chunks: list[dict] | None = None,
    assets: list[dict] | None = None,
    created_at: float | None = None,
) -> dict:
    indexed_chunks = chunks or [{
        'ordinal': 0,
        'section': '',
        'location': '',
        'content': f'{name} content',
        'search_text': f'{name} content',
        'assets': [],
    }]
    now = time.time() if created_at is None else created_at
    return {
        'id': identity,
        'sha256': hashlib.sha256(identity.encode()).hexdigest(),
        'name': name,
        'stored_name': f'{identity}{kind}',
        'kind': kind,
        'size_bytes': 100,
        'method': 'knowledge-contract-test',
        'warnings_json': json.dumps([]),
        'text_chars': sum(len(chunk['content']) for chunk in indexed_chunks),
        'chunk_count': len(indexed_chunks),
        'pages': 0,
        'created_at': now,
        'updated_at': now,
        'chunks': indexed_chunks,
        'assets': list(assets or []),
    }


def test_concurrent_identical_upload_is_idempotent_and_preserves_source(
    isolated_knowledge, tmp_path,
):
    raw = ('并发上传必须只有一个文档，但赢家文件不能被失败方删除。\n' * 80).encode()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda index: isolated_knowledge.add_document(
                raw, f'concurrent-{index}.txt', user_id=TEST_OWNER_USER_ID),
            range(8),
        ))

    assert sum(result['duplicate'] is False for result in results) == 1
    assert sum(result['duplicate'] is True for result in results) == 7
    status = isolated_knowledge.get_status(user_id=TEST_OWNER_USER_ID)
    assert status['totals']['documents'] == 1
    source_root = (
        tmp_path / 'knowledge-files' / str(TEST_OWNER_USER_ID) / 'sources')
    sources = list(source_root.iterdir())
    assert len(sources) == 1
    assert sources[0].read_bytes() == raw
    assert hashlib.sha256(raw).hexdigest() in sources[0].name


def test_concurrent_visual_reindex_keeps_only_the_committed_asset(
    isolated_knowledge, tmp_path, monkeypatch,
):
    image_module = pytest.importorskip('PIL.Image')
    initial = image_module.new('RGB', (64, 48), (10, 20, 30))
    initial_buffer = io.BytesIO()
    initial.save(initial_buffer, format='PNG')
    document = isolated_knowledge.add_document(
        initial_buffer.getvalue(), 'diagram.png', user_id=TEST_OWNER_USER_ID)

    variants = []
    for index in range(6):
        image = image_module.new('RGB', (64, 48), (index * 30, 90, 170))
        output = io.BytesIO()
        image.save(output, format='PNG')
        variants.append(output.getvalue())
    counter = {'value': 0}
    counter_lock = threading.Lock()

    def parse(_raw, _name):
        from lib.knowledge.assets import standalone_image

        with counter_lock:
            index = counter['value']
            counter['value'] += 1
        return {
            'text': '',
            'kind': '.png',
            'method': 'concurrent-image-test',
            'warnings': [],
            'pages': 0,
            'assets': [standalone_image(variants[index], f'v{index}.png')],
        }

    monkeypatch.setattr(isolated_knowledge, 'extract', parse)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda _index: isolated_knowledge.reindex_document(
                document['id'], user_id=TEST_OWNER_USER_ID),
            range(6),
        ))

    assert all(result and result['asset_count'] == 1 for result in results)
    stored = KnowledgeRepository(TEST_OWNER_USER_ID).document(document['id'])
    disk_assets = list((
        tmp_path / 'knowledge-files' / str(TEST_OWNER_USER_ID) / 'assets'
    ).glob('*'))
    assert len(stored['assets']) == len(disk_assets) == 1
    assert disk_assets[0].name == stored['assets'][0]['stored_name']


def test_invalid_chunk_ordinals_roll_back_document_and_default_settings(
    isolated_knowledge,
):
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    document = _document('doc-invalid-ordinals', chunks=[
        {
            'ordinal': 0, 'section': '', 'location': '',
            'content': 'first', 'search_text': 'first', 'assets': [],
        },
        {
            'ordinal': 0, 'section': '', 'location': '',
            'content': 'second', 'search_text': 'second', 'assets': [],
        },
    ])

    with pytest.raises(StorageError) as raised:
        repository.create_document(document, command_id='invalid-ordinals')

    assert raised.value.code == 'database_protocol_error'
    assert repository.documents() == []
    assert repository.settings() == {
        'enabled': False, 'visual_enrichment': False}


def test_unknown_asset_reference_rolls_back_the_complete_document(
    isolated_knowledge,
):
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    document = _document('doc-invalid-link', chunks=[{
        'ordinal': 0,
        'section': 'Visual evidence',
        'location': '',
        'content': 'diagram',
        'search_text': 'diagram',
        'assets': [{'id': 'missing-asset', 'relation': 'primary'}],
    }])

    with pytest.raises(StorageError) as raised:
        repository.create_document(document, command_id='invalid-asset-link')

    assert raised.value.code == 'database_protocol_error'
    assert repository.documents() == []


def test_catalog_snapshot_is_bounded_filterable_and_category_aware(
    isolated_knowledge,
):
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    now = time.time()
    for index in range(37):
        spreadsheet = index < 7
        name = f'sheet-{index:02d}.csv' if spreadsheet else f'note-{index:02d}.md'
        kind = '.csv' if spreadsheet else '.md'
        chunks = [{
            'ordinal': ordinal,
            'section': f'Section {ordinal}',
            'location': f'lines {ordinal + 1}-{ordinal + 2}',
            'content': f'{name} content {ordinal}',
            'search_text': f'{name} content {ordinal}',
            'assets': [],
        } for ordinal in range(5)]
        repository.create_document(
            _document(
                f'doc-{index:03d}', name=name, kind=kind,
                chunks=chunks, created_at=now + index),
            command_id=f'catalog-document-{index}',
        )

    page = repository.catalog(page=2, page_size=10)
    assert page['totals']['documents'] == 37
    assert page['totals']['chunks'] == 185
    assert len(page['documents']) == 10
    assert page['pagination'] == {
        'page': 2,
        'page_size': 10,
        'total_items': 37,
        'total_pages': 4,
        'has_previous': True,
        'has_next': True,
    }
    assert {item['category']: item['count'] for item in page['facets']} == {
        'spreadsheet': 7, 'text': 30}

    filtered = repository.catalog(
        page=1, page_size=3, query='sheet-',
        category='spreadsheet', sort='name_asc')
    assert filtered['pagination']['total_items'] == 7
    assert [row['name'] for row in filtered['documents']] == [
        'sheet-00.csv', 'sheet-01.csv', 'sheet-02.csv']
    assert all(row['category'] == 'spreadsheet'
               for row in filtered['documents'])


def test_document_content_reads_only_the_requested_chunk_page(
    isolated_knowledge,
):
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    chunks = [{
        'ordinal': ordinal,
        'section': f'Section {ordinal}',
        'location': f'lines {ordinal + 1}-{ordinal + 1}',
        'content': f'bounded evidence {ordinal}',
        'search_text': f'bounded evidence {ordinal}',
        'assets': [],
    } for ordinal in range(205)]
    repository.create_document(
        _document('large-document', chunks=chunks),
        command_id='create-large-document',
    )

    page = repository.document_content(
        'large-document', offset=197, limit=5)

    assert page is not None
    assert [chunk['ordinal'] for chunk in page['chunks']] == [
        197, 198, 199, 200, 201]
    assert page['pagination'] == {
        'offset': 197,
        'limit': 5,
        'total_items': 205,
        'has_more': True,
    }
    assert 'chunks' not in page['document']
    assert 'assets' not in page['document']


def test_candidate_limit_prefers_chunks_matching_more_query_terms(
    isolated_knowledge,
):
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    chunks = [{
        'ordinal': ordinal,
        'section': '',
        'location': '',
        'content': 'common rare' if ordinal == 80 else 'common',
        'search_text': 'common rare' if ordinal == 80 else 'common',
        'assets': [],
    } for ordinal in range(81)]
    repository.create_document(
        _document('candidate-ranking', chunks=chunks),
        command_id='create-candidate-ranking',
    )

    candidates = repository.search_candidates(
        ['common', 'rare'], limit=80)

    assert len(candidates) == 80
    assert any(row['ordinal'] == 80 for row in candidates)
    rare = next(row for row in candidates if row['ordinal'] == 80)
    assert rare['matched_terms'] == 2
