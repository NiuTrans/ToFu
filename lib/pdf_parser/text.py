"""lib/pdf_parser/text.py — Core text extraction from PDF.

Strategy 1: pymupdf4llm  → Markdown with table/header preservation
Strategy 2: pymupdf raw  → plain-text page-by-page fallback

Public extraction entry points reserve the process-wide classic PDF budget.
The full-document core uses the explicitly named already-admitted private
entry so one document is counted exactly once in both server and child paths.
"""

import re

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf  # PyMuPDF <1.24.3 legacy module name
    except ImportError:
        pymupdf = None  # type: ignore[assignment]
        # Warning already logged by _common.py — silent here to avoid duplicate noise

from lib.log import get_logger
from lib.pdf_parser._common import HAS_PYMUPDF4LLM, MAX_PDF_BYTES, PYMUPDF_LOCK
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
from lib.pdf_parser.math import postprocess_math_blocks
from lib.pdf_parser.policy import (
    bounded_pdf_pages,
    bounded_pdf_text_chars,
    resolve_classic_pdf_budget,
)
from lib.pdf_parser.postprocess import cleanup_markdown, strip_manuscript_line_numbers

logger = get_logger(__name__)

__all__ = ['extract_pdf_text', 'extract_pdf_text_with_meta', 'validate_pdf_bytes']


def validate_pdf_bytes(pdf_bytes):
    """Check that ``pdf_bytes`` is a genuinely openable PDF with >= 1 page.

    This is the ingest-time gate that stops a truncated / aborted / empty upload
    (e.g. a 15-byte ``%PDF-1.4`` header-only stub) from being committed to disk
    and seeded into the bookshelf as a permanent non-viewable ghost. A file is
    "real" only when pymupdf can OPEN it AND it reports at least one page —
    exactly the precondition text/image extraction needs, so gating on validity
    is equivalent to gating on recoverability.

    Args:
        pdf_bytes: the raw bytes to validate.

    Returns:
        (ok, page_count, error): ``ok`` True only for an openable, non-empty
        PDF; ``page_count`` the page count (0 when invalid); ``error`` a short
        human-readable reason when invalid, else ''. Never raises.
    """
    if not pdf_bytes or len(pdf_bytes) < 32:
        return False, 0, 'empty or truncated file (%d bytes)' % (len(pdf_bytes or b''),)
    if pymupdf is None:
        # Parser unavailable — cannot validate. Fail OPEN (treat as valid) so a
        # deployment without pymupdf is not blocked from ingesting; the reader's
        # own recovery path still surfaces any downstream parse failure.
        logger.debug('[PDF] validate_pdf_bytes: pymupdf unavailable, skipping validation')
        return True, 0, ''
    doc = None
    try:
        with PYMUPDF_LOCK:
            doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
            pages = doc.page_count
        if pages < 1:
            return False, 0, 'PDF has no pages'
        return True, pages, ''
    except Exception as e:
        logger.debug('[PDF] validate_pdf_bytes: open/validate failed: %s', e)
        return False, 0, '%s: %s' % (type(e).__name__, e)
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception as e:
                logger.debug('[PDF] validate_pdf_bytes: doc.close failed: %s', e)


def _safe_progress(cb, page: int, total: int) -> None:
    """Invoke a progress callback without ever letting its exceptions propagate."""
    if cb is None:
        return
    try:
        cb(page, total)
    except Exception as e:
        logger.debug('[PDF] progress_callback raised (ignored): %s', e)


