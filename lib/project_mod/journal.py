"""Project evolution-journal maintenance: injection budgeting + rotation.

``JOURNAL.md`` is the free-form dev log the assistant reads at the start of a
session and appends to as the project evolves (see
:mod:`lib.project_mod.indexer` for the auto-seed / auto-inject wiring).  Left
unbounded it grows without limit — the file this project ships with already
holds 1000+ entries / several MB — which creates two distinct problems:

1. **Injection starvation (the silent one).**  ``get_context_for_prompt`` used
   to inject only the FIRST 32 KB of the file.  As individual entries grew more
   detailed, that fixed byte window covered fewer and fewer entries — down to a
   single day's worth — so the model's "read the journal first to understand
   how we got here" instruction quietly degraded to "read only today".

2. **Disk growth.**  The on-disk file keeps growing regardless of what we
   inject.

This module addresses both:

* :func:`build_injection` — decouples *what we inject* from *how big the file
  is*: the newest entries go in FULL, and every older entry is listed as a
  one-line title index (date + title) so the model still knows the history
  exists and can ``grep`` / ``read_files`` back into it on demand.

* :func:`maybe_rotate` — when the file crosses a size threshold, the OLDEST
  entries (the bottom of the file — entries are newest-at-top) are moved into
  monthly archive files under ``.tofu/journal-archive/`` so the live file stays
  bounded while the full history remains on disk and greppable.

Concurrency
-----------
Multiple sibling conversations append to the SAME ``JOURNAL.md`` concurrently.
Rotation is a read-modify-write, so it takes the same per-path thread lock +
cross-process ``flock`` sidecar that :mod:`lib.json_store` uses.  Two ordering
invariants make it safe against a concurrent top-append:

* Agent appends only ever touch the TOP (newest-at-top); rotation only ever
  removes a SUFFIX (the oldest entries).  Re-reading fresh inside the lock
  immediately before the rewrite means any top-append already on disk is
  preserved (it is the newest → kept).
* The archive file is written BEFORE the main file is truncated, so a crash or
  a race between the two steps can only leave an entry present in BOTH places
  (harmless, self-heals on the next rotation) — never lose it from both.
"""

from __future__ import annotations

import os
import re

from lib.json_store import (
    _interprocess_lock,
    _path_lock,
    read_text,
    write_text_atomic,
)
from lib.log import get_logger

logger = get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to ``default``."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except (ValueError, TypeError) as e:
        logger.debug('[Journal] bad %s=%r (%s) — using default %d', name, raw, e, default)
        return default


# ── Tunables (env-overridable, sensible defaults per §3.5) ──────────

# Byte budget for the FULL-TEXT recent window injected into the prompt.
# Newest entries fill this budget verbatim; the rest become the title index.
RECENT_INJECT_BYTES = _env_int('TOFU_JOURNAL_RECENT_BYTES', 24_000)
# Max number of older entries listed in the title-only index.
INJECT_INDEX_MAX = _env_int('TOFU_JOURNAL_INDEX_MAX', 80)
# Cap the header (intro prose above the first entry) we prepend.
_HEADER_CAP = 2_000
# One title line is truncated to this many chars.
_TITLE_CAP = 160

# Rotate the on-disk file when it grows beyond this…
ROTATE_THRESHOLD_BYTES = _env_int('TOFU_JOURNAL_ROTATE_THRESHOLD', 512 * 1024)
# …keeping this many bytes of the NEWEST entries in the live file.
ROTATE_KEEP_BYTES = _env_int('TOFU_JOURNAL_ROTATE_KEEP', 200 * 1024)
# Never read more than this from disk when building the injection (a safety
# bound in case rotation could not run — e.g. read-only root).  Rotation keeps
# the file well under this in the normal case.
_READ_CAP_BYTES = _env_int('TOFU_JOURNAL_READ_CAP', 1_500_000)

_ENTRY_RE = re.compile(r'(?m)^### ')
_DATE_RE = re.compile(r'(\d{4})-(\d{2})-\d{2}')


# ── Parsing ─────────────────────────────────────────────────────────

def split_entries(text: str) -> tuple[str, list[str]]:
    """Split journal ``text`` into ``(header, entries)``.

    ``header`` is everything before the first ``### `` line (the intro prose
    the seed writes).  ``entries`` is the list of ``### …`` blocks in FILE
    ORDER — which, by the newest-at-top convention, is newest→oldest.  Each
    entry string includes its own ``### `` heading and trailing newline(s).
    """
    matches = list(_ENTRY_RE.finditer(text))
    if not matches:
        return text, []
    header = text[:matches[0].start()]
    entries = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append(text[start:end])
    return header, entries


def _partition_by_budget(entries: list[str], max_bytes: int) -> tuple[list[str], list[str]]:
    """Split newest-first ``entries`` into ``(recent, older)`` by a byte budget.

    Accumulates entries from the front (newest) until adding the next one would
    exceed ``max_bytes``.  Always keeps at least the single newest entry even
    if it alone blows the budget, so a very large latest entry still shows.
    """
    recent: list[str] = []
    total = 0
    for i, e in enumerate(entries):
        b = len(e.encode('utf-8'))
        if recent and total + b > max_bytes:
            return entries[:i], entries[i:]
        recent.append(e)
        total += b
    return entries, []


def _entry_title(entry: str) -> str:
    """Return the first heading line of ``entry`` (``### `` stripped, capped)."""
    lines = entry.splitlines()
    first = lines[0] if lines else ''
    return first.lstrip('#').strip()[:_TITLE_CAP]


