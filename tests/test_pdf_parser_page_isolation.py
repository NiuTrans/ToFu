"""Regression tests for resilient per-page PyMuPDF4LLM extraction."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _pdf_bytes(labels: list[str]) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for label in labels:
        page = doc.new_page()
        page.insert_text((72, 72), label)
    payload = doc.tobytes()
    doc.close()
    return payload


def test_header_inference_is_reused_and_strict_tables_are_requested(monkeypatch):
    import lib.pdf_parser.text as text

    marker = object()
    header_calls = []
    markdown_calls = []

    def fake_headers(doc):
        header_calls.append(len(doc))
        return marker

    def fake_markdown(doc, **kwargs):
        markdown_calls.append(kwargs)
        page = kwargs['pages'][0]
        return [{'text': f'## rich page {page + 1}'}]

    monkeypatch.setattr(text, 'HAS_PYMUPDF4LLM', True)
    monkeypatch.setattr(text, '_classic_header_info', fake_headers)
    monkeypatch.setattr(text, '_to_markdown_classic', fake_markdown)

    out, extractor = text.extract_pdf_text_with_meta(
        _pdf_bytes(['one', 'two', 'three']))

    assert extractor == 'pymupdf4llm'
    assert header_calls == [3], 'heading scan must be O(N), not once per page'
    assert len(markdown_calls) == 3
    assert all(call['hdr_info'] is marker for call in markdown_calls)
    assert all(call['table_strategy'] == 'lines_strict'
               for call in markdown_calls)
    assert 'rich page 1' in out and 'rich page 3' in out


def test_one_markdown_failure_falls_back_only_that_page(monkeypatch):
    import lib.pdf_parser.text as text

    def fake_markdown(doc, **kwargs):
        page = kwargs['pages'][0]
        if page == 1:
            raise ValueError('third-party page bug')
        return [{'text': f'RICH-{page + 1}'}]

    monkeypatch.setattr(text, 'HAS_PYMUPDF4LLM', True)
    monkeypatch.setattr(text, '_classic_header_info', lambda doc: False)
    monkeypatch.setattr(text, '_to_markdown_classic', fake_markdown)

    out, extractor = text.extract_pdf_text_with_meta(
        _pdf_bytes(['RAW-PAGE-1', 'RAW-PAGE-2', 'RAW-PAGE-3']))

    assert extractor == 'pymupdf4llm-partial'
    assert 'RICH-1' in out and 'RICH-3' in out
    assert 'RAW-PAGE-2' in out
    assert 'RAW-PAGE-1' not in out and 'RAW-PAGE-3' not in out, (
        'a single bad page downgraded good Markdown pages too')


def test_table_duplication_expansion_uses_bounded_raw_page(monkeypatch):
    import lib.pdf_parser.text as text

    monkeypatch.setattr(text, 'HAS_PYMUPDF4LLM', True)
    monkeypatch.setattr(text, '_classic_header_info', lambda doc: False)
    monkeypatch.setattr(
        text, '_to_markdown_classic',
        lambda doc, **kwargs: [{'text': 'DUPLICATED-TABLE|' * 3_000}],
    )

    out, extractor = text.extract_pdf_text_with_meta(
        _pdf_bytes(['compact raw table text']))

    assert extractor == 'pymupdf4llm-partial'
    assert 'compact raw table text' in out
    assert len(out) < text._MAX_PAGE_MARKDOWN_CHARS


def test_partial_extractor_version_never_equals_full_cache_key():
    from lib.pdf_parser._common import (current_parser_version,
                                        expected_parser_version)

    partial = current_parser_version('pymupdf4llm-partial')
    assert partial.startswith('pymupdf4llm-partial-')
    assert partial != expected_parser_version()
