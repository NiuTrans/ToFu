"""tofu_search.fetch.pdf_extract — PDF text extraction.

Entry point: extract_pdf_text. Optional native Markdown/raw readers take
precedence; the base pypdf reader handles text when native libraries are absent.
All readers load lazily, close documents and share literal text-budget rules.
"""

import io
import threading

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['PDFExtractionError', 'extract_pdf_text']

MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB

_pymupdf = None
_pymupdf4llm = None
_pypdf = None
_dependencies_loaded = False
_dependency_lock = threading.Lock()


class PDFExtractionError(RuntimeError):
    """PDF capability or extraction failure, never extracted source text."""


def _load_pdf_dependencies():
    """Load the available PDF reader once, only when the capability is used."""
    global _pymupdf, _pymupdf4llm, _pypdf, _dependencies_loaded
    if _dependencies_loaded:
        return
    with _dependency_lock:
        if _dependencies_loaded:
            return
        try:
            import pymupdf as core
            _pymupdf = core
        except (ImportError, OSError) as exc:
            logger.debug('PDF core unavailable: %s', exc)
        if _pymupdf is not None:
            try:
                import pymupdf4llm as rich
                _pymupdf4llm = rich
            except (ImportError, OSError) as exc:
                logger.debug('Rich PDF extraction unavailable: %s', exc)
        else:
            try:
                import pypdf as pure
                _pypdf = pure
            except ImportError as exc:
                logger.debug('Base PDF reader unavailable: %s', exc)
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


def _collect_page_text(pages, max_chars, page_count, separator):
    """Project page text without guessing which source numbers are disposable."""
    parts, used, has_source = [], 0, False
    truncated_page = None
    for index, page_text in enumerate(pages):
        boundary = separator if index else ''
        piece = boundary + page_text
        remaining = max_chars - used if max_chars > 0 else len(piece)
        delivered = piece[:max(0, remaining)]
        source_delivered = delivered[len(boundary):]
        has_source = has_source or bool(source_delivered.strip())
        parts.append(delivered)
        used += len(delivered)
        if len(delivered) < len(piece):
            truncated_page = index + 1
            break
        if max_chars > 0 and used >= max_chars and index + 1 < page_count:
            truncated_page = index + 1
            break
    if not has_source:
        raise PDFExtractionError('PDF contains no extractable text within the source budget.')
    text = ''.join(parts)
    if truncated_page is not None:
        text += f'\n[...truncated at {used:,} chars, page {truncated_page}/{page_count}]'
    return text


def _extract_pypdf(pdf_bytes, max_chars):
    """Read text without native libraries, retaining upstream resource limits."""
    if _pypdf is None:
        raise PDFExtractionError('PDF extraction unavailable: install tofu-search with its pypdf dependency.')
    try:
        with io.BytesIO(pdf_bytes) as stream:
            doc = _pypdf.PdfReader(stream)
            try:
                return _collect_page_text((page.extract_text() or '' for page in doc.pages),
                                          max_chars, len(doc.pages), '\n\n')
            finally:
                doc.close()
    except PDFExtractionError:
        raise
    except Exception as exc:
        raise PDFExtractionError('PDF extraction failed: the document could not be parsed.') from exc


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 0, url: str = '') -> str:
    """Extract text from PDF as Markdown.

    Strategy 1: pymupdf4llm -> Markdown with table/header preservation
    Strategy 2: pymupdf raw -> plain-text page-by-page fallback
    Without native libraries: pypdf -> plain text, including rotated text.
    This does not perform OCR or disable upstream decompression/parser limits.

    Raises PDFExtractionError when extraction is unavailable or produces no
    source text. Callers must keep diagnostics outside the evidence channel.
    """
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise PDFExtractionError(f'PDF exceeds the extraction size limit ({MAX_PDF_BYTES:,} bytes).')
    _load_pdf_dependencies()
    if _pymupdf is None:
        return _extract_pypdf(pdf_bytes, max_chars)

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

            pages = (chunk.get('text', '') if isinstance(chunk, dict) else str(chunk)
                     for chunk in chunks)
            text = _collect_page_text(pages, max_chars, n, '\n\n---\n\n')
            logger.debug('pymupdf4llm OK: %d pages, %s chars — %s', n, f'{len(text):,}', url[:60])
            return text
        except Exception as e:
            logger.warning('pymupdf4llm failed, falling back to raw: %s', e, exc_info=True)

    # Strategy 2: raw pymupdf
    try:
        doc = _pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            n = len(doc)
            return _collect_page_text((page.get_text() for page in doc),
                                      max_chars, n, '\n\n')
        finally:
            doc.close()
    except PDFExtractionError:
        raise
    except Exception as e:
        logger.warning('[PDF] extraction failed for %s: %s', url[:80] if url else '?', e, exc_info=True)
        raise PDFExtractionError('PDF extraction failed: the document could not be parsed.') from e
