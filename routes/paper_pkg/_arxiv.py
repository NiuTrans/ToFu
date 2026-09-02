"""Paper routes — arXiv fetch endpoints (JSON + SSE stream)."""

import asyncio
import json
import os
import time

from quart import Response


from lib.api_response import (
    api_bad_request,
    api_error,
    api_ok,
)
from lib.log import get_logger
from lib.paper.arxiv import (
    _extract_arxiv_id,
    fetch_arxiv_title,
)
from lib.paper.images.figures import extract_paper_figures
from lib.paper_identity import PAPER_DIR, _paper_hash
from lib.request_parser import async_parse_body

logger = get_logger(__name__)


def http_get(*args, **kwargs):
    """Request-loaded HTTP seam retained for paper route tests."""
    from lib.http_client import http_get as _http_get

    return _http_get(*args, **kwargs)

from routes.paper_pkg._common import (
    _PaperDownloadTooLarge,
    _declared_pdf_length,
    _load_valid_cached_pdf,
    _new_pdf_part,
    _paper_pdf_limit,
    _read_pdf_bounded,
    api_v1_paper_bp,
    paper_bp,
)
from routes.paper_pkg._library import (
    _persist_ingested_library_row,
)
from routes.api_v1.auth import request_user_id


@api_v1_paper_bp.route("/api/v1/paper/fetch-arxiv", methods=["POST"])
async def fetch_arxiv():
    """Download PDF from arXiv URL and serve it locally.

    Body JSON:
        url: str — arXiv URL (abs page, pdf link, or just the ID like 2301.12345)
    Returns:
        { ok: true, pdf_url: str, title: str, arxiv_id: str }
    """
    data = await async_parse_body()
    url_input = data.get("url", "").strip()
    if not url_input:
        logger.warning("[Paper:arXiv] Fetch request with no URL")
        return api_bad_request("No URL provided")

    arxiv_id = _extract_arxiv_id(url_input)
    if not arxiv_id:
        logger.warning("[Paper:arXiv] Could not parse arXiv ID from: %.200s", url_input)
        return api_bad_request("Could not parse arXiv ID from URL")

    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    filename = f"arxiv_{arxiv_id.replace('/', '_')}.pdf"
    filepath = os.path.join(PAPER_DIR, filename)

    cached_pdf = await asyncio.to_thread(_load_valid_cached_pdf, filepath)
    if cached_pdf is not None:
        file_size = len(cached_pdf)
        logger.info(
            "[Paper:arXiv] Cache hit for %s — %d bytes at %s",
            arxiv_id,
            file_size,
            filepath,
        )
        return api_ok(
            {
                "pdf_url": f"/api/paper/pdf/{filename}",
                "arxiv_id": arxiv_id,
                "cached": True,
            }
        )

    from requests import RequestException as _RequestException
    from requests import Timeout as _RequestTimeout

    # Blocking network download + disk write — offload off the event loop.
    def _download():
        logger.info("[Paper:arXiv] Downloading PDF: %s", pdf_url)
        t0 = time.time()
        resp = None
        part_path = None
        try:
            resp = http_get(
                pdf_url,
                timeout=60,
                stream=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; TofuBot/1.0)"},
            )
            resp.raise_for_status()
            _declared_pdf_length(resp)
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" not in content_type and "octet-stream" not in content_type:
                logger.warning(
                    "[Paper:arXiv] Unexpected content type: %s for %s",
                    content_type,
                    pdf_url,
                )

            fd, part_path = _new_pdf_part(filepath)
            downloaded = 0
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > _paper_pdf_limit():
                        raise _PaperDownloadTooLarge(
                            f"PDF exceeds the {_paper_pdf_limit() // 1048576} MB limit"
                        )
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())

            pdf_bytes = _read_pdf_bounded(part_path)
            from lib.pdf_parser.text import validate_pdf_bytes as _validate_pdf_bytes

            _ok, _np, _verr = _validate_pdf_bytes(pdf_bytes)
            if not _ok:
                raise ValueError("Downloaded file is not a readable PDF: " + _verr)
            os.replace(part_path, filepath)
            part_path = None
        finally:
            if resp is not None:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
            if part_path is not None:
                try:
                    os.remove(part_path)
                except FileNotFoundError as e:
                    logger.debug("[Paper:arXiv] partial already absent: %s", e)
                except OSError as e:
                    logger.debug("[Paper:arXiv] partial cleanup failed: %s", e)

        size = len(pdf_bytes)
        if not size:
            try:
                os.remove(filepath)
            except OSError as e:
                logger.debug("[Paper:arXiv] empty-cache cleanup failed: %s", e)
            raise ValueError("Downloaded PDF body was empty")
        elapsed = time.time() - t0
        logger.info(
            "[Paper:arXiv] Downloaded %s: %d bytes in %.1fs", arxiv_id, size, elapsed
        )
        return size

    try:
        file_size = await asyncio.to_thread(_download)
        return api_ok(
            {
                "pdf_url": f"/api/paper/pdf/{filename}",
                "arxiv_id": arxiv_id,
                "file_size": file_size,
            }
        )

    except _PaperDownloadTooLarge as e:
        logger.warning("[Paper:arXiv] Oversized PDF for %s: %s", arxiv_id, e)
        return api_error(str(e), status=413)
    except ValueError as e:
        logger.warning("[Paper:arXiv] Rejected invalid PDF for %s: %s", arxiv_id, e)
        return api_bad_request(str(e))
    except _RequestTimeout:
        logger.warning("[Paper:arXiv] Download timeout (60s): %s", pdf_url)
        return api_error("Download timed out (60s)", status=504)
    except _RequestException as e:
        logger.warning("[Paper:arXiv] Download failed: %s — %s", pdf_url, e)
        return api_error(f"Download failed: {str(e)}", status=502)
    except OSError as e:
        logger.error(
            "[Paper:arXiv] Disk write failed for %s: %s", filepath, e, exc_info=True
        )
        return api_error("Could not store the downloaded PDF", status=507)


