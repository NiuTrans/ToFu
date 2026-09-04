"""Paper routes — bounded file streaming, PDF serving, and reparse."""

import asyncio
import os
import re
import time

from quart import Response, request


from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
    api_service_unavailable,
)
from lib.log import get_logger
from lib.paper_identity import PAPER_DIR
from lib.pdf_parser.admission import (
    PdfParseCapacityExceeded,
    PdfParseTimeoutError,
)
from lib.request_parser import async_parse_body

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    api_v1_paper_bp,
    paper_bp,
)


def _stream_file_response(filepath, mimetype, chunk_size=262144):
    """Stream a file from disk in fixed chunks, honouring Range ourselves.

    FALLBACK for when a buffering cloud-IDE proxy defeats ``send_file``'s ranged
    serving (i.e. the transport log shows one ``range=False -> 200`` full GET
    instead of many ``range=True -> 206``). Instead of handing the proxy a
    single tens-of-MB body it can buffer into a timeout, we yield the bytes in
    ``chunk_size`` pieces through a ``Response`` generator (the same proven
    sync-generator pattern the SSE endpoints use) and set the anti-buffering
    headers from the proxy-buffering lesson: ``no-transform`` +
    ``Content-Encoding: identity`` + ``X-Accel-Buffering: no``. We also parse
    ``Range`` manually so this path stays range-capable (206 with the exact
    slice) when the proxy DOES forward Range.

    Dormant by default — wired in only when ``TOFU_PAPER_PDF_STREAM=1`` so a
    single-box install stays byte-identical to the ``send_file`` path.
    """
    file_size = os.path.getsize(filepath)
    start, end = 0, file_size - 1
    status = 200
    m = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("Range", "") or "")
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
        else:  # suffix range: bytes=-N → last N bytes
            start = max(0, file_size - int(m.group(2)))
            end = file_size - 1
        start = max(0, start)
        end = min(end, file_size - 1)
        if start > end:
            resp = Response(status=416)
            resp.headers["Content-Range"] = "bytes */%d" % file_size
            return resp
        status = 206
    length = end - start + 1

    def generate():
        remaining = length
        with open(filepath, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(generate(), status=status, mimetype=mimetype)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    resp.headers["Cache-Control"] = "public, max-age=43200, no-transform"
    resp.headers["Content-Encoding"] = "identity"
    resp.headers["X-Accel-Buffering"] = "no"
    if status == 206:
        resp.headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, file_size)
    return resp


@paper_bp.route("/api/paper/pdf/<filename>")
def serve_paper_pdf(filename):
    """Serve a downloaded paper PDF.

    This stays synchronous for the same executor/file-response boundary as
    ``serve_paper_image``. No DB or request-body parsing occurs here.
    """
    filename = os.path.basename(filename)
    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        logger.debug("[Paper] PDF not found: %s", filename)
        return api_not_found("PDF not found")
    # FALLBACK (opt-in): if the transport log proves the proxy buffers the
    # whole-file 200 (single ``range=False -> 200``), flip TOFU_PAPER_PDF_STREAM=1
    # to serve the PDF as a chunked generator the proxy can't buffer into a
    # timeout. Default off → byte-identical to the send_file path below.
    if os.environ.get("TOFU_PAPER_PDF_STREAM") == "1":
        resp = _stream_file_response(filepath, "application/pdf")
        logger.info(
            "[Paper] serve pdf=%s range=%s -> %s (stream)",
            filename,
            bool(request.headers.get("Range")),
            resp.status_code,
        )
        return resp
    # conditional=True → make_conditional(accept_ranges=True): honour HTTP
    # Range so pdf.js can range-load a large PDF in small chunks. Without it
    # send_file always returns 200 + the whole file (tens of MB); a buffering
    # cloud-IDE proxy can truncate/time-out that single response, which pdf.js
    # surfaces as "Missing PDF" or per-page "failed to render".
    from lib.file_serving import send_file_conditional

    resp = send_file_conditional(filepath, mimetype="application/pdf")
    # Advertise ranged capability on the INITIAL (non-Range) 200 too. pdf.js's
    # validateRangeRequestCapabilities only switches to ranged loading when the
    # FIRST response carries ``Accept-Ranges: bytes`` — Quart's make_conditional
    # sets it only on the 206 (Range-present) path, so without this the viewer
    # does one giant full GET and conditional=True is inert for it.
    resp.headers.setdefault("Accept-Ranges", "bytes")
    # Transport diagnostic (acceptance gate): after a restart+refresh, opening a
    # large PDF through the proxy should log many ``range=True -> 206`` lines
    # (pdf.js is range-loading and the proxy passes it through). A single
    # ``range=False -> 200`` means the proxy did one buffered full GET → ranged
    # loading is moot and we fall back to chunked streaming (see _stream_pdf).
    logger.info(
        "[Paper] serve pdf=%s range=%s -> %s",
        filename,
        bool(request.headers.get("Range")),
        resp.status_code,
    )
    return resp


@api_v1_paper_bp.route("/api/v1/paper/reparse", methods=["POST"])
async def reparse_paper():
    """Re-parse an already-stored paper PDF to recover its text.

    Used to recover library entries that were saved before server-side parsing
    (or whose parse step failed). Given a filename already under PAPER_DIR,
    reads it and returns extracted text + page count.

    Body JSON:
        filename: str — basename of the PDF under PAPER_DIR

    Returns:
        { ok: true, text: str, total_pages: int, text_length: int }
    """
    data = await async_parse_body()
    filename = os.path.basename((data.get("filename") or "").strip())
    if not filename:
        logger.warning("[Paper:Reparse] No filename provided")
        return api_bad_request("No filename")

    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        logger.warning("[Paper:Reparse] PDF not found: %s", filename)
        return api_not_found("PDF not found")

    # Blocking read + pymupdf parse — offload off the event loop.
    def _reparse():
        with open(filepath, "rb") as f:
            pdf_bytes = f.read()
        from lib.pdf_parser.pool import parse_pdf_pooled as _parse_pdf

        t0 = time.time()
        result = _parse_pdf(pdf_bytes, max_text_chars=0, max_images=0)
        elapsed = time.time() - t0
        text = result.get("text") or ""
        total_pages = result.get("totalPages", 0)
        text_length = result.get("textLength", len(text))
        logger.info(
            "[Paper:Reparse] %s — %d pages, %d chars in %.1fs",
            filename,
            total_pages,
            text_length,
            elapsed,
        )
        return text, total_pages, text_length

    try:
        text, total_pages, text_length = await asyncio.to_thread(_reparse)
        return api_ok(
            {
                "text": text,
                "total_pages": total_pages,
                "text_length": text_length,
            }
        )
    except PdfParseCapacityExceeded as e:
        logger.info("[Paper:Reparse] Capacity full for %s: %s", filename, e)
        return api_service_unavailable(
            str(e),
            retry_after=1,
            kind="server_busy",
            retryable=True,
        )
    except PdfParseTimeoutError as e:
        logger.warning("[Paper:Reparse] Timed out for %s: %s", filename, e)
        return api_error(
            str(e),
            status=504,
            kind="timeout",
            retryable=True,
        )
    except Exception as e:
        logger.error("[Paper:Reparse] Failed for %s: %s", filename, e, exc_info=True)
        return api_internal_error(f"Reparse failed: {e}")
