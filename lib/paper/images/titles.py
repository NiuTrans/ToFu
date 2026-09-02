"""Recover paper titles, backfill library metadata, and repair report headings."""

import re
import uuid

from lib.log import get_logger

from lib.paper_identity import _safe_hash_dir
from ..library_repository import PaperLibraryRepository

logger = get_logger(__name__)


def lookup_paper_title(phash: str, *, user_id: int) -> str:
    """Return this owner's stored title for ``phash``, or ``''``.

    A content hash identifies bytes, not authority. Requiring the owner here
    prevents a title or arXiv identifier from another bookshelf being exposed
    merely because both users ingested the same document.
    """
    if not _safe_hash_dir(phash):
        return ''
    try:
        identity = PaperLibraryRepository(user_id).identity(phash)
    except Exception as e:
        logger.warning('[Paper:Report] Title lookup failed for hash=%s: %s', phash, e)
        return ''
    if identity is None:
        logger.info('[Paper:Report] No paper_library row for hash=%s — title prepend skipped', phash)
        return ''
    title = identity.title.strip()
    if title:
        return title
    arxiv = identity.arxiv_id.strip()
    return f'arXiv:{arxiv}' if arxiv else ''


def extract_title_from_report(report_md: str) -> str:
    """Pull the paper title out of the report's Paper Card table.

    The report prompt emits a Paper Card whose first row is::

        | **Title** | Attention Is All You Need |   (EN)
        | **标题**  | … |                          (ZH)

    Returns the cleaned title, or '' if the row is missing / still holds a
    placeholder. Used to self-heal library rows whose title is stuck at the
    bare ``arXiv:<id>`` because the up-front arXiv title lookup failed.
    """
    if not report_md:
        return ''
    m = re.search(
        r'^\|\s*\*{0,2}\s*(?:Title|标题)\s*\*{0,2}\s*\|\s*(.+?)\s*\|',
        report_md, re.MULTILINE | re.IGNORECASE)
    if not m:
        return ''
    raw = m.group(1).strip()
    # Strip markdown bold/italic/code and collapse links to their text.
    raw = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', raw)   # [text](url) → text
    raw = re.sub(r'[*`_]', '', raw).strip()
    # Reject leftover prompt placeholders.
    placeholders = {'(full title)', '（完整标题）', '(完整标题)', 'full title',
                    '完整标题', 'n/a', 'na', '-', '—', 'title'}
    if not raw or raw.lower() in placeholders:
        return ''
    # A title that is itself just an arXiv id is no better than what we have.
    if re.match(r'^arxiv[:\s]', raw, re.IGNORECASE):
        return ''
    return re.sub(r'\s+', ' ', raw)[:500]


def backfill_library_title(phash: str, new_title: str, *, user_id: int) -> str:
    """Backfill a recovered title on this owner's matching library rows.

    ONLY overwrites a row whose stored title is empty or still a bare
    ``arXiv:<id>`` placeholder — never clobbers a user-renamed or
    correctly-resolved title. Returns the title that is now authoritative for
    the hash (the new one if any row was updated, else the existing stored
    title, else '').

    Safe to call from a background worker thread: the complete conditional
    update is one Sidecar transaction.
    """
    new_title = (new_title or '').strip()
    if not _safe_hash_dir(phash) or not new_title:
        return ''
    try:
        result = PaperLibraryRepository(user_id).backfill_title(
            phash,
            new_title,
            command_id=f'paper.library.title.backfill:{uuid.uuid4().hex}',
        )
    except Exception as e:
        logger.warning('[Paper:Report] Title backfill failed for hash=%s: %s', phash, e)
        return ''
    updated = int(result.get('updated') or 0)
    if updated:
        logger.info('[Paper:Report] Backfilled %d title row(s) for hash=%s: %.120s',
                    updated, phash, new_title)
    else:
        logger.info('[Paper:Report] Title backfill skipped for hash=%s — '
                    'no placeholder row', phash)
    return str(result.get('title') or new_title)


def is_placeholder_title(t: str) -> bool:
    """True when a title is empty or a bare ``arXiv:<id>`` placeholder.

    Single source of truth for the placeholder predicate, shared by the
    title-prepend, backfill, and heading-repair paths so they never drift.
    """
    t = (t or '').strip()
    return (not t) or bool(re.match(r'^arxiv[:\s]', t, re.IGNORECASE))


def ensure_title_heading(report_md: str, phash: str, *, user_id: int) -> str:
    """Idempotently give a report a correct `# Title` heading.

    Two failure modes are repaired here so every cache / re-render path
    benefits (live generation, DB cache hit, export):

    1. **Missing heading** — older cached reports were persisted before the
       title-prepend logic existed, so they render without a top-level
       heading. Prepend the resolved title.
    2. **Placeholder heading** — a report whose body starts with a bare
       ``# arXiv:<id>`` (the up-front arXiv lookup failed at generation
       time). The real title lives in the report's own Paper Card row, so
       swap the placeholder H1 for it. This is what makes the report header
       show the paper title instead of the arXiv id.

    The DB row is never rewritten — this only repairs the rendered copy.
    """
    if not report_md:
        return report_md

    existing_h1 = re.match(r'^\s*#\s+(.+?)\s*$', report_md, re.MULTILINE)
    has_h1 = bool(re.match(r'^\s*#\s+\S', report_md))

    # Best title: a non-placeholder Paper Card title (the report's own
    # ground truth) wins; fall back to the stored library title.
    card_title = extract_title_from_report(report_md)
    title = card_title or lookup_paper_title(phash, user_id=user_id)

    if has_h1:
        # Repair a placeholder H1 in-place when we have something better.
        first_h1 = existing_h1.group(1).strip() if existing_h1 else ''
        if (is_placeholder_title(first_h1)
                and title and not is_placeholder_title(title)):
            return re.sub(r'^\s*#\s+.+?\s*$', f'# {title}',
                          report_md, count=1, flags=re.MULTILINE)
        return report_md

    if not title:
        return report_md
    return f'# {title}\n\n' + report_md.lstrip()
