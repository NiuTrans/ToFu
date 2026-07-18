"""lib/file_reader/_router.py — Extension categories, limits, and dispatcher.

Owns the shared constants (``IMAGE_EXTENSIONS`` / ``SUPPORTED_EXTENSIONS`` /
``_EXT_MIME`` / byte & char limits) plus ``read_local_file`` — the top-level
router that dispatches by file extension to the image / PDF / office / text
readers.

Dependency direction is acyclic: ``read_local_file`` imports the leaf readers
(``_read_image`` from ``._image``; ``_read_pdf`` / ``_read_office`` /
``_read_text`` from ``._docs``) lazily inside the function body, while those
leaf modules import only the constants declared here.
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)

# ── Extension categories ──────────────────────────────────────────────

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}

PDF_EXTENSIONS = {'.pdf'}

OFFICE_EXTENSIONS = {
    '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
}

# Text extensions — we'll also try any unknown extension as text
TEXT_EXTENSIONS = {
    '.txt', '.md', '.markdown', '.csv', '.tsv',
    '.json', '.jsonl', '.xml', '.html', '.htm',
    '.log', '.yaml', '.yml', '.toml', '.ini', '.cfg',
    '.rst', '.tex', '.bib', '.srt', '.vtt',
    '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp',
    '.go', '.rs', '.rb', '.php', '.sh', '.bash', '.zsh',
    '.css', '.scss', '.less', '.sql', '.r', '.m', '.swift',
    '.jsx', '.tsx', '.vue', '.svelte',
}

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS | OFFICE_EXTENSIONS | TEXT_EXTENSIONS

# ── Limits ──────────────────────────────────────────────────────────

MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 20 MB max for images
MAX_FILE_BYTES = 50 * 1024 * 1024    # 50 MB max for documents
MAX_TEXT_CHARS = 50 * 1024 * 1024    # ★ char cap lifted to the byte bound; MAX_FILE_BYTES is the real limit

_EXT_MIME = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
}


def read_local_file(path: str) -> dict | str:
    """Read a local file and return its content.

    Args:
        path: Absolute or user-expandable file path.

    Returns:
        For images: dict with ``__screenshot__`` protocol (sent as image_url
        to VLM).
        For all other files: str with extracted text content.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the file is too large or unsupported.
    """
    # Leaf readers imported here so the router owns dispatch while the
    # doc/pdf parsers themselves stay lazily loaded inside _read_pdf/_read_office.
    from ._docs import _read_office, _read_pdf, _read_text
    from ._image import _read_image

    # Strip file:// URI prefix if present (e.g. file:///home/user/doc.pdf → /home/user/doc.pdf)
    if path.startswith('file://'):
        path = path[7:]  # len('file://') == 7
        logger.debug('[FileReader] Stripped file:// prefix → %s', path)
    # Expand ~ and resolve
    path = os.path.expanduser(path)
    path = os.path.abspath(path)

    if not os.path.isfile(path):
        return f'Error: File not found: {path}'

    file_size = os.path.getsize(path)
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()

    logger.info('[FileReader] Reading file: %s (ext=%s, size=%s)',
                path, ext, f'{file_size:,}')

    # ── Images → native VLM upload ────────────────────────────────
    if ext in IMAGE_EXTENSIONS:
        return _read_image(path, ext, file_size)

    # ── PDFs → text extraction ────────────────────────────────────
    if ext in PDF_EXTENSIONS:
        return _read_pdf(path, file_size)

    # ── Office documents → text extraction ────────────────────────
    if ext in OFFICE_EXTENSIONS:
        return _read_office(path, filename, file_size)

    # ── Everything else → try as text ─────────────────────────────
    return _read_text(path, filename, file_size)
