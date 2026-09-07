"""lib/pdf_parser/core.py — Unified PDF parsing entry point (text + images)."""

from lib.log import get_logger
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
from lib.pdf_parser._common import (
    HAS_PYMUPDF4LLM,
    PYMUPDF4LLM_UNAVAILABLE_REASON,
    PYMUPDF_LOCK,
)

logger = get_logger(__name__)

# Import pymupdf from _common (guarded there) — callers check HAS_PYMUPDF
try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf  # PyMuPDF <1.24.3 legacy module name
    except ImportError:
        pymupdf = None  # type: ignore[assignment]
        logger.debug('pymupdf not available in core — guarded by HAS_PYMUPDF')
from lib.pdf_parser.images import detect_and_clip_figures
from lib.pdf_parser.policy import (
    bounded_pdf_image_count,
    bounded_pdf_image_width,
    bounded_pdf_pages,
    bounded_pdf_text_chars,
    resolve_classic_pdf_budget,
)
from lib.pdf_parser.text import _extract_pdf_text_with_meta_without_admission

__all__ = ['parse_pdf']


def _parse_pdf_without_admission(pdf_bytes: bytes, *,
                                 max_text_chars: int = 0,
                                 max_image_width: int = 1024,
                                 max_images: int = 20,
                                 min_img_dim: int = 80,
                                 min_img_bytes: int = 2000,
                                 progress_callback=None,
                                 text_mode: str = 'rich',
                                 max_pages: int = 0,
) -> dict:
    """Full PDF parsing: text extraction + figure/table image extraction.

    Args:
        text_mode: Passed to ``extract_pdf_text(mode=...)``. One of
            ``'rich'`` (pymupdf4llm, default), ``'structured'`` (docling,
            opt-in heavy dep), or ``'fast'`` (raw get_text).
        progress_callback: Optional ``Callable[[str, int, int], None]`` invoked
            as ``(stage, done, total)`` where ``stage`` is ``'text'`` during
            text extraction and ``'images'`` during figure clipping. Exceptions
            from the callback are swallowed (logged at DEBUG).
        max_text_chars / max_pages: Zero selects the launch-derived classic
            extraction budget. Positive request values may lower but never
            raise the process ceiling.

    Returns dict with keys:
        text, images, totalPages, textLength, isScanned, method, extractor,
        warnings

        ``extractor`` is the per-document winner reported by
        ``extract_pdf_text_with_meta`` (``'pymupdf4llm'`` /
        ``'pymupdf4llm-partial'`` / ``'pymupdf-raw'`` / ``'docling'`` /
        ``'error'``) — the value ``parser_version`` stamping is keyed on.
        ``method`` is the legacy label derived from it.
    """
    # Defensive normalize — accept None / unknown modes gracefully.
    if text_mode not in ('rich', 'structured', 'fast'):
        logger.debug('[PDF] parse_pdf: unknown text_mode=%r, coercing to rich',
                     text_mode)
        text_mode = 'rich'

    budget = resolve_classic_pdf_budget()
    max_chars = bounded_pdf_text_chars(max_text_chars, budget)
    page_limit = bounded_pdf_pages(max_pages, budget)
    image_limit = bounded_pdf_image_count(max_images)
    image_width = bounded_pdf_image_width(max_image_width)
    text_pages_done = 0

    # ── Text (opens/closes its own doc internally) ──
    def _text_cb(done, total):
        nonlocal text_pages_done
        try:
            text_pages_done = max(text_pages_done, int(done))
        except (TypeError, ValueError, OverflowError):
            pass
        if progress_callback is None:
            return
        try:
            progress_callback('text', done, total)
        except Exception as e:
            logger.debug('[PDF] progress_callback raised (ignored): %s', e)

    text, extractor = _extract_pdf_text_with_meta_without_admission(
        pdf_bytes,
        max_chars,
        progress_callback=_text_cb,
        mode=text_mode,
        max_pages=page_limit,
    )
    text = text or ''

    with PYMUPDF_LOCK:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            total_pages = len(doc)
            processed_pages = min(
                total_pages,
                page_limit,
                max(1, text_pages_done or min(total_pages, page_limit)),
            )
            truncated = (
                total_pages > page_limit
                or text_pages_done < min(total_pages, page_limit)
                or len(text) >= max_chars
                or 'resource budget]' in text[-256:]
                or '…truncated at' in text[-256:]
            )
            avg_chars = len(text) / max(processed_pages, 1)
            is_scanned = (avg_chars < 50)
            # Method label = the strategy that ACTUALLY produced the text
            # (per-document truth from extract_pdf_text_with_meta — a
            # pymupdf4llm attempt that fell back to raw is tagged raw).
            method = {'pymupdf4llm': 'pymupdf4llm',
                      'pymupdf4llm-partial': 'pymupdf4llm',
                      'docling': 'docling',
                      'pymupdf-raw': 'pymupdf_raw'}.get(
                          extractor,
                          'pymupdf4llm' if HAS_PYMUPDF4LLM else 'pymupdf_raw')

            warnings = []
            if is_scanned:
                warnings.append('PDF appears scanned / image-only; text may be incomplete.')
            if not HAS_PYMUPDF4LLM:
                if PYMUPDF4LLM_UNAVAILABLE_REASON.startswith('version'):
                    warnings.append('pymupdf4llm unavailable (version/ABI '
                                    'mismatch); tables/headers not preserved.')
                else:
                    warnings.append('pymupdf4llm not installed; '
                                    'tables/headers not preserved.')
            elif extractor == 'pymupdf4llm-partial':
                warnings.append('Some PDF pages required raw-text fallback; '
                                'most Markdown structure was preserved.')
            if truncated:
                warnings.append(
                    'PDF extraction reached its page or text resource budget; '
                    'the returned text is an explicit bounded prefix.')

            # ── Images (figures & tables) ──
            images = []
            if image_limit > 0:
                pages_to_render = min(total_pages, page_limit)
                for pi in range(pages_to_render):
                    if len(images) >= image_limit:
                        break
                    page = doc[pi]
                    page_imgs = detect_and_clip_figures(
                        page, pi, total_pages,
                        max_image_width=image_width,
                        min_dim=min_img_dim,
                        min_bytes=min_img_bytes,
                    )
                    for img in page_imgs:
                        if len(images) >= image_limit:
                            break
                        images.append(img)
                    if progress_callback is not None:
                        try:
                            progress_callback('images', pi + 1, pages_to_render)
                        except Exception as e:
                            logger.debug('[PDF] progress_callback raised (ignored): %s', e)
        finally:
            doc.close()

    return {
        'text': text,
        'images': images,
        'totalPages': total_pages,
        'processedPages': processed_pages,
        'textLength': len(text),
        'truncated': truncated,
        'isScanned': is_scanned,
        'method': method,
        'extractor': extractor,
        'warnings': warnings,
        'limits': {
            'maxPages': page_limit,
            'maxTextChars': max_chars,
            'maxImages': image_limit,
            'maxImageWidth': image_width,
        },
    }


def parse_pdf(pdf_bytes: bytes, *,
              max_text_chars: int = 0,
              max_image_width: int = 1024,
              max_images: int = 20,
              min_img_dim: int = 80,
              min_img_bytes: int = 2000,
              progress_callback=None,
              text_mode: str = 'rich',
              max_pages: int = 0,
) -> dict:
    """Admit one direct parse into the process-wide compressed-input budget."""
    budget = resolve_classic_pdf_budget()
    lease = CLASSIC_PDF_ADMISSION.reserve(budget.unfinished_capacity)
    try:
        return _parse_pdf_without_admission(
            pdf_bytes,
            max_text_chars=max_text_chars,
            max_image_width=max_image_width,
            max_images=max_images,
            min_img_dim=min_img_dim,
            min_img_bytes=min_img_bytes,
            progress_callback=progress_callback,
            text_mode=text_mode,
            max_pages=max_pages,
        )
    finally:
        lease.release()
