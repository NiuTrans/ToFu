"""Concurrency and atomicity contracts for the knowledge data-layer owner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import sqlite3
import threading
import time

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    from lib.knowledge import store

    monkeypatch.setattr(
        store, '_DB_PATH_OVERRIDE', str(tmp_path / 'knowledge.sqlite3'))
    monkeypatch.setattr(
        store, '_SOURCE_ROOT_OVERRIDE', str(tmp_path / 'sources'))
    return store


def test_concurrent_identical_upload_is_idempotent_and_preserves_source(
        isolated_store, tmp_path):
    raw = ('并发上传必须只有一个文档，但赢家文件不能被失败方删除。\n' * 80).encode()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda index: isolated_store.add_document(
                raw, f'concurrent-{index}.txt'),
            range(8),
        ))

    assert sum(result['duplicate'] is False for result in results) == 1
    assert sum(result['duplicate'] is True for result in results) == 7
    status = isolated_store.get_status()
    assert status['totals']['documents'] == 1
    sources = list((tmp_path / 'sources').iterdir())
    assert len(sources) == 1
    assert sources[0].read_bytes() == raw
    assert hashlib.sha256(raw).hexdigest() in sources[0].name


def test_concurrent_visual_reindex_keeps_only_the_committed_asset(
        isolated_store, tmp_path, monkeypatch):
    image_module = pytest.importorskip('PIL.Image')
    initial = image_module.new('RGB', (64, 48), (10, 20, 30))
    initial_buffer = io.BytesIO()
    initial.save(initial_buffer, format='PNG')
    document = isolated_store.add_document(
        initial_buffer.getvalue(), 'diagram.png')

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
            'text': '', 'kind': '.png', 'method': 'concurrent-image-test',
            'warnings': [], 'pages': 0,
            'assets': [standalone_image(variants[index], f'v{index}.png')],
        }

    monkeypatch.setattr(isolated_store, 'extract', parse)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda _index: isolated_store.reindex_document(document['id']),
            range(6)))

    assert all(result and result['asset_count'] == 1 for result in results)
    from lib.database import knowledge_repository as repository
    assets = repository.list_assets(
        isolated_store._db_path(), document_id=document['id'])
    disk_assets = list((tmp_path / 'assets').glob('*'))
    assert len(assets) == len(disk_assets) == 1
    assert disk_assets[0].name == assets[0]['stored_name']


def test_chunk_failure_rolls_back_document_and_settings(tmp_path):
    from lib.database import knowledge_repository as repository

    path = tmp_path / 'knowledge.sqlite3'
    now = time.time()
    document = {
        'id': 'doc-rollback',
        'sha256': 'a' * 64,
        'name': 'rollback.txt',
        'stored_name': 'rollback-source.txt',
        'kind': '.txt',
        'size_bytes': 12,
        'method': 'text',
        'warnings_json': json.dumps([]),
        'text_chars': 12,
        'chunk_count': 2,
        'pages': 0,
        'created_at': now,
        'updated_at': now,
    }
    # Duplicate ordinals fail on the second chunk after the document and first
    # FTS row were inserted. The whole semantic unit must disappear.
    chunks = [
        {
            'ordinal': 0, 'section': '', 'location': '',
            'content': 'first', 'search_text': 'first',
        },
        {
            'ordinal': 0, 'section': '', 'location': '',
            'content': 'second', 'search_text': 'second',
        },
    ]

    with pytest.raises(sqlite3.IntegrityError):
        repository.insert_document(path, document, chunks)

    assert repository.list_documents(path) == []
    assert repository.tool_available(path) is False
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            'SELECT COUNT(*) FROM knowledge_chunks').fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM knowledge_chunks_fts').fetchone()[0] == 0
        assert conn.execute(
            "SELECT value FROM knowledge_settings WHERE key='enabled'"
        ).fetchone() is None
    finally:
        conn.close()


def test_asset_and_chunk_link_failure_rolls_back_the_whole_document(tmp_path):
    from lib.database import knowledge_repository as repository

    path = tmp_path / 'knowledge.sqlite3'
    now = time.time()
    document = {
        'id': 'doc-image-rollback', 'sha256': 'b' * 64,
        'name': 'diagram.png', 'stored_name': 'source.png', 'kind': '.png',
        'size_bytes': 100, 'method': 'image-local', 'warnings_json': '[]',
        'text_chars': 0, 'chunk_count': 1, 'pages': 0,
        'created_at': now, 'updated_at': now,
    }
    asset = {
        'id': 'asset-one', 'ordinal': 0, 'kind': 'image',
        'stored_name': 'asset-one.png', 'mime_type': 'image/png',
        'sha256': 'c' * 64, 'size_bytes': 100, 'width': 10, 'height': 10,
        'created_at': now, 'updated_at': now,
    }
    chunk = {
        'ordinal': 0, 'section': 'Visual evidence', 'location': '',
        'content': 'diagram', 'search_text': 'diagram',
        'assets': [{'id': 'missing-asset', 'relation': 'primary'}],
    }

    with pytest.raises(sqlite3.IntegrityError):
        repository.insert_document(path, document, [chunk], [asset])

    assert repository.list_documents(path) == []
    assert repository.list_assets(path) == []
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM knowledge_chunks').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM knowledge_chunk_assets').fetchone()[0] == 0
    finally:
        conn.close()


def test_v1_store_migrates_to_visual_schema_without_losing_documents(
        isolated_store, tmp_path):
    from lib.database import knowledge_repository as repository

    document = isolated_store.add_document(b'preserved migration evidence', 'old.txt')
    path = tmp_path / 'knowledge.sqlite3'
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE knowledge_settings SET value='1' WHERE key='__schema_version__'")
        conn.execute('DROP TABLE knowledge_chunk_assets')
        conn.execute('DROP TABLE knowledge_assets')
        conn.commit()
    finally:
        conn.close()
    repository._schema_ready.clear()

    repository.set_enabled(path, True)

    rows = repository.list_documents(path)
    assert [row['id'] for row in rows] == [document['id']]
    assert rows[0]['asset_count'] == 0
    assert repository.list_assets(path) == []


def test_catalog_snapshot_is_bounded_filterable_and_category_aware(tmp_path):
    from lib.database import knowledge_repository as repository

    path = tmp_path / 'knowledge.sqlite3'
    now = time.time()
    for index in range(37):
        spreadsheet = index < 7
        name = f'sheet-{index:02d}.csv' if spreadsheet else f'note-{index:02d}.md'
        kind = '.csv' if spreadsheet else '.md'
        chunks = [{
            'ordinal': ordinal, 'section': f'Section {ordinal}',
            'location': f'lines {ordinal + 1}-{ordinal + 2}',
            'content': f'{name} content {ordinal}',
            'search_text': f'{name} content {ordinal}',
        } for ordinal in range(5)]
        repository.insert_document(path, {
            'id': f'doc-{index:03d}',
            'sha256': hashlib.sha256(name.encode()).hexdigest(),
            'name': name,
            'stored_name': f'source-{index:03d}{kind}',
            'kind': kind,
            'size_bytes': 100 + index,
            'method': 'catalog-test',
            'warnings_json': '[]',
            'text_chars': 50,
            'chunk_count': len(chunks),
            'pages': 0,
            'created_at': now + index,
            'updated_at': now + index,
        }, chunks)

    page = repository.catalog_snapshot(path, page=2, page_size=10)
    assert page['totals']['documents'] == 37
    assert page['totals']['chunks'] == 185
    assert len(page['documents']) == 10
    assert page['pagination'] == {
        'page': 2, 'page_size': 10, 'total_items': 37,
        'total_pages': 4, 'has_previous': True, 'has_next': True,
    }
    assert {item['category']: item['count'] for item in page['facets']} == {
        'spreadsheet': 7, 'text': 30,
    }

    filtered = repository.catalog_snapshot(
        path, page=1, page_size=3, query='sheet-',
        category='spreadsheet', sort='name_asc')
    assert filtered['pagination']['total_items'] == 7
    assert [row['name'] for row in filtered['documents']] == [
        'sheet-00.csv', 'sheet-01.csv', 'sheet-02.csv']
    assert all(row['category'] == 'spreadsheet'
               for row in filtered['documents'])

    chunks = repository.list_document_chunks(
        path, 'doc-000', offset=2, limit=2)
    assert [chunk['ordinal'] for chunk in chunks] == [2, 3]


def test_auxiliary_store_owner_coexists_with_canonical_owner(
        tmp_path, monkeypatch):
    from lib.database import sqlite_owner
    from lib.database import sqlite_store_owner

    sqlite_store_owner.release_store_owners()
    sqlite_owner.release_owner()
    monkeypatch.setenv('TOFU_SQLITE_OWNER_GUARD', '1')
    monkeypatch.setenv('TOFU_SERVER_PROCESS', '1')
    monkeypatch.setenv('TOFU_DB_HOST_ID', 'unit-host')
    canonical = tmp_path / 'tofu.db'
    auxiliary = tmp_path / 'knowledge' / 'knowledge.sqlite3'
    auxiliary.parent.mkdir()

    try:
        canonical_claim = sqlite_owner.claim_owner(str(canonical))
        sqlite_store_owner.assert_store_owner(
            auxiliary, purpose='knowledge unit owner check')

        assert canonical_claim['db'] == str(canonical.resolve())
        assert sqlite_owner._claim['db'] == str(canonical.resolve())
        aux_marker, _lock = sqlite_store_owner._paths(str(auxiliary))
        assert aux_marker.is_file()
    finally:
        sqlite_store_owner.release_store_owners()
        sqlite_owner.release_owner()
