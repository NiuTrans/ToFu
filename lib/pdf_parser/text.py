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


def _safe_progress(cb, page: int, total: int) -> None:
    """Invoke a progress callback without ever letting its exceptions propagate."""
    if cb is None:
        return
    try:
        cb(page, total)
    except Exception as e:
        logger.debug('[PDF] progress_callback raised (ignored): %s', e)


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 0, url: str = '',
                     progress_callback=None, mode: str = 'rich') -> str:
    """Extract text from PDF as Markdown.

    Strategy 1: pymupdf4llm  → Markdown with table/header preservation
    Strategy 2: pymupdf raw  → plain-text page-by-page fallback

    Args:
        pdf_bytes: Raw PDF file bytes.
        max_chars: Soft upper bound on total output length (0 = no limit).
        url: Optional URL for log context.
        progress_callback: Optional ``Callable[[int, int], None]`` invoked with
            ``(pages_done, total_pages)`` after each page is processed. Lets
            long-running parses stream real progress to the UI. Exceptions
            raised by the callback are logged at DEBUG level and swallowed.
        mode: ``'rich'`` (default) → use pymupdf4llm with table_strategy='lines'
            for full Markdown preservation (tables, headers, math). Best
            quality but 1-3s/page on dense academic PDFs.
            ``'fast'`` → skip pymupdf4llm entirely, use raw pymupdf
            ``page.get_text()`` directly. ≈50× faster (~0.05s/page) but loses
            Markdown structure. Use for web_search/fetch_url callers that only
            need plain text for BM25 ranking or short snippets.

    Returns Markdown string (rich mode) or plain text (fast mode), or an
    error message string.
    """
    if len(pdf_bytes) > MAX_PDF_BYTES:
        logger.warning('[PDF] File too large (%s MB, limit %s MB) — %s',
                       len(pdf_bytes) // (1024*1024), MAX_PDF_BYTES // (1024*1024),
                       url[:80])
        return f'[PDF too large: {len(pdf_bytes) // (1024*1024)} MB exceeds {MAX_PDF_BYTES // (1024*1024)} MB limit]'

    limit = max_chars if max_chars > 0 else 999_999_999

    # ── Fast mode: jump straight to Strategy 2 (raw get_text) ──
    # Skips pymupdf4llm + table_strategy='lines' entirely. Used by
    # web_search and fetch_url callers that only need plain text for
    # BM25 ranking / snippet display. ≈50× faster on academic PDFs.
    if mode == 'fast':
        logger.debug('[PDF] fast mode (raw get_text) — %s', url[:60])
        # Fall through to Strategy 2 below by skipping the pymupdf4llm block.
        # The pymupdf4llm `if` branch is guarded by `HAS_PYMUPDF4LLM and mode != 'fast'`.

    # ── Strategy 1: pymupdf4llm, page-by-page for real progress ──
    # We iterate one page at a time (pages=[i]) rather than calling to_markdown
    # in bulk. This adds ~5-10% overhead vs a bulk call, but it's the only way
    # pymupdf4llm exposes per-page completion. The bulk form is a single
    # blocking call that leaves the UI stuck with no feedback for 10-60s on
    # larger papers. Cross-page tables may split at page boundaries — an
    # acceptable tradeoff for honest progress reporting.
    if HAS_PYMUPDF4LLM and mode != 'fast':
        try:
            import pymupdf4llm
            md_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            try:
                n = len(md_doc)
                _safe_progress(progress_callback, 0, n)
                parts = []
                total = 0
                truncated = False
                for pi in range(n):
                    chunks = pymupdf4llm.to_markdown(
                        md_doc,
                        pages=[pi],
                        page_chunks=True,
                        show_progress=False,
                        table_strategy="lines",
                    )
                    page_md = ''
                    if chunks:
                        c0 = chunks[0]
                        page_md = c0.get('text', '') if isinstance(c0, dict) else str(c0)
                    page_md = strip_manuscript_line_numbers(page_md)
                    page_md = postprocess_math_blocks(page_md)
                    page_md = cleanup_markdown(page_md)
                    plen = len(page_md)
                    if total + plen > limit:
                        remaining = limit - total
                        if remaining > 200:
                            parts.append(page_md[:remaining])
                        parts.append(f'\n[…truncated at {total + remaining:,} chars, '
                                     f'page {pi + 1}/{n}]')
                        total += remaining
                        truncated = True
                        _safe_progress(progress_callback, pi + 1, n)
                        break
                    parts.append(page_md)
                    total += plen
                    _safe_progress(progress_callback, pi + 1, n)
            finally:
                md_doc.close()

            text = '\n\n---\n\n'.join(parts)
            logger.debug('pymupdf4llm OK: %d pages, %s chars '
                         '(table_strategy=lines, per-page, truncated=%s) — %s',
                         n, f'{total:,}', truncated, url[:60])
            return text

        except Exception as e:
            logger.warning('pymupdf4llm failed (%s), falling back to pymupdf raw', e, exc_info=True)

    # ── Strategy 2: pymupdf raw get_text ──
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            n = len(doc)
            _safe_progress(progress_callback, 0, n)
            parts = []
            total = 0
            for pi, page in enumerate(doc):
                raw = page.get_text()
                plen = len(raw)
                total += plen
                parts.append(raw)
                _safe_progress(progress_callback, pi + 1, n)
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
