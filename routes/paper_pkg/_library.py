"""Paper routes — library ingestion, upload, CRUD, and broken-row pruning."""

import asyncio
import os
import uuid
import re
import time


from lib.quart_sync import request_files, request_form

from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
    api_payload,
    safe_route,
)
from lib.log import get_logger
from lib.identity import require_user_id
from lib.paper.images.figures import extract_paper_figures
from lib.paper.library import (
    _LIB_IMAGES_CAP,
    _LIB_PARSED_TEXT_CAP,
    _LIB_QA_HISTORY_CAP,
    _LIB_TITLE_CAP,
)
from lib.paper_identity import PAPER_DIR, _paper_hash
from lib.request_parser import async_parse_body
from lib.paper.library_repository import (
    PaperLibraryEntry,
    PaperLibraryRepository,
)
from routes.api_v1.auth import request_user_id

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    _PaperDownloadTooLarge,
    _PaperInvalidPDF,
    _store_uploaded_pdf_atomic,
    api_v1_paper_bp,
    paper_bp,
)


# A PDF at or above this size is assumed real and never re-opened during a
# listing (validating every large PDF on every list would be wasteful). Only
# small files — plausible truncation stubs like the 15-byte ``%PDF-1.4`` header
# — are validity-checked. Generous vs the ~15-byte stubs actually seen.
_GHOST_PDF_MAX_STUB_BYTES = 2048


def _is_ghost_library_row(paper):
    """A bookshelf row is a GHOST (non-viewable) when it has no usable PDF:
    an empty ``pdfFilename``, or a filename whose file is missing from
    PAPER_DIR. Left by the OLD fire-and-forget persistence (a client PUT that
    raced/replaced a failed upload). A transient stat error (FUSE hiccup) is
    treated as NOT-ghost so a real paper is never hidden by a flaky mount.

    EXCEPTION — a saved *recommendation* is a legitimate empty-PDF row: it has
    no ``pdfFilename`` yet (never ingested) but carries an ``arxivId``, which
    makes it re-openable via lazy ingest. Keep it, otherwise the auto-persisted
    describe-to-recommend cards would silently vanish on reload.
    """
    fn = (paper.get("pdfFilename") or "").strip()
    if not fn:
        if (paper.get("arxivId") or "").strip():
            return False
        return True
    try:
        path = os.path.join(PAPER_DIR, os.path.basename(fn))
        if not os.path.exists(path):
            return True
        # File is present — but a truncated / aborted upload leaves a stub
        # (e.g. a 15-byte ``%PDF-1.4`` header) that EXISTS yet is not an
        # openable PDF. Such a row dead-ends the reader on "load a PDF first",
        # so treat a present-but-unopenable PDF as a ghost too. Only stubs small
        # enough to be a plausible truncation are validated (a large real PDF is
        # never re-opened on every listing — that would be needless work and a
        # transient FUSE read error must not hide a real paper).
        try:
            size = os.path.getsize(path)
        except OSError:
            return False  # transient stat error — never hide a real paper
        if size < _GHOST_PDF_MAX_STUB_BYTES:
            from lib.pdf_parser.text import validate_pdf_bytes

            with open(path, "rb") as f:
                ok, _pages, _err = validate_pdf_bytes(f.read())
            if not ok:
                logger.debug(
                    "[Paper:Library] row %s has a present-but-invalid PDF "
                    "(%d bytes) — treating as ghost",
                    (paper.get("id") or "")[:16],
                    size,
                )
                return True
        return False
    except OSError as e:
        logger.debug(
            "[Paper:Library] pdf existence check failed for %s: %s",
            (paper.get("id") or "")[:16],
            e,
        )
        return False


