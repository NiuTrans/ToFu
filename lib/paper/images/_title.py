"""Paper title lookup, self-healing backfill, and report heading repair.

The up-front arXiv title lookup can fail, leaving library rows stuck at a
bare ``arXiv:<id>`` placeholder. These helpers recover the real title from
the report's own Paper Card and backfill it, and idempotently repair a
report's ``# Title`` heading on every cache / re-render path.
"""

import re
import uuid

from lib.log import get_logger

from ..hashing import _safe_hash_dir

logger = get_logger(__name__)


def _lookup_paper_title(phash: str) -> str:
    """Best-effort title lookup for a paper hash.

    Pulls the most recently-updated `paper_library` row matching the hash
    (across all users — paper_hash is content-addressable, not user-scoped).
    Returns '' if no row exists or the lookup fails.

    The lookup is safe from request and background threads because the caller
    only holds an RPC client; database connections remain in the Sidecar.
    """
    if not _safe_hash_dir(phash):
        return ''
    try:
        from lib.storage import get_storage_client
        row = get_storage_client().query(
            'paper.library.identity', {'paper_hash': phash})
    except Exception as e:
        logger.warning('[Paper:Report] Title lookup failed for hash=%s: %s', phash, e)
        return ''
    if not row:
        logger.info('[Paper:Report] No paper_library row for hash=%s — title prepend skipped', phash)
        return ''
    title = (row.get('title') or '').strip()
    if title:
        return title
    arxiv = (row.get('arxiv_id') or '').strip()
    return f'arXiv:{arxiv}' if arxiv else ''


def _extract_title_from_report(report_md: str) -> str:
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


def _backfill_library_title(phash: str, new_title: str) -> str:
    """Upsert a recovered title into paper_library rows for this hash.

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
        from lib.storage import get_storage_client
        result = get_storage_client(write=True).command(
            'paper.library.title.backfill', {
                'paper_hash': phash,
                'title': new_title,
            }, f'paper.library.title.backfill:{uuid.uuid4().hex}')
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


def _is_placeholder_title(t: str) -> bool:
    """True when a title is empty or a bare ``arXiv:<id>`` placeholder.

    Single source of truth for the placeholder predicate, shared by the
    title-prepend, backfill, and heading-repair paths so they never drift.
    """
    t = (t or '').strip()
    return (not t) or bool(re.match(r'^arxiv[:\s]', t, re.IGNORECASE))


def _ensure_title_heading(report_md: str, phash: str) -> str:
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
    card_title = _extract_title_from_report(report_md)
    title = card_title or _lookup_paper_title(phash)

    if has_h1:
        # Repair a placeholder H1 in-place when we have something better.
        first_h1 = existing_h1.group(1).strip() if existing_h1 else ''
        if (_is_placeholder_title(first_h1)
                and title and not _is_placeholder_title(title)):
            return re.sub(r'^\s*#\s+.+?\s*$', f'# {title}',
                          report_md, count=1, flags=re.MULTILINE)
        return report_md

    if not title:
        return report_md
    return f'# {title}\n\n' + report_md.lstrip()
