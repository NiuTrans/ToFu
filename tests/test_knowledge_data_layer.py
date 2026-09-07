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


def test_attachment_scope_reuses_digest_without_polluting_library(
    isolated_knowledge,
):
    raw = ('attachment-only evidence\n' * 40).encode()
    attached = isolated_knowledge.add_document(
        raw, 'notes.txt', user_id=TEST_OWNER_USER_ID, scope='attachment')

    assert attached['scope'] == 'attachment'
    assert isolated_knowledge.get_status(
        user_id=TEST_OWNER_USER_ID)['totals']['documents'] == 0
    assert KnowledgeRepository(TEST_OWNER_USER_ID + 1).document_metadata(
        attached['id']) is None

    promoted = isolated_knowledge.add_document(
        raw, 'notes.txt', user_id=TEST_OWNER_USER_ID, scope='library')
    assert promoted['duplicate'] is True
    assert promoted['id'] == attached['id']
    assert promoted['scope'] == 'shared'
    assert isolated_knowledge.get_status(
        user_id=TEST_OWNER_USER_ID)['totals']['documents'] == 1

    assert isolated_knowledge.remove_library_document(
        attached['id'], user_id=TEST_OWNER_USER_ID)
    metadata = isolated_knowledge.get_document_metadata(
        attached['id'], user_id=TEST_OWNER_USER_ID)
    assert metadata['scope'] == 'attachment'
    assert isolated_knowledge.read_source_path(
        attached['id'], user_id=TEST_OWNER_USER_ID).read_bytes() == raw


def test_video_source_and_derived_evidence_share_one_delete_lifecycle(
    isolated_knowledge, tmp_path,
):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'video-source-bytes' * 100)
    document = isolated_knowledge.create_media_source(
        source, 'clip.mp4', user_id=TEST_OWNER_USER_ID,
        media_metadata={'media_kind': 'video', 'status': 'processing'})
    frame = tmp_path / 'frame.jpg'
    frame.write_bytes(b'\xff\xd8\xff' + b'frame-bytes' * 40)

    ready = isolated_knowledge.replace_media_evidence(
        document['id'], user_id=TEST_OWNER_USER_ID,
        chunks=[{
            'section': 'Audio transcript', 'location': 'Video',
            'content': 'bounded transcript evidence', 'asset_ordinals': [0],
        }],
        assets=[{
            'path': str(frame), 'kind': 'video_frame',
            'mime_type': 'image/jpeg', 'suffix': '.jpg',
            'metadata': {'timestamp_s': 1.25},
        }],
        media_metadata={
            'media_kind': 'video', 'status': 'ready', 'frame_count': 1,
        },
    )

    assert ready['scope'] == 'attachment'
    assert ready['media_metadata']['status'] == 'ready'
    assert ready['media_metadata']['poster_asset_id']
    assets = isolated_knowledge.list_document_assets(
        document['id'], user_id=TEST_OWNER_USER_ID)
    assert assets[0]['metadata'] == {'timestamp_s': 1.25}
    source_path = isolated_knowledge.read_source_path(
        document['id'], user_id=TEST_OWNER_USER_ID)
    asset_root = (
        tmp_path / 'knowledge-files' / str(TEST_OWNER_USER_ID) / 'assets')
    assert source_path.is_file() and len(list(asset_root.iterdir())) == 1

    assert isolated_knowledge.delete_document(
        document['id'], user_id=TEST_OWNER_USER_ID)
    assert not source_path.exists()
    assert list(asset_root.iterdir()) == []


def test_turn_keeps_only_attachment_ref_and_model_projection_is_bounded(
    isolated_knowledge,
):
    from lib.chat.turn_builder import build_user_msg_from_payload
    from lib.media_attachments import attachment_ref
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

    secret = 'model-visible attachment evidence ' * 120
    document = isolated_knowledge.add_document(
        secret.encode(), 'evidence.txt', user_id=TEST_OWNER_USER_ID,
        scope='draft')
    canonical = attachment_ref(document)
    message = build_user_msg_from_payload(
        {'text': 'summarize the evidence', 'attachments': [canonical]}, {},
        user_id=TEST_OWNER_USER_ID)

    assert message['attachments'] == [canonical]
    assert 'pdfTexts' not in message and 'videos' not in message
    assert secret not in json.dumps(message)
    assert isolated_knowledge.get_document_metadata(
        document['id'], user_id=TEST_OWNER_USER_ID)['scope'] == 'draft'

    from lib.media_attachments import resolve_client_refs
    resolve_client_refs(
        message['attachments'], user_id=TEST_OWNER_USER_ID, retain=True)
    assert isolated_knowledge.get_document_metadata(
        document['id'], user_id=TEST_OWNER_USER_ID)['scope'] == 'attachment'

    projected = _transform_messages(
        [message], {'model': 'text-only-test'}, user_id=TEST_OWNER_USER_ID)
    assert isinstance(projected[0]['content'], list)
    wire_text = '\n'.join(
        block.get('text', '') for block in projected[0]['content']
        if block.get('type') == 'text')
    assert 'model-visible attachment evidence' in wire_text
    assert f'att_media_{document["id"]}' in wire_text


