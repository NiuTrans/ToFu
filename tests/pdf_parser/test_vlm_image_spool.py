"""Whole-document VLM staging, explicit budgets and joined file ownership."""
from __future__ import annotations

import threading
import time

import pytest

from lib.llm.llm_errors import AbortedError
from lib.pdf_parser.images import _render
from lib.pdf_parser.vlm import _parse

pytestmark = pytest.mark.unit


def _fake_pdf(monkeypatch, *, pages, fail_page=None):
    closed = []

    class Pixmap:
        def __init__(self, index):
            self.index = index

        def tobytes(self, kind):
            assert kind == 'jpeg'
            return self.index.to_bytes(4, 'big') + b'x' * 1020

    class Page:
        def __init__(self, index):
            self.index = index

        def get_pixmap(self, *, dpi):
            if self.index == fail_page:
                raise OSError('injected pixmap failure')
            return Pixmap(self.index)

    class Document:
        def __len__(self):
            return pages

        def __getitem__(self, index):
            return Page(index)

        def close(self):
            closed.append(True)

    class Pdf:
        @staticmethod
        def open(**kwargs):
            return Document()

    monkeypatch.setattr(_render, 'pymupdf', Pdf())
    # Unit transcription cases borrow the shared in-process render kernel;
    # test_vlm_render_process covers the real production process boundary.
    monkeypatch.setattr(_parse, 'render_pdf_pages', _render.render_pdf_pages)
    return closed


def _track_files(monkeypatch):
    files = []
    create = _render.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        result = create(*args, **kwargs)
        files.append(result)
        return result

    monkeypatch.setattr(_render.tempfile, 'TemporaryFile', temporary)
    return files


def test_all_pages_above_retired_limit_spool_and_transcribe_in_order(monkeypatch):
    count = 2051
    closed = _fake_pdf(monkeypatch, pages=count)
    files = _track_files(monkeypatch)
    monkeypatch.setenv('TOFU_PDF_VLM_MAX_PAGES', '1')
    barrier = threading.Barrier(8, timeout=10)
    labels = []
    progress = []
    lock = threading.Lock()

    def call(images, label, model, **kwargs):
        first = int(label.split('-')[0][2:]) - 1
        assert all(image == (first + i).to_bytes(4, 'big') + b'x' * 1020
                   for i, image in enumerate(images))
        with lock:
            labels.append(label)
        barrier.wait()
        return label

    monkeypatch.setattr(_parse, '_vlm_call_pages', call)
    started = time.monotonic()
    result = _parse.vlm_parse_pdf(b'pdf', model='vision', max_workers=1,
        batch_pages=257, progress_cb=lambda done, total: progress.append((done, total)))
    expected = [f'p.{i + 1}-{min(i + 257, count)}' for i in range(0, count, 257)]
    assert result.split('\n\n') == expected
    assert set(labels) == set(expected)
    assert progress[-1] == (count, count) and len(progress) == 8
    assert closed == [True] and len(files) == 1 and files[0].closed
    assert not any(t.name.startswith('vlm_') for t in threading.enumerate())
    print(f'2051 pages / {count * 1024} spool bytes: '
          f'{(time.monotonic() - started) * 1000:.2f} ms; 0 spool descriptors')


@pytest.mark.parametrize('failure', ['pixmap', 'write', 'abort', 'page_budget'])
def test_render_failure_closes_document_and_owned_spool(monkeypatch, failure):
    closed = _fake_pdf(monkeypatch, pages=5, fail_page=2 if failure == 'pixmap' else None)
    files = _track_files(monkeypatch)
    if failure == 'write':
        def write(self, image):
            raise OSError('injected spool write failure')
        monkeypatch.setattr(_render.RenderedPageSpool, 'append', write)
    checks = 0

    def abort():
        nonlocal checks
        checks += 1
        return failure == 'abort' and checks == 3

    expected = (AbortedError if failure == 'abort' else
                _render.PdfPageLimitExceeded if failure == 'page_budget' else OSError)
    with pytest.raises(expected):
        _render.render_pdf_pages(b'pdf', spool=True, abort_check=abort,
                                 max_pages=1 if failure == 'page_budget' else None)
    assert closed == [True] and len(files) == 1 and files[0].closed


@pytest.mark.parametrize('failure', ['abort', 'progress'])
def test_parser_joins_readers_before_spool_close_on_failure(monkeypatch, failure):
    _fake_pdf(monkeypatch, pages=8)
    files = _track_files(monkeypatch)
    barrier = threading.Barrier(8, timeout=10)
    active = set()
    lock = threading.Lock()
    original_close = _render.RenderedPageSpool.close

    def close(spool):
        assert not active
        original_close(spool)

    def call(images, label, model, **kwargs):
        with lock:
            active.add(label)
        try:
            barrier.wait()
            if label == 'p.1':
                if failure == 'abort':
                    raise AbortedError('injected call abort')
            else:
                time.sleep(0.03)
            assert not files[0].closed
            return label
        finally:
            with lock:
                active.remove(label)

    def progress(done, total):
        raise RuntimeError('injected progress failure')

    monkeypatch.setattr(_render.RenderedPageSpool, 'close', close)
    monkeypatch.setattr(_parse, '_vlm_call_pages', call)
    with pytest.raises(AbortedError if failure == 'abort' else RuntimeError):
        _parse.vlm_parse_pdf(b'pdf', model='vision', batch_pages=1, max_workers=1,
                            progress_cb=progress if failure == 'progress' else None)
    assert not active and files[0].closed
    assert not any(t.name.startswith('vlm_') for t in threading.enumerate())