def _is_broken_stub_row(paper):
    """A row that is DEFINITIVELY broken and safe to hard-delete: its
    ``pdfFilename`` points at a file that is PRESENT on disk, small, and fails
    ``validate_pdf_bytes`` (a truncated / aborted-upload stub — e.g. the 15-byte
    ``%PDF-1.4`` header). Deliberately NARROWER than ``_is_ghost_library_row``:
    it does NOT include a MISSING file (which can be a transient FUSE hiccup) nor
    an empty-pdfFilename recommendation row — only a proven-unopenable file. Used
    by the opt-in prune endpoint so a destructive cleanup can never remove a row
    that might still be a real (transiently-unreachable) paper.
    """
    fn = (paper.get("pdfFilename") or "").strip()
    if not fn:
        return False
    try:
        path = os.path.join(PAPER_DIR, os.path.basename(fn))
        if not os.path.exists(path):
            return False  # missing != broken (could be a flaky mount)
        size = os.path.getsize(path)
        if size >= _GHOST_PDF_MAX_STUB_BYTES:
            return False  # a large file is not a truncation stub
        from lib.pdf_parser.text import validate_pdf_bytes

        with open(path, "rb") as f:
            ok, _pages, _err = validate_pdf_bytes(f.read())
        return not ok
    except OSError as e:
        logger.debug(
            "[Paper:Library] stub check failed for %s: %s",
            (paper.get("id") or "")[:16],
            e,
        )
        return False


def _persist_ingested_library_row(
    paper_id,
    *,
    user_id,
    title,
    pdf_url,
    pdf_filename,
    arxiv_id,
    paper_hash,
    parsed_text,
    images,
    page_count,
    parser_version="",
):
    r"""Create/refresh a ``paper_library`` row at INGEST time (server-authoritative).

    The ingestion endpoints (``/api/paper/upload``, ``/api/paper/fetch-arxiv-stream``)
    already hold every server-derived column, so they persist the bookshelf row
    THEMSELVES rather than relying on the client's fire-and-forget PUT. This is
    what makes an uploaded/fetched paper survive a tab-close / refresh that races
    (or never fires) the client save — the durable fix for the ``qa=0 imgs=0``
    ghost-row / vanishing-paper bug.

    Preserves an existing row's ``created_at`` / ``qa_history`` / ``babel_cache``
    (a rare re-ingest of the same id must not wipe the user's Q&A). Best-effort:
    logs and returns False on failure, never raises into the ingest path.

    Args:
        paper_id: client-generated bookshelf id (``[\w.\-]{1,128}``).
        user_id: authenticated positive owner; never inferred in this service.
    Returns:
        True on a successful write, else False.
    """
    owner_user_id = require_user_id(
        user_id, context="paper library ingest persist")
    paper_id = (paper_id or "").strip()
    if not paper_id or len(paper_id) > 128 or not re.fullmatch(r"[\w.\-]+", paper_id):
        logger.warning(
            "[Paper:Ingest] Skip library persist — bad paper_id: %.60s", paper_id
        )
        return False
    now_ms = int(time.time() * 1000)
    try:
        repository = PaperLibraryRepository(owner_user_id)
        existing = repository.get(paper_id)
        entry = PaperLibraryEntry(
            paper_id=paper_id,
            title=(title or '')[:_LIB_TITLE_CAP],
            pdf_url=(pdf_url or '')[:2000],
            pdf_filename=os.path.basename(pdf_filename or '')[:500],
            arxiv_id=(arxiv_id or '')[:64],
            paper_hash=(paper_hash or '')[:64],
            parsed_text=(parsed_text or '')[:_LIB_PARSED_TEXT_CAP],
            parser_version=(parser_version or '')[:128],
            qa_history=list(existing.qa_history) if existing else [],
            images=(images[:_LIB_IMAGES_CAP]
                    if isinstance(images, list) else []),
            babel_cache=dict(existing.babel_cache) if existing else {},
            page_count=int(page_count or 0),
            folder_id=existing.folder_id if existing else '',
            created_at=existing.created_at if existing else now_ms,
            updated_at=now_ms,
            has_report=existing.has_report if existing else False,
        )
        saved = repository.put(
            entry,
            command_id=(
                f'paper-library-ingest:{owner_user_id}:{paper_id}:{now_ms}'
            ),
        )
        logger.info(
            '[Paper:Ingest] Persisted library row %s — hash=%s imgs=%d',
            paper_id[:16], (paper_hash or '')[:12], len(entry.images),
        )
        return saved
    except Exception as e:
        logger.error(
            "[Paper:Ingest] Library persist failed for %s: %s",
            paper_id[:16],
            e,
            exc_info=True,
        )
        return False


