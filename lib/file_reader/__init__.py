"""lib/file_reader/ — Read arbitrary local files: images, PDFs, Office docs, text.

Façade package. Provides a unified ``read_local_file(path)`` function that handles:
  - **Images** (.png, .jpg, .gif, .webp, .bmp): returns structured dict with
    base64 data for native VLM upload (``__screenshot__`` protocol).
  - **PDFs** (.pdf): text extraction via ``lib.pdf_parser``.
  - **Office docs** (.docx, .xlsx, .pptx, .doc, .xls, .ppt): text extraction
    via ``lib.doc_parser``.
  - **Plain text** (any other text-decodable file): direct read with encoding
    detection.

This module is called by ``read_files`` (via ``_read_absolute_file`` in
``lib/project_mod/read_tools.py``) when the path is absolute.

Internally split into:
  - ``_router``  — extension categories / limits / ``read_local_file`` dispatch.
  - ``_image``   — ``_read_image`` / ``inspect_image_file`` / ``_compress_image``.
  - ``_docs``    — ``_read_pdf`` / ``_read_office`` / ``_read_text``.

The import path is UNCHANGED: ``from lib.file_reader import X`` works exactly
as before for every previously-public symbol. Private helpers used by the
test-suite (``_compress_image``, ``_INSPECT_MAX_PX``) are re-exported too.
"""

from lib.log import get_logger

from ._docs import _read_office, _read_pdf, _read_text
from ._image import (
    _IMAGE_MAGICS,
    _INSPECT_JPEG_QUALITY,
    _INSPECT_MAX_PX,
    _compress_image,
    _read_image,
    inspect_image_bytes,
    inspect_image_file,
)
from ._router import (
    IMAGE_EXTENSIONS,
    MAX_FILE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_TEXT_CHARS,
    OFFICE_EXTENSIONS,
    PDF_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TEXT_EXTENSIONS,
    _EXT_MIME,
    read_local_file,
)

logger = get_logger(__name__)

# Preserved verbatim from the pre-split module.
__all__ = ['read_local_file', 'inspect_image_file', 'inspect_image_bytes',
           'IMAGE_EXTENSIONS', 'SUPPORTED_EXTENSIONS']
