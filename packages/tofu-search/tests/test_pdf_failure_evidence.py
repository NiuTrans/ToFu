"""PDF capability/parse failures must not enter the source-evidence channel."""

from types import SimpleNamespace

import pytest

from tofu_search import configure, fetch_page_content
from tofu_search.fetch import core, pdf_extract


class Document:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __iter__(self):
        return iter(SimpleNamespace(get_text=lambda text=text: text) for text in self.pages)

    def close(self):
        self.closed = True


def prepare(monkeypatch, pages=()):
    configure(block_private_addresses=False)
    document = Document(pages)
    monkeypatch.setattr(pdf_extract, '_dependencies_loaded', True)
    monkeypatch.setattr(pdf_extract, '_pymupdf', SimpleNamespace(open=lambda **kw: document))
    monkeypatch.setattr(pdf_extract, '_pymupdf4llm', None)
    response = SimpleNamespace(headers={'Content-Type': 'application/pdf'}, encoding='utf-8')
    requests = []
    def request(url, *args, **kwargs):
        requests.append(url)
        return response, b'%PDF-synthetic fixture'
    monkeypatch.setattr(core, '_do_request', request)
    def forbidden(*args, **kwargs):
        pytest.fail('PDF extraction failure must not invoke browser/HTML recovery')
    monkeypatch.setattr(core, '_try_browser_fetch', forbidden)
    monkeypatch.setattr(core, '_try_playwright_fallback', forbidden)
    return document, requests


@pytest.mark.parametrize('failure', ['missing_dependency', 'oversize', 'malformed', 'zero_pages', 'blank_pages'])
def test_pdf_failure_is_not_body_text(monkeypatch, failure):
    document, _ = prepare(monkeypatch, [' \n\t', ''] if failure == 'blank_pages' else [])
    if failure == 'missing_dependency':
        monkeypatch.setattr(pdf_extract, '_pymupdf', None)
    elif failure == 'oversize':
        monkeypatch.setattr(pdf_extract, 'MAX_PDF_BYTES', 4)
    elif failure == 'malformed':
        def invalid(**kwargs):
            raise ValueError('synthetic broken PDF')
        monkeypatch.setattr(pdf_extract, '_pymupdf', SimpleNamespace(open=invalid))
    diag = {}
    body = fetch_page_content(f'https://pdf.example/{failure}.pdf', diag=diag)
    assert body is None
    assert diag['reason'] == 'pdf_extraction_error'
    assert diag['detail']
    if failure in {'zero_pages', 'blank_pages'}:
        assert document.closed


def test_missing_dependency_failure_does_not_poison_the_body_cache(monkeypatch):
    text = 'This is the independently extracted PDF evidence about quasar calibration.'
    document, requests = prepare(monkeypatch, [text])
    available = pdf_extract._pymupdf
    monkeypatch.setattr(pdf_extract, '_pymupdf', None)
    url = 'https://pdf.example/after-capability-recovery.pdf'
    assert fetch_page_content(url) is None
    monkeypatch.setattr(pdf_extract, '_pymupdf', available)
    assert fetch_page_content(url) == text
    assert len(requests) == 2
    assert document.closed


@pytest.mark.parametrize('rich', [False, True])
def test_error_shaped_source_text_remains_real_evidence(monkeypatch, rich):
    text = '[PDF extraction unavailable: pymupdf not installed] is the error discussed in this troubleshooting article.'
    document, _ = prepare(monkeypatch, [text])
    if rich:
        monkeypatch.setattr(pdf_extract, '_pymupdf4llm', object())
        monkeypatch.setattr(pdf_extract, '_to_markdown_classic', lambda *a, **kw: [{'text': text}])
    diag = {}
    assert fetch_page_content(f'https://pdf.example/error-quotation-{rich}.pdf', diag=diag) == text
    assert 'reason' not in diag
    assert document.closed


def test_empty_rich_extraction_can_still_recover_raw_text(monkeypatch):
    text = 'The raw PDF layer contains useful evidence that the optional rich extractor missed.'
    document, _ = prepare(monkeypatch, [text])
    monkeypatch.setattr(pdf_extract, '_pymupdf4llm', object())
    monkeypatch.setattr(pdf_extract, '_to_markdown_classic', lambda *a, **kw: [{'text': ' \n'}, {'text': ''}])
    assert fetch_page_content('https://pdf.example/rich-empty-raw-works.pdf') == text
    assert document.closed
