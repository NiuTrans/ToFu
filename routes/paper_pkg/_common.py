"""Shared paper-route blueprint, PDF guards, and artifact-cache projections."""

import asyncio
import os
import tempfile
from collections.abc import Mapping

from quart import Blueprint


from lib.log import get_logger
from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport
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


class _PaperDownloadTooLarge(ValueError):
    pass


class _PaperInvalidPDF(ValueError):
    pass


def _paper_pdf_limit() -> int:
    # Lazy import keeps the PDF engine out of the ordinary server import path.
    from lib.pdf_parser._common import MAX_PDF_BYTES

    return MAX_PDF_BYTES


def _declared_pdf_length(resp) -> int:
    """Parse and enforce Content-Length; streamed bytes are checked again."""
    try:
        declared = int(resp.headers.get("Content-Length") or 0)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.debug("[Paper] invalid PDF Content-Length ignored: %s", exc)
        declared = 0
    if declared > _paper_pdf_limit():
        raise _PaperDownloadTooLarge(
            f"PDF exceeds the {_paper_pdf_limit() // 1048576} MB limit"
        )
    return max(0, declared)


def _new_pdf_part(filepath: str):
    directory = os.path.dirname(filepath)
    os.makedirs(directory, exist_ok=True)
    return tempfile.mkstemp(
        prefix=f".{os.path.basename(filepath)}.", suffix=".part", dir=directory
    )


