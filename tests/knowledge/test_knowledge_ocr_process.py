"""Knowledge OCR runs in owned children: GIL freedom, exact output, failures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import threading
import time

import pytest

from lib.knowledge import _ocr_worker, ocr
from lib.runtime import owned_helper_process

pytestmark = pytest.mark.unit


def _native_ocr():
    pymupdf = pytest.importorskip('pymupdf', exc_type=ImportError)
    try:
        pymupdf.get_tessdata()
    except Exception as error:  # Hosts without Tesseract data cannot run OCR.
        pytest.skip(f'Tesseract language data unavailable: {error}')
    return pymupdf


def _screenshot(pymupdf, lines, *, width=640, height=110):
    document = pymupdf.open()
    try:
        page = document.new_page(width=width, height=height)
        for index, line in enumerate(lines):
            page.insert_text((16, 36 + 30 * index), line, fontname='china-s', fontsize=20)
        return page.get_pixmap(dpi=96).tobytes('png')
    finally:
        document.close()


def _scanned_pdf(pymupdf, images):
    document = pymupdf.open()
    try:
        for image in images:
            page = document.new_page(width=640, height=110)
            page.insert_image(page.rect, stream=image)
        return document.tobytes()
    finally:
        document.close()


def _former_in_process_ocr(pymupdf, raw, filetype):
    """The pre-offload server-side implementation, kept as an output oracle."""
    image_document = pymupdf.open(stream=raw, filetype=filetype)
    try:
        pdf_bytes = image_document.convert_to_pdf()
    finally:
        image_document.close()
    pdf = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    try:
        page = pdf[0]
        for language in ('chi_sim+eng', 'eng'):
            try:
                text_page = page.get_textpage_ocr(language=language, dpi=150, full=True)
                text = page.get_text('text', textpage=text_page).strip()
                if text:
                    return text[:20_000]
            except Exception:
                continue
    finally:
        pdf.close()
    return ''


def _max_tick_gap(work):
    """Run ``work`` on a task thread while this thread ticks every millisecond."""
    outcome = {}
    finished = threading.Event()

    def run():
        started = time.perf_counter()
        try:
            outcome['value'] = work()
        finally:
            outcome['seconds'] = time.perf_counter() - started
            finished.set()

    worker = threading.Thread(target=run, name='ocr-tick-probe')
    stamps = [time.perf_counter()]
    worker.start()
    while not finished.is_set():
        time.sleep(0.001)
        stamps.append(time.perf_counter())
    worker.join()
    return outcome['value'], outcome['seconds'], max(b - a for a, b in zip(stamps, stamps[1:]))


def test_ocr_child_keeps_server_threads_ticking():
    from lib.knowledge import assets

    pymupdf = _native_ocr()
    raw = _screenshot(pymupdf, ['Tofu OCR 2026 截图识别', 'knowledge 知识库 screenshot',
                                'Settings 设置 window 3', 'Search 搜索 results 4'], height=170)

    in_process, in_process_seconds, in_process_gap = _max_tick_gap(
        lambda: _ocr_worker.recognize_image(pymupdf, raw, 'png', {}))
    child, child_seconds, child_gap = _max_tick_gap(
        lambda: assets._ocr_image(raw, 'image/png'))

    print(f'in-process OCR {in_process_seconds:.2f}s, max tick gap {in_process_gap:.3f}s; '
          f'child OCR {child_seconds:.2f}s, max tick gap {child_gap:.3f}s')
    assert child == in_process and child
    # Control: PyMuPDF holds the GIL for the whole Tesseract pass in-process.
    assert in_process_gap >= 0.5 * in_process_seconds
    assert child_seconds >= in_process_seconds * 0.5
    assert child_gap < min(0.25, in_process_gap / 4)


def test_child_output_matches_the_former_in_process_ocr():
    pymupdf = _native_ocr()
    mixed = _screenshot(pymupdf, ['Hello OCR 2026 中文识别'])
    blank = _screenshot(pymupdf, [])
    jpeg = pymupdf.Pixmap(mixed).tobytes('jpeg')

    for raw, mime_type, filetype in ((mixed, 'image/png', 'png'),
                                     (blank, 'image/png', 'png'),
                                     (jpeg, 'image/jpeg', 'jpeg')):
        expected = _former_in_process_ocr(pymupdf, raw, filetype)
        assert ocr.recognize_image(raw, mime_type) == expected
    assert 'OCR' in ocr.recognize_image(mixed, 'image/png')
    assert ocr.recognize_image(blank, 'image/png') == ''


def test_scanned_pdf_ocr_runs_in_a_child_with_the_caller_page_range():
    from lib.knowledge import ingest

    pymupdf = _native_ocr()
    pdf = _scanned_pdf(pymupdf, [_screenshot(pymupdf, ['First page 第一页']),
                                 _screenshot(pymupdf, ['Second page 第二页'])])

    expected = _ocr_worker.recognize_pdf(
        pymupdf, pdf, page_limit=1, max_chars=100_000, failures={})
    assert ingest._ocr_scanned_pdf(pdf, 1, 100_000) == expected
    text, warnings = expected
    assert text.startswith('## Page 1\n\n') and '## Page 2' not in text
    assert warnings == ['OCR read 1 of 2 pages; remaining pages were not OCRed']


class _ScriptedPage:
    """A page whose OCR outcome per language is scripted text or an error."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.languages = []

    def get_textpage_ocr(self, *, language, dpi, full):
        self.languages.append(language)
        outcome = self.outcomes[language]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_text(self, kind, *, textpage):
        return textpage


