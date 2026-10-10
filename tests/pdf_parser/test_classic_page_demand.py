"""Caller ranges, not resource estimates, decide classic PDF page traversal."""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('requested, expected', [(0, 5001), (2, 2), (10000, 5001)])
def test_fast_parser_retains_tail_beyond_old_page_hard_max(monkeypatch, requested, expected):
    from lib.pdf_parser import text
    visited, closed, progress = [], [], []
    class Page:
        def __init__(self, index): self.index = index
        def get_text(self, *args, **kwargs): return f'page-evidence-{self.index}'
    class Document:
        def __len__(self): return 5001
        def __getitem__(self, index): visited.append(index); return Page(index)
        def close(self): closed.append('document')
    monkeypatch.setattr(text, 'pymupdf', SimpleNamespace(open=lambda **kwargs: Document()))
    monkeypatch.setenv('TOFU_PDF_MAX_PAGES', '1')
    rendered, method = text._extract_pdf_text_with_meta_without_admission(
        b'%PDF-' + b'x' * 40, max_chars=1000000, max_pages=requested, mode='fast',
        progress_callback=lambda done, total: progress.append((done, total)))
    assert visited == list(range(expected))
    assert f'page-evidence-{expected-1}' in rendered
    assert method == 'pymupdf-raw' and closed == ['document']
    assert progress[-1] == (expected, 5001)


def test_core_metadata_reports_all_pages_and_zero_system_quota(monkeypatch):
    from lib.pdf_parser import core
    closed = []
    class Document:
        def __len__(self): return 5001
        def close(self): closed.append('document')
    monkeypatch.setattr(core, 'pymupdf', SimpleNamespace(open=lambda **kwargs: Document()))
    def extracted(raw, max_chars, progress_callback, **kwargs):
        assert kwargs['max_pages'] == 0
        progress_callback(5001, 5001)
        return 'tail evidence', 'pymupdf'
    monkeypatch.setattr(core, '_extract_pdf_text_with_meta_without_admission', extracted)
    monkeypatch.setenv('TOFU_PDF_MAX_PAGES', '1')
    result = core._parse_pdf_without_admission(b'%PDF-' + b'x' * 40, max_images=0)
    assert result['processedPages'] == result['totalPages'] == 5001
    assert result['limits']['maxPages'] == 0 and result['truncated'] is False
    assert closed == ['document']


def test_rich_parser_visits_tail_beyond_retired_quota(monkeypatch):
    from lib.pdf_parser import text
    visited, closed = [], []
    class Document:
        def __len__(self): return 5001
        def close(self): closed.append(True)
    monkeypatch.setattr(text, 'pymupdf', SimpleNamespace(open=lambda **kwargs: Document()))
    monkeypatch.setattr(text, 'HAS_PYMUPDF4LLM', True)
    monkeypatch.setattr(text, '_classic_header_info', lambda doc: object())
    def page(doc, index, header, count):
        visited.append(index)
        return f'page-evidence-{index}', 0
    monkeypatch.setattr(text, '_extract_classic_page', page)
    monkeypatch.setenv('TOFU_PDF_MAX_PAGES', '1')
    rendered, method = text._extract_pdf_text_with_meta_without_admission(
        b'%PDF-' + b'x' * 40, max_chars=1000000, mode='rich')
    assert visited == list(range(5001))
    assert 'page-evidence-5000' in rendered
    assert method == 'pymupdf4llm' and closed == [True]
