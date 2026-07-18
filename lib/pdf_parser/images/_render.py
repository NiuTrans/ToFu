"""lib/pdf_parser/images/_render.py — Full-page PDF rendering to image bytes."""

try:
    import pymupdf
except ImportError:
    pymupdf = None  # type: ignore[assignment]
    # Warning already logged by _common.py — debug-only here to avoid noise

from lib.log import get_logger
from lib.pdf_parser._common import PYMUPDF_LOCK

logger = get_logger(__name__)


def render_pdf_pages(pdf_bytes: bytes, *, dpi: int = 150) -> list[bytes]:
    """Render each PDF page to JPEG bytes.

    Returns list of JPEG byte strings, one per page.
    """
    with PYMUPDF_LOCK:
        doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
        try:
            pages = []
            n = len(doc)
            for i in range(n):
                pix = doc[i].get_pixmap(dpi=dpi)
                pages.append(pix.tobytes('jpeg'))
        finally:
            doc.close()
    return pages
