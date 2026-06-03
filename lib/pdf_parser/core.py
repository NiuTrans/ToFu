"""lib/pdf_parser/core.py — Unified PDF parsing entry point (text + images)."""

from lib.log import get_logger
from lib.pdf_parser._common import HAS_PYMUPDF4LLM, PYMUPDF_LOCK

logger = get_logger(__name__)

# Import pymupdf from _common (guarded there) — callers check HAS_PYMUPDF
try:
    import pymupdf
except ImportError:
    pymupdf = None  # type: ignore[assignment]
    logger.debug('pymupdf not available in core — guarded by HAS_PYMUPDF')
from lib.pdf_parser.images import detect_and_clip_figures
from lib.pdf_parser.text import extract_pdf_text

__all__ = ['parse_pdf']


def parse_pdf(pdf_bytes: bytes, *,
              max_text_chars: int = 0,
              max_image_width: int = 1024,
              max_images: int = 20,
              min_img_dim: int = 80,
              min_img_bytes: int = 2000,
              progress_callback=None,
              text_mode: str = 'rich',
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

    Returns dict with keys:
        text, images, totalPages, textLength, isScanned, method, warnings
    """
    # Defensive normalize — accept None / unknown modes gracefully.
    if text_mode not in ('rich', 'structured', 'fast'):
        logger.debug('[PDF] parse_pdf: unknown text_mode=%r, coercing to rich',
                     text_mode)
        text_mode = 'rich'

    max_chars = max_text_chars if max_text_chars > 0 else 999_999_999

    # ── Text (opens/closes its own doc internally) ──
    def _text_cb(done, total):
        if progress_callback is None:
            return
        try:
            progress_callback('text', done, total)
        except Exception as e:
            logger.debug('[PDF] progress_callback raised (ignored): %s', e)

    text = extract_pdf_text(pdf_bytes, max_chars,
                            progress_callback=_text_cb,
                            mode=text_mode) or ''

    with PYMUPDF_LOCK:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            total_pages = len(doc)
            avg_chars = len(text) / max(total_pages, 1)
            is_scanned = (avg_chars < 50)
            # Method label reflects the winning strategy — docling may have
            # been requested but silently fallen back to pymupdf4llm.
            if text_mode == 'structured':
                # Best-effort: import guard mirrors text.py logic. We don't
                # track the actual winner from here, so we tag both.
                from lib.pdf_parser._common import HAS_DOCLING
                method = 'docling' if HAS_DOCLING else ('pymupdf4llm' if HAS_PYMUPDF4LLM else 'pymupdf_raw')
            else:
                method = 'pymupdf4llm' if HAS_PYMUPDF4LLM else 'pymupdf_raw'

            warnings = []
            if is_scanned:
                warnings.append('PDF appears scanned / image-only; text may be incomplete.')
            if not HAS_PYMUPDF4LLM:
                warnings.append('pymupdf4llm not installed; tables/headers not preserved.')

            # ── Images (figures & tables) ──
            images = []
            if max_images > 0:
                pages_to_render = total_pages
                for pi in range(pages_to_render):
                    if len(images) >= max_images:
                        break
                    page = doc[pi]
                    page_imgs = detect_and_clip_figures(
                        page, pi, total_pages,
                        max_image_width=max_image_width,
                        min_dim=min_img_dim,
                        min_bytes=min_img_bytes,
                    )
                    for img in page_imgs:
                        if len(images) >= max_images:
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
        'textLength': len(text),
        'isScanned': is_scanned,
        'method': method,
        'warnings': warnings,
    }
