"""Received channel media reuse the owner-scoped attachment pipeline (C9).

Runs ``lib.media_attachments.ingest_received_file`` against a real knowledge
Sidecar: originals become retained attachments that model projection,
``read_files`` and ``inspect_image`` already understand; formats the parser
cannot read keep their verbatim original. Nothing is executed or opened.
"""
from __future__ import annotations

import io
import zipfile

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)

TEST_OWNER_USER_ID = 1
ORIGIN = {'channel': 'dx', 'eventId': 'dx:gid:500/7000000000000000501'}


def _png() -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (200, 30, 30)).save(buffer, format='PNG')
    return buffer.getvalue()


def _received(tmp_path, data: bytes):
    path = tmp_path / 'received.bin'
    path.write_bytes(data)
    return str(path)


def test_a_received_image_is_an_attachment_agents_can_see(isolated_knowledge, tmp_path):
    from lib.attachments import resolve_attachment
    from lib.media_attachments import ingest_received_file, project_for_model

    # 大象 often labels PNG bytes as jpg: the bytes decide the type.
    ref = ingest_received_file(_received(tmp_path, _png()), 'image-7000000000000000501.jpg', 'image/jpeg',
                               user_id=TEST_OWNER_USER_ID, origin=ORIGIN)
    assert (ref['kind'], ref['status'], ref['mimeType']) == ('document', 'ready', 'image/png')
    assert ref['name'] == 'image-7000000000000000501.jpg'
    assert ref['sourceUrl'] == f'/api/v4/media/attachments/{ref["attachmentId"]}/source'
    document = isolated_knowledge.get_document_metadata(ref['attachmentId'], user_id=TEST_OWNER_USER_ID)
    assert document['scope'] == 'attachment'
    assert document['media_metadata']['origin'] == 'channel'
    assert document['media_metadata']['channel_event'] == ORIGIN['eventId']
    seen = resolve_attachment(f'att_media_{ref["attachmentId"]}', user_id=TEST_OWNER_USER_ID)
    assert seen['kind'] == 'image' and seen['mime_type'] == 'image/png'
    assert project_for_model([ref], user_id=TEST_OWNER_USER_ID, query='', model='')['image_count'] == 1


def test_a_received_document_is_indexed_and_readable(isolated_knowledge, tmp_path):
    from lib.attachments import read_text_attachment_ref
    from lib.media_attachments import ingest_received_file

    ref = ingest_received_file(_received(tmp_path, '# 季度复盘\n收入增长 12%。\n'.encode('utf-8')), '季度复盘.md',
                               'text/markdown', user_id=TEST_OWNER_USER_ID, origin=ORIGIN)
    assert (ref['status'], ref['mimeType'], ref['name']) == ('ready', 'text/markdown', '季度复盘.md')
    assert ref['textChars'] > 0
    read = read_text_attachment_ref(f'att_media_{ref["attachmentId"]}', user_id=TEST_OWNER_USER_ID)
    assert '收入增长 12%' in read['text']


@pytest.mark.parametrize('name, mime, data', [
    ('voice-7000000000000000504.amr', 'audio/amr', b'#!AMR\n' + bytes([0x3c] + [0] * 31) * 4),
    ('bundle.zip', 'application/zip', None),
    ('broken.pdf', 'application/pdf', b'%PDF-1.7 not really a pdf'),
])
def test_formats_the_parser_cannot_read_keep_their_verbatim_original(isolated_knowledge, tmp_path,
                                                                    name, mime, data):
    from lib.media_attachments import ingest_received_file

    if data is None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('run.sh', '#!/bin/sh\necho never executed\n')
        data = buffer.getvalue()
    ref = ingest_received_file(_received(tmp_path, data), name, mime,
                               user_id=TEST_OWNER_USER_ID, origin=ORIGIN)
    assert (ref['status'], ref['mimeType'], ref['name'], ref['sizeBytes']) == ('ready', mime, name, len(data))
    source = isolated_knowledge.read_source_path(ref['attachmentId'], user_id=TEST_OWNER_USER_ID)
    assert source.read_bytes() == data
    document = isolated_knowledge.get_document_metadata(ref['attachmentId'], user_id=TEST_OWNER_USER_ID)
    assert document['scope'] == 'attachment' and document['media_metadata']['origin'] == 'channel'


def test_the_same_original_received_twice_is_one_attachment(isolated_knowledge, tmp_path):
    from lib.media_attachments import ingest_received_file

    first = ingest_received_file(_received(tmp_path, b'#!AMR\n\x3c'), 'voice-1.amr', 'audio/amr',
                                 user_id=TEST_OWNER_USER_ID, origin=ORIGIN)
    again = ingest_received_file(_received(tmp_path, b'#!AMR\n\x3c'), 'voice-1.amr', 'audio/amr',
                                 user_id=TEST_OWNER_USER_ID, origin=ORIGIN)
    assert first['attachmentId'] == again['attachmentId']