@paper_bp.route("/api/paper/upload", methods=["POST"])
def upload_paper():
    """Upload a PDF and run the full server-side ingestion pipeline.

    Single round-trip: save PDF → parse text → extract figures →
    return everything the frontend needs to populate library state.

    This remains synchronous because PDF parsing and figure extraction are
    blocking work. The multipart body crosses Quart's async boundary through
    the explicit ``request_files`` helper while Quart runs this handler in its
    executor.

    Returns:
        {
            ok: true,
            pdf_url: str,
            filename: str,
            file_size: int,
            parsed_text: str,
            total_pages: int,
            text_length: int,
            paper_hash: str,
            images: [{url, caption, page, source, width, height}],
            parse_error: str (only on parse failure — PDF is still served)
        }
    """
    owner_user_id = int(request_user_id())
    files = request_files()
    if "file" not in files:
        logger.warning("[Paper:Upload] No file in request")
        return api_bad_request("No file")
    file = files["file"]
    if not file.filename:
        logger.warning("[Paper:Upload] Empty filename")
        return api_bad_request("No filename")
    if not file.filename.lower().endswith(".pdf"):
        logger.warning("[Paper:Upload] Non-PDF file rejected: %s", file.filename)
        return api_bad_request("Only PDF files are supported")

    original_name = file.filename
    # Client-generated bookshelf id — the server persists the library row itself
    # (server-authoritative ingest), so a paper survives even if the client's
    # PUT never lands. Absent → skip persist (back-compat) but still serve.
    client_paper_id = (request_form().get("paper_id") or "").strip()
    # Cap the user-controlled stem below NAME_MAX and add entropy: two parallel
    # uploads of the same file in one millisecond must never overwrite each
    # other. Keep the original name only as display metadata.
    _safe_stem = re.sub(r"[^\w\-.]", "_", os.path.splitext(original_name)[0])
    _safe_stem = (_safe_stem or "paper")[:160]
    filename = f"{int(time.time() * 1000)}_{os.urandom(4).hex()}_{_safe_stem}.pdf"
    filepath = os.path.join(PAPER_DIR, filename)

    try:
        # NOTE: Quart's FileStorage.save is an async coroutine. This is a SYNC
        # handler (see docstring), so consume its sync-safe stream explicitly.
        # The helper bounds the stream and publishes only after PDF validation.
        file.stream.seek(0)
        pdf_bytes = _store_uploaded_pdf_atomic(file.stream, filepath)
        file_size = len(pdf_bytes)
        logger.info(
            "[Paper:Upload] Saved: %s (%d bytes) — original=%s",
            filename,
            file_size,
            original_name,
        )
    except _PaperDownloadTooLarge as e:
        logger.warning("[Paper:Upload] Rejected oversized %s: %s", original_name, e)
        return api_error(str(e), status=413)
    except _PaperInvalidPDF as e:
        logger.warning("[Paper:Upload] Rejected invalid PDF %s: %s", original_name, e)
        return api_bad_request(
            "The uploaded file is not a readable PDF (it may be truncated or "
            "corrupted). Please re-upload. [" + str(e) + "]"
        )
    except OSError as e:
        logger.error(
            "[Paper:Upload] Failed to store %s: %s", filename, e, exc_info=True
        )
        return api_error("Could not store the uploaded PDF", status=507)
    except Exception as e:
        logger.error("[Paper:Upload] Failed to save %s: %s", filename, e, exc_info=True)
        return api_internal_error(f"Upload failed: {str(e)}")

    parsed_text = ""
    total_pages = 0
    text_length = 0
    parse_error = ""
    try:
        from lib.pdf_parser.core import parse_pdf as _parse_pdf

        t0 = time.time()
        result = _parse_pdf(pdf_bytes, max_text_chars=0, max_images=0)
        parsed_text = result.get("text") or ""
        total_pages = result.get("totalPages", 0)
        text_length = result.get("textLength", len(parsed_text))
        logger.info(
            "[Paper:Upload] Parsed %s — %d pages, %d chars in %.1fs",
            filename,
            total_pages,
            text_length,
            time.time() - t0,
        )
    except Exception as e:
        logger.warning(
            "[Paper:Upload] PDF parse failed for %s: %s", filename, e, exc_info=True
        )
        parse_error = f"PDF parse failed: {e}"

    phash = _paper_hash(parsed_text) if parsed_text else ""
    images = extract_paper_figures(filepath, phash) if phash else []

    # Server-authoritative persist: the PDF saved fine, so the paper is real —
    # write the bookshelf row NOW (don't wait on the client's PUT). The PDF is
    # viewable even when parsing failed, so we persist regardless of parse_error.
    if client_paper_id:
        from lib.pdf_parser._common import current_parser_version as _cpv

        _persist_ingested_library_row(
            client_paper_id,
            user_id=owner_user_id,
            title=original_name,
            pdf_url=f"/api/paper/pdf/{filename}",
            pdf_filename=filename,
            arxiv_id="",
            paper_hash=phash,
            parsed_text=parsed_text,
            images=images,
            page_count=total_pages,
            parser_version=(_cpv(result.get("extractor")) if parsed_text else ""),
        )

    resp = {
        "ok": True,
        "id": client_paper_id,
        "pdf_url": f"/api/paper/pdf/{filename}",
        "filename": filename,
        "file_size": file_size,
        "parsed_text": parsed_text,
        "total_pages": total_pages,
        "text_length": text_length,
        "paper_hash": phash,
        "images": images,
    }
    if parse_error:
        resp["parse_error"] = parse_error
    return api_payload(resp, 200)