@paper_bp.route("/api/paper/fetch-arxiv-stream", methods=["POST"])
async def fetch_arxiv_stream():
    """Download PDF from arXiv and parse it — SSE stream of progress events.

    Body JSON:
        url: str — arXiv URL or ID

    SSE events (each one JSON on a ``data:`` line):
        {stage: 'resolve', arxiv_id: str, title: str, pdf_url: str}  — URL parsed
        {stage: 'download', downloaded: int, total: int}  — download progress
        {stage: 'download_done', file_size: int, elapsed: float}
        {stage: 'parse_start'}
        {stage: 'parse_done', total_pages: int, text_length: int, elapsed: float}
        {stage: 'done', ok: true, pdf_url: str, arxiv_id: str, title: str,
               parsed_text: str, total_pages: int, text_length: int, cached: bool}
        {stage: 'error', error: str}
    """
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    url_input = (data.get("url") or "").strip()
    # Client-generated bookshelf id — the server persists the library row itself
    # at the 'done' stage (server-authoritative ingest) so a fetched paper
    # survives a tab-close/refresh that races the client PUT.
    client_paper_id = (data.get("paper_id") or "").strip()
    if not url_input:
        logger.warning("[Paper:arXiv:Stream] Fetch request with no URL")
        return api_bad_request("No URL provided")

    arxiv_id = _extract_arxiv_id(url_input)
    if not arxiv_id:
        logger.warning(
            "[Paper:arXiv:Stream] Could not parse arXiv ID from: %.200s", url_input
        )
        return api_bad_request("Could not parse arXiv ID from URL")

    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    filename = f"arxiv_{arxiv_id.replace('/', '_')}.pdf"
    filepath = os.path.join(PAPER_DIR, filename)

    def _sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        from requests import RequestException as _RequestException
        from requests import Timeout as _RequestTimeout

        # SSE padding: flush proxy/gateway buffers (VSCode port-forward, nginx, etc.)
        # so the first real event reaches the client immediately. Without this,
        # small events (~60B each) get buffered and the UI appears stuck on the
        # initial 'resolve' state until the buffer fills. See also trading_brain.py.
        yield ":" + (" " * 2048) + "\n\n"
        yield ":" + (" " * 2048) + "\n\n"
        # Resolve the real paper title up front so the UI can label the
        # paper by title instead of the bare arXiv ID. Best-effort: an empty
        # string just falls back to "arXiv:<id>" on the client.
        paper_title = fetch_arxiv_title(arxiv_id)
        yield _sse(
            {
                "stage": "resolve",
                "arxiv_id": arxiv_id,
                "title": paper_title,
                "pdf_url": f"/api/paper/pdf/{filename}",
            }
        )

        # ── Step 1: Download PDF (cached or fresh) ──
        pdf_bytes = None
        cached = False
        try:
            pdf_bytes = _load_valid_cached_pdf(filepath)
            if pdf_bytes is not None:
                cached = True
                file_size = len(pdf_bytes)
                logger.info(
                    "[Paper:arXiv:Stream] Cache hit for %s — %d bytes",
                    arxiv_id,
                    file_size,
                )
                yield _sse(
                    {
                        "stage": "download_done",
                        "file_size": file_size,
                        "elapsed": 0.0,
                        "cached": True,
                    }
                )
            else:
                logger.info("[Paper:arXiv:Stream] Downloading PDF: %s", pdf_url)
                t0 = time.time()
                resp = None
                part_path = None
                try:
                    resp = http_get(
                        pdf_url,
                        timeout=60,
                        stream=True,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; TofuBot/1.0)"},
                    )
                    resp.raise_for_status()
                    total = _declared_pdf_length(resp)
                    content_type = resp.headers.get("Content-Type", "")
                    if "pdf" not in content_type and "octet-stream" not in content_type:
                        logger.warning(
                            "[Paper:arXiv:Stream] Unexpected content type: %s for %s",
                            content_type,
                            pdf_url,
                        )

                    downloaded = 0
                    last_progress_ts = 0.0
                    fd, part_path = _new_pdf_part(filepath)
                    with os.fdopen(fd, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > _paper_pdf_limit():
                                raise _PaperDownloadTooLarge(
                                    f"PDF exceeds the {_paper_pdf_limit() // 1048576} MB limit"
                                )
                            f.write(chunk)
                            # Emit at most ~10 progress events per second
                            now = time.time()
                            if now - last_progress_ts >= 0.1:
                                last_progress_ts = now
                                yield _sse(
                                    {
                                        "stage": "download",
                                        "downloaded": downloaded,
                                        "total": total,
                                    }
                                )
                        f.flush()
                        os.fsync(f.fileno())
                    pdf_bytes = _read_pdf_bounded(part_path)
                    from lib.pdf_parser.text import validate_pdf_bytes as _validate_pdf_bytes

                    _ok, _np, _verr = _validate_pdf_bytes(pdf_bytes)
                    if not _ok:
                        raise ValueError(
                            "Downloaded file is not a readable PDF (truncated or "
                            "corrupted): " + _verr
                        )
                    os.replace(part_path, filepath)
                    part_path = None
                finally:
                    if resp is not None:
                        close = getattr(resp, "close", None)
                        if callable(close):
                            close()
                    if part_path is not None:
                        try:
                            os.remove(part_path)
                        except FileNotFoundError as e:
                            logger.debug(
                                "[Paper:arXiv:Stream] partial already absent: %s", e
                            )
                        except OSError as e:
                            logger.debug(
                                "[Paper:arXiv:Stream] partial cleanup failed: %s", e
                            )

                file_size = len(pdf_bytes)
                elapsed = time.time() - t0
                logger.info(
                    "[Paper:arXiv:Stream] Downloaded %s: %d bytes in %.1fs",
                    arxiv_id,
                    file_size,
                    elapsed,
                )
                yield _sse(
                    {
                        "stage": "download_done",
                        "file_size": file_size,
                        "elapsed": round(elapsed, 2),
                        "cached": False,
                    }
                )
        except _PaperDownloadTooLarge as e:
            logger.warning("[Paper:arXiv:Stream] Oversized PDF for %s: %s", arxiv_id, e)
            yield _sse({"stage": "error", "error": str(e)})
            return
        except ValueError as e:
            logger.warning("[Paper:arXiv:Stream] Rejected PDF for %s: %s", arxiv_id, e)
            yield _sse({"stage": "error", "error": str(e)})
            return
        except _RequestTimeout:
            logger.warning("[Paper:arXiv:Stream] Download timeout (60s): %s", pdf_url)
            yield _sse({"stage": "error", "error": "Download timed out (60s)"})
            return
        except _RequestException as e:
            logger.warning("[Paper:arXiv:Stream] Download failed: %s — %s", pdf_url, e)
            yield _sse({"stage": "error", "error": f"Download failed: {e}"})
            return
        except OSError as e:
            logger.error(
                "[Paper:arXiv:Stream] Disk write failed for %s: %s",
                filepath,
                e,
                exc_info=True,
            )
            yield _sse({"stage": "error", "error": f"Disk write failed: {e}"})
            return

        # ── Step 2: Parse PDF text on server (no second client round-trip) ──
        if not pdf_bytes:
            logger.warning(
                "[Paper:arXiv:Stream] No PDF bytes after download for %s", arxiv_id
            )
            yield _sse({"stage": "error", "error": "PDF body was empty after download"})
            return

        yield _sse({"stage": "parse_start"})
        try:
            from lib.pdf_parser.core import parse_pdf as _parse_pdf
            import queue as _queue
            import threading as _threading

            # Run the blocking parse in a worker thread and bridge its
            # per-page progress callback to SSE events via a queue. This
            # turns pymupdf4llm's opaque multi-second call into a
            # streaming "page N/M" progress bar in the UI.
            progress_q: "_queue.Queue" = _queue.Queue()
            result_holder = {"result": None, "exception": None}

            def _on_progress(stage, done, total):
                progress_q.put(("progress", stage, done, total))

            def _worker():
                try:
                    result_holder["result"] = _parse_pdf(
                        pdf_bytes,
                        max_text_chars=0,
                        max_images=0,
                        progress_callback=_on_progress,
                    )
                except Exception as ex:
                    # Surface the failure via shared state — parent thread
                    # logs and re-raises with full context.
                    logger.debug("[Paper] PDF parse worker captured exception: %s", ex)
                    result_holder["exception"] = ex
                finally:
                    progress_q.put(("done", None, None, None))

            t0 = time.time()
            worker = _threading.Thread(
                target=_worker, name=f"pdf-parse-{arxiv_id}", daemon=True
            )
            worker.start()

            last_emit = 0.0
            last_done = -1
            while True:
                try:
                    msg = progress_q.get(timeout=1.0)
                except _queue.Empty:
                    # Heartbeat comment — keeps connection alive through
                    # proxies during a long silent stretch.
                    yield ":hb\n\n"
                    continue
                kind = msg[0]
                if kind == "done":
                    break
                _, stage, done, total = msg
                # Throttle: emit at most ~10 events/sec, but always emit
                # the first and last page.
                now = time.time()
                is_last = total and done >= total
                if (
                    now - last_emit >= 0.1 or is_last or last_done < 0
                ) and done != last_done:
                    last_emit = now
                    last_done = done
                    yield _sse(
                        {
                            "stage": "parse_progress",
                            "parse_stage": stage,
                            "page": done,
                            "total_pages": total,
                        }
                    )

            worker.join(timeout=5.0)
            if result_holder["exception"] is not None:
                raise result_holder["exception"]
            result = result_holder["result"] or {}
            elapsed = time.time() - t0
            parsed_text = result.get("text") or ""
            total_pages = result.get("totalPages", 0)
            text_length = result.get("textLength", len(parsed_text))
            logger.info(
                "[Paper:arXiv:Stream] Parsed %s — %d pages, %d chars in %.1fs",
                arxiv_id,
                total_pages,
                text_length,
                elapsed,
            )
            yield _sse(
                {
                    "stage": "parse_done",
                    "total_pages": total_pages,
                    "text_length": text_length,
                    "elapsed": round(elapsed, 2),
                }
            )
        except Exception as e:
            # Parsing failed — still return the PDF URL so the viewer can render it,
            # but surface the error so the UI can warn the user.
            logger.error(
                "[Paper:arXiv:Stream] PDF parse failed for %s: %s",
                arxiv_id,
                e,
                exc_info=True,
            )
            yield _sse(
                {
                    "stage": "done",
                    "ok": True,
                    "pdf_url": f"/api/paper/pdf/{filename}",
                    "arxiv_id": arxiv_id,
                    "title": paper_title,
                    "parsed_text": "",
                    "total_pages": 0,
                    "text_length": 0,
                    "paper_hash": "",
                    "images": [],
                    "cached": cached,
                    "parse_error": f"PDF parse failed: {e}",
                }
            )
            return

        # ── Step 3: Extract figure/table images (server-side, before
        #     handing control back to the client — eliminates the race where
        #     the user clicks Report before background extraction finishes).
        phash = _paper_hash(parsed_text) if parsed_text else ""
        images = []
        if phash:
            yield _sse({"stage": "extract_start"})
            t_ex = time.time()
            try:
                images = extract_paper_figures(filepath, phash)
            except Exception as e:
                logger.warning("[Paper:arXiv:Stream] Image extraction failed: %s", e)
            yield _sse(
                {
                    "stage": "extract_done",
                    "images_count": len(images),
                    "elapsed": round(time.time() - t_ex, 2),
                }
            )

        # Server-authoritative persist: write the bookshelf row before handing
        # control back, so the fetched paper survives even if the client's PUT
        # never lands (tab closed mid-stream). Runs on the SSE generator thread.
        if client_paper_id:
            from lib.pdf_parser._common import current_parser_version as _cpv

            _persist_ingested_library_row(
                client_paper_id,
                user_id=owner_user_id,
                title=(paper_title or f"arXiv:{arxiv_id}"),
                pdf_url=f"/api/paper/pdf/{filename}",
                pdf_filename=filename,
                arxiv_id=arxiv_id,
                paper_hash=phash,
                parsed_text=parsed_text,
                images=images,
                page_count=total_pages,
                parser_version=(_cpv(result.get("extractor")) if parsed_text else ""),
            )

        # ── Done — return everything the client needs ──
        yield _sse(
            {
                "stage": "done",
                "ok": True,
                "pdf_url": f"/api/paper/pdf/{filename}",
                "arxiv_id": arxiv_id,
                "title": paper_title,
                "parsed_text": parsed_text,
                "total_pages": total_pages,
                "text_length": text_length,
                "paper_hash": phash,
                "images": images,
                "cached": cached,
            }
        )

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )
