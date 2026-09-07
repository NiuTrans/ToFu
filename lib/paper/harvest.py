"""lib/paper/harvest.py — batch crawl + parse-once ingest primitive (R1).

The FIRST of the auto-research recipe's two new primitives
(docs/modules/ingest_media.md §3 阶段 2 ``harvest``): given a list of
arXiv IDs, download + parse each paper ONCE and land it in the shared
``paper_library`` bookshelf, so every later stage (survey / ideate / typeset)
and the reading-mode UI reuse a single parsed copy instead of re-parsing.

WHY parse-once is a *correctness* property here, not just a cost win
--------------------------------------------------------------------
The entire "越用越省" story in the design note rests on ONE invariant:

    a paper harvested here MUST get the byte-identical ``paper_hash``
    (``phash``) it would get from reading-mode ingest.

If the two ingest paths normalized PDF text even slightly differently, the same
paper would hash two ways, "已读过 → 命中缓存" would silently miss, and every
"reuse" would actually re-parse at full cost while looking like a hit.

This module guarantees that identity **structurally, not by re-implementation**:
it computes the phash through the EXACT same two functions reading-mode ingest
uses —

    text  = lib.pdf_parser.parse_pdf(pdf_bytes)['text']   # same parser, same mode
    phash = lib.paper_identity._paper_hash(text)          # sole canonicalization

``_paper_hash`` is the single, already-tested canonicalization point (it
``strip()``s before ``sha256`` — see tests/test_paper_hash_canonical.py and the
identity-fork history in its docstring). Harvest does ZERO text normalization of
its own; adding any would be the exact bug the design note warns about. The
NEUTER in tests/test_paper_harvest.py proves that: inject a normalization step
and the cross-path phash identity assertion goes red.

Two-level dedup (both matter, for different reasons)
----------------------------------------------------
* **Pre-download, keyed on ``arxiv_id``**: if a live ``paper_library`` row
  already carries this ``arxiv_id`` WITH a non-empty ``parsed_text``, we skip
  the network download AND the parse entirely — this is the "already in the
  bookshelf (read it, or a prior harvest)" fast path. It is what makes a second
  harvest of an overlapping topic nearly free. Since 2026-07-27 the row's
  ``parser_version`` must also match ``expected_parser_version()`` — a row
  written by an older stack, by the raw fallback, or before the column existed
  is re-parsed, so a parser upgrade (or a healed extractor) invalidates
  naturally instead of freezing degraded text into the corpus forever.
* **Post-parse, keyed on ``phash``**: the content hash is the library's true
  identity; the upsert is keyed on it so a paper that arrived under a different
  ``arxiv_id`` alias (or with no id) still coalesces onto one row.

Everything is best-effort per paper: one bad download/parse is logged and
recorded as a failure in the result, never raised — a 40-paper harvest must not
die on paper #7. This mirrors the reading-mode ingest's own fail-open posture.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import uuid
from typing import Callable, Iterable, Optional

from lib.log import get_logger
from lib.paper.contracts import (
    PAPER_FANIN_MAX_PAPERS,
    PAPER_TITLE_BATCH_MAX_IDS,
    PAPER_TITLE_HINT_MAX_CHARS,
)

logger = get_logger(__name__)

__all__ = ['harvest_arxiv_id', 'harvest_arxiv_batch', 'HarvestResult']


# The harvest bookshelf id namespace. Reading-mode uses client-minted ids like
# ``paper_<ts>``; harvest mints deterministic ids from the arXiv id so a
# re-harvest of the same paper coalesces onto the same row (and so the id is
# reproducible for tests / crash-resume). Kept within the ``[\w.\-]{1,128}``
# shape _persist_ingested_library_row validates.
_HARVEST_ID_PREFIX = 'harvest_'


# A transient download/parse failure (arXiv timeout / rate-limit / flaky read)
# gets one retry with a short linear backoff — a real paper must not be
# permanently dropped and starve the survey corpus (R2/R3 seam finding). A
# permanent failure (invalid PDF, empty text) is not retried.
_HARVEST_FETCH_ATTEMPTS = 2
_HARVEST_RETRY_SLEEP = 3.0
_HARVEST_MAX_PAPERS = PAPER_FANIN_MAX_PAPERS
_HARVEST_PDF_SPOOL_MEMORY_BYTES = 1024 * 1024


def _harvest_paper_id(arxiv_id: str) -> str:
    """Deterministic bookshelf id for a harvested arXiv paper.

    ``harvest_<arxiv_id with / → _>`` — reproducible (same paper → same id →
    same row on re-harvest) and within the ingest id validator's charset.
    """
    safe = re.sub(r'[^\w.\-]', '_', (arxiv_id or '').strip())
    return f'{_HARVEST_ID_PREFIX}{safe}'


class HarvestResult:
    """Outcome of harvesting one paper. A small value object (not a dataclass
    to keep the module dependency-free and trivially JSON-projectable)."""

    __slots__ = ('arxiv_id', 'phash', 'status', 'title', 'page_count',
                 'text_length', 'paper_id', 'error', 'degraded')

    #: status ∈ {'parsed', 'cache_hit', 'error'} — 'parsed' = downloaded+parsed
    #: this run; 'cache_hit' = an existing library row was reused (no reparse).
    def __init__(self, arxiv_id: str, *, status: str, phash: str = '',
                 title: str = '', page_count: int = 0, text_length: int = 0,
                 paper_id: str = '', error: str = '', degraded: bool = False):
        self.arxiv_id = arxiv_id
        self.status = status
        self.phash = phash
        self.title = title
        self.page_count = page_count
        self.text_length = text_length
        self.paper_id = paper_id
        self.error = error
        #: True when the parse fell back to a LOWER-quality extractor than the
        #: environment's expected one (e.g. raw text because the markdown
        #: pipeline failed). The row is tagged with the raw parser_version, so
        #: it never counts as a markdown-corpus cache hit — but the flag must
        #: also be VISIBLE here, not only discoverable in the DB.
        self.degraded = degraded

    def to_dict(self) -> dict:
        return {
            'arxivId': self.arxiv_id, 'status': self.status, 'phash': self.phash,
            'title': self.title, 'pageCount': self.page_count,
            'textLength': self.text_length, 'paperId': self.paper_id,
            'error': self.error, 'degraded': self.degraded,
        }


class HarvestPDFTooLargeError(ValueError):
    """A deterministic PDF byte-budget rejection that must not be retried."""


def _harvest_pdf_byte_limit() -> int:
    """Request-loaded parser byte ceiling; patchable in focused tests."""
    from lib.pdf_parser._common import MAX_PDF_BYTES
    return MAX_PDF_BYTES


# ── Seams (monkeypatchable, same pattern as the recipe modules) ───────────
#
# Resolved lazily/through this module so a test can patch ``harvest._paper_hash``
# / ``harvest.parse_pdf`` / ``harvest._download_pdf_bytes`` and have the patch
# bite the whole chain. Importing the real ones at module top would bake the
# reference and defeat the NEUTER.

def _paper_hash(text: str) -> str:
    """The SOLE canonicalization point — reused verbatim from reading-mode
    ingest so a harvested paper's identity is byte-identical. Do NOT wrap this
    in any normalization here (that is the parse-once-breaking bug)."""
    from lib.paper_identity import _paper_hash as _ph
    return _ph(text)


def parse_pdf(pdf_bytes: bytes, **kw) -> dict:
    """The SAME parser reading-mode ingest calls. Defaults mirror the arXiv
    ingest path (``text_mode='rich'``); ``max_images=0`` because figure
    extraction is a separate, phash-keyed concern handled after the row lands."""
    from lib.pdf_parser.core import parse_pdf as _pp
    return _pp(pdf_bytes, max_text_chars=0, max_images=0, text_mode='rich', **kw)


def http_get(*args, **kwargs):
    """Request-loaded HTTP seam for bounded harvest transport tests."""
    from lib.http_client import http_get as _http_get
    return _http_get(*args, **kwargs)


def _download_pdf_bytes(arxiv_id: str, *, timeout: int = 60) -> bytes:
    """Download an arXiv PDF to memory. Raises on network / validity failure.

    Mirrors the reading-mode arXiv download: same URL shape, same UA, same
    ``validate_pdf_bytes`` gate so a truncated download is rejected rather than
    parsed into garbage (which would mint a garbage phash)."""
    from lib.pdf_parser.text import validate_pdf_bytes

    pdf_url = f'https://arxiv.org/pdf/{arxiv_id}.pdf'
    response = http_get(
        pdf_url, timeout=timeout, stream=True,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
    limit = _harvest_pdf_byte_limit()
    try:
        response.raise_for_status()
        try:
            declared = int(
                (getattr(response, 'headers', {}) or {}).get(
                    'Content-Length') or 0)
        except (TypeError, ValueError) as error:
            logger.debug(
                '[Paper:Harvest] invalid PDF Content-Length ignored: %s',
                error)
            declared = 0
        if declared > limit:
            raise HarvestPDFTooLargeError(
                f'PDF exceeds the {limit // 1048576} MiB limit')

        total = 0
        with tempfile.SpooledTemporaryFile(
                max_size=_HARVEST_PDF_SPOOL_MEMORY_BYTES,
                mode='w+b') as spool:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise HarvestPDFTooLargeError(
                        f'PDF exceeds the {limit // 1048576} MiB limit')
                spool.write(chunk)
            spool.seek(0)
            data = spool.read(limit + 1)
        if len(data) > limit:
            raise HarvestPDFTooLargeError(
                f'PDF exceeds the {limit // 1048576} MiB limit')
        ok, _pages, verr = validate_pdf_bytes(data)
        if not ok:
            raise ValueError(f'downloaded file is not a readable PDF: {verr}')
        return data
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            close()


# ── Library lookup / persist (reuse the ingest write contract) ────────────

def _existing_rows_for_arxiv_ids(arxiv_ids, user_id: int) -> Optional[dict]:
    """Return bounded current-parser hits with one owner-scoped projection."""
    from lib.paper.library_repository import PaperLibraryRepository
    from lib.pdf_parser._common import expected_parser_version

    try:
        entries = PaperLibraryRepository(user_id).by_arxiv_ids(arxiv_ids)
    except Exception as error:
        logger.warning(
            '[Paper:Harvest] batch cache probe failed: %s', error)
        return None
    parser_version = expected_parser_version()
    hits = {}
    for entry in entries:
        if not entry.arxiv_id or entry.arxiv_id in hits:
            continue
        if (
            entry.parsed_text_length <= 0
            or entry.parser_version != parser_version
        ):
            continue
        hits[entry.arxiv_id] = {
            'id': entry.paper_id,
            'paper_hash': entry.paper_hash,
            'title': entry.title,
            'text_length': entry.parsed_text_length,
            'page_count': entry.page_count,
        }
    return hits


def _existing_row_for_arxiv(arxiv_id: str, user_id: int) -> Optional[dict]:
    """Return a current-parser bookshelf hit without downloading the PDF."""
    if not arxiv_id:
        return None
    hits = _existing_rows_for_arxiv_ids([arxiv_id], user_id)
    return hits.get(arxiv_id) if hits is not None else None


def _cache_hit_result(arxiv_id: str, hit: dict) -> HarvestResult:
    logger.info('[Paper:Harvest] cache hit for %s — reusing row %s (hash=%s), '
                'no reparse', arxiv_id, (hit['id'] or '')[:24],
                (hit['paper_hash'] or '')[:12])
    text_length = hit.get('text_length')
    if not isinstance(text_length, int) or text_length < 0:
        text_length = len(hit.get('parsed_text') or '')
    return HarvestResult(
        arxiv_id, status='cache_hit', phash=hit['paper_hash'],
        title=hit['title'], page_count=hit['page_count'],
        text_length=text_length, paper_id=hit['id'])


def _persist_row(
    paper_id: str,
    *,
    title: str,
    arxiv_id: str,
    phash: str,
    parsed_text: str,
    page_count: int,
    images,
    folder_id: str,
    user_id: int,
    parser_version: str = '',
) -> bool:
    """Upsert one harvested paper through the owner-scoped repository."""
    from lib.paper.library import (
        _LIB_IMAGES_CAP,
        _LIB_PARSED_TEXT_CAP,
        _LIB_TITLE_CAP,
    )
    from lib.paper.library_repository import (
        PaperLibraryEntry,
        PaperLibraryRepository,
    )

    now_ms = int(time.time() * 1000)
    try:
        repository = PaperLibraryRepository(user_id)
        existing = repository.get(paper_id)
        filename = (
            f'arxiv_{re.sub(r"/", "_", arxiv_id)}.pdf' if arxiv_id else '')
        entry = PaperLibraryEntry(
            paper_id=paper_id,
            title=(title or '')[:_LIB_TITLE_CAP],
            pdf_url=f'/api/paper/pdf/{filename}' if filename else '',
            pdf_filename=filename[:500],
            arxiv_id=(arxiv_id or '')[:64],
            paper_hash=(phash or '')[:64],
            parsed_text=(parsed_text or '')[:_LIB_PARSED_TEXT_CAP],
            parser_version=(parser_version or '')[:128],
            qa_history=list(existing.qa_history) if existing else [],
            images=(
                images[:_LIB_IMAGES_CAP] if isinstance(images, list) else []),
            babel_cache=dict(existing.babel_cache) if existing else {},
            page_count=int(page_count or 0),
            folder_id=(folder_id or '')[:128],
            created_at=existing.created_at if existing else now_ms,
            updated_at=now_ms,
            has_report=existing.has_report if existing else False,
        )
        saved = repository.put(
            entry,
            command_id=(
                f'paper-harvest:{user_id}:{paper_id}:{uuid.uuid4().hex}'
            ),
        )
        if saved:
            logger.info(
                '[Paper:Harvest] persisted row %s — arxiv=%s hash=%s pages=%d',
                paper_id[:24], arxiv_id, (phash or '')[:12], page_count,
            )
        return saved
    except Exception as error:
        logger.error(
            '[Paper:Harvest] persist failed for %s: %s',
            paper_id[:24], error, exc_info=True,
        )
        return False


# ── Public API ────────────────────────────────────────────────────────────

def harvest_arxiv_id(arxiv_id: str, *, folder_id: str = '', user_id: int,
                     extract_figures: bool = False,
                     force_reparse: bool = False, title_hint: str = '',
                     allow_title_lookup: bool = True) -> HarvestResult:
    """Harvest ONE arXiv paper into the library, parse-once.

    Args:
        arxiv_id: a bare arXiv id (``2301.12345`` / ``hep-th/0601001``), with or
            without a version suffix.
        folder_id: bookshelf folder to file the paper under (the research task's
            dedicated folder). Empty = the default shelf.
        user_id: explicit positive owner scope.
        extract_figures: when True, also run the phash-keyed figure extractor
            after a fresh parse (default False — R1 is about text; figures are
            a later stage's concern).
        force_reparse: skip the pre-download cache probe and always
            download+parse (used to refresh a stale row). The phash is still
            content-derived, so a forced reparse of unchanged bytes is a no-op
            upsert onto the same row.
        title_hint: bounded title already returned by discovery. When present,
            reuse it instead of issuing another arXiv request.
        allow_title_lookup: direct single-paper calls retain robust title
            recovery. Batch callers set this false after one bounded title
            batch so a partial/outage cannot fan out into per-paper retries.

    Returns:
        A :class:`HarvestResult`. ``status='cache_hit'`` means an existing
        parsed row was reused (no download, no parse); ``'parsed'`` means it
        was downloaded+parsed this run; ``'error'`` carries the reason.
    """
    import lib.paper.harvest as _self  # resolve seams through the module
    from lib.identity import require_user_id

    user_id = require_user_id(user_id, context='paper harvest')

    arxiv_id = (arxiv_id or '').strip()
    if not arxiv_id:
        return HarvestResult('', status='error', error='empty arxiv_id')
    title_hint = (
        title_hint.strip()[:PAPER_TITLE_HINT_MAX_CHARS]
        if isinstance(title_hint, str) else '')

    # ── Pre-download cache probe (arxiv_id → existing parsed row) ──
    if not force_reparse:
        hit = _existing_row_for_arxiv(arxiv_id, user_id)
        if hit:
            return _cache_hit_result(arxiv_id, hit)

    # ── Download + parse (once) ──
    # A TRANSIENT download/parse failure (arXiv timeout / rate-limit / a flaky
    # read) should not permanently drop a real paper — that starves the survey
    # corpus (R2/R3 seam finding). Retry the download+parse ONCE with a short
    # backoff before giving up. A permanent failure (empty/invalid PDF) is not
    # retried (validate_pdf_bytes already rejected it deterministically).
    pdf_bytes = None
    last_err = None
    for attempt in range(1, _HARVEST_FETCH_ATTEMPTS + 1):
        try:
            pdf_bytes = _self._download_pdf_bytes(arxiv_id)
            break
        except HarvestPDFTooLargeError as e:
            last_err = e
            logger.warning(
                '[Paper:Harvest] permanent download rejection for %s: %s',
                arxiv_id, e)
            break
        except Exception as e:
            last_err = e
            if attempt < _HARVEST_FETCH_ATTEMPTS:
                logger.warning('[Paper:Harvest] download failed for %s (attempt %d/%d): %s '
                               '— retrying', arxiv_id, attempt, _HARVEST_FETCH_ATTEMPTS, e)
                time.sleep(_HARVEST_RETRY_SLEEP * attempt)
            else:
                logger.warning('[Paper:Harvest] download failed for %s (final): %s', arxiv_id, e)
    if pdf_bytes is None:
        return HarvestResult(arxiv_id, status='error', error=f'download failed: {last_err}')

    try:
        result = _self.parse_pdf(pdf_bytes)
    except Exception as e:
        logger.error('[Paper:Harvest] parse failed for %s: %s', arxiv_id, e, exc_info=True)
        return HarvestResult(arxiv_id, status='error', error=f'parse failed: {e}')

    parsed_text = result.get('text') or ''
    page_count = int(result.get('totalPages') or 0)
    if not parsed_text.strip():
        logger.warning('[Paper:Harvest] empty parsed text for %s (scanned?)', arxiv_id)
        return HarvestResult(arxiv_id, status='error', page_count=page_count,
                             error='parser produced empty text')

    # THE identity: byte-identical to reading-mode ingest (same parse_pdf text
    # → same _paper_hash). No normalization here on purpose.
    phash = _self._paper_hash(parsed_text)

    # Title: prefer arXiv's canonical title (cheap, cached by the API layer).
    title = title_hint
    if not title and allow_title_lookup:
        try:
            from lib.paper.arxiv import fetch_arxiv_title
            title = fetch_arxiv_title(arxiv_id) or ''
        except Exception as e:
            logger.debug(
                '[Paper:Harvest] title lookup failed for %s: %s', arxiv_id, e)
    if not title:
        title = f'arXiv:{arxiv_id}'

    # Optional figure extraction (phash-keyed, same dir layout as ingest).
    images = []
    if extract_figures and phash:
        try:
            from lib.paper_identity import PAPER_DIR
            from lib.paper.images.figures import extract_paper_figures
            # Persist the PDF so the figure extractor (which reads a filepath)
            # and the reading-mode PDF viewer can both find it.
            filename = f'arxiv_{re.sub(r"/", "_", arxiv_id)}.pdf'
            filepath = os.path.join(PAPER_DIR, filename)
            os.makedirs(PAPER_DIR, exist_ok=True)
            if not (os.path.exists(filepath) and os.path.getsize(filepath) > 1000):
                with open(filepath, 'wb') as f:
                    f.write(pdf_bytes)
            images = extract_paper_figures(filepath, phash) or []
        except Exception as e:
            logger.warning('[Paper:Harvest] figure extraction failed for %s: %s',
                           arxiv_id, e)

    paper_id = _harvest_paper_id(arxiv_id)

    # ── Stamp the parse-once version + loudly flag a degraded parse ──
    # The version key reflects the extractor that ACTUALLY won this document
    # (reported by parse_pdf). A degraded write — the environment expected
    # the markdown pipeline but the document fell back to raw — is tagged
    # with the RAW version, so it never satisfies the markdown probe (the
    # row self-heals on next harvest). But a raw-tagged row that only shows
    # up as a log line is exactly the silent-quality-regression shape the
    # owner banned: flag it at ERROR level, in the audit trail, and on the
    # returned result so the batch summary and the research stage see it.
    from lib.pdf_parser._common import (HAS_PYMUPDF4LLM,
                                        current_parser_version,
                                        expected_parser_version)
    extractor = (result.get('extractor') or '').strip() or (
        'pymupdf4llm' if HAS_PYMUPDF4LLM else 'pymupdf-raw')
    parser_version = current_parser_version(extractor)
    degraded = bool(parser_version) and parser_version != expected_parser_version()
    if degraded:
        logger.error('[Paper:Harvest] DEGRADED parse for %s — extractor=%s '
                     '(expected %s); row tagged with the raw parser_version '
                     'and will NOT count as a markdown-corpus cache hit',
                     arxiv_id, parser_version, expected_parser_version())
        try:
            from lib.log import audit_log
            audit_log('paper_parse_degraded', arxiv_id=arxiv_id,
                      extractor=extractor, parser_version=parser_version,
                      expected=expected_parser_version())
        except Exception as e:
            logger.debug('[Paper:Harvest] audit_log failed (ignored): %s', e)

    _persist_row(paper_id, title=title, arxiv_id=arxiv_id, phash=phash,
                 parsed_text=parsed_text, page_count=page_count, images=images,
                 folder_id=folder_id, user_id=user_id,
                 parser_version=parser_version)

    return HarvestResult(arxiv_id, status='parsed', phash=phash, title=title,
                         page_count=page_count, text_length=len(parsed_text),
                         paper_id=paper_id, degraded=degraded)


def harvest_arxiv_batch(arxiv_ids: Iterable[str], *, folder_id: str = '',
                        user_id: int, extract_figures: bool = False,
                        on_progress: Optional[Callable[[dict], None]] = None,
                        abort_check: Optional[Callable[[], bool]] = None,
                        titles_by_arxiv_id=None) -> dict:
    """Harvest a batch of arXiv papers into the library, parse-once each.

    Deduplicates the input id list, then harvests each id in order. Every
    paper is best-effort: a failure is recorded and the batch continues.

    Args:
        arxiv_ids: iterable of at most 40 arXiv ids (deduped, order preserved).
            Oversized input is rejected after consuming at most 41 items,
            before any storage or network work.
        folder_id / user_id / extract_figures: forwarded to
            :func:`harvest_arxiv_id`.
        titles_by_arxiv_id: optional discovery titles keyed by exact input id;
            only keys in this bounded batch are read and values cap at 500
            characters.
        on_progress: optional ``fn(event_dict)`` fired per paper with a
            ``{'type': 'paper_done', 'index', 'total', 'result': <dict>}``
            event — the stage runner projects this to the production progress
            card.
        abort_check: optional zero-arg predicate; when it returns True the
            batch stops early (papers already harvested are kept — they're in
            the library).

    Returns:
        {
          'total': int,               # distinct ids attempted
          'parsed': int,              # downloaded+parsed this run
          'cache_hits': int,          # reused an existing parsed row (no reparse)
          'errors': int,
          'reparse_count': int,       # == 'parsed' — the number of real parses
          'results': [ <HarvestResult.to_dict()>, ... ],
          'aborted': bool,
        }
    """
    from lib.identity import require_user_id

    user_id = require_user_id(user_id, context='paper harvest batch')
    # Dedup while preserving order.
    seen: set = set()
    ids = []
    for index, raw in enumerate(arxiv_ids or []):
        if index >= _HARVEST_MAX_PAPERS:
            raise ValueError(
                'paper harvest accepts at most '
                f'{_HARVEST_MAX_PAPERS} arxiv ids')
        if not isinstance(raw, str):
            raise ValueError('paper harvest arxiv ids must be strings')
        aid = raw.strip()
        if aid and aid not in seen:
            seen.add(aid)
            ids.append(aid)

    total = len(ids)
    out = {'total': total, 'parsed': 0, 'cache_hits': 0, 'errors': 0,
           'reparse_count': 0, 'degraded': 0, 'results': [], 'aborted': False}
    logger.info('[Paper:Harvest] batch start — %d distinct id(s), folder=%s',
                total, folder_id or '(default)')
    if abort_check is not None and abort_check():
        out['aborted'] = True
        return out

    # One bounded projection replaces N full-bookshelf scans. If the storage
    # probe itself fails, preserve the old per-paper fallback/retry behavior.
    prefetched_hits = _existing_rows_for_arxiv_ids(ids, user_id)

    title_hints = {}
    if hasattr(titles_by_arxiv_id, 'get'):
        for aid in ids:
            candidate = titles_by_arxiv_id.get(aid)
            if isinstance(candidate, str) and candidate.strip():
                title_hints[aid] = (
                    candidate.strip()[:PAPER_TITLE_HINT_MAX_CHARS])
    unresolved_titles = [
        aid for aid in ids
        if aid not in title_hints
        and not (prefetched_hits is not None and aid in prefetched_hits)
    ]
    if unresolved_titles:
        from lib.paper.arxiv import fetch_arxiv_titles_batch
        for offset in range(
                0, len(unresolved_titles), PAPER_TITLE_BATCH_MAX_IDS):
            if abort_check is not None and abort_check():
                out['aborted'] = True
                return out
            title_batch = unresolved_titles[
                offset:offset + PAPER_TITLE_BATCH_MAX_IDS]
            try:
                resolved = fetch_arxiv_titles_batch(title_batch)
            except Exception as error:
                logger.warning(
                    '[Paper:Harvest] title batch failed for %d paper(s): %s',
                    len(title_batch), error)
                resolved = {}
            if isinstance(resolved, dict):
                for aid in title_batch:
                    candidate = resolved.get(aid)
                    if isinstance(candidate, str) and candidate.strip():
                        title_hints[aid] = (
                            candidate.strip()[:PAPER_TITLE_HINT_MAX_CHARS])

    for index, aid in enumerate(ids, 1):
        if abort_check is not None and abort_check():
            logger.info('[Paper:Harvest] batch aborted after %d/%d', index - 1, total)
            out['aborted'] = True
            break
        if prefetched_hits is not None and aid in prefetched_hits:
            res = _cache_hit_result(aid, prefetched_hits[aid])
        else:
            res = harvest_arxiv_id(
                aid, folder_id=folder_id, user_id=user_id,
                extract_figures=extract_figures,
                force_reparse=(prefetched_hits is not None),
                title_hint=title_hints.get(aid, ''),
                allow_title_lookup=False)
        out['results'].append(res.to_dict())
        if res.status == 'parsed':
            out['parsed'] += 1
            out['reparse_count'] += 1
            if res.degraded:
                out['degraded'] += 1
        elif res.status == 'cache_hit':
            out['cache_hits'] += 1
        else:
            out['errors'] += 1
        if on_progress is not None:
            try:
                on_progress({'type': 'paper_done', 'index': index,
                             'total': total, 'result': res.to_dict()})
            except Exception as e:
                logger.debug('[Paper:Harvest] on_progress raised (ignored): %s', e)

    logger.info('[Paper:Harvest] batch done — %d parsed / %d cache-hit / %d error '
                '(of %d)', out['parsed'], out['cache_hits'], out['errors'], total)
    if out['degraded']:
        logger.error('[Paper:Harvest] %d/%d paper(s) parsed DEGRADED (raw fallback '
                     'instead of the markdown pipeline) — tagged raw, excluded from '
                     'markdown-corpus cache hits; check the parser stack',
                     out['degraded'], out['parsed'])
    return out
