"""Real renderer children: concurrent documents, cancellation and receipts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest

from lib.llm.llm_errors import AbortedError
from lib.pdf_parser.images import _render

pytestmark = pytest.mark.unit


@pytest.fixture
def renderer(monkeypatch, tmp_path):
    provider = tmp_path / 'provider'
    provider.mkdir()
    (provider / 'pymupdf.py').write_text('''
import json, os, pathlib, signal, time
class Document:
    def __init__(self, request):
        self.request = request
        self.root = pathlib.Path(request['root'])
        self.root.joinpath(str(request['id']) + '.entered').write_text(
            str(os.getpid()) + ':' + str('TOFU_TEST_SECRET' in os.environ))
        if request.get('stall'):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            self.root.joinpath('stall.ready').touch()
            while True:
                time.sleep(.01)
        limit = time.monotonic() + 10
        while len(list(self.root.glob('*.entered'))) < request.get('barrier', 1):
            if time.monotonic() > limit:
                raise RuntimeError('document rendezvous timed out')
            time.sleep(.005)
    def __len__(self):
        return self.request['pages']
    def __getitem__(self, index):
        root, request = self.root, self.request
        class Page:
            def get_pixmap(self, *, dpi):
                root.joinpath(str(request['id']) + '.pixmap').touch()
                if request.get('fail') and index == 1:
                    raise OSError('injected native page failure')
                class Pixmap:
                    def tobytes(self, kind):
                        return index.to_bytes(4, 'big') + b'x' * 4092
                return Pixmap()
        return Page()
    def close(self):
        self.root.joinpath(str(self.request['id']) + '.closed').touch()
def open(*, stream, filetype):
    return Document(json.loads(stream))
''')
    monkeypatch.setenv('PYTHONPATH', str(provider))
    monkeypatch.setenv('TOFU_TEST_SECRET', 'must-not-enter-child')
    processes, files = [], []
    lock = threading.Lock()
    popen = _render.subprocess.Popen
    temporary = _render.tempfile.TemporaryFile

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        with lock:
            processes.append(process)
        return process

    def create(*args, **kwargs):
        file = temporary(*args, **kwargs)
        with lock:
            files.append(file)
        return file

    monkeypatch.setattr(_render.subprocess, 'Popen', launch)
    monkeypatch.setattr(_render.tempfile, 'TemporaryFile', create)

    def payload(index=0, **kwargs):
        return json.dumps(dict(root=str(tmp_path), id=index, pages=8, **kwargs)).encode()

    yield payload, processes, files, tmp_path
    # Never abandon a failed test's owned child or file.
    for process in processes:
        _render._join_renderer(process)
    for file in files:
        file.close()


def test_independent_documents_render_concurrently_without_a_global_lock(renderer):
    payload, processes, files, root = renderer
    count = 16
    started = time.monotonic()
    results = []
    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(_render.render_pdf_pages_isolated,
                payload(index, barrier=count), spool=True) for index in range(count)]
            results = [future.result(timeout=20) for future in futures]
        assert len(processes) == count
        assert len({p.pid for p in processes}) == count
        assert all(p.poll() == 0 for p in processes)
        for pages in results:
            assert len(pages) == 8
            assert list(pages) == [i.to_bytes(4, 'big') + b'x' * 4092 for i in range(8)]
        assert len(list(root.glob('*.closed'))) == count
        assert all(marker.read_text().endswith(':False') for marker in root.glob('*.entered'))
        print(f'16 render children / {count * 8 * 4096} image bytes: '
              f'{(time.monotonic() - started) * 1000:.2f} ms')
    finally:
        for result in results:
            result.close()
    assert all(file.closed for file in files)


@pytest.mark.parametrize('mode', ['abort', 'callback_error', 'terminate_error'])
def test_cancellation_reaps_a_native_child_ignoring_terminate(renderer, mode):
    payload, processes, files, root = renderer

    def abort():
        if not (root / 'stall.ready').exists():
            return False
        if mode == 'callback_error':
            raise RuntimeError('injected abort callback failure')
        if mode == 'terminate_error':
            def failed_terminate():
                raise OSError('injected physical signal failure')
            processes[0].terminate = failed_terminate
        return True

    started = time.monotonic()
    with pytest.raises(RuntimeError if mode == 'callback_error' else AbortedError):
        _render.render_pdf_pages_isolated(payload(stall=True), spool=True, abort_check=abort)
    assert time.monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None
    assert all(file.closed for file in files)


@pytest.mark.parametrize('mode', ['page_budget', 'native_error', 'launch_failure'])
def test_failed_render_returns_no_partial_document_and_closes_files(renderer, monkeypatch, mode):
    payload, processes, files, root = renderer
    expected = OSError
    kwargs = {'spool': True}
    if mode == 'page_budget':
        expected = _render.PdfPageLimitExceeded
        kwargs['max_pages'] = 1
    if mode == 'launch_failure':
        monkeypatch.setattr(_render, '_render_command', lambda *args: [str(root / 'absent-renderer')])
    with pytest.raises(expected):
        _render.render_pdf_pages_isolated(payload(fail=mode == 'native_error'), **kwargs)
    assert all(process.poll() is not None for process in processes)
    assert all(file.closed for file in files)
    if mode == 'page_budget':
        assert not list(root.glob('*.pixmap'))
    if mode != 'launch_failure':
        assert (root / '0.closed').exists()


def test_incomplete_worker_output_is_an_error(renderer, monkeypatch):
    payload, processes, files, root = renderer
    script = root / 'incomplete.py'
    script.write_text("import sys; sys.stdout.buffer.write((4).to_bytes(8,'big') + b'JPEG')")
    monkeypatch.setattr(_render, '_render_command',
        lambda *args: [_render.sys.executable, str(script)])
    with pytest.raises(RuntimeError, match='incomplete page evidence'):
        _render.render_pdf_pages_isolated(payload(), spool=True)
    assert processes[0].poll() == 0 and all(file.closed for file in files)


def test_native_pdf_render_round_trip_when_host_library_is_compatible():
    pdf = pytest.importorskip('pymupdf', exc_type=ImportError)
    with _render.PYMUPDF_LOCK:
        document = pdf.open()
        try:
            document.new_page().insert_text((72, 72), 'First page')
            document.new_page().insert_text((72, 72), 'Second page')
            payload = document.tobytes()
        finally:
            document.close()
    pages = _render.render_pdf_pages_isolated(payload, spool=True)
    try:
        assert len(pages) == 2
        assert all(page.startswith(b'\xff\xd8') for page in pages)
    finally:
        pages.close()


def test_vlm_transcription_uses_owned_render_child_and_joined_spool(renderer, monkeypatch):
    from lib.pdf_parser.vlm import _parse

    payload, processes, files, root = renderer
    barrier = threading.Barrier(4, timeout=5)
    progress = []

    def call(images, label, model, **kwargs):
        assert processes[0].poll() == 0
        first = int(label.split('-')[0][2:]) - 1
        assert images == [i.to_bytes(4, 'big') + b'x' * 4092 for i in range(first, first + 2)]
        barrier.wait()
        return label

    monkeypatch.setattr(_parse, '_vlm_call_pages', call)
    result = _parse.vlm_parse_pdf(payload(), model='vision', batch_pages=2, max_workers=1,
        progress_cb=lambda done, total: progress.append((done, total)))
    assert result.split('\n\n') == ['p.1-2', 'p.3-4', 'p.5-6', 'p.7-8']
    assert progress[-1] == (8, 8)
    assert len(processes) == 1 and all(file.closed for file in files)
    assert not any(thread.name.startswith('vlm_') for thread in threading.enumerate())