# ══════════════════════════════════════════════════════
#  Paper Library — server-side bookshelf
# ══════════════════════════════════════════════════════


@api_v1_paper_bp.route("/api/v1/paper/library", methods=["GET"])
@safe_route
async def list_library():
    """Return the current owner's viewable bookshelf entries, newest first."""
    owner_user_id = int(request_user_id())
    repository = PaperLibraryRepository(owner_user_id)
    entries = await asyncio.to_thread(repository.list_entries)
    papers = [
        entry.to_projection()
        for entry in entries
        if not _is_ghost_library_row(entry.to_projection())
    ]
    hidden_count = len(entries) - len(papers)
    if hidden_count:
        logger.info(
            "[Paper:Library] Hid %d entry(s) whose PDF is not currently "
            "viewable; storage rows were left untouched",
            hidden_count,
        )
    return api_ok({"papers": papers})


@api_v1_paper_bp.route("/api/v1/paper/library/<paper_id>", methods=["PUT"])
@safe_route
async def upsert_library_entry(paper_id):
    """Create or update one owner-scoped bookshelf entry.

    Omitted mutable fields preserve their stored value. An explicitly empty
    folderId removes the folder assignment.
    """
    owner_user_id = int(request_user_id())
    paper_id = (paper_id or "").strip()
    if not paper_id or len(paper_id) > 128 or not re.fullmatch(r"[\w.\-]+", paper_id):
        logger.warning("[Paper:Library] Upsert rejected bad id: %.60s", paper_id)
        return api_bad_request("invalid id")

    data = await async_parse_body()
    now_ms = int(time.time() * 1000)
    repository = PaperLibraryRepository(owner_user_id)
    existing = await asyncio.to_thread(repository.get, paper_id)
    if existing is None:
        existing = PaperLibraryEntry(paper_id=paper_id)

    def _text(request_key, stored_value, *, maximum, basename=False):
        value = data.get(request_key)
        if value is None or value == "":
            value = stored_value
        value = str(value or "")
        if basename:
            value = os.path.basename(value)
        return value[:maximum]

    if "qaHistory" in data:
        qa_history = data.get("qaHistory")
        qa_history = qa_history if isinstance(qa_history, list) else []
        qa_history = qa_history[-_LIB_QA_HISTORY_CAP:]
    else:
        qa_history = list(existing.qa_history)

    if "images" in data:
        images = data.get("images")
        images = images if isinstance(images, list) else []
        images = images[:_LIB_IMAGES_CAP]
    else:
        images = list(existing.images)

    if "babelCache" in data:
        babel_cache = data.get("babelCache")
        babel_cache = babel_cache if isinstance(babel_cache, dict) else {}
    else:
        babel_cache = dict(existing.babel_cache)

    if "pageCount" in data:
        try:
            page_count = max(0, int(data.get("pageCount") or 0))
        except (TypeError, ValueError):
            return api_bad_request("pageCount must be an integer")
    else:
        page_count = existing.page_count

    if "createdAt" in data and not existing.created_at:
        try:
            created_at = max(0, int(data.get("createdAt") or now_ms))
        except (TypeError, ValueError):
            return api_bad_request("createdAt must be an integer")
    else:
        created_at = existing.created_at or now_ms

    folder_id = (
        str(data.get("folderId") or "")[:64]
        if "folderId" in data else existing.folder_id
    )
    parser_version = (
        str(data.get("parserVersion") or "")[:128]
        if "parserVersion" in data else existing.parser_version
    )
    entry = PaperLibraryEntry(
        paper_id=paper_id,
        title=_text("title", existing.title, maximum=_LIB_TITLE_CAP),
        pdf_url=_text("pdfUrl", existing.pdf_url, maximum=2000),
        pdf_filename=_text(
            "pdfFilename", existing.pdf_filename, maximum=500, basename=True,
        ),
        arxiv_id=_text("arxivId", existing.arxiv_id, maximum=64),
        paper_hash=_text("paperHash", existing.paper_hash, maximum=64),
        parsed_text=_text(
            "parsedText", existing.parsed_text, maximum=_LIB_PARSED_TEXT_CAP,
        ),
        parser_version=parser_version,
        qa_history=qa_history,
        images=images,
        babel_cache=babel_cache,
        page_count=page_count,
        folder_id=folder_id,
        created_at=created_at,
        updated_at=now_ms,
        has_report=existing.has_report,
    )
    saved = await asyncio.to_thread(
        repository.put,
        entry,
        command_id=(
            f"paper-library-put:{owner_user_id}:{paper_id}:{uuid.uuid4().hex}"
        ),
    )
    if not saved:
        return api_internal_error("Paper library write was not acknowledged")
    logger.info(
        "[Paper:Library] Upserted %s — title=%.60s qa=%d imgs=%d",
        paper_id[:16], entry.title, len(entry.qa_history), len(entry.images),
    )
    return api_ok({"id": paper_id, "updatedAt": now_ms})