def test_attachment_text_budget_is_shared_across_the_model_request(
        isolated_knowledge):
    from lib.media_attachments import (
        MODEL_TEXT_REQUEST_CAP,
        MediaProjectionBudget,
        attachment_ref,
        project_for_model,
    )

    references = []
    for index in range(3):
        raw = ((f'document-{index} bounded evidence ' * 3500) + '\n').encode()
        document = isolated_knowledge.add_document(
            raw, f'evidence-{index}.txt', user_id=TEST_OWNER_USER_ID,
            scope='attachment')
        references.append(attachment_ref(document))

    budget = MediaProjectionBudget(attachments_remaining=len(references))
    projections = [
        project_for_model(
            [reference], user_id=TEST_OWNER_USER_ID, query='',
            model='text-only-test', projection_budget=budget)
        for reference in references
    ]

    assert sum(item['text_chars'] for item in projections) \
        <= MODEL_TEXT_REQUEST_CAP
    assert budget.text_chars_remaining == 0
    assert all(item['text_chars'] > 0 for item in projections)
    overflow = project_for_model(
        [references[-1]], user_id=TEST_OWNER_USER_ID, query='',
        model='text-only-test', projection_budget=budget)
    final_text = '\n'.join(
        block.get('text', '') for block in overflow['blocks'])
    assert 'request budget exhausted' in final_text


@pytest.mark.auth_mode('open')
def test_media_routes_share_the_owner_scoped_attachment_lifecycle(
    isolated_knowledge, flask_client,
):
    from werkzeug.datastructures import FileStorage

    raw = ('route-owned attachment evidence\n' * 60).encode()
    uploaded = flask_client.post(
        '/api/v1/media/attachments', form={}, files={'file': FileStorage(
            stream=io.BytesIO(raw), filename='route-notes.txt',
            content_type='text/plain',
        )},
    )
    assert uploaded.status_code == 200
    attachment = uploaded.get_json()['attachment']
    attachment_id = attachment['attachmentId']
    assert attachment['kind'] == 'document'
    assert attachment['status'] == 'ready'

    metadata = flask_client.get(
        f'/api/v1/media/attachments/{attachment_id}')
    assert metadata.status_code == 200
    assert metadata.get_json()['attachment'] == attachment

    source = flask_client.get(
        f'/api/v1/media/attachments/{attachment_id}/source')
    assert source.status_code == 200
    assert source.data == raw
    assert source.headers['Cache-Control'] == 'private, no-store'
    assert source.headers['X-Content-Type-Options'] == 'nosniff'

    deleted = flask_client.delete(
        f'/api/v1/media/attachments/{attachment_id}')
    assert deleted.status_code == 200
    assert isolated_knowledge.get_document_metadata(
        attachment_id, user_id=TEST_OWNER_USER_ID) is None
    assert flask_client.get(
        f'/api/v1/media/attachments/{attachment_id}').status_code == 404


@pytest.mark.auth_mode('open')
def test_composer_discard_cannot_delete_reused_library_content(
        isolated_knowledge, flask_client):
    from werkzeug.datastructures import FileStorage

    raw = ('library source survives draft cleanup\n' * 50).encode()
    library = isolated_knowledge.add_document(
        raw, 'library.txt', user_id=TEST_OWNER_USER_ID, scope='library')
    uploaded = flask_client.post(
        '/api/v1/media/attachments', form={}, files={'file': FileStorage(
            stream=io.BytesIO(raw), filename='library.txt',
            content_type='text/plain',
        )},
    )
    attachment_id = uploaded.get_json()['attachment']['attachmentId']
    assert attachment_id == library['id']
    assert isolated_knowledge.get_document_metadata(
        attachment_id, user_id=TEST_OWNER_USER_ID)['scope'] == 'library'

    discarded = flask_client.delete(
        f'/api/v1/media/attachments/{attachment_id}?draft=1')
    assert discarded.status_code == 409
    assert isolated_knowledge.read_source_path(
        attachment_id, user_id=TEST_OWNER_USER_ID).read_bytes() == raw


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