# pymupdf4llm ≥1.26 flips its module-global default to the NEW layout/OCR
# pipeline whenever the optional ``pymupdf.layout`` package is importable
# (import-time ``use_layout(True)``). That pipeline needs ONNX models and a
# compatible RapidOCR, crashes on born-digital PDFs in our env
# (``'RapidOCR' object has no attribute 'text_detector'``), and silently
# swallows the kwargs this module relies on (``table_strategy`` /
# ``page_chunks`` / ``show_progress`` fall into ``**kwargs``). The CLASSIC
# implementation — the one that honors those kwargs — lives on at
# ``pymupdf4llm.helpers.pymupdf_rag``. Call it directly so the markdown
# contract here does not depend on a process-global flag keyed off whether
# an optional package happens to be installed. Measured on arXiv 1706.03762
# with the layout package PRESENT: classic → 40,608 chars with 35 table
# rows; layout-routed top-level call → crash → raw fallback with 0.
_pymupdf_rag = None
_pymupdf_rag_tried = False

# A single born-digital page should not plausibly expand to tens or hundreds
# of thousands of Markdown characters.  PyMuPDF4LLM's permissive ``lines``
# table strategy can duplicate a wide table cell once per detected column
# (measured: a 6,070-char arXiv page became 250,662 chars).  ``lines_strict``
# fixes that upstream failure shape; this ratio guard is the last-resort fuse
# for future extractor regressions.
_MAX_PAGE_MARKDOWN_CHARS = 32_000
_MAX_PAGE_MARKDOWN_TO_RAW_RATIO = 8


def _truncate_with_marker(text: str, limit: int, marker: str) -> str:
    """Keep user-visible truncation evidence inside the text budget."""
    bounded_limit = max(1, int(limit))
    rendered_marker = f'\n[{marker}]'
    if len(rendered_marker) >= bounded_limit:
        return rendered_marker[:bounded_limit]
    return text[:bounded_limit - len(rendered_marker)] + rendered_marker


def _load_pymupdf_rag():
    """Load the classic implementation once without importing layout mode."""
    global _pymupdf_rag, _pymupdf_rag_tried
    if not _pymupdf_rag_tried:
        _pymupdf_rag_tried = True
        try:
            # Upstream prints a layout-backend promotion on import even when
            # Tofu intentionally selected the classic path. Avoid confusing a
            # successful parse/install with an action item for the user.
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                from pymupdf4llm.helpers import pymupdf_rag as _rag
            _pymupdf_rag = _rag
        except Exception as e:
            logger.debug('[PDF] pymupdf_rag direct import unavailable, '
                         'falling back to top-level to_markdown: %s', e)
    return _pymupdf_rag


def _to_markdown_classic(md_doc, **kw):
    """``pymupdf4llm.to_markdown`` pinned to the classic rag implementation."""
    rag = _load_pymupdf_rag()
    if rag is not None:
        return rag.to_markdown(md_doc, **kw)
    import pymupdf4llm
    return pymupdf4llm.to_markdown(md_doc, **kw)


def _classic_header_info(md_doc):
    """Infer headings once per document; degrade headings, never the document.

    The classic helper normally constructs ``IdentifyHeaders(doc)`` inside
    every ``to_markdown(pages=[i])`` call.  Our honest per-page progress loop
    therefore scanned an N-page document N times (quadratic work), and an
    upstream header-inference ``ValueError: min() iterable argument is empty``
    sent the *whole* paper to raw fallback.  Reuse one inference object.  If
    that optional heuristic fails, ``False`` tells PyMuPDF4LLM to keep its
    Markdown/table path while merely disabling inferred headings.
    """
    rag = _load_pymupdf_rag()
    if rag is None or not hasattr(rag, 'IdentifyHeaders'):
        return False
    try:
        return rag.IdentifyHeaders(md_doc)
    except Exception as e:
        logger.warning('[PDF] heading inference failed (%s); continuing with '
                       'Markdown extraction and headings disabled', e,
                       exc_info=True)
        return False


def _raw_page_text(md_doc, page_index: int) -> str:
    """Best-effort local fallback for one page of an already-open document."""
    try:
        return md_doc[page_index].get_text() or ''
    except Exception as e:
        logger.warning('[PDF] raw fallback failed for page %d: %s',
                       page_index + 1, e, exc_info=True)
        return ''