def _month_of(entry: str) -> str:
    """Return the ``YYYY-MM`` of an entry's date, or ``'undated'``."""
    lines = entry.splitlines()
    m = _DATE_RE.search(lines[0] if lines else '')
    return f'{m.group(1)}-{m.group(2)}' if m else 'undated'


# ── Injection ───────────────────────────────────────────────────────

def build_injection(text: str, *, recent_bytes: int = RECENT_INJECT_BYTES,
                    index_max: int = INJECT_INDEX_MAX) -> str:
    """Build the journal block to inject into the system prompt.

    The newest entries (up to ``recent_bytes``) are included VERBATIM; every
    older entry is emitted as a single title-index line so the model knows the
    history exists and can expand it on demand via ``grep`` / ``read_files`` /
    the ``.tofu/journal-archive`` files.  This keeps the injected size roughly
    constant no matter how large the on-disk file becomes.
    """
    header, entries = split_entries(text)
    if not entries:
        return text.strip()

    recent, older = _partition_by_budget(entries, recent_bytes)
    parts: list[str] = []
    h = header.strip()
    if h:
        parts.append(h[:_HEADER_CAP])
    parts.append(''.join(recent).rstrip())

    if older:
        idx = [
            '',
            f'<!-- The {len(older)} older entries below are shown as an INDEX only '
            '(title + date). Their full text is still in JOURNAL.md on disk and in '
            '.tofu/journal-archive/*.md — grep the file or read_files the specific '
            'lines to expand any of them. -->',
        ]
        for e in older[:index_max]:
            idx.append(f'- {_entry_title(e)}')
        if len(older) > index_max:
            idx.append(f'- … and {len(older) - index_max} more older entries '
                       '(see JOURNAL.md / .tofu/journal-archive/)')
        parts.append('\n'.join(idx))

    return '\n\n'.join(parts).strip()


def read_for_injection(journal_path: str) -> str:
    """Read the journal (bounded) and return the built injection block.

    Reads at most ``_READ_CAP_BYTES`` so a pathologically large file (e.g. if
    rotation could not run on a read-only root) can never blow up prompt
    assembly.  Returns ``''`` on any read error (logged).
    """
    try:
        with open(journal_path, encoding='utf-8', errors='replace') as f:
            raw = f.read(_READ_CAP_BYTES)
    except OSError as e:
        logger.warning('[Journal] read failed for %s: %s', journal_path, e)
        return ''
    return build_injection(raw)


# ── Rotation ────────────────────────────────────────────────────────

def _archive_dir(project_root: str) -> str:
    """Return the archive directory (under the gitignored ``.tofu/``)."""
    return os.path.join(project_root, '.tofu', 'journal-archive')


def _archive_entries(project_root: str, entries: list[str]) -> None:
    """Append ``entries`` (oldest batch, newest-first) to monthly archives.

    Grouped into ``JOURNAL-YYYY-MM.md`` by each entry's date.  Each archive
    write is itself an atomic locked read-append-write so concurrent rotations
    don't lose batches.
    """
    adir = _archive_dir(project_root)
    by_month: dict[str, list[str]] = {}
    for e in entries:
        by_month.setdefault(_month_of(e), []).append(e)
    for month, es in by_month.items():
        path = os.path.join(adir, f'JOURNAL-{month}.md')
        lk = _path_lock(path)
        with lk, _interprocess_lock(path):
            existing = read_text(path, default='')
            if not existing.strip():
                existing = (f'# Journal archive — {month}\n\n'
                            '> Entries rotated out of the live JOURNAL.md, kept for '
                            'grep / history. Cold storage; ordering within a file is '
                            'per-rotation-batch, not strictly global.\n')
            block = ''.join(es).rstrip('\n')
            new = existing.rstrip('\n') + '\n\n' + block + '\n'
            write_text_atomic(path, new)


def maybe_rotate(journal_path: str, project_root: str, *,
                 threshold: int = ROTATE_THRESHOLD_BYTES,
                 keep_bytes: int = ROTATE_KEEP_BYTES) -> int | None:
    """Rotate the oldest entries out of ``journal_path`` when it grows large.

    No-op (returns ``None``) when the file is under ``threshold``.  Otherwise
    the newest entries totalling up to ``keep_bytes`` stay in the live file and
    the rest (the oldest) are moved to ``.tofu/journal-archive/``.  Best-effort:
    any failure is logged and swallowed, never raised into prompt assembly.

    Returns the number of entries archived, or ``None`` when nothing was done.
    """
    try:
        size = os.path.getsize(journal_path)
    except OSError as e:
        logger.debug('[Journal] size probe failed for %s: %s', journal_path, e)
        return None
    if size < threshold:
        return None

    lock = _path_lock(journal_path)
    try:
        with lock, _interprocess_lock(journal_path):
            # Re-read fresh INSIDE the lock: a top-append that already landed
            # is the newest content and will be kept; the window vs a
            # non-locking writer is the same one every RMW in this repo accepts.
            text = read_text(journal_path, default='')
            if len(text.encode('utf-8')) < threshold:
                return None
            header, entries = split_entries(text)
            keep, archive = _partition_by_budget(entries, keep_bytes)
            if not archive:
                return None
            # Archive FIRST (superset-safe) …
            _archive_entries(project_root, archive)
            # … then truncate the live file to header + newest kept entries.
            new_text = header + ''.join(keep)
            if not new_text.endswith('\n'):
                new_text += '\n'
            write_text_atomic(journal_path, new_text)
    except OSError as e:
        logger.warning('[Journal] rotation failed for %s: %s', journal_path, e)
        return None
    logger.info('[Journal] Rotated %d oldest entries out of %s (kept newest ~%d KB)',
                len(archive), journal_path, keep_bytes // 1024)
    return len(archive)
