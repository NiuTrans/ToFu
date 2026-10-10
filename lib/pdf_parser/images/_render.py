"""lib/pdf_parser/images/_render.py — Full-page PDF rendering to image bytes."""

from collections.abc import Callable, Sequence
import tempfile
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

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
from lib.pdf_parser.images._render_worker import (
    COMPLETION_MAX_BYTES, FRAME_HEADER_BYTES, PdfPageLimitExceeded, render_document,
)

logger = get_logger(__name__)


class RenderedPageSpool(Sequence[bytes]):
    """Caller-owned anonymous JPEG file; only offsets remain resident.

    Rendering appends pages; concurrent model batches read completed slices.
    The caller closes only after every borrowing batch has joined.
    """

    def __init__(self):
        self._file = tempfile.TemporaryFile(mode='w+b', buffering=0)
        self._pages: list[tuple[int, int]] = []
        self._lock = threading.Lock()

    def append(self, image: bytes) -> None:
        with self._lock:
            offset = self._file.seek(0, 2)
            view = memoryview(image)
            while view:
                written = self._file.write(view)
                if not written:
                    raise OSError('PDF image spool write made no progress')
                view = view[written:]
            self._pages.append((offset, len(image)))

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        offset, length = self._pages[index]
        with self._lock:
            self._file.seek(offset)
            image = self._file.read(length)
        if len(image) != length:
            raise OSError('PDF image spool returned an incomplete page')
        return image

    def close(self):
        self._file.close()


def render_pdf_pages(
    pdf_bytes: bytes,
    *,
    dpi: int = 150,
    max_pages: int | None = None,
    abort_check: Callable[[], bool] | None = None,
    spool: bool = False,
) -> list[bytes] | RenderedPageSpool:
    """Render each PDF page to JPEG bytes or a caller-owned anonymous spool.

    The page ceiling is checked before the first pixmap allocation. Cancellation
    is checked between pages so a removed VLM attachment releases renderer
    memory without waiting for the whole document.
    """
    pages = RenderedPageSpool() if spool else []
    try:
        with PYMUPDF_LOCK:
            def check_abort():
                if abort_check is not None and abort_check():
                    from lib.llm_errors import AbortedError
                    raise AbortedError('VLM PDF rendering aborted')
            render_document(pymupdf, pdf_bytes, dpi=dpi, max_pages=max_pages,
                            sink=pages, abort_check=check_abort)
    except BaseException:
        if isinstance(pages, RenderedPageSpool):
            pages.close()
        raise
    return pages


def _render_command(dpi, max_pages):
    return [sys.executable, str(Path(__file__).with_name('_render_worker.py')),
            str(int(dpi)), '-' if max_pages is None else str(int(max_pages))]


def _join_renderer(process):
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass  # The exact owned child exited between poll and terminate.
        except OSError as error:
            # A failed signal grants no permission to close borrowed files.
            logger.warning('PDF renderer terminate failed; retaining child ownership: %s', error)
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass  # The exact owned child exited between wait and kill.
            except OSError as error:
                logger.warning('PDF renderer kill failed; waiting for owned child: %s', error)
    process.wait()


def _adopt_render_frames(pages, returncode):
    stream = pages._file
    end = stream.seek(0, 2)
    stream.seek(0)
    while stream.tell() + FRAME_HEADER_BYTES <= end:
        length = int.from_bytes(stream.read(FRAME_HEADER_BYTES), 'big')
        if length == 0:
            if end - stream.tell() > COMPLETION_MAX_BYTES:
                raise RuntimeError('PDF renderer completion exceeded its wire bound')
            try:
                completion = json.loads(stream.read())
                error = completion['error']
                total = completion['pages']
            except (ValueError, KeyError, TypeError) as error:
                raise RuntimeError('PDF renderer returned an invalid completion') from error
            if error is not None:
                error_types = {'PdfPageLimitExceeded': PdfPageLimitExceeded,
                    'ValueError': ValueError, 'OSError': OSError, 'ImportError': ImportError,
                    'RuntimeError': RuntimeError}
                if not isinstance(error, dict):
                    raise RuntimeError('PDF renderer returned an invalid error')
                raise error_types.get(error.get('type'), RuntimeError)(
                    str(error.get('message', 'PDF rendering failed')))
            if type(total) is not int or returncode != 0 or total != len(pages):
                raise RuntimeError(f'PDF renderer did not complete (exit {returncode})')
            return
        offset = stream.tell()
        if offset + length > end:
            break
        pages._pages.append((offset, length))
        stream.seek(length, 1)
    raise RuntimeError(f'PDF renderer returned incomplete page evidence (exit {returncode})')


def render_pdf_pages_isolated(pdf_bytes: bytes, *, dpi=150, max_pages=None,
                              abort_check=None, spool=False):
    """Start an owned render process per document, with no global render slot.

    The child writes JPEG frames directly to the caller's anonymous file. Only
    offsets become resident. Cancellation terminates and joins the child before
    any descriptor closes; no ambiguous failed launch retries in the parent.
    """
    pages = RenderedPageSpool()
    process = None
    try:
        with tempfile.TemporaryFile(mode='w+b') as source:
            source.write(pdf_bytes)
            source.seek(0)
            if abort_check is not None and abort_check():
                from lib.llm_errors import AbortedError
                raise AbortedError('VLM PDF rendering aborted')
            # Native PDF parsing needs loader/temp/locale paths, never account
            # credentials or application lifecycle authority.
            names = ('PATH', 'SystemRoot', 'WINDIR', 'LD_LIBRARY_PATH', 'LD_PRELOAD',
                     'DYLD_LIBRARY_PATH', 'PYTHONPATH', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL')
            environment = {name: os.environ[name] for name in names if name in os.environ}
            process = subprocess.Popen(_render_command(dpi, max_pages), stdin=source,
                stdout=pages._file, stderr=subprocess.DEVNULL, env=environment, close_fds=True)
            try:
                while True:
                    if abort_check is not None and abort_check():
                        from lib.llm_errors import AbortedError
                        raise AbortedError('VLM PDF rendering aborted')
                    try:
                        returncode = process.wait(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                _join_renderer(process)
        _adopt_render_frames(pages, returncode)
        if spool:
            return pages
        result = list(pages)
        pages.close()
        return result
    except BaseException:
        pages.close()
        raise
