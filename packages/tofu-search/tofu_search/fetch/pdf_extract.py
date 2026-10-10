"""tofu_search.fetch.pdf_extract — PDF text extraction.

Uses pymupdf4llm (preferred) or pymupdf raw text as fallback.
"""

import re
import threading

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['extract_pdf_text']

MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB

_pymupdf = None
_pymupdf4llm = None
_dependencies_loaded = False
_dependency_lock = threading.Lock()


def _load_pdf_dependencies():
    """Load optional native/layout libraries once, only for PDF capability use."""
    global _pymupdf, _pymupdf4llm, _dependencies_loaded
    if _dependencies_loaded:
        return
    with _dependency_lock:
        if _dependencies_loaded:
            return
        try:
            import pymupdf as core
            _pymupdf = core
        except ImportError as exc:
            logger.debug('PDF core unavailable: %s', exc)
        try:
            import pymupdf4llm as rich
            _pymupdf4llm = rich
        except ImportError as exc:
            logger.debug('Rich PDF extraction unavailable: %s', exc)
        _dependencies_loaded = True


def __getattr__(name):
    # Preserve explicit availability/module queries without making an ordinary
    # web-search import activate PDF layout/OCR models and native thread pools.
    if name not in ('pymupdf', 'pymupdf4llm', 'HAS_PYMUPDF', 'HAS_PYMUPDF4LLM'):
        raise AttributeError(name)
    _load_pdf_dependencies()
    return {'pymupdf': _pymupdf, 'pymupdf4llm': _pymupdf4llm,
            'HAS_PYMUPDF': _pymupdf is not None,
            'HAS_PYMUPDF4LLM': _pymupdf4llm is not None}[name]

_pymupdf_rag = None
_pymupdf_rag_tried = False


def _to_markdown_classic(md_doc, **kw):
    """``pymupdf4llm.to_markdown`` pinned to the classic rag implementation.

    pymupdf4llm ≥1.26 routes the top-level call through its NEW layout/OCR
    pipeline whenever the optional ``pymupdf.layout`` package is importable
    (import-time ``use_layout(True)``). That pipeline's OCR adapters call
    ``RapidOCR.text_detector`` — an attribute that only existed on
    rapidocr-onnxruntime ≤1.2; modern versions (1.3.x AND 1.4.x) name it
    ``text_det``. Every page whose layout analysis votes needs_ocr (bad
    chars / structured scan-like images) therefore crashes with
    ``'RapidOCR' object has no attribute 'text_detector'`` and the WHOLE
    document degrades to the raw fallback — measured ×21/day in production
    (2026-08-01) and reproduced on arXiv 1706.03762 (top-level call →
    crash at rapidtess_api.py:189 → 39.5k chars raw; classic seam →
    ~40.6k chars rich markdown). Upstream 1.28.0 keeps the same broken
    call behind a louder RuntimeError, so upgrading is NOT a fix. The
    classic implementation — the one honoring ``page_chunks`` /
    ``table_strategy`` / ``show_progress`` — lives on at
    ``pymupdf4llm.helpers.pymupdf_rag``. Same seam chatui's
    lib/pdf_parser/text.py has used in production.
    """
    global _pymupdf_rag, _pymupdf_rag_tried
    if not _pymupdf_rag_tried:
        _pymupdf_rag_tried = True
        try:
            from pymupdf4llm.helpers import pymupdf_rag as _rag
            _pymupdf_rag = _rag
        except Exception as e:
            logger.debug('pymupdf_rag direct import unavailable, '
                         'falling back to top-level to_markdown: %s', e)
    if _pymupdf_rag is not None:
        return _pymupdf_rag.to_markdown(md_doc, **kw)
    return _pymupdf4llm.to_markdown(md_doc, **kw)


