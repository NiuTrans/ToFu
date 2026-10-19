"""Atomicity, integrity, and bounded-read contracts for knowledge storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
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
    assert projections[0]['text_chars'] > 0
    assert projections[-1]['text_chars'] == 0
    assert any('request budget exhausted' in block.get('text', '')
               for block in projections[-1]['blocks'])
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
        '/api/v4/media/attachments', form={}, files={'file': FileStorage(
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
        f'/api/v4/media/attachments/{attachment_id}')
    assert metadata.status_code == 200
    assert metadata.get_json()['attachment'] == attachment

    source = flask_client.get(
        f'/api/v4/media/attachments/{attachment_id}/source')
    assert source.status_code == 200
    assert source.data == raw
    assert source.headers['Cache-Control'] == 'private, no-store'
    assert source.headers['X-Content-Type-Options'] == 'nosniff'

    deleted = flask_client.delete(
        f'/api/v4/media/attachments/{attachment_id}')
    assert deleted.status_code == 200
    assert isolated_knowledge.get_document_metadata(
        attachment_id, user_id=TEST_OWNER_USER_ID) is None
    assert flask_client.get(
        f'/api/v4/media/attachments/{attachment_id}').status_code == 404


@pytest.mark.auth_mode('open')
def test_composer_discard_cannot_delete_reused_library_content(
        isolated_knowledge, flask_client):
    from werkzeug.datastructures import FileStorage

    raw = ('library source survives draft cleanup\n' * 50).encode()
    library = isolated_knowledge.add_document(
        raw, 'library.txt', user_id=TEST_OWNER_USER_ID, scope='library')
    uploaded = flask_client.post(
        '/api/v4/media/attachments', form={}, files={'file': FileStorage(
            stream=io.BytesIO(raw), filename='library.txt',
            content_type='text/plain',
        )},
    )
    attachment_id = uploaded.get_json()['attachment']['attachmentId']
    assert attachment_id == library['id']
    assert isolated_knowledge.get_document_metadata(
        attachment_id, user_id=TEST_OWNER_USER_ID)['scope'] == 'library'

    discarded = flask_client.delete(
        f'/api/v4/media/attachments/{attachment_id}?draft=1')
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


def test_large_document_retains_all_index_terms_and_tail_retrieval(isolated_knowledge):
    tokens = [f'w{index:05d}' for index in range(20_000)]
    raw = ('\n'.join(' '.join(tokens[offset:offset + 10])
                     for offset in range(0, len(tokens), 10))).encode()
    document = isolated_knowledge.add_document(
        raw, 'large-manuscript.txt', user_id=TEST_OWNER_USER_ID)
    assert document['chunk_count'] > 40
    assert not any('Index trimmed' in warning for warning in document['warnings'])
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    stored = repository.document_content(document['id'], offset=0, limit=document['chunk_count'])
    indexed = {token for chunk in stored['chunks'] for token in chunk['search_text'].split()}
    assert set(tokens) <= indexed
    matches = isolated_knowledge.search_document_candidates(
        document['id'], tokens[-1], user_id=TEST_OWNER_USER_ID)
    assert matches and tokens[-1] in matches[0]['content']
    source = isolated_knowledge.read_source_path(document['id'], user_id=TEST_OWNER_USER_ID)
    assert source.read_bytes() == raw
    print(f'knowledge_index_demand: {len(raw)} source bytes, {len(tokens)} retained unique terms, '
          f'{document["chunk_count"]} chunks')


def test_complete_attachment_refs_beyond_20_are_resolved_and_retained(isolated_knowledge):
    from lib.media_attachments import resolve_client_refs
    documents = [isolated_knowledge.add_document(
        f'document evidence {index}'.encode(), f'doc-{index}.txt',
        user_id=TEST_OWNER_USER_ID, scope='draft') for index in range(33)]
    candidates = [{'attachmentId': document['id'], 'name': 'forged name', 'status': 'forged'}
                  for document in documents]
    # Invalid and duplicate inputs must not consume capacity for later refs.
    inputs = [None, {}, *candidates[:20], candidates[0], *candidates[20:]]
    resolved = resolve_client_refs(inputs, user_id=TEST_OWNER_USER_ID, retain=True)
    assert [item['attachmentId'] for item in resolved] == [document['id'] for document in documents]
    assert [item['name'] for item in resolved] == [f'doc-{index}.txt' for index in range(33)]
    for document in documents:
        assert isolated_knowledge.get_document_metadata(
            document['id'], user_id=TEST_OWNER_USER_ID)['scope'] == 'attachment'


def test_complete_media_index_can_exceed_old_write_and_chunk_term_limits(isolated_knowledge):
    document = isolated_knowledge.add_document(
        b'original source', 'source.txt', user_id=TEST_OWNER_USER_ID, scope='draft')
    chunks = [{'content': f'evidence{index}', 'section': '', 'location': ''}
              for index in range(14_400)]
    updated = isolated_knowledge.replace_media_evidence(
        document['id'], chunks=chunks, assets=[], media_metadata={'status': 'ready'},
        user_id=TEST_OWNER_USER_ID)
    assert updated['chunk_count'] == len(chunks)
    matches = isolated_knowledge.search_document_candidates(
        document['id'], 'evidence14399', user_id=TEST_OWNER_USER_ID)
    assert matches and 'evidence14399' in matches[0]['content']
    tokens = [f'wide{index}' for index in range(5501)]
    updated = isolated_knowledge.replace_media_evidence(
        document['id'], chunks=[{'content': ' '.join(tokens)}], assets=[],
        media_metadata={'status': 'ready'}, user_id=TEST_OWNER_USER_ID)
    assert updated['chunk_count'] == 1
    matches = isolated_knowledge.search_document_candidates(
        document['id'], tokens[-1], user_id=TEST_OWNER_USER_ID)
    assert matches and tokens[-1] in matches[0]['content']
    assert isolated_knowledge.read_source_path(
        document['id'], user_id=TEST_OWNER_USER_ID).read_bytes() == b'original source'


def test_video_evidence_beyond_200_frames_persists_and_late_failure_rolls_back(
        isolated_knowledge, tmp_path, monkeypatch):
    from PIL import Image
    from lib.media_attachments import create_video, complete_video
    import lib.knowledge.store as store
    source = tmp_path / 'original.mp4'
    original = b'owned video source fixture'
    source.write_bytes(original)
    attachment, duplicate = create_video(
        str(source), 'long.mp4', user_id=TEST_OWNER_USER_ID)
    assert duplicate is False
    frames = []
    for index in range(257):
        path = tmp_path / f'frame-{index}.jpg'
        Image.new('RGB', (8, 8), (index % 256, index // 256, 7)).save(path, format='JPEG')
        frames.append({'path': str(path), 't': float(index * 10)})
    ready = complete_video(
        attachment['attachmentId'], frames=frames, transcript='complete audio',
        transcript_status='ok', transcript_model='fixture-stt',
        storyboard='complete visual evidence', storyboard_status='ok',
        storyboard_model='fixture-vision', probe={'duration': 2570, 'filename': 'long.mp4'},
        user_id=TEST_OWNER_USER_ID)
    assert ready['status'] == 'ready' and ready['frameCount'] == 257
    document_id = attachment['attachmentId']
    assets = []
    offset = 0
    while page := isolated_knowledge.list_document_assets(
            document_id, user_id=TEST_OWNER_USER_ID, offset=offset, limit=200):
        assets.extend(page)
        offset += len(page)
    assert len(assets) == len(frames)
    assert [asset['ordinal'] for asset in assets] == list(range(len(frames)))
    for frame, asset in zip(frames, assets):
        row, persisted = isolated_knowledge.read_asset(asset['id'], user_id=TEST_OWNER_USER_ID)
        assert persisted == Path(frame['path']).read_bytes()
        assert json.loads(row['metadata_json'])['timestamp_s'] == frame['t']
    source_path = isolated_knowledge.read_source_path(document_id, user_id=TEST_OWNER_USER_ID)
    assert source_path.read_bytes() == original
    root = store._asset_root(TEST_OWNER_USER_ID)
    names_before = {path.name for path in root.iterdir()}
    old_document = KnowledgeRepository(TEST_OWNER_USER_ID).document(document_id)
    write = store._write_asset
    writes = 0
    def fail_late(raw, name, *, user_id):
        nonlocal writes
        writes += 1
        if writes == 213:
            raise OSError('injected late evidence write failure')
        return write(raw, name, user_id=user_id)
    monkeypatch.setattr(store, '_write_asset', fail_late)
    with pytest.raises(OSError, match='late evidence write failure'):
        complete_video(
            document_id, frames=frames, transcript='replacement',
            transcript_status='ok', transcript_model='fixture-stt',
            storyboard='replacement', storyboard_status='ok',
            storyboard_model='fixture-vision', probe={'duration': 2570, 'filename': 'long.mp4'},
            user_id=TEST_OWNER_USER_ID)
    assert writes == 213
    assert {path.name for path in root.iterdir()} == names_before
    assert KnowledgeRepository(TEST_OWNER_USER_ID).document(document_id) == old_document
    assert source_path.read_bytes() == original
    print(f'media_asset_demand: {len(assets)} frames, '
          f'{sum(Path(frame["path"]).stat().st_size for frame in frames)} JPEG bytes')
    assert isolated_knowledge.delete_document(document_id, user_id=TEST_OWNER_USER_ID)
    assert not source_path.exists()
    assert all(not (root / asset['stored_name']).exists() for asset in assets)


@pytest.mark.auth_mode('open')
def test_large_document_upload_preserves_original_and_digest_identity(
        isolated_knowledge, flask_client):
    from werkzeug.datastructures import FileStorage
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml', '<?xml version="1.0" encoding="UTF-8"?>'
            '<office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
            '<office:body><office:text><text:p>Large upload searchable evidence '
            'and tailuploadmarker</text:p></office:text></office:body></office:document-content>')
        archive.writestr('META-INF/manifest.xml', '<?xml version="1.0"?>'
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
            'manifest:version="1.2"><manifest:file-entry manifest:full-path="/" '
            'manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            '<manifest:file-entry manifest:full-path="payload.bin" '
            'manifest:media-type="application/octet-stream"/></manifest:manifest>')
        # An uncompressed package resource grows the actual original without
        # substituting a fake parser or demanding a huge searchable corpus.
        archive.writestr('payload.bin', b'x' * (51 * 1024 * 1024))
    raw = buffer.getvalue()
    assert len(raw) > 50 * 1024 * 1024
    def upload():
        return flask_client.post('/api/v4/media/attachments', form={}, files={
            'file': FileStorage(stream=io.BytesIO(raw), filename='large-source.odt',
                                content_type='application/vnd.oasis.opendocument.text')})
    first = upload()
    assert first.status_code == 200, first.get_json()
    attachment = first.get_json()['attachment']
    assert attachment['sizeBytes'] == len(raw)
    assert attachment['status'] == 'ready'
    document_id = attachment['attachmentId']
    matches = isolated_knowledge.search_document_candidates(
        document_id, 'tailuploadmarker', user_id=TEST_OWNER_USER_ID)
    assert matches and 'tailuploadmarker' in matches[0]['content']
    response = flask_client.get(f'/api/v4/media/attachments/{document_id}/source')
    assert response.status_code == 200
    assert hashlib.sha256(response.data).digest() == hashlib.sha256(raw).digest()
    source_path = isolated_knowledge.read_source_path(document_id, user_id=TEST_OWNER_USER_ID)
    assert source_path.stat().st_size == len(raw)
    second = upload()
    assert second.status_code == 200
    assert second.get_json()['attachment']['attachmentId'] == document_id
    assert isolated_knowledge.delete_document(document_id, user_id=TEST_OWNER_USER_ID)
    assert not source_path.exists()
    print(f'document_upload_demand: {len(raw)} original ODT bytes, searchable, exact source and digest reuse')


@pytest.mark.auth_mode('open')
def test_document_receive_failure_returns_existing_error_envelope(flask_client, monkeypatch):
    from types import SimpleNamespace
    import routes.api_v1.media as routes
    class FailedSource:
        def read(self):
            raise OSError('injected document receive failure')
    monkeypatch.setattr(routes, 'request_files', lambda: {
        'file': SimpleNamespace(filename='source.txt', stream=FailedSource())})
    response = flask_client.post('/api/v4/media/attachments')
    assert response.status_code == 500
    assert response.get_json()['ok'] is False
    assert 'injected document receive failure' not in str(response.get_json())


def test_text_beyond_old_default_is_indexed_and_tail_remains_searchable(isolated_knowledge):
    line = 'Repeated source evidence preserved across the complete document.\n'
    text = line * (12_000_000 // len(line) + 1) + 'lateevidencemarker'
    raw = text.encode('utf-8')
    assert len(text) > 12_000_000
    document = isolated_knowledge.add_document(
        raw, 'complete-text.txt', user_id=TEST_OWNER_USER_ID)
    assert document['text_chars'] == len(text)
    assert not any('truncat' in warning.lower() for warning in document['warnings'])
    matches = isolated_knowledge.search_document_candidates(
        document['id'], 'lateevidencemarker', user_id=TEST_OWNER_USER_ID)
    assert matches and 'lateevidencemarker' in matches[0]['content']
    path = isolated_knowledge.read_source_path(document['id'], user_id=TEST_OWNER_USER_ID)
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(raw).digest()
    final_page = isolated_knowledge.get_document_content(
        document['id'], user_id=TEST_OWNER_USER_ID, offset=document['chunk_count'] - 1, limit=1)
    assert 'lateevidencemarker' in final_page['chunks'][0]['content']
    print(f'knowledge_text_demand: {len(text)} characters, {document["chunk_count"]} chunks, exact source and tail retrieval')


@pytest.mark.parametrize('case', ['member_count', 'aggregate_bytes', 'required_member_ratio'])
def test_office_packages_beyond_old_admission_limits_are_complete(isolated_knowledge, case):
    import zipfile
    buffer = io.BytesIO()
    xml_start = ('<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
        '<office:body><office:text>').encode()
    xml_end = ('<text:p>Complete Office evidence packageendmarker</text:p>'
        '</office:text></office:body></office:document-content>').encode()
    resource_names = []
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text',
                         compress_type=zipfile.ZIP_STORED)
        with archive.open('content.xml', 'w') as content:
            content.write(xml_start)
            if case == 'required_member_ratio':
                for _ in range(33):
                    content.write(b' ' * (1024 * 1024))
            content.write(xml_end)
        if case == 'member_count':
            resource_names = [f'resources/{index}.bin' for index in range(20_001)]
            for name in resource_names:
                archive.writestr(name, b'')
        elif case == 'aggregate_bytes':
            resource_names = ['resources/large.bin']
            with archive.open(resource_names[0], 'w') as resource:
                for _ in range(257):
                    resource.write(b'x' * (1024 * 1024))
        manifest = ('<manifest:manifest '
            'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
            'manifest:version="1.2"><manifest:file-entry manifest:full-path="/" '
            'manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            + ''.join(f'<manifest:file-entry manifest:full-path="{name}" '
                      'manifest:media-type="application/octet-stream"/>' for name in resource_names)
            + '</manifest:manifest>')
        archive.writestr('META-INF/manifest.xml', manifest)
    raw = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        expanded = sum(info.file_size for info in infos)
        compressed = max(1, sum(info.compress_size for info in infos))
        if case == 'member_count':
            assert len(infos) > 20_000
        elif case == 'aggregate_bytes':
            assert expanded > 256 * 1024 * 1024
        else:
            assert expanded > 32 * 1024 * 1024 and expanded / compressed > 200
    document = isolated_knowledge.add_document(
        raw, f'complete-{case}.odt', user_id=TEST_OWNER_USER_ID)
    assert document['method'] == 'opendocument-local'
    matches = isolated_knowledge.search_document_candidates(
        document['id'], 'packageendmarker', user_id=TEST_OWNER_USER_ID)
    assert matches and 'packageendmarker' in matches[0]['content']
    path = isolated_knowledge.read_source_path(document['id'], user_id=TEST_OWNER_USER_ID)
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(raw).digest()
    print(f'office_package_demand: {case}, {len(infos)} members, {expanded} declared expanded bytes, '
          f'{len(raw)} source bytes')
    assert isolated_knowledge.delete_document(document['id'], user_id=TEST_OWNER_USER_ID)
    assert not path.exists()


def test_corrupt_required_office_member_fails_before_publication(isolated_knowledge):
    import zipfile
    from lib.knowledge.ingest import KnowledgeIngestError
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml', '<uniquecontent>broken member</uniquecontent>')
    raw = buffer.getvalue().replace(b'<uniquecontent>', b'<Uniquecontent>', 1)
    before = isolated_knowledge.list_documents(user_id=TEST_OWNER_USER_ID)
    with pytest.raises(KnowledgeIngestError, match='Invalid OpenDocument content'):
        isolated_knowledge.add_document(raw, 'corrupt.odt', user_id=TEST_OWNER_USER_ID)
    assert isolated_knowledge.list_documents(user_id=TEST_OWNER_USER_ID) == before


@pytest.mark.auth_mode('open')
@pytest.mark.parametrize('source_mib', [51, 201])
def test_large_knowledge_upload_preserves_source_and_search(
        isolated_knowledge, flask_client, source_mib):
    from werkzeug.datastructures import FileStorage
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml', '<?xml version="1.0" encoding="UTF-8"?>'
            '<office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
            '<office:body><office:text><text:p>Large upload searchable evidence '
            'and tailuploadmarker</text:p></office:text></office:body></office:document-content>')
        archive.writestr('META-INF/manifest.xml', '<?xml version="1.0"?>'
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
            'manifest:version="1.2"><manifest:file-entry manifest:full-path="/" '
            'manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            '<manifest:file-entry manifest:full-path="payload.bin" '
            'manifest:media-type="application/octet-stream"/></manifest:manifest>')
        # An uncompressed package resource grows the actual original without
        # substituting a fake parser or demanding a huge searchable corpus.
        archive.writestr('payload.bin', b'x' * (source_mib * 1024 * 1024))
    raw = buffer.getvalue()
    assert len(raw) > 50 * 1024 * 1024
    def upload():
        boundary = 'knowledge-complete-original'
        header = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="large-source.odt"\r\nContent-Type: application/vnd.oasis.opendocument.text\r\n\r\n').encode()
        body = header + raw + f'\r\n--{boundary}--\r\n'.encode()
        return flask_client.post('/api/v4/knowledge/documents', data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    first = upload()
    assert first.status_code == 200, first.get_json()
    assert first.get_json()['errors'] == []
    attachment = first.get_json()['indexed'][0]
    assert attachment['size_bytes'] == len(raw)
    document_id = attachment['id']
    matches = isolated_knowledge.search_document_candidates(
        document_id, 'tailuploadmarker', user_id=TEST_OWNER_USER_ID)
    assert matches and 'tailuploadmarker' in matches[0]['content']
    source_path = isolated_knowledge.read_source_path(document_id, user_id=TEST_OWNER_USER_ID)
    assert source_path.stat().st_size == len(raw)
    assert hashlib.sha256(source_path.read_bytes()).digest() == hashlib.sha256(raw).digest()
    if source_mib == 51:
        second = upload()
        assert second.status_code == 200
        assert second.get_json()['indexed'][0]['id'] == document_id
    assert isolated_knowledge.delete_document(document_id, user_id=TEST_OWNER_USER_ID)
    assert not source_path.exists()
    print(f'document_upload_demand: {len(raw)} original ODT bytes, searchable, exact source and digest reuse')


@pytest.mark.auth_mode('open')
def test_knowledge_batch_indexes_all_files_past_old_count_limit(isolated_knowledge, flask_client):
    boundary = 'knowledge-owned-batch'
    body = b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="evidence-{i}.md"\r\nContent-Type: text/markdown\r\n\r\nUnique evidence batchmarker{i}\r\n').encode() for i in range(33))
    body += f'--{boundary}--\r\n'.encode()
    response = flask_client.post('/api/v4/knowledge/documents', data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload['errors'] == []
    assert len(payload['indexed']) == 33
    for i, document in enumerate(payload['indexed']):
        assert document['name'] == f'evidence-{i}.md'
        assert isolated_knowledge.read_source_path(document['id'], user_id=TEST_OWNER_USER_ID).read_bytes() == f'Unique evidence batchmarker{i}'.encode()
        assert isolated_knowledge.delete_document(document['id'], user_id=TEST_OWNER_USER_ID)


@pytest.mark.auth_mode('open')
def test_knowledge_receive_failure_reports_file_and_continues_batch(isolated_knowledge, flask_client, monkeypatch):
    from types import SimpleNamespace
    import routes.api_v1.knowledge as routes
    class FailedSource:
        def read(self):
            raise OSError('injected receive failure')
    uploads = [SimpleNamespace(filename='failed.md', stream=FailedSource()),
               SimpleNamespace(filename='accepted.md', stream=io.BytesIO(b'accepted evidence'))]
    monkeypatch.setattr(routes, '_uploaded_files', lambda: uploads)
    response = flask_client.post('/api/v4/knowledge/documents')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['errors'] == [{'name': 'failed.md', 'error': 'Unexpected indexing failure'}]
    assert len(payload['indexed']) == 1
    document = payload['indexed'][0]
    assert isolated_knowledge.read_source_path(document['id'], user_id=TEST_OWNER_USER_ID).read_bytes() == b'accepted evidence'
    assert isolated_knowledge.delete_document(document['id'], user_id=TEST_OWNER_USER_ID)
