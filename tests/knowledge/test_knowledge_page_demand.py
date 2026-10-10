"""Knowledge traversal retains late-page evidence despite retired local quotas."""
import base64
import hashlib
import importlib
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_visual_traversal_keeps_2049_pages_despite_all_old_page_knobs(monkeypatch):
    from lib.knowledge import assets
    from lib.pdf_parser import images
    visited, closed = [], []
    class Page:
        def __init__(self, index): self.index = index
        def get_text(self, mode): return 'page context ' * 20
        def get_images(self, **kwargs): return []
    class Document:
        page_count = 2049
        def __getitem__(self, index):
            visited.append(index)
            return Page(index)
        def close(self): closed.append('document')
    for name in ('TOFU_KNOWLEDGE_VISUAL_MAX_PAGES', 'TOFU_KNOWLEDGE_OCR_MAX_PAGES',
                 'TOFU_PDF_MAX_PAGES'):
        monkeypatch.setenv(name, '1')
    monkeypatch.setitem(sys.modules, 'pymupdf', SimpleNamespace(open=lambda **kwargs: Document()))
    monkeypatch.setattr(images, 'detect_and_clip_figures', lambda page, *args, **kwargs: [{
        'base64': base64.b64encode(page.index.to_bytes(4, 'big')).decode(),
        'page': page.index + 1, 'caption': f'page {page.index + 1}'}])
    def candidate(raw, _budget=None, **values):
        return dict(raw=raw, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw),
                    mime_type='image/png', suffix='.png', **values)
    monkeypatch.setattr(assets, '_asset_from_raw', candidate)
    result, warnings = assets._extract_pdf_assets_without_admission(b'synthetic')
    try:
        assert visited == list(range(2049)) and len(result) == 2049
        assert result[-1]['caption'] == 'page 2049'
        assert result[-1]['raw'] == (2048).to_bytes(4, 'big')
        assert warnings == [] and closed == ['document']
    finally:
        stream = result._stream
        result.close()
    assert stream.closed


def test_ocr_reads_tail_page_when_no_caller_page_range_is_requested(monkeypatch):
    from lib.knowledge import ingest
    from lib.pdf_parser import (
        images,  # initialize native dependency before substituting
    )
    visited, closed = [], []
    class Page:
        def __init__(self, index): self.index = index
        def get_textpage_ocr(self, **kwargs): return object()
        def get_text(self, *args, **kwargs): return f'late evidence {self.index}'
    class Document:
        page_count = 513
        def __getitem__(self, index):
            visited.append(index)
            return Page(index)
        def close(self): closed.append('document')
    monkeypatch.setitem(sys.modules, 'pymupdf', SimpleNamespace(open=lambda **kwargs: Document()))
    monkeypatch.setenv('TOFU_KNOWLEDGE_OCR_MAX_PAGES', '1')
    text, warnings = ingest._ocr_scanned_pdf(b'synthetic', page_limit=None, max_chars=100000)
    assert visited == list(range(513))
    assert '## Page 513' in text and 'late evidence 512' in text
    assert warnings == [] and closed == ['document']


def test_explicit_ocr_page_range_still_controls_output(monkeypatch):
    from lib.knowledge import ingest
    visited = []
    class Page:
        def get_textpage_ocr(self, **kwargs): return object()
        def get_text(self, *args, **kwargs): return 'retained'
    class Document:
        page_count = 10
        def __getitem__(self, index): visited.append(index); return Page()
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'pymupdf', SimpleNamespace(open=lambda **kwargs: Document()))
    text, warnings = ingest._ocr_scanned_pdf(b'synthetic', page_limit=2, max_chars=10000)
    assert visited == [0, 1] and '## Page 2' in text and '## Page 3' not in text
    assert any('OCR read 2 of 10' in message for message in warnings)
