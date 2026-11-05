"""Knowledge OCR in an owned helper process, never under the server's GIL.

Responsibility
--------------
PyMuPDF's OCR entry (``fz_write_pixmap_as_pdfocr``) holds the GIL for its whole
Tesseract run, so in-process OCR froze every server thread for seconds. Each
OCR job here starts one child from the pinned ``_ocr_worker.py`` source through
the shared ``lib.runtime.owned_helper_process`` launcher; the calling thread
only waits. Children start per job, with no resident pool and no count limit.
The kernel output is returned unchanged; child failures raise ``OcrWorkerError``
and callers keep their own best-effort policy.

Entry points
------------
``recognize_image(raw, mime_type)`` and
``recognize_pdf(raw, page_limit=..., max_chars=...)``; both accept a
``cancelled`` callback that raises to terminate and join the child.

Dependencies
------------
``lib.runtime.owned_helper_process``, ``lib.runtime.pinned_helper_source``.
"""
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys
import tempfile

from lib.log import get_logger
from lib.runtime.owned_helper_process import run_owned_helper
from lib.runtime.pinned_helper_source import PinnedHelperSource

logger = get_logger(__name__)

_OCR_WORKER = PinnedHelperSource(Path(__file__).with_name('_ocr_worker.py'))
# PyMuPDF finds Tesseract language data through this variable; without it the
# child would probe a `tesseract` binary instead of the server's data set.
_OCR_ENVIRONMENT_NAMES = ('TESSDATA_PREFIX',)
_PYMUPDF_IMAGE_TYPES = {
    'image/png': 'png',
    'image/jpeg': 'jpeg',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/bmp': 'bmp',
}


class OcrWorkerError(RuntimeError):
    """The OCR child failed, crashed, or returned no valid completion."""

    def __init__(self, message: str, *, error_type: str = '') -> None:
        super().__init__(message)
        self.error_type = error_type


def _completion(encoded: bytes, returncode: int) -> dict:
    try:
        completion = json.loads(encoded)
        text, warnings = completion['text'], completion['warnings']
        failures, error = completion['failures'], completion['error']
    except (ValueError, KeyError, TypeError) as exc:
        raise OcrWorkerError(
            f'OCR worker returned no valid completion (exit {returncode})') from exc
    if error is not None:
        if not isinstance(error, dict):
            raise OcrWorkerError('OCR worker returned an invalid error')
        error_type = str(error.get('type') or 'Error')
        raise OcrWorkerError(
            f"{error_type}: {error.get('message', 'OCR failed')}", error_type=error_type)
    if (not isinstance(text, str) or not isinstance(warnings, list)
            or not all(isinstance(item, str) for item in warnings)
            or not isinstance(failures, dict)):
        raise OcrWorkerError('OCR worker returned an invalid completion')
    if returncode != 0:
        raise OcrWorkerError(f'OCR worker did not complete (exit {returncode})')
    for message, count in failures.items():
        logger.debug('[Knowledge] OCR pass failed %s time(s): %s', count, message)
    return completion


def _run_worker(arguments: list[str], raw: bytes,
                cancelled: Callable[[], None] | None) -> dict:
    with tempfile.TemporaryFile(mode='w+b') as source, \
            tempfile.TemporaryFile(mode='w+b') as output:
        source.write(raw)
        source.seek(0)
        returncode = run_owned_helper(
            [sys.executable, _OCR_WORKER.path(), *arguments],
            stdin=source, stdout=output,
            extra_environment_names=_OCR_ENVIRONMENT_NAMES, cancelled=cancelled)
        output.seek(0)
        encoded = output.read()
    return _completion(encoded, returncode)


def recognize_image(raw: bytes, mime_type: str, *,
                    cancelled: Callable[[], None] | None = None) -> str:
    filetype = _PYMUPDF_IMAGE_TYPES[mime_type]
    return _run_worker(['image', filetype], raw, cancelled)['text']


def recognize_pdf(raw: bytes, *, page_limit: int | None, max_chars: int,
                  cancelled: Callable[[], None] | None = None) -> tuple[str, list[str]]:
    completion = _run_worker(
        ['pdf', '-' if page_limit is None else str(int(page_limit)), str(int(max_chars))],
        raw, cancelled)
    return completion['text'], completion['warnings']


__all__ = ['OcrWorkerError', 'recognize_image', 'recognize_pdf']
