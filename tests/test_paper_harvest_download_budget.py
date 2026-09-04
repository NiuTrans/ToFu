"""Bounded harvest PDF transport and deterministic rejection behavior."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, chunks, *, declared=None):
        self._chunks = list(chunks)
        self.headers = (
            {'Content-Length': str(declared)} if declared is not None else {})
        self.closed = False
        self.chunk_sizes = []

    @staticmethod
    def raise_for_status():
        return None

    def iter_content(self, chunk_size):
        self.chunk_sizes.append(chunk_size)
        yield from self._chunks

    def close(self):
        self.closed = True


def test_declared_oversize_rejects_before_stream_and_closes(monkeypatch):
    import lib.paper.harvest as harvest

    response = _Response(
        [b'must-not-be-consumed'], declared=11)
    monkeypatch.setattr(harvest, '_harvest_pdf_byte_limit', lambda: 10)
    monkeypatch.setattr(harvest, 'http_get', lambda *args, **kwargs: response)

    with pytest.raises(harvest.HarvestPDFTooLargeError, match='limit'):
        harvest._download_pdf_bytes('2608.00001')
    assert response.chunk_sizes == []
    assert response.closed is True


def test_chunked_oversize_rejects_without_trusting_headers(monkeypatch):
    import lib.paper.harvest as harvest

    response = _Response([b'12345', b'678901'])
    monkeypatch.setattr(harvest, '_harvest_pdf_byte_limit', lambda: 10)
    monkeypatch.setattr(harvest, 'http_get', lambda *args, **kwargs: response)

    with pytest.raises(harvest.HarvestPDFTooLargeError, match='limit'):
        harvest._download_pdf_bytes('2608.00001')
    assert response.chunk_sizes == [64 * 1024]
    assert response.closed is True


def test_bounded_download_validates_one_materialized_body(monkeypatch):
    import lib.paper.harvest as harvest
    import lib.pdf_parser.text as pdf_text

    body = b'%PDF-' + (b'x' * 40)
    response = _Response([body[:9], body[9:]], declared=len(body))
    validated = []
    monkeypatch.setattr(harvest, '_harvest_pdf_byte_limit', lambda: 64)
    monkeypatch.setattr(harvest, 'http_get', lambda *args, **kwargs: response)
    monkeypatch.setattr(
        pdf_text, 'validate_pdf_bytes',
        lambda data: validated.append(data) or (True, 1, ''))

    got = harvest._download_pdf_bytes('2608.00001')
    assert got == body
    assert validated == [body]
    assert response.closed is True


def test_permanent_oversize_is_not_retried(monkeypatch):
    import lib.paper.harvest as harvest

    calls = []

    def reject(arxiv_id):
        calls.append(arxiv_id)
        raise harvest.HarvestPDFTooLargeError('PDF exceeds limit')

    monkeypatch.setattr(harvest, '_download_pdf_bytes', reject)
    monkeypatch.setattr(
        harvest.time, 'sleep',
        lambda seconds: (_ for _ in ()).throw(
            AssertionError('permanent rejection must not back off/retry')))

    got = harvest.harvest_arxiv_id(
        '2608.00001', user_id=7, force_reparse=True)
    assert got.status == 'error' and 'exceeds limit' in got.error
    assert calls == ['2608.00001']
