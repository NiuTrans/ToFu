"""Runtime access boundary for the retired conversation transcript archive.

``conversations.messages`` remains in the schema during the reversible
rows-authority rollout, but it is no longer current once normalized rows are
authoritative.  Static checks protect first-party code; this module also
guards SQL issued by dynamically loaded plugins and future call sites.  Both
reads *and writes* are denied: an archive write after row-authority cutover
would otherwise report success while the canonical transcript stayed
unchanged.

The guard is authority-wide, not merely server-local. Migrations and offline
verification tools can inspect the archive only through the explicit context
manager below, keeping every exceptional read visible in code review.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import re
import threading


class TranscriptArchiveAccessError(RuntimeError):
    """A runtime query attempted to treat the retired JSON archive as truth."""


_local = threading.local()
_TRUE_VALUES = frozenset(('1', 'true', 'yes', 'on'))
_SELECT_STATEMENT_RE = re.compile(
    r'(?:^|;)\s*(?:WITH\b[^;]*?\b)?SELECT\b',
    re.IGNORECASE | re.DOTALL,
)
_MUTATING_STATEMENT_RE = re.compile(
    r'(?:^|;)\s*(?:WITH\b[^;]*?\b)?'
    r'(?:UPDATE|INSERT(?:\s+OR\s+[A-Z_]+)?|REPLACE|DELETE)\b',
    re.IGNORECASE | re.DOTALL,
)
_CONVERSATIONS_RE = re.compile(r'\bconversations\b', re.IGNORECASE)
_MESSAGES_COLUMN_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?messages'
    r'(?![A-Za-z0-9_])',
    re.IGNORECASE,
)
_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
_LINE_COMMENT_RE = re.compile(r'--[^\r\n]*')
_QUOTED_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
_QUOTED_IDENTIFIER_RE = re.compile(
    r'[`"]([A-Za-z_][A-Za-z0-9_]*)[`"]|\[([A-Za-z_][A-Za-z0-9_]*)\]')


def _truthy(value) -> bool:
    return str(value or '').strip().lower() in _TRUE_VALUES


def rows_authority_configured() -> bool:
    """Return the one canonical env/dotenv/persistent authority decision."""
    # Lazy import avoids a wrapper/import cycle while ensuring server, plugins
    # and standalone tools cannot disagree about whether the archive is frozen.
    from lib.database.messages_rows import rows_authority_enabled
    return rows_authority_enabled()


def transcript_archive_guard_enabled() -> bool:
    """Return whether all data-layer SQL must reject direct archive access."""
    override = os.environ.get('TOFU_ENFORCE_TRANSCRIPT_AUTHORITY')
    if override is not None and str(override).strip() != '':
        return _truthy(override) and rows_authority_configured()
    return rows_authority_configured()


def _looks_like_transcript_archive_access(sql) -> bool:
    text = str(sql or '')
    # Ignore comments and string literals so diagnostic text and JSON paths do
    # not look like column references. This is a narrow deny rule, not a SQL
    # parser: all three exact tokens must occur in the same submitted statement.
    text = _BLOCK_COMMENT_RE.sub(' ', text)
    text = _LINE_COMMENT_RE.sub(' ', text)
    text = _QUOTED_LITERAL_RE.sub("''", text)
    text = _QUOTED_IDENTIFIER_RE.sub(
        lambda match: match.group(1) or match.group(2), text)
    mentions_archive = bool(
        _CONVERSATIONS_RE.search(text)
        and _MESSAGES_COLUMN_RE.search(text)
    )
    if not mentions_archive:
        return False
    return bool(
        _SELECT_STATEMENT_RE.search(text)
        or _MUTATING_STATEMENT_RE.search(text)
    )


def enforce_sql_access(sql) -> None:
    """Reject direct access to ``conversations.messages`` after cutover."""
    if getattr(_local, 'archive_allow_depth', 0):
        return
    if (transcript_archive_guard_enabled()
            and _looks_like_transcript_archive_access(sql)):
        raise TranscriptArchiveAccessError(
            'conversations.messages is a retired archive in row-authority '
            'mode; access transcripts through '
            'lib.database.conversation_repository')


@contextmanager
def allow_transcript_archive_access():
    """Explicitly allow migration/admin access to the frozen archive."""
    depth = int(getattr(_local, 'archive_allow_depth', 0) or 0)
    _local.archive_allow_depth = depth + 1
    try:
        yield
    finally:
        _local.archive_allow_depth = depth


__all__ = [
    'TranscriptArchiveAccessError', 'allow_transcript_archive_access',
    'enforce_sql_access', 'rows_authority_configured',
    'transcript_archive_guard_enabled',
]
