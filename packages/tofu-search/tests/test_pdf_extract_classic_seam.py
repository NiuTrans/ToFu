"""Regression: pdf_extract rides the CLASSIC pymupdf4llm seam (pymupdf_rag).

WHY
---
2026-08-01: production error.log showed ×21/day
``pymupdf4llm failed, falling back to raw: 'RapidOCR' object has no
attribute 'text_detector'``. Root cause chain:

  * pymupdf4llm ≥1.26 routes the top-level ``to_markdown`` through its
    NEW layout/OCR pipeline whenever ``pymupdf.layout`` is importable.
  * That pipeline's OCR adapters (rapidtess/paddletess ``exec_ocr``)
    call ``RapidOCR.text_detector`` — an attribute that only existed on
    rapidocr-onnxruntime ≤1.2. Modern 1.3.x AND 1.4.x name it
    ``text_det``. (Verified offline: 1.3.24 and 1.4.4 wheels both use
    ``text_det``; upstream pymupdf4llm 1.28.0 still calls
    ``text_detector`` behind a louder RuntimeError, so upgrading is not
    a fix either.)
  * Any page whose layout analysis votes needs_ocr (bad chars, or
    scan-like images with high variance/edge energy) crashes the WHOLE
    document into the raw fallback — rich markdown (tables, headers)
    is lost on exactly the academic PDFs that have figures.

Fix: call ``pymupdf4llm.helpers.pymupdf_rag.to_markdown`` directly —
the classic implementation that honors page_chunks/table_strategy and
never touches the OCR pipeline. Same seam chatui's lib/pdf_parser has
used in production. Verified on arXiv 1706.03762: top-level → crash →
39,512 raw chars; classic seam → 40,608 chars with 35 table rows.

The tests below are hermetic: the needs_ocr trigger is a synthetic PDF
whose noise image has the variance/edge signature analyze_page() looks
for (verified pre-fix to raise the exact production AttributeError when
fed to the top-level call).
"""

import logging
import random

import pytest

from tofu_search.fetch import pdf_extract
from tofu_search.fetch.pdf_extract import extract_pdf_text


def _needs_ocr_trigger_pdf() -> bytes:
    """A 2-page PDF whose second page carries a noise image.

    The noise image's variance/edge energy is what pymupdf4llm's
    analyze_page() keys on to vote needs_ocr=True — which, pre-fix,
    drove the layout pipeline into the broken ``text_detector`` call.
    """
    pymupdf = pytest.importorskip('pymupdf')
    random.seed(42)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), 'Cover text marker', fontsize=12)
    page2 = doc.new_page()
    noise = bytes(random.randrange(256) for _ in range(300 * 200 * 3))
    pix = pymupdf.Pixmap(pymupdf.csRGB, 300, 200, noise, False)
    page2.insert_image(pymupdf.Rect(72, 72, 372, 272), pixmap=pix)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_needs_ocr_page_keeps_rich_markdown(caplog):
    """The production shape: a needs_ocr page must NOT degrade the doc.

    Pre-fix, this exact PDF shape crashed the layout pipeline
    (AttributeError: 'RapidOCR' object has no attribute 'text_detector')
    and the whole document fell back to raw get_text. Post-fix the
    classic seam extracts the legible text with no fallback warning.
    """
    pytest.importorskip('pymupdf4llm')
    with caplog.at_level(logging.WARNING, logger='tofu_search.fetch.pdf_extract'):
        out = extract_pdf_text(_needs_ocr_trigger_pdf(), url='synthetic-needs-ocr')
    assert 'Cover text marker' in out
    assert not any('falling back to raw' in r.message for r in caplog.records), (
        'the needs_ocr page degraded the document to the raw fallback — '
        'the classic-seam fix regressed')


def test_extract_rides_classic_rag_seam(monkeypatch):
    """extract_pdf_text must call pymupdf_rag.to_markdown, never top-level."""
    pytest.importorskip('pymupdf')
    calls = []

    class _FakeRag:
        @staticmethod
        def to_markdown(md_doc, **kw):
            calls.append(kw)
            return [{'text': 'fake classic markdown'}]

    def _boom(*a, **kw):  # top-level call = regression
        raise AssertionError('top-level pymupdf4llm.to_markdown was called')

    monkeypatch.setattr(pdf_extract, '_pymupdf_rag', _FakeRag)
    monkeypatch.setattr(pdf_extract, '_pymupdf_rag_tried', True)
    monkeypatch.setattr(pdf_extract.pymupdf4llm, 'to_markdown', _boom)

    out = extract_pdf_text(_needs_ocr_trigger_pdf(), url='seam-check')
    assert 'fake classic markdown' in out
    assert calls and calls[0].get('table_strategy') == 'lines', (
        'the classic seam must receive the table_strategy kwarg the '
        'layout pipeline used to swallow')
    assert calls[0].get('page_chunks') is True


def test_source_pin_classic_seam():
    """Cheap drift pin: the module keeps the pymupdf_rag seam wiring."""
    import inspect
    src = inspect.getsource(pdf_extract)
    assert 'from pymupdf4llm.helpers import pymupdf_rag' in src
    assert '_to_markdown_classic(' in src


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
