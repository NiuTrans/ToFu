"""lib/artifacts/core.py — Storage + lifecycle for chat artifacts.

See lib/artifacts/__init__.py for the public-API contract.

Schema lives in lib/database/_schema_{pg,sqlite}.py — table ``chat_artifacts``.
Tied into the dual-backend layer so PG (JSONB) and SQLite (TEXT) both work
through the same wrappers.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

from lib.database import (
    DOMAIN_CHAT,
    db_execute_with_retry,
    get_thread_db,
    json_dumps_pg,
)
from lib.log import audit_log, get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)


# ── Limits ─────────────────────────────────────────────────────────────
# Hard upper bound for any single artifact.  Producers should already
# enforce a smaller per-feature cap (e.g. 1 MiB for write_file artifacts);
# this is a defense-in-depth ceiling.  Above this, persistence is rejected.
_HARD_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB

# Allowed format strings.  Stored verbatim in the ``format`` column.
ALLOWED_FORMATS = ('markdown', 'html', 'svg')

# Map file extension → format string.  Lowercase compare.
_EXT_TO_FORMAT = {
    '.md':       'markdown',
    '.markdown': 'markdown',
    '.html':     'html',
    '.htm':      'html',
    '.svg':      'svg',
}


class ArtifactNotFoundError(LookupError):
    """Raised by accessors when the requested id has no live row."""


class ArtifactSizeError(ValueError):
    """Raised when content exceeds _HARD_MAX_BYTES."""


# ═══════════════════════════════════════════════════════════════════════
#  Predicates / format helpers
# ═══════════════════════════════════════════════════════════════════════

def is_renderable_path(path: str) -> bool:
    """True when ``path`` looks like a renderable artifact by extension.

    Cheap predicate used by Producer A before doing any disk I/O.  We
    intentionally match by extension only — content-type sniffing is a
    separate concern handled by the route that serves the raw bytes.
    """
    if not path:
        return False
    _, ext = os.path.splitext(path.lower())
    return ext in _EXT_TO_FORMAT


def detect_format(path: str) -> str | None:
    """Return the format string for a path, or None if not renderable."""
    if not path:
        return None
    _, ext = os.path.splitext(path.lower())
    return _EXT_TO_FORMAT.get(ext)


# ═══════════════════════════════════════════════════════════════════════
#  Internal helpers
# ═══════════════════════════════════════════════════════════════════════

def _sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


_now_ms = now_ms


def _row_to_meta(row, with_content: bool = False) -> dict:
    """Translate a DictRow / sqlite3.Row → public dict.  No PII filtering;
    callers strip sensitive fields before serializing if needed."""
    if row is None:
        return {}
    # source_ref / meta are JSON in both backends; PG returns dict already
    def _maybe_json(v):
        if v is None or v == '':
            return {}
        if isinstance(v, (dict, list)):
            return v
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError) as _e_audit:
            logger.debug('[core] _maybe_json caught %s: %s', type(_e_audit).__name__, _e_audit)
            return {}

    out = {
        'id':             row['id'],
        'conv_id':        row['conv_id'],
        'task_id':        row['task_id'] or '',
        'msg_id':         row['msg_id'] or '',
        'source':         row['source'],
        'source_ref':     _maybe_json(row['source_ref']),
        'format':         row['format'],
        'title':          row['title'] or '',
        'content_sha256': row['content_sha256'],
        'size_bytes':     int(row['size_bytes'] or 0),
        'version':        int(row['version'] or 1),
        'parent_id':      row['parent_id'] or '',
        'pinned':         bool(row['pinned']),
        'meta':           _maybe_json(row['meta']),
        'created_at':     int(row['created_at'] or 0),
    }
    if with_content:
        out['content'] = row['content'] or ''
    return out


# Whitelist of artifact-meta fields safe to expose over the HTTP API.
# Anything NOT in this set is internal-only and stripped by ``public_meta``.
# The notable exclusion is the free-form ``meta`` dict (producer bookkeeping
# such as word_count / has_scripts / toolName), which the frontend never
# reads and which could accrue internal fields over time. Add a field here
# ONLY after confirming it carries nothing environment-specific or sensitive.
_PUBLIC_META_FIELDS = frozenset({
    'id', 'conv_id', 'task_id', 'msg_id', 'source', 'source_ref',
    'format', 'title', 'content_sha256', 'size_bytes', 'version',
    'parent_id', 'pinned', 'created_at', 'content',
})


def public_meta(meta: dict) -> dict:
    """Return a copy of an artifact-meta dict with internal-only fields removed.

    Whitelist-based (not blacklist) so a NEW field added to ``_row_to_meta``
    or stuffed into the row by a producer cannot silently leak over the API:
    it stays server-side until explicitly added to ``_PUBLIC_META_FIELDS``.

    ``content`` is passed through when present (callers that already fetched
    the body intend to serve it); content-suppression is the storage layer's
    job (``get_artifact_meta`` selects ``'' AS content``), not this filter's.
    """
    if not meta:
        return {}
    return {k: v for k, v in meta.items() if k in _PUBLIC_META_FIELDS}


def _find_dedupe_row(db, conv_id: str, content_sha256: str) -> Any:
    """Return existing live row in the same conv with matching sha, or None.

    Dedupe scope is per-conversation: re-emitting the exact same report in
    a different conv should still yield a separate artifact row (so that
    "delete this conv" cascades cleanly).
    """
    return db.execute(
        '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                  format, title, content, content_sha256, size_bytes,
                  version, parent_id, pinned, meta, created_at
           FROM chat_artifacts
           WHERE conv_id=? AND content_sha256=? AND deleted_at=0
           ORDER BY created_at DESC LIMIT 1''',
        (conv_id, content_sha256)
    ).fetchone()


def _find_previous_version_by_path(db, conv_id: str, path: str) -> Any:
    """Return the most-recent live row for the same (conv_id, path) so we
    can link the new artifact as version N+1 via parent_id.

    Path is read from source_ref.path (stored as JSON).  PG can match via
    JSONB operators; SQLite stores as TEXT and we LIKE-match cheaply.
    Returns None when no previous version exists.
    """
    if not path:
        return None
    # Use a LIKE pattern that works on both backends — JSON is stored as a
    # serialized string in SQLite and JSONB in PG (the wrapper layer
    # serializes for both).  The pattern matches '"path": "<value>"'.
    # This is a fast best-effort lookup, not a strict equality probe;
    # for the rare false positive (a path embedded as substring of
    # another), the behavior is "incorrect parent linkage" which is
    # cosmetic only — the new row is still inserted correctly.
    json_substr = '%"path": "' + path.replace('%', r'\%').replace('_', r'\_') + '"%'
    return db.execute(
        '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                  format, title, content, content_sha256, size_bytes,
                  version, parent_id, pinned, meta, created_at
           FROM chat_artifacts
           WHERE conv_id=? AND deleted_at=0
                 AND CAST(source_ref AS TEXT) LIKE ? ESCAPE '\\'
           ORDER BY created_at DESC LIMIT 1''',
        (conv_id, json_substr)
    ).fetchone()


# ═══════════════════════════════════════════════════════════════════════
#  CRUD
# ═══════════════════════════════════════════════════════════════════════

def create_artifact(
    *,
    conv_id: str,
    content: str,
    format: str,
    source: str,
    source_ref: dict | None = None,
    task_id: str = '',
    msg_id: str = '',
    title: str = '',
    parent_id: str = '',
    meta: dict | None = None,
) -> dict:
    """Persist a new artifact row (or reuse a dedupe match).

    Args:
        conv_id: conversation id; required for cascade + listing.
        content: the raw markdown / html / svg bytes (UTF-8 string).
        format: one of ALLOWED_FORMATS.
        source: free-form producer tag (e.g. 'write_file', 'inline_fence',
                'inline_doc').  Stored verbatim; not validated.
        source_ref: producer-specific provenance dict, e.g.
                    {'path': 'report.html'} or {'fence_index': 0}.
        task_id, msg_id: optional foreign keys for filter/audit.
        title: display title (file basename, first H1, <title>, etc.).
        parent_id: id of the previous version when this is a regen.
        meta: free-form metadata (word_count, has_scripts, …).

    Returns:
        Public metadata dict (NO content) of the persisted (or deduped) row.

    Raises:
        ArtifactSizeError: content exceeds _HARD_MAX_BYTES.
        ValueError: invalid format string or empty conv_id.
    """
    if not conv_id:
        raise ValueError('conv_id is required')
    if format not in ALLOWED_FORMATS:
        raise ValueError(f'Invalid format: {format!r} (allowed: {ALLOWED_FORMATS})')
    if not isinstance(content, str):
        raise TypeError(f'content must be str, got {type(content).__name__}')

    size = len(content.encode('utf-8', errors='replace'))
    if size > _HARD_MAX_BYTES:
        raise ArtifactSizeError(
            f'Artifact too large: {size} bytes > hard cap {_HARD_MAX_BYTES}'
        )

    sha = _sha256_hex(content)
    db = get_thread_db(DOMAIN_CHAT)

    # Dedupe: same conv + identical sha → return the existing row.
    existing = _find_dedupe_row(db, conv_id, sha)
    if existing is not None:
        existing_meta = _row_to_meta(existing)
        logger.info(
            '[Artifacts] dedupe hit conv=%s sha=%s existing=%s '
            'producer=%s size=%d',
            conv_id[:8], sha[:8], existing_meta['id'][:8], source, size,
        )
        return existing_meta

    artifact_id = str(uuid.uuid4())
    now_ms = _now_ms()
    source_ref_obj = source_ref or {}
    meta_obj = meta or {}
    title_clean = (title or '').strip()[:300]

    # ── Versioning: same (conv_id, source_ref.path) with NEW content
    #    becomes version N+1 with parent_id linking to the previous row.
    #    Only consulted when the caller didn't already supply parent_id.
    auto_version = 1
    auto_parent = parent_id or ''
    path_for_version = (source_ref_obj or {}).get('path') if isinstance(source_ref_obj, dict) else None
    if not auto_parent and path_for_version:
        try:
            prev = _find_previous_version_by_path(db, conv_id, path_for_version)
        except Exception as e:
            logger.debug('[Artifacts] previous-version lookup failed (non-fatal): %s', e)
            prev = None
        if prev is not None:
            try:
                auto_parent = prev['id']
                auto_version = int(prev['version'] or 1) + 1
                logger.info(
                    '[Artifacts] versioning: conv=%s path=%s parent=%s v%d→v%d',
                    conv_id[:8], path_for_version, auto_parent[:8],
                    auto_version - 1, auto_version,
                )
            except Exception as e:
                logger.debug('[Artifacts] auto-version stamping failed (non-fatal): %s', e)
                auto_parent = parent_id or ''
                auto_version = 1

    try:
        db_execute_with_retry(
            db,
            '''INSERT INTO chat_artifacts
                 (id, conv_id, task_id, msg_id, source, source_ref,
                  format, title, content, content_sha256, size_bytes,
                  version, parent_id, pinned, meta, created_at, deleted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                artifact_id, conv_id, task_id or '', msg_id or '',
                source, json_dumps_pg(source_ref_obj),
                format, title_clean, content, sha, size,
                auto_version, auto_parent, False, json_dumps_pg(meta_obj),
                now_ms, 0,
            ),
        )
    except Exception as e:
        logger.error(
            '[Artifacts] insert failed conv=%s sha=%s producer=%s size=%d: %s',
            conv_id[:8], sha[:8], source, size, e, exc_info=True,
        )
        raise

    logger.info(
        '[Artifacts] created id=%s conv=%s producer=%s format=%s size=%d sha=%s task=%s version=%d parent=%s',
        artifact_id[:8], conv_id[:8], source, format, size, sha[:8],
        (task_id or '')[:8], auto_version, (auto_parent or '')[:8] or 'none',
    )
    try:
        audit_log('artifact_create',
                  artifact_id=artifact_id, conv_id=conv_id,
                  source=source, format=format, size_bytes=size,
                  task_id=task_id, msg_id=msg_id)
    except Exception as e:
        logger.debug('[Artifacts] audit_log artifact_create failed: %s', e)

    return {
        'id':             artifact_id,
        'conv_id':        conv_id,
        'task_id':        task_id or '',
        'msg_id':         msg_id or '',
        'source':         source,
        'source_ref':     source_ref_obj,
        'format':         format,
        'title':          title_clean,
        'content_sha256': sha,
        'size_bytes':     size,
        'version':        auto_version,
        'parent_id':      auto_parent,
        'pinned':         False,
        'meta':           meta_obj,
        'created_at':     now_ms,
    }


def get_artifact(artifact_id: str) -> dict:
    """Return full artifact dict (with content).  Raises ArtifactNotFoundError."""
    if not artifact_id:
        raise ArtifactNotFoundError('empty id')
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                  format, title, content, content_sha256, size_bytes,
                  version, parent_id, pinned, meta, created_at
           FROM chat_artifacts
           WHERE id=? AND deleted_at=0''',
        (artifact_id,)
    ).fetchone()
    if row is None:
        raise ArtifactNotFoundError(artifact_id)
    return _row_to_meta(row, with_content=True)


def get_artifact_meta(artifact_id: str) -> dict:
    """Return artifact metadata WITHOUT the content blob (cheap)."""
    if not artifact_id:
        raise ArtifactNotFoundError('empty id')
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                  format, title, '' AS content, content_sha256, size_bytes,
                  version, parent_id, pinned, meta, created_at
           FROM chat_artifacts
           WHERE id=? AND deleted_at=0''',
        (artifact_id,)
    ).fetchone()
    if row is None:
        raise ArtifactNotFoundError(artifact_id)
    return _row_to_meta(row, with_content=False)


def list_artifacts(conv_id: str, *, include_deleted: bool = False) -> list[dict]:
    """List artifact metadata for a conv, newest first.  No content."""
    if not conv_id:
        return []
    db = get_thread_db(DOMAIN_CHAT)
    if include_deleted:
        rows = db.execute(
            '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                      format, title, '' AS content, content_sha256, size_bytes,
                      version, parent_id, pinned, meta, created_at
               FROM chat_artifacts
               WHERE conv_id=?
               ORDER BY created_at DESC''',
            (conv_id,)
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                      format, title, '' AS content, content_sha256, size_bytes,
                      version, parent_id, pinned, meta, created_at
               FROM chat_artifacts
               WHERE conv_id=? AND deleted_at=0
               ORDER BY created_at DESC''',
            (conv_id,)
        ).fetchall()
    return [_row_to_meta(r) for r in rows]


def delete_artifact(artifact_id: str) -> bool:
    """Soft-delete by stamping ``deleted_at``.  Returns True if a row was
    affected.  Pinned artifacts ARE deleted — pin only protects against
    automatic GC, not user-initiated deletion."""
    if not artifact_id:
        return False
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = _now_ms()
    try:
        db_execute_with_retry(
            db,
            'UPDATE chat_artifacts SET deleted_at=? WHERE id=? AND deleted_at=0',
            (now_ms, artifact_id),
        )
    except Exception as e:
        logger.error('[Artifacts] delete failed id=%s: %s',
                     artifact_id[:8], e, exc_info=True)
        return False
    # Verify it actually flipped (cheap re-read; SELECT changes() is dialect-specific)
    row = db.execute(
        'SELECT 1 FROM chat_artifacts WHERE id=? AND deleted_at=?',
        (artifact_id, now_ms),
    ).fetchone()
    deleted = row is not None
    if deleted:
        logger.info('[Artifacts] deleted id=%s', artifact_id[:8])
        try:
            audit_log('artifact_delete', artifact_id=artifact_id)
        except Exception as e:
            logger.debug('[Artifacts] audit_log artifact_delete failed: %s', e)
    return deleted


def list_versions(artifact_id: str) -> list[dict]:
    """Return the version chain for an artifact (oldest → newest).

    Walks parent_id backward until a row with no parent is reached, then
    walks forward via children (rows whose parent_id is one of the chain).
    Returns metadata dicts (no content).  The given id may be any node
    in the chain — we anchor by walking up first.
    """
    if not artifact_id:
        return []
    db = get_thread_db(DOMAIN_CHAT)

    # 1. Walk up to the root (parent_id == '').
    row = db.execute(
        '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                  format, title, '' AS content, content_sha256, size_bytes,
                  version, parent_id, pinned, meta, created_at
           FROM chat_artifacts WHERE id=? AND deleted_at=0''',
        (artifact_id,)
    ).fetchone()
    if row is None:
        return []
    seen: set[str] = set()
    while row and row['parent_id'] and row['parent_id'] not in seen:
        seen.add(row['parent_id'])
        prev = db.execute(
            '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                      format, title, '' AS content, content_sha256, size_bytes,
                      version, parent_id, pinned, meta, created_at
               FROM chat_artifacts WHERE id=? AND deleted_at=0''',
            (row['parent_id'],)
        ).fetchone()
        if prev is None:
            break
        row = prev
    root = row
    if root is None:
        return []

    # 2. Walk forward via children iteratively.
    chain = [_row_to_meta(root)]
    cur_id = root['id']
    while True:
        nxt = db.execute(
            '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                      format, title, '' AS content, content_sha256, size_bytes,
                      version, parent_id, pinned, meta, created_at
               FROM chat_artifacts
               WHERE parent_id=? AND deleted_at=0
               ORDER BY created_at ASC LIMIT 1''',
            (cur_id,)
        ).fetchone()
        if nxt is None:
            break
        chain.append(_row_to_meta(nxt))
        cur_id = nxt['id']
    return chain


def list_pinned_or_recent(*, limit: int = 50) -> list[dict]:
    """Library-mode listing: all pinned artifacts (newest first), then a
    cap of recent un-pinned ones to fill the panel.  No content blob."""
    if limit <= 0:
        return []
    db = get_thread_db(DOMAIN_CHAT)
    rows = db.execute(
        '''SELECT id, conv_id, task_id, msg_id, source, source_ref,
                  format, title, '' AS content, content_sha256, size_bytes,
                  version, parent_id, pinned, meta, created_at
           FROM chat_artifacts
           WHERE deleted_at=0
           ORDER BY pinned DESC, created_at DESC
           LIMIT ?''',
        (int(limit),)
    ).fetchall()
    return [_row_to_meta(r) for r in rows]


def set_pinned(artifact_id: str, pinned: bool) -> bool:
    """Toggle the pin flag.  Returns True on success."""
    if not artifact_id:
        return False
    db = get_thread_db(DOMAIN_CHAT)
    try:
        db_execute_with_retry(
            db,
            'UPDATE chat_artifacts SET pinned=? WHERE id=? AND deleted_at=0',
            (bool(pinned), artifact_id),
        )
    except Exception as e:
        logger.error('[Artifacts] set_pinned failed id=%s pinned=%s: %s',
                     artifact_id[:8], pinned, e, exc_info=True)
        return False
    logger.info('[Artifacts] set_pinned id=%s pinned=%s',
                artifact_id[:8], pinned)
    return True
