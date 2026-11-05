"""Shared paper-route blueprint, PDF guards, and artifact-cache projections."""

import os
import tempfile
from collections.abc import Mapping

from quart import Blueprint


from lib.log import get_logger
from lib.paper.artifact_repository import PaperReport
from lib.paper.report_artifact_keys import (
    checkpoints_lang_key,
    insight_lang_key,
    report_reopen_sibling_langs,
    termfill_lang_key,
)
from lib.paper.review import is_review_family, parse_report_lang

logger = get_logger(__name__)

def _task_poll_fields(runtime, task_id: str, cursor: int) -> dict:
    """Project the unified replay page onto paper poll responses."""
    # Preserve the producer-owned page verbatim. Domain poll handlers only add
    # their terminal snapshot fields; they must not maintain a second list of
    # replay metadata (clocks, correlation IDs, cursor reset information).
    return dict(runtime.poll(task_id, cursor))


class _PaperInvalidPDF(ValueError):
    pass


def _declared_pdf_length(resp) -> int:
    """Parse advisory progress length without reserving or refusing source bytes."""
    try:
        declared = int(resp.headers.get("Content-Length") or 0)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.debug("[Paper] invalid PDF Content-Length ignored: %s", exc)
        declared = 0
    return max(0, declared)


def _new_pdf_part(filepath: str):
    directory = os.path.dirname(filepath)
    os.makedirs(directory, exist_ok=True)
    return tempfile.mkstemp(
        prefix=f".{os.path.basename(filepath)}.", suffix=".part", dir=directory
    )


def _read_pdf_source(filepath: str) -> bytes:
    with open(filepath, "rb") as stream:
        return stream.read()


