"""lib/pdf_parser/text.py — Core text extraction from PDF.

Strategy 1: pymupdf4llm  → Markdown with table/header preservation
Strategy 2: pymupdf raw  → plain-text page-by-page fallback
"""

import re

try:
    import pymupdf
except ImportError:
    pymupdf = None  # type: ignore[assignment]
    # Warning already logged by _common.py — silent here to avoid duplicate noise

from lib.log import get_logger
from lib.pdf_parser._common import HAS_PYMUPDF4LLM, MAX_PDF_BYTES
from lib.pdf_parser.math import postprocess_math_blocks
from lib.pdf_parser.postprocess import cleanup_markdown, strip_manuscript_line_numbers

logger = get_logger(__name__)

__all__ = ['extract_pdf_text']


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 0, url: str = '') -> str:
    """Extract text from PDF as Markdown.

    Strategy 1: pymupdf4llm  → Markdown with table/header preservation
    Strategy 2: pymupdf raw  → plain-text page-by-page fallback

    Returns Markdown string, or an error message string.
    """
    if len(pdf_bytes) > MAX_PDF_BYTES:
        logger.warning('[PDF] File too large (%s MB, limit %s MB) — %s',
                       len(pdf_bytes) // (1024*1024), MAX_PDF_BYTES // (1024*1024),
                       url[:80])
        return f'[PDF too large: {len(pdf_bytes) // (1024*1024)} MB exceeds {MAX_PDF_BYTES // (1024*1024)} MB limit]'

    limit = max_chars if max_chars > 0 else 999_999_999

    # ── Strategy 1: pymupdf4llm ──
    if HAS_PYMUPDF4LLM:
        try:
            import pymupdf4llm
            md_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            try:
                n = len(md_doc)
                chunks = pymupdf4llm.to_markdown(
                    md_doc,
                    page_chunks=True,
                    show_progress=False,
                    table_strategy="lines",
                )
            finally:
                md_doc.close()

            parts = []
            total = 0
            for ci, chunk in enumerate(chunks):
                page_md = chunk.get('text', '') if isinstance(chunk, dict) else str(chunk)
                page_md = strip_manuscript_line_numbers(page_md)
                page_md = postprocess_math_blocks(page_md)
                page_md = cleanup_markdown(page_md)
                plen = len(page_md)
                if total + plen > limit:
                    remaining = limit - total
                    if remaining > 200:
                        parts.append(page_md[:remaining])
                    parts.append(f'\n[…truncated at {total + remaining:,} chars, '
                                 f'page {ci + 1}/{n}]')
                    total += remaining
                    break
                parts.append(page_md)
                total += plen

            text = '\n\n---\n\n'.join(parts)
            logger.debug('pymupdf4llm OK: %d pages, %s chars '
                         '(table_strategy=lines) — %s',
                         n, f'{total:,}', url[:60])
            return text

        except Exception as e:
            logger.warning('pymupdf4llm failed (%s), falling back to pymupdf raw', e, exc_info=True)

    # ── Strategy 2: pymupdf raw get_text ──
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            n = len(doc)
            parts = []
            total = 0
            for page in doc:
                raw = page.get_text()
                plen = len(raw)
                total += plen
                parts.append(raw)
                if limit < 999_999_999 and total > limit:
                    parts.append(f'\n[…truncated at {total:,} chars]')
                    break
        finally:
            doc.close()
        if not parts:
            return '[PDF: no extractable text]'
        full = re.sub(r'\n{3,}', '\n\n', '\n\n'.join(parts))
        logger.debug('get_text fallback OK: %d pages, %s chars — %s',
                     n, f'{total:,}', url[:60])
        return full
    except Exception as e:
        logger.warning('[PDF] get_text fallback extraction failed for %s: %s',
                       url[:80] if url else '?', e, exc_info=True)
        return f'[PDF extraction failed: {e}]'