@pytest.mark.parametrize(('outcomes', 'languages', 'text'), [
    ({'chi_sim+eng': 'mixed 中文'}, ['chi_sim+eng'], 'mixed 中文'),
    # Empty text is a result: a blank screenshot costs one Tesseract pass.
    ({'chi_sim+eng': ' \n'}, ['chi_sim+eng'], ' \n'),
    ({'chi_sim+eng': RuntimeError('Tesseract language initialisation failed'),
      'eng': 'english only'}, ['chi_sim+eng', 'eng'], 'english only'),
    ({'chi_sim+eng': RuntimeError('no data'), 'eng': RuntimeError('no data')},
     ['chi_sim+eng', 'eng'], ''),
])
def test_eng_retry_runs_only_when_the_primary_pass_cannot_run(outcomes, languages, text):
    page = _ScriptedPage(outcomes)
    failures = {}
    assert _ocr_worker.recognize_page(page, failures) == text
    assert page.languages == languages
    assert sum(failures.values()) == sum(
        isinstance(outcome, Exception) for outcome in outcomes.values())


def test_child_failures_propagate_and_callers_stay_best_effort():
    from lib.knowledge import assets, ingest

    _native_ocr()
    corrupt = b'\x89PNG\r\n\x1a\n' + b'not an image' * 8
    with pytest.raises(ocr.OcrWorkerError) as raised:
        ocr.recognize_image(corrupt, 'image/png')
    assert raised.value.error_type and str(raised.value).startswith(raised.value.error_type)
    assert assets._ocr_image(corrupt, 'image/png') == ''
    assert ingest._ocr_scanned_pdf(b'%PDF-1.7 corrupt', None, 1000) == (
        '', ['Scanned PDF OCR was unavailable; image-only pages may be missing'])


def test_worker_refuses_arguments_from_another_revision():
    completed = subprocess.run(
        [sys.executable, _ocr_worker.__file__, 'image'], input=b'', capture_output=True,
        timeout=60, check=False)
    completion = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert 'restart the Tofu server' in completion['error']['message']