@api_v1_paper_bp.route("/api/v1/paper/library/<paper_id>", methods=["DELETE"])
@safe_route
async def delete_library_entry(paper_id):
    """Remove one owned bookshelf entry without deleting shared PDF assets."""
    owner_user_id = int(request_user_id())
    paper_id = (paper_id or "").strip()
    if not paper_id:
        return api_bad_request("invalid id")

    repository = PaperLibraryRepository(owner_user_id)
    deleted = await asyncio.to_thread(
        repository.delete,
        paper_id,
        command_id=(
            f"paper-library-delete:{owner_user_id}:{paper_id}:{uuid.uuid4().hex}"
        ),
    )
    if not deleted:
        return api_not_found("not_found")
    logger.info("[Paper:Library] Deleted %s", paper_id[:16])
    return api_ok()


@api_v1_paper_bp.route("/api/v1/paper/library/prune-broken", methods=["POST"])
@safe_route
async def prune_broken_library_rows():
    """Delete rows whose local PDF is conclusively an invalid truncation stub.

    Missing files are never deleted from storage because a mount can be
    temporarily unavailable. The file removal is best-effort and happens only
    after the owner-scoped storage row is deleted.
    """
    owner_user_id = int(request_user_id())
    repository = PaperLibraryRepository(owner_user_id)
    entries = await asyncio.to_thread(repository.list_entries)
    pruned_ids = []
    for entry in entries:
        paper = entry.to_projection()
        if not _is_broken_stub_row(paper):
            continue
        deleted = await asyncio.to_thread(
            repository.delete,
            entry.paper_id,
            command_id=(
                f"paper-library-prune:{owner_user_id}:{entry.paper_id}:"
                f"{uuid.uuid4().hex}"
            ),
        )
        if not deleted:
            continue
        pruned_ids.append(entry.paper_id)
        filename = os.path.basename(entry.pdf_filename.strip())
        if filename:
            try:
                os.remove(os.path.join(PAPER_DIR, filename))
            except OSError as error:
                logger.debug(
                    "[Paper:Prune] stub file remove failed %s: %s",
                    filename,
                    error,
                )
    if pruned_ids:
        logger.info(
            "[Paper:Prune] Deleted %d invalid stub row(s)", len(pruned_ids))
    return api_ok({"pruned": len(pruned_ids), "ids": pruned_ids})
