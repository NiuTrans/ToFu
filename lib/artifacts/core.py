"""Storage and lifecycle facade for renderable chat artifacts.

The application validates user-facing inputs here, then calls named
``storage.v1`` operations. Dedupe, path versioning, soft deletion, and pinning
are serialized inside the Storage Sidecar; this process never opens a database.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

from lib.log import audit_log, get_logger
from lib.timeutil import now_ms


logger = get_logger(__name__)

_HARD_MAX_BYTES = 8 * 1024 * 1024
ALLOWED_FORMATS = ('markdown', 'html', 'svg')
_EXT_TO_FORMAT = {
    '.md': 'markdown',
    '.markdown': 'markdown',
    '.html': 'html',
    '.htm': 'html',
    '.svg': 'svg',
}


class ArtifactNotFoundError(LookupError):
    """Raised by accessors when the requested id has no live row."""


class ArtifactSizeError(ValueError):
    """Raised when content exceeds the artifact hard limit."""


def is_renderable_path(path: str) -> bool:
    """Return whether a path has a supported renderable extension."""
    if not path:
        return False
    _, extension = os.path.splitext(path.lower())
    return extension in _EXT_TO_FORMAT


def detect_format(path: str) -> str | None:
    """Return the storage format for a path, if renderable."""
    if not path:
        return None
    _, extension = os.path.splitext(path.lower())
    return _EXT_TO_FORMAT.get(extension)


def _sha256_hex(content: str) -> str:
    return hashlib.sha256(
        content.encode('utf-8', errors='replace')).hexdigest()


_now_ms = now_ms


def _row_to_meta(row, with_content: bool = False) -> dict:
    """Compatibility decoder for callers/tests holding an older row shape."""
    if row is None:
        return {}

    def maybe_json(value):
        if value is None or value == '':
            return {}
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug('artifact metadata JSON decode failed: %s', exc)
            return {}

    result = {
        'id': row['id'], 'conv_id': row['conv_id'],
        'task_id': row['task_id'] or '', 'msg_id': row['msg_id'] or '',
        'source': row['source'], 'source_ref': maybe_json(row['source_ref']),
        'format': row['format'], 'title': row['title'] or '',
        'content_sha256': row['content_sha256'],
        'size_bytes': int(row['size_bytes'] or 0),
        'version': int(row['version'] or 1),
        'parent_id': row['parent_id'] or '', 'pinned': bool(row['pinned']),
        'meta': maybe_json(row['meta']),
        'created_at': int(row['created_at'] or 0),
    }
    if with_content:
        result['content'] = row['content'] or ''
    return result


_PUBLIC_META_FIELDS = frozenset({
    'id', 'conv_id', 'task_id', 'msg_id', 'source', 'source_ref',
    'format', 'title', 'content_sha256', 'size_bytes', 'version',
    'parent_id', 'pinned', 'created_at', 'content',
})


def public_meta(meta: dict) -> dict:
    """Whitelist artifact fields safe for public HTTP responses."""
    if not meta:
        return {}
    return {key: value for key, value in meta.items()
            if key in _PUBLIC_META_FIELDS}


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
    """Persist a new artifact or return a same-conversation dedupe match."""
    if not conv_id:
        raise ValueError('conv_id is required')
    if format not in ALLOWED_FORMATS:
        raise ValueError(
            f'Invalid format: {format!r} (allowed: {ALLOWED_FORMATS})')
    if not isinstance(content, str):
        raise TypeError(f'content must be str, got {type(content).__name__}')
    size = len(content.encode('utf-8', errors='replace'))
    if size > _HARD_MAX_BYTES:
        raise ArtifactSizeError(
            f'Artifact too large: {size} bytes > hard cap {_HARD_MAX_BYTES}')
    if source_ref is not None and not isinstance(source_ref, dict):
        raise TypeError('source_ref must be a dict')
    if meta is not None and not isinstance(meta, dict):
        raise TypeError('meta must be a dict')

    artifact_id = str(uuid.uuid4())
    from lib.storage import get_storage_client
    try:
        result = get_storage_client(write=True).command('artifact.create', {
            'artifact_id': artifact_id, 'conv_id': conv_id,
            'task_id': task_id or '', 'msg_id': msg_id or '',
            'source': source, 'source_ref': source_ref or {},
            'format': format, 'title': (title or '').strip()[:300],
            'content': content, 'parent_id': parent_id or '',
            'meta': meta or {}, 'created_at': _now_ms(),
        }, f'artifact.create:{artifact_id}')
    except Exception as exc:
        logger.error(
            '[Artifacts] insert failed conv=%s producer=%s size=%d: %s',
            conv_id[:8], source, size, exc, exc_info=True)
        raise

    artifact = result['artifact']
    if not result['created']:
        logger.info(
            '[Artifacts] dedupe hit conv=%s existing=%s producer=%s size=%d',
            conv_id[:8], artifact['id'][:8], source, size)
        return artifact
    logger.info(
        '[Artifacts] created id=%s conv=%s producer=%s format=%s size=%d '
        'task=%s version=%d parent=%s',
        artifact['id'][:8], conv_id[:8], source, format, size,
        (task_id or '')[:8], artifact['version'],
        (artifact['parent_id'] or '')[:8] or 'none')
    try:
        audit_log(
            'artifact_create', artifact_id=artifact['id'], conv_id=conv_id,
            source=source, format=format, size_bytes=size,
            task_id=task_id, msg_id=msg_id)
    except Exception as exc:
        logger.debug('[Artifacts] create audit skipped: %s', exc)
    return artifact


def get_artifact(artifact_id: str) -> dict:
    """Return a live artifact including its content."""
    if not artifact_id:
        raise ArtifactNotFoundError('empty id')
    from lib.storage import get_storage_client
    artifact = get_storage_client().query('artifact.get', {
        'artifact_id': artifact_id, 'include_content': True,
    })
    if artifact is None:
        raise ArtifactNotFoundError(artifact_id)
    return artifact


def get_artifact_meta(artifact_id: str) -> dict:
    """Return a live artifact without transferring its content."""
    if not artifact_id:
        raise ArtifactNotFoundError('empty id')
    from lib.storage import get_storage_client
    artifact = get_storage_client().query('artifact.get', {
        'artifact_id': artifact_id, 'include_content': False,
    })
    if artifact is None:
        raise ArtifactNotFoundError(artifact_id)
    return artifact


def list_artifacts(conv_id: str, *, include_deleted: bool = False) -> list[dict]:
    """List artifact metadata for one conversation, newest first."""
    if not conv_id:
        return []
    from lib.storage import get_storage_client
    return get_storage_client().query('artifact.list', {
        'conv_id': conv_id, 'include_deleted': bool(include_deleted),
    })


def delete_artifact(artifact_id: str) -> bool:
    """Idempotently soft-delete an artifact."""
    if not artifact_id:
        return False
    from lib.storage import get_storage_client
    result = get_storage_client(write=True).command('artifact.delete', {
        'artifact_id': artifact_id, 'deleted_at': _now_ms(),
    }, f'artifact.delete:{uuid.uuid4().hex}')
    if not result['deleted']:
        return False
    try:
        audit_log('artifact_delete', artifact_id=artifact_id)
    except Exception as exc:
        logger.debug('[Artifacts] delete audit skipped: %s', exc)
    return True


def list_versions(artifact_id: str) -> list[dict]:
    """Return a live artifact's version chain from oldest to newest."""
    if not artifact_id:
        return []
    from lib.storage import get_storage_client
    return get_storage_client().query(
        'artifact.versions', {'artifact_id': artifact_id})


def list_pinned_or_recent(*, limit: int = 50) -> list[dict]:
    """List pinned artifacts first, then recent unpinned artifacts."""
    if limit <= 0:
        return []
    from lib.storage import get_storage_client
    return get_storage_client().query(
        'artifact.library', {'limit': min(int(limit), 200)})


def set_pinned(artifact_id: str, pinned: bool) -> bool:
    """Set the pin state of a live artifact."""
    if not artifact_id:
        return False
    from lib.storage import get_storage_client
    result = get_storage_client(write=True).command('artifact.pin', {
        'artifact_id': artifact_id, 'pinned': bool(pinned),
    }, f'artifact.pin:{uuid.uuid4().hex}')
    return bool(result['changed'])