@pytest.fixture
def fake_ocr_provider(monkeypatch, tmp_path):
    """A controllable PyMuPDF stand-in imported only by OCR children."""
    provider = tmp_path / 'provider'
    provider.mkdir()
    (provider / 'pymupdf.py').write_text('''
import json, os, pathlib, signal, time
class Page:
    def __init__(self, request):
        self.request = request
    def get_textpage_ocr(self, *, language, dpi, full):
        root = pathlib.Path(self.request['root'])
        root.joinpath(str(self.request['id']) + '.entered').write_text(json.dumps({
            'pid': os.getpid(), 'secret': 'TOFU_TEST_SECRET' in os.environ,
            'tessdata': os.environ.get('TESSDATA_PREFIX')}))
        if self.request.get('mode') == 'stall':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            root.joinpath('stall.ready').touch()
            while True:
                time.sleep(.01)
        if self.request.get('mode') == 'crash':
            os.kill(os.getpid(), signal.SIGKILL)
        limit = time.monotonic() + 10
        while len(list(root.glob('*.entered'))) < self.request.get('barrier', 1):
            if time.monotonic() > limit:
                raise RuntimeError('OCR rendezvous timed out')
            time.sleep(.005)
        return language
    def get_text(self, kind, *, textpage):
        return str(self.request['id']) + ':' + textpage
class Document:
    page_count = 1
    def __init__(self, request):
        self.request = request
    def convert_to_pdf(self):
        return json.dumps(self.request).encode()
    def __getitem__(self, index):
        return Page(self.request)
    def close(self):
        pass
def open(*, stream, filetype):
    return Document(json.loads(stream))
''')
    monkeypatch.setenv('PYTHONPATH', str(provider))
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path / 'tessdata'))
    monkeypatch.setenv('TOFU_TEST_SECRET', 'must-not-enter-child')
    processes = []
    lock = threading.Lock()
    popen = owned_helper_process.subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        with lock:
            processes.append(process)
        return process

    monkeypatch.setattr(owned_helper_process.subprocess, 'Popen', launch)

    def request(index=0, **values):
        return json.dumps(dict(root=str(tmp_path), id=index, **values)).encode()

    yield request, processes, tmp_path
    for process in processes:
        owned_helper_process.join_owned_process(process)


def test_each_job_starts_its_own_child_without_a_shared_slot(fake_ocr_provider):
    request, processes, root = fake_ocr_provider
    count = 8
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(ocr.recognize_image, request(index, barrier=count), 'image/png')
                   for index in range(count)]
        results = [future.result(timeout=30) for future in futures]
    assert results == [f'{index}:chi_sim+eng' for index in range(count)]
    assert len({process.pid for process in processes}) == count
    assert all(process.poll() == 0 for process in processes)
    entered = [json.loads(path.read_text()) for path in root.glob('*.entered')]
    assert {entry['tessdata'] for entry in entered} == {str(root / 'tessdata')}
    assert not any(entry['secret'] for entry in entered)


def test_cancellation_reaps_a_child_ignoring_terminate(fake_ocr_provider):
    request, processes, root = fake_ocr_provider

    class Stopped(Exception):
        pass

    def cancelled():
        if (root / 'stall.ready').exists():
            raise Stopped()

    started = time.monotonic()
    with pytest.raises(Stopped):
        ocr.recognize_image(request(mode='stall'), 'image/png', cancelled=cancelled)
    assert time.monotonic() - started < 10
    assert len(processes) == 1 and processes[0].poll() is not None


def test_knowledge_entry_points_run_ocr_in_owned_children(fake_ocr_provider):
    from lib.knowledge import assets, ingest

    request, processes, _root = fake_ocr_provider
    assert assets._ocr_image(request(0), 'image/png') == '0:chi_sim+eng'
    assert ingest._ocr_scanned_pdf(request(1), None, 1000) == (
        '## Page 1\n\n1:chi_sim+eng', [])
    assert len(processes) == 2 and all(process.poll() == 0 for process in processes)


def test_a_crashed_child_is_an_error_not_empty_text(fake_ocr_provider):
    request, processes, _root = fake_ocr_provider
    with pytest.raises(ocr.OcrWorkerError, match='no valid completion'):
        ocr.recognize_image(request(mode='crash'), 'image/png')
    assert processes[0].poll() is not None
