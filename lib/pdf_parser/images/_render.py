"""lib/pdf_parser/images/_render.py — Full-page PDF rendering to image bytes."""

from collections.abc import Callable

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf  # PyMuPDF <1.24.3 legacy module name
    except ImportError:
        pymupdf = None  # type: ignore[assignment]
        # Warning already logged by _common.py — debug-only here to avoid noise

from lib.log import get_logger
from lib.pdf_parser._common import PYMUPDF_LOCK

logger = get_logger(__name__)


class PdfPageLimitExceeded(ValueError):
    """The document exceeds the caller's pre-render page budget."""


def render_pdf_pages(
    pdf_bytes: bytes,
    *,
    dpi: int = 150,
    max_pages: int | None = None,
    abort_check: Callable[[], bool] | None = None,
) -> list[bytes]:
    """Render each PDF page to JPEG bytes.

    The page ceiling is checked before the first pixmap allocation. Cancellation
    is checked between pages so a removed VLM attachment releases renderer
    memory without waiting for the whole document.
    """
    with PYMUPDF_LOCK:
        doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
        try:
            pages = []
            n = len(doc)
            if max_pages is not None and n > max(0, int(max_pages)):
                raise PdfPageLimitExceeded(
                    f'PDF has {n} pages; VLM page limit is {int(max_pages)}')
            for i in range(n):
                if abort_check is not None and abort_check():
                    from lib.llm_errors import AbortedError
                    raise AbortedError('VLM PDF rendering aborted')
                pix = doc[i].get_pixmap(dpi=dpi)
                pages.append(pix.tobytes('jpeg'))
        finally:
            doc.close()
    return pages