def _store_uploaded_pdf_atomic(stream, filepath: str) -> bytes:
    """Stream complete source into owned staging, validate, then publish atomically.

    The parser consumes a complete byte string; this is not constant parser RAM.
    Failed receive/write/validation never publishes a staging part.
    """
    fd, part_path = _new_pdf_part(filepath)
    published = False
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())

        pdf_bytes = _read_pdf_source(part_path)
        from lib.pdf_parser.text import validate_pdf_bytes

        valid, _pages, error = validate_pdf_bytes(pdf_bytes)
        if not valid:
            raise _PaperInvalidPDF(error or "invalid PDF")

        os.replace(part_path, filepath)
        published = True
        # Make the rename durable where the filesystem supports directory
        # fsync. Some FUSE implementations reject it; the file itself was
        # already fsynced, so that rejection is not an upload failure.
        try:
            dir_fd = os.open(os.path.dirname(filepath), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            logger.debug("[Paper:Upload] parent fsync unavailable: %s", exc)
        return pdf_bytes
    finally:
        if not published:
            try:
                os.remove(part_path)
            except FileNotFoundError as exc:
                logger.debug("[Paper:Upload] partial already absent: %s", exc)
            except OSError as exc:
                logger.debug("[Paper:Upload] partial cleanup failed: %s", exc)


def _load_valid_cached_pdf(filepath: str):
    """Return validated cache bytes, deleting a stale partial/corrupt file."""
    try:
        if not os.path.exists(filepath) or os.path.getsize(filepath) <= 1000:
            return None
        pdf_bytes = _read_pdf_source(filepath)
        from lib.pdf_parser.text import validate_pdf_bytes

        valid, _pages, error = validate_pdf_bytes(pdf_bytes)
        if valid:
            return pdf_bytes
        logger.warning("[Paper:arXiv] Discarding invalid cache %s: %s", filepath, error)
    except OSError as exc:
        logger.warning("[Paper:arXiv] Discarding unusable cache %s: %s", filepath, exc)
    try:
        os.remove(filepath)
    except FileNotFoundError as exc:
        logger.debug("[Paper:arXiv] invalid cache already absent: %s", exc)
    except OSError as exc:
        logger.debug("[Paper:arXiv] Invalid-cache cleanup failed: %s", exc)
    return None


paper_bp = Blueprint("paper", __name__)

from routes.api_v1.paper import (  # noqa: E402
    api_v1_paper_bp as api_v1_paper_bp,
)


# v1 blueprint for the JSON routes (the 5 carve-outs above stay on paper_bp).


def _parse_report_meta(row):
    """Return the Sidecar-decoded report metadata, if it is a document."""
    raw = row.meta if isinstance(row, PaperReport) else (
        row.get("meta") if isinstance(row, Mapping) else None)
    return dict(raw) if isinstance(raw, Mapping) else None


def _report_reopen_sibling_groups(lang, fallback_lang=None):
    """Map every selectable plain base language to its persisted companions."""
    sibling_groups = {}
    for candidate in (lang, fallback_lang):
        if not candidate or is_review_family(candidate):
            continue
        sibling_groups[candidate] = report_reopen_sibling_langs(
            parse_report_lang(candidate)["ui_lang"])
    return sibling_groups


def _structured_insight_payload(row, ui_lang):
    if not row or not row.report:
        return None
    meta = _parse_report_meta(row)
    if not isinstance(meta, dict) or not isinstance(meta.get("items"), dict):
        return None
    return {
        "items": meta["items"],
        "baseline": meta.get("baseline"),
        "usage": meta.get("usage"),
        "markdown": row.report,
        "lang": ui_lang,
    }


def _append_legacy_insight(body, row, *, phash, cache_key):
    if not row or not row.report:
        return body
    section = row.report.strip()
    if not section:
        return body
    header_line = section.splitlines()[0].strip()
    if header_line and header_line in body:
        return body
    logger.info(
        "[Paper:Report] Merged cached insight into reopened report — "
        "hash=%s key=%s (+%d chars)",
        phash,
        cache_key,
        len(section),
    )
    return body.rstrip() + "\n\n" + section + "\n"


def _project_cached_insight(body, row, *, phash, ui_lang):
    """Project one prefetched Insight row without another storage read."""
    payload = _structured_insight_payload(row, ui_lang)
    if payload:
        return body, payload
    return _append_legacy_insight(
        body, row, phash=phash, cache_key=insight_lang_key(ui_lang)), None


def _project_cached_checkpoints(row, *, ui_lang):
    """Project one prefetched checkpoint row without another storage read."""
    meta = _parse_report_meta(row) if row else None
    if not isinstance(meta, dict) or not isinstance(meta.get("items"), list):
        return None
    return {"items": meta["items"], "lang": ui_lang}


def _merge_prefetched_termfill(body, meta, row, *, phash, ui_lang):
    """Merge one prefetched termfill row without another storage read."""
    if not row or not row.report:
        return body, meta
    addendum = row.report.strip()
    if not addendum:
        return body, meta
    # The addendum's persistence is proof the glossary is now complete — drop the
    # stale warning card so a reopened report doesn't contradict its own glossary.
    if isinstance(meta, dict) and meta.get("terminologyAudit"):
        meta = dict(meta)
        meta["terminologyAudit"] = None
    header_line = addendum.splitlines()[0].strip() if addendum else ""
    if header_line and header_line in body:
        return body, meta  # already merged / baked in
    logger.info(
        "[Paper:Report] Merged cached termfill addendum into reopened report — "
        "hash=%s key=%s (+%d chars)",
        phash,
        termfill_lang_key(ui_lang),
        len(addendum),
    )
    return body.rstrip() + "\n\n" + addendum + "\n", meta


def _project_prefetched_report_siblings(body, meta, phash, lang, siblings):
    """Project all prefetched additive rows without further repository I/O."""
    if is_review_family(lang):
        return body, meta, None, None
    ui_lang = parse_report_lang(lang)["ui_lang"]
    body, insight = _project_cached_insight(
        body,
        siblings.get(insight_lang_key(ui_lang)),
        phash=phash,
        ui_lang=ui_lang,
    )
    body, meta = _merge_prefetched_termfill(
        body,
        meta,
        siblings.get(termfill_lang_key(ui_lang)),
        phash=phash,
        ui_lang=ui_lang,
    )
    checkpoints = _project_cached_checkpoints(
        siblings.get(checkpoints_lang_key(ui_lang)),
        ui_lang=ui_lang,
    )
    return body, meta, insight, checkpoints