def extract_pdf_text(
    pdf_bytes: bytes,
    max_chars: int = 0,
    url: str = '',
    progress_callback=None,
    mode: str = 'rich',
    max_pages: int = 0,
) -> str:
    """Back-compat wrapper returning only the text. See
    :func:`extract_pdf_text_with_meta`."""
    return extract_pdf_text_with_meta(
        pdf_bytes, max_chars=max_chars, url=url,
        progress_callback=progress_callback, mode=mode,
        max_pages=max_pages)[0]


def extract_pdf_text_with_meta(
    pdf_bytes: bytes,
    max_chars: int = 0,
    url: str = '',
    progress_callback=None,
    mode: str = 'rich',
    max_pages: int = 0,
):
    """Admit one public text extraction into the classic PDF budget."""
    budget = resolve_classic_pdf_budget()
    lease = CLASSIC_PDF_ADMISSION.reserve(budget.unfinished_capacity)
    try:
        return _extract_pdf_text_with_meta_without_admission(
            pdf_bytes,
            max_chars=max_chars,
            url=url,
            progress_callback=progress_callback,
            mode=mode,
            max_pages=max_pages,
        )
    finally:
        lease.release()


def _extract_pdf_text_with_meta_without_admission(
    pdf_bytes: bytes,
    max_chars: int = 0,
    url: str = '',
    progress_callback=None,
    mode: str = 'rich',
    max_pages: int = 0,
):
    """Extract admitted PDF text and report WHICH strategy won.

    Strategy 0: docling           → Layout-aware (TableFormer + math model);
                                    only when ``mode='structured'`` AND
                                    the optional ``docling`` package is
                                    installed. Falls through to Strategy 1
                                    on any failure.
    Strategy 1: pymupdf4llm       → Markdown with table/header preservation
    Strategy 2: pymupdf raw       → plain-text page-by-page fallback

    Args:
        pdf_bytes: Raw PDF file bytes.
        max_chars: Soft output ceiling. Zero uses the launch-derived default;
            positive values may lower but never raise that process budget.
        max_pages: Page CPU ceiling with the same default/lower-only contract.
        url: Optional URL for log context.
        progress_callback: Optional ``Callable[[int, int], None]`` invoked with
            ``(pages_done, total_pages)`` after each page is processed. Lets
            long-running parses stream real progress to the UI. Exceptions
            raised by the callback are logged at DEBUG level and swallowed.
            NOTE: Docling (``mode='structured'``) does not expose mid-call
            per-page progress; only start (0/N) and end (N/N) ticks fire.
        mode: ``'rich'`` (default) → use pymupdf4llm with
            table_strategy='lines_strict'
            for full Markdown preservation (tables, headers, math). Best
            quality / latency tradeoff with no extra deps.
            ``'structured'`` → try docling first (best for borderless tables
            and math formulas on academic PDFs), then fall back to pymupdf4llm
            if docling is unavailable or fails. Opt-in heavy dep (~2 GB).
            ``'fast'`` → skip pymupdf4llm entirely, use raw pymupdf
            ``page.get_text()`` directly. ≈50× faster (~0.05s/page) but loses
            Markdown structure. Use for web_search/fetch_url callers that only
            need plain text for BM25 ranking or short snippets.

    Returns Markdown string (rich/structured) or plain text (fast), or an
    error message string.
    """
    if len(pdf_bytes) > MAX_PDF_BYTES:
        logger.warning('[PDF] File too large (%s MB, limit %s MB) — %s',
                       len(pdf_bytes) // (1024*1024), MAX_PDF_BYTES // (1024*1024),
                       url[:80])
        return f'[PDF too large: {len(pdf_bytes) // (1024*1024)} MB exceeds {MAX_PDF_BYTES // (1024*1024)} MB limit]', 'error'

    budget = resolve_classic_pdf_budget()
    limit = bounded_pdf_text_chars(max_chars, budget)
    page_limit = bounded_pdf_pages(max_pages, budget)

    # ── Strategy 0: Docling layout-aware pipeline (opt-in) ──
    if mode == 'structured':
        # Docling converts the whole document in one native call and cannot
        # accept a page subset. Oversized documents therefore use the bounded
        # per-page rich path instead of doing unobservable work past the cap.
        try:
            with PYMUPDF_LOCK:
                page_probe = pymupdf.open(stream=pdf_bytes, filetype='pdf')
                try:
                    structured_pages = len(page_probe)
                finally:
                    page_probe.close()
            if structured_pages > page_limit:
                logger.warning(
                    '[PDF] structured mode skipped: %d pages exceeds the '
                    '%d-page classic extraction budget',
                    structured_pages,
                    page_limit,
                )
                mode = 'rich'
        except Exception as exc:
            logger.debug(
                '[PDF] structured page preflight failed; parser will '
                'classify the document: %s', exc)
    if mode == 'structured':
        try:
            from lib.pdf_parser.docling import extract_pdf_text_docling
            md = extract_pdf_text_docling(
                pdf_bytes,
                max_chars=limit,
                url=url,
                progress_callback=progress_callback,
            )
            if md is not None:
                # Run the same math-block + cleanup pass we apply to
                # pymupdf4llm output, so downstream consumers see a
                # consistent shape regardless of which strategy ran.
                md = postprocess_math_blocks(md)
                md = cleanup_markdown(md)
                if len(md) > limit:
                    md = _truncate_with_marker(
                        md,
                        limit,
                        f'PDF text truncated at {limit:,} characters by the '
                        'resource budget',
                    )
                return md, 'docling'
            logger.info("[PDF] structured mode: docling unavailable/failed, "
                        "falling back to pymupdf4llm — %s", url[:60])
            # fall through to Strategy 1
        except Exception as e:
            logger.warning('[PDF] structured mode: unexpected error %s '
                           '(falling back to pymupdf4llm)', e, exc_info=True)

    # ── Fast mode: jump straight to Strategy 2 (raw get_text) ──
    # Skips pymupdf4llm + table_strategy='lines_strict' entirely. Used by
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
            with PYMUPDF_LOCK:
                md_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
                try:
                    n = len(md_doc)
                    pages_to_process = min(n, page_limit)
                    _safe_progress(progress_callback, 0, n)
                    parts = []
                    total = 0
                    char_truncated = False
                    page_fallbacks = 0
                    # Reusing this object changes header scanning from O(N²)
                    # to O(N) and isolates the upstream empty-minimum bug.
                    header_info = _classic_header_info(md_doc)
                    for pi in range(pages_to_process):
                        page_md = ''
                        try:
                            chunks = _to_markdown_classic(
                                md_doc,
                                pages=[pi],
                                page_chunks=True,
                                show_progress=False,
                                table_strategy="lines_strict",
                                hdr_info=header_info,
                            )
                            if chunks:
                                c0 = chunks[0]
                                page_md = (c0.get('text', '')
                                           if isinstance(c0, dict) else str(c0))
                        except Exception as e:
                            # One malformed page must not downgrade every good
                            # page in the paper to flat raw text.
                            logger.warning('[PDF] pymupdf4llm page %d/%d failed '
                                           '(%s); using raw text for this page',
                                           pi + 1, n, e, exc_info=True)
                            page_md = _raw_page_text(md_doc, pi)
                            page_fallbacks += 1

                        # Protect the corpus from table-cell multiplication.
                        # Only replace when BOTH an absolute and relative bound
                        # are exceeded, so genuinely dense pages remain intact.
                        if len(page_md) > _MAX_PAGE_MARKDOWN_CHARS:
                            raw_page = _raw_page_text(md_doc, pi)
                            raw_len = len(raw_page)
                            if raw_page and len(page_md) > max(
                                    _MAX_PAGE_MARKDOWN_CHARS,
                                    raw_len * _MAX_PAGE_MARKDOWN_TO_RAW_RATIO):
                                logger.warning('[PDF] pymupdf4llm page %d/%d '
                                               'expanded %d raw chars to %d '
                                               'Markdown chars; using raw page '
                                               'to prevent table duplication',
                                               pi + 1, n, raw_len, len(page_md))
                                page_md = raw_page
                                page_fallbacks += 1
                        page_md = strip_manuscript_line_numbers(page_md)
                        page_md = postprocess_math_blocks(page_md)
                        page_md = cleanup_markdown(page_md)
                        plen = len(page_md)
                        if total + plen > limit:
                            remaining = max(0, limit - total)
                            if remaining:
                                parts.append(page_md[:remaining])
                            total += remaining
                            char_truncated = True
                            _safe_progress(progress_callback, pi + 1, n)
                            break
                        parts.append(page_md)
                        total += plen
                        _safe_progress(progress_callback, pi + 1, n)
                finally:
                    md_doc.close()

            text = '\n\n---\n\n'.join(parts)
            if len(text) > limit:
                char_truncated = True
            if char_truncated:
                text = _truncate_with_marker(
                    text,
                    limit,
                    f'PDF text truncated at {limit:,} characters by the '
                    'resource budget',
                )
            elif pages_to_process < n:
                text = _truncate_with_marker(
                    text,
                    limit,
                    f'PDF truncated after {pages_to_process:,} of {n:,} '
                    'pages by the resource budget',
                )
            logger.debug('pymupdf4llm OK: %d/%d pages, %s chars '
                         '(table_strategy=lines_strict, per-page, '
                         'char_truncated=%s, page_fallbacks=%d) — %s',
                         pages_to_process, n, f'{len(text):,}',
                         char_truncated, page_fallbacks, url[:60])
            extractor = ('pymupdf4llm-partial' if page_fallbacks
                         else 'pymupdf4llm')
            return text, extractor

        except Exception as e:
            logger.warning('pymupdf4llm failed (%s), falling back to pymupdf raw', e, exc_info=True)

    # ── Strategy 2: pymupdf raw get_text ──
    try:
        with PYMUPDF_LOCK:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            try:
                n = len(doc)
                pages_to_process = min(n, page_limit)
                _safe_progress(progress_callback, 0, n)
                parts = []
                total = 0
                char_truncated = False
                for pi in range(pages_to_process):
                    page = doc[pi]
                    raw = page.get_text()
                    plen = len(raw)
                    if total + plen > limit:
                        remaining = max(0, limit - total)
                        if remaining:
                            parts.append(raw[:remaining])
                        total += remaining
                        char_truncated = True
                    else:
                        total += plen
                        parts.append(raw)
                    _safe_progress(progress_callback, pi + 1, n)
                    if char_truncated:
                        break
            finally:
                doc.close()
        if not parts:
            return '[PDF: no extractable text]', 'error'
        full = re.sub(r'\n{3,}', '\n\n', '\n\n'.join(parts))
        if len(full) > limit:
            char_truncated = True
        if char_truncated:
            full = _truncate_with_marker(
                full,
                limit,
                f'PDF text truncated at {limit:,} characters by the '
                'resource budget',
            )
        elif pages_to_process < n:
            full = _truncate_with_marker(
                full,
                limit,
                f'PDF truncated after {pages_to_process:,} of {n:,} pages '
                'by the resource budget',
            )
        logger.debug('get_text fallback OK: %d/%d pages, %s chars — %s',
                     pages_to_process, n, f'{len(full):,}', url[:60])
        return full, 'pymupdf-raw'
    except Exception as e:
        logger.warning('[PDF] get_text fallback extraction failed for %s: %s',
                       url[:80] if url else '?', e, exc_info=True)
        return f'[PDF extraction failed: {e}]', 'error'