def _read_pdf_bounded(filepath: str) -> bytes:
    limit = _paper_pdf_limit()
    with open(filepath, "rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise _PaperDownloadTooLarge(f"PDF exceeds the {limit // 1048576} MB limit")
    return data


def _store_uploaded_pdf_atomic(stream, filepath: str) -> bytes:
    """Bound, validate and atomically publish one uploaded PDF.

    The multipart parser may spool the incoming body, but the route must not
    turn that into an unbounded second in-memory copy. Stream into a same-dir
    staging file, enforce the parser's byte ceiling independently of request
    headers, then materialize exactly one bounded byte string for validation
    and parsing. A disconnect, invalid document, ENOSPC or parser rejection
    never leaves a partially-written final cache entry.
    """
    limit = _paper_pdf_limit()
    fd, part_path = _new_pdf_part(filepath)
    published = False
    try:
        total = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _PaperDownloadTooLarge(
                        f"PDF exceeds the {limit // 1048576} MB limit"
                    )
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())

        pdf_bytes = _read_pdf_bounded(part_path)
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
        pdf_bytes = _read_pdf_bounded(filepath)
        from lib.pdf_parser.text import validate_pdf_bytes

        valid, _pages, error = validate_pdf_bytes(pdf_bytes)
        if valid:
            return pdf_bytes
        logger.warning("[Paper:arXiv] Discarding invalid cache %s: %s", filepath, error)
    except (OSError, _PaperDownloadTooLarge) as exc:
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


def _parse_insight_row_meta(row):
    """Return the Sidecar-decoded insight metadata document."""
    return _parse_report_meta(row)


def _report_reopen_sibling_groups(lang, fallback_lang=None):
    """Map every selectable plain base language to its persisted companions."""
    sibling_groups = {}
    for candidate in (lang, fallback_lang):
        if not candidate or is_review_family(candidate):
            continue
        sibling_groups[candidate] = report_reopen_sibling_langs(
            parse_report_lang(candidate)["ui_lang"])
    return sibling_groups


async def _cached_insight_row(phash, lang, *, user_id: int):
    """Read the one sibling insight row used by both reopen projections."""
    if is_review_family(lang):
        return None, "", ""
    ui_lang = parse_report_lang(lang)["ui_lang"]
    try:
        cache_key = insight_lang_key(ui_lang)
        row = await asyncio.to_thread(
            PaperArtifactRepository(user_id).get_report,
            phash,
            cache_key,
        )
    except Exception as e:
        logger.warning(
            "[Paper:Report] Cached-insight lookup failed hash=%s: %s", phash, e
        )
        return None, ui_lang, ""
    return row, ui_lang, cache_key


def _structured_insight_payload(row, ui_lang):
    if not row or not row.report:
        return None
    meta = _parse_insight_row_meta(row)
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


async def _enrich_cached_insight(body, phash, lang, *, user_id: int):
    """Return ``(body, structured_payload)`` from one sibling-row read."""
    row, ui_lang, cache_key = await _cached_insight_row(
        phash, lang, user_id=user_id)
    if cache_key:
        return _project_cached_insight(
            body, row, phash=phash, ui_lang=ui_lang)
    return body, None


async def _load_cached_insight_payload(phash, lang, *, user_id: int):
    """Load the STRUCTURED insight payload for a v2 row, else None.

    v2 rows (meta carries grounded ``items`` with resolved ``anchor_idx``) are
    served as a separate payload so the reader can distribute anchored cards
    (design §3.2) — their markdown is NOT merged into the report body. v1
    rows (legacy, meta without items) return None here and keep the merged
    read path in ``_append_cached_insight``.
    """
    row, ui_lang, _cache_key = await _cached_insight_row(
        phash, lang, user_id=user_id)
    return _structured_insight_payload(row, ui_lang)


async def _load_cached_checkpoints_payload(phash, lang, *, user_id: int):
    """Load the persisted checkpoint items for this paper+lang, else None.

    Read-path only — never regenerates. The frontend distributes flip cards
    from the structured items; nothing merges into the report body.
    """
    if is_review_family(lang):
        return None
    ui_lang = parse_report_lang(lang)["ui_lang"]
    try:
        row = await asyncio.to_thread(
            PaperArtifactRepository(user_id).get_report,
            phash,
            checkpoints_lang_key(ui_lang),
        )
    except Exception as e:
        logger.warning(
            "[Paper:Report] Cached-checkpoints lookup failed hash=%s: %s", phash, e
        )
        return None
    return _project_cached_checkpoints(row, ui_lang=ui_lang)


def _project_cached_checkpoints(row, *, ui_lang):
    """Project one prefetched checkpoint row without another storage read."""
    meta = _parse_insight_row_meta(row) if row else None
    if not isinstance(meta, dict) or not isinstance(meta.get("items"), list):
        return None
    return {"items": meta["items"], "lang": ui_lang}


async def _append_cached_insight(body, phash, lang, *, user_id: int):
    """Merge the sibling persisted ``insight:<ui>`` row into a cached report body.

    Read-path only — NEVER triggers a new insight generation. When a plain
    report is served from the DB cache, look up the separately-persisted insight
    section (key ``insight:<ui_lang>``) and append its markdown so a reopened
    paper shows the insight the reader generated earlier, instead of it silently
    vanishing until a forced regenerate.

    Guards (so this is byte-identical to today for papers without an insight):
      * skips Review Mode entirely (insight is only produced for plain reports);
      * no-op when no insight row exists / it is empty;
      * never double-appends if ``body`` already contains the section (a cache
        row that was persisted with the insight baked in, or a re-entry);
      * v2 rows (structured items in meta) are NOT merged — the reader gets
        them via ``_load_cached_insight_payload`` and distributes anchored
        cards client-side.
    """
    ins_row, _ui_lang, cache_key = await _cached_insight_row(
        phash, lang, user_id=user_id)
    # v2 row → served as structured payload, never merged (see above).
    _meta = _parse_insight_row_meta(ins_row)
    if isinstance(_meta, dict) and isinstance(_meta.get("items"), dict):
        return body
    # Match the exact header line, not a bare emoji/prefix that can occur in a
    # normal report heading (the P0 fixture covers that historical collision).
    return _append_legacy_insight(
        body, ins_row, phash=phash, cache_key=cache_key)


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
