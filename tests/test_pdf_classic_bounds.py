"""Page, text, and image amplification bounds for classic PDF extraction."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def _pdf_bytes(labels: list[str]) -> bytes:
    pymupdf = pytest.importorskip('pymupdf')
    document = pymupdf.open()
    for label in labels:
        page = document.new_page()
        page.insert_text((72, 72), label)
    payload = document.tobytes()
    document.close()
    return payload


def test_core_reports_page_truncation_and_clamps_image_request(monkeypatch):
    from lib.pdf_parser.core import parse_pdf

    monkeypatch.setenv('TOFU_PDF_MAX_PAGES', '2')
    monkeypatch.setenv('TOFU_PDF_MAX_TEXT_MIB', '1')
    result = parse_pdf(
        _pdf_bytes(['page one', 'page two', 'page three']),
        max_pages=999_999,
        max_images=999_999,
        max_image_width=999_999,
        text_mode='fast',
    )

    assert result['totalPages'] == 3
    assert result['processedPages'] == 2
    assert result['truncated'] is True
    assert '2 of 3 pages' in result['text']
    assert result['limits'] == {
        'maxPages': 2,
        'maxTextChars': 1024 * 1024,
        'maxImages': 64,
        'maxImageWidth': 2_048,
    }
    assert any('resource budget' in warning for warning in result['warnings'])


def test_rich_text_limit_is_strict_and_keeps_visible_evidence(monkeypatch):
    from lib.pdf_parser import text

    monkeypatch.setattr(text, 'HAS_PYMUPDF4LLM', True)
    monkeypatch.setattr(text, '_classic_header_info', lambda _doc: False)
    monkeypatch.setattr(
        text,
        '_to_markdown_classic',
        lambda _doc, **_kwargs: [{'text': 'x' * 10_000}],
    )

    rendered, extractor = text.extract_pdf_text_with_meta(
        _pdf_bytes(['short source']),
        max_chars=256,
    )

    assert extractor == 'pymupdf4llm'
    assert len(rendered) == 256
    assert 'resource budget' in rendered
