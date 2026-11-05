"""Tool-result images commit before OCR; an owned background job indexes the text."""
from __future__ import annotations

import hashlib
import io
import threading
import time

import pytest

from lib.knowledge.repository import KnowledgeRepository

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)

TEST_OWNER_USER_ID = 1
OCR_TEXT = 'Settings 设置 screenshot'


def _png(seed: int) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new('RGB', (48, 32), (seed % 256, 90, 170)).save(output, format='PNG')
    return output.getvalue()


def _until(predicate, *, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('condition not reached')
        time.sleep(0.01)


@pytest.fixture
def gated_ocr(monkeypatch):
    """Replace the OCR child with a gate that honours owner cancellation."""
    from lib.knowledge import image_ocr_jobs, ocr

    gate = threading.Event()
    calls = []

    def recognize_image(raw, mime_type, *, cancelled=None):
        calls.append(hashlib.sha256(raw).hexdigest())
        while not gate.wait(0.01):
            if cancelled is not None:
                cancelled()
        return OCR_TEXT

    monkeypatch.setattr(ocr, 'recognize_image', recognize_image)
    jobs = image_ocr_jobs.AttachmentImageOcr()
    monkeypatch.setattr(image_ocr_jobs, '_ATTACHMENT_OCR', jobs)
    yield gate, calls, jobs
    gate.set()
    assert jobs.stop(timeout=10)


def _ingest(raw, call_id):
    from lib.media_attachments import ingest_mcp_image

    return ingest_mcp_image(
        raw, 'image/png', user_id=TEST_OWNER_USER_ID,
        source_tool='mcp__browser__screenshot', tool_call_id=call_id, ordinal=0)


def test_tool_image_receipt_commits_before_ocr_and_indexes_like_synchronous(
        isolated_knowledge, gated_ocr):
    gate, calls, jobs = gated_ocr
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    raw = _png(1)

    image = _ingest(raw, 'call-1')

    document_id = image['attachmentId']
    assert repository.document(document_id)['assets'][0]['ocr_text'] == ''
    assert isolated_knowledge.read_source_path(
        document_id, user_id=TEST_OWNER_USER_ID).read_bytes() == raw
    _until(lambda: calls)
    assert jobs.snapshot()['running'] == 1
    gate.set()
    _until(lambda: jobs.snapshot()['running'] == 0)
    deferred = repository.document(document_id)
    assert deferred['assets'][0]['ocr_text'] == OCR_TEXT
    assert jobs.snapshot()['published'] == 1
    assert [[asset['id'] for asset in row['assets']]
            for row in isolated_knowledge.search_document_candidates(
                document_id, '设置', user_id=TEST_OWNER_USER_ID)] == [
        [deferred['assets'][0]['id']]]

    # The same name and bytes indexed synchronously give the identical index.
    assert isolated_knowledge.delete_document(document_id, user_id=TEST_OWNER_USER_ID)
    synchronous = isolated_knowledge.add_document(
        raw, image['name'], user_id=TEST_OWNER_USER_ID, scope='attachment')
    expected = repository.document(synchronous['id'])
    assert [(chunk['section'], chunk['location'], chunk['content'], chunk['search_text'])
            for chunk in deferred['chunks']] == [
        (chunk['section'], chunk['location'], chunk['content'], chunk['search_text'])
        for chunk in expected['chunks']]
    assert expected['assets'][0]['ocr_text'] == OCR_TEXT
    assert len(calls) == 2


def test_identical_tool_image_bytes_reuse_the_content_addressed_record(
        isolated_knowledge, gated_ocr):
    gate, calls, jobs = gated_ocr
    raw = _png(2)

    first = _ingest(raw, 'call-a')
    _until(lambda: calls)
    in_flight = _ingest(raw, 'call-b')
    gate.set()
    _until(lambda: jobs.snapshot()['running'] == 0)
    settled = _ingest(raw, 'call-c')

    assert first['attachmentId'] == in_flight['attachmentId'] == settled['attachmentId']
    assert calls == [hashlib.sha256(raw).hexdigest()]
    assert jobs.snapshot()['accepted'] == 1
    # Reindex is the explicit invalidation: it recomputes OCR from the original.
    reindexed = isolated_knowledge.reindex_document(
        first['attachmentId'], user_id=TEST_OWNER_USER_ID)
    assert reindexed is not None and len(calls) == 2


def test_stop_cancels_running_ocr_writes_nothing_and_reopens(isolated_knowledge, gated_ocr):
    gate, calls, jobs = gated_ocr
    repository = KnowledgeRepository(TEST_OWNER_USER_ID)
    image = _ingest(_png(3), 'call-stop')
    _until(lambda: calls)

    assert jobs.stop(timeout=10)

    assert jobs.snapshot() == {'running': 0, 'accepted': 1, 'published': 0, 'empty': 0,
                               'missing': 0, 'failed': 0, 'cancelled': 1}
    document = repository.document(image['attachmentId'])
    assert document['assets'][0]['ocr_text'] == ''
    assert 'OCR text' not in document['chunks'][0]['content']
    assert not [thread for thread in threading.enumerate()
                if thread.name == 'knowledge-attachment-ocr']
    gate.set()
    assert jobs.schedule(user_id=TEST_OWNER_USER_ID, asset_id=document['assets'][0]['id'])
    _until(lambda: jobs.snapshot()['published'] == 1)
    assert repository.document(image['attachmentId'])['assets'][0]['ocr_text'] == OCR_TEXT


def test_ocr_failure_keeps_the_committed_attachment(isolated_knowledge, monkeypatch):
    from lib.knowledge import image_ocr_jobs, ocr

    def failed(raw, mime_type, *, cancelled=None):
        raise ocr.OcrWorkerError('FzErrorFormat: injected', error_type='FzErrorFormat')

    monkeypatch.setattr(ocr, 'recognize_image', failed)
    jobs = image_ocr_jobs.AttachmentImageOcr()
    monkeypatch.setattr(image_ocr_jobs, '_ATTACHMENT_OCR', jobs)
    image = _ingest(_png(4), 'call-failed')
    _until(lambda: jobs.snapshot()['failed'] == 1)
    assert jobs.stop(timeout=10)
    document = KnowledgeRepository(TEST_OWNER_USER_ID).document(image['attachmentId'])
    assert document['assets'][0]['ocr_text'] == '' and document['method'] == 'image-local'