def _strip_manuscript_line_numbers(text):
    """Remove line numbers commonly found in review/manuscript PDFs."""
    lines = text.split('\n')
    non_blank = [l for l in lines if l.strip()]
    if len(non_blank) < 10:
        return text

    standalone_num = re.compile(r'^\s*\d{1,5}\s*$')
    num_count = sum(1 for l in non_blank if standalone_num.match(l))
    ratio = num_count / len(non_blank)

    if ratio > 0.15:
        cleaned = [l for l in lines if not standalone_num.match(l)]
        return '\n'.join(cleaned)

    leading_num = re.compile(r'^(\d{1,5})([ \t]{2,})(.*)')
    matches = [leading_num.match(l) for l in non_blank]
    leading_count = sum(1 for m in matches if m and len(m.group(3).strip()) > 0)
    leading_ratio = leading_count / len(non_blank)

    if leading_ratio > 0.25:
        nums = []
        for m in matches:
            if m and len(m.group(3).strip()) > 0:
                nums.append(int(m.group(1)))
        if len(nums) >= 5:
            increments = sum(1 for a, b in zip(nums, nums[1:], strict=False) if 0 < b - a <= 3)
            seq_ratio = increments / max(len(nums) - 1, 1)
            if seq_ratio > 0.4:
                def _strip_leading(line):
                    m = leading_num.match(line)
                    if m and len(m.group(3).strip()) > 0:
                        return m.group(3)
                    return line
                cleaned = [_strip_leading(l) for l in lines]
                return '\n'.join(cleaned)

    return text


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 0, url: str = '') -> str:
    """Extract text from PDF as Markdown.

    Strategy 1: pymupdf4llm -> Markdown with table/header preservation
    Strategy 2: pymupdf raw -> plain-text page-by-page fallback
    """
    _load_pdf_dependencies()
    if _pymupdf is None:
        return '[PDF extraction unavailable: pymupdf not installed]'

    if len(pdf_bytes) > MAX_PDF_BYTES:
        return f'[PDF too large: {len(pdf_bytes) // (1024*1024)} MB]'

    limit = max_chars if max_chars > 0 else 999_999_999

    if _pymupdf4llm is not None:
        try:
            md_doc = _pymupdf.open(stream=pdf_bytes, filetype="pdf")
            try:
                n = len(md_doc)
                chunks = _to_markdown_classic(
                    md_doc, page_chunks=True, show_progress=False,
                    table_strategy="lines",
                )
            finally:
                md_doc.close()

            parts = []
            total = 0
            for ci, chunk in enumerate(chunks):
                page_md = chunk.get('text', '') if isinstance(chunk, dict) else str(chunk)
                page_md = _strip_manuscript_line_numbers(page_md)
                plen = len(page_md)
                if total + plen > limit:
                    remaining = limit - total
                    if remaining > 200:
                        parts.append(page_md[:remaining])
                    parts.append(f'\n[...truncated at {total + remaining:,} chars, page {ci + 1}/{n}]')
                    break
                parts.append(page_md)
                total += plen

            text = '\n\n---\n\n'.join(parts)
            logger.debug('pymupdf4llm OK: %d pages, %s chars — %s', n, f'{total:,}', url[:60])
            return text
        except Exception as e:
            logger.warning('pymupdf4llm failed, falling back to raw: %s', e, exc_info=True)

    # Strategy 2: raw pymupdf
    try:
        doc = _pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            n = len(doc)
            parts = []
            total = 0
            for page in doc:
                raw = page.get_text()
                total += len(raw)
                parts.append(raw)
                if limit < 999_999_999 and total > limit:
                    parts.append(f'\n[...truncated at {total:,} chars]')
                    break
        finally:
            doc.close()
        if not parts:
            logger.info('[PDF] no extractable text (%d pages) — %s', n, url[:80] if url else '?')
            return '[PDF: no extractable text]'
        full = re.sub(r'\n{3,}', '\n\n', '\n\n'.join(parts))
        return full
    except Exception as e:
        logger.warning('[PDF] extraction failed for %s: %s', url[:80] if url else '?', e, exc_info=True)
        return f'[PDF extraction failed: {e}]'
