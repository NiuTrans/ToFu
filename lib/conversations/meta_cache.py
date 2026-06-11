"""In-memory cache for conversation metadata (sidebar list).

Holds the ETag-validated JSON blob of the user's conversation metadata so
the sidebar can refresh cheaply without re-querying the DB on every poll.
``invalidate_meta_cache()`` is called on every conversation mutation
(create / update / delete); the TTL is a safety net, not the primary
freshness mechanism.

Relocated from ``routes/common.py`` (2026-06) so lib-layer mutators
(``lib/tasks_pkg/manager``, ``autopilot``, ``message_queue``) can invalidate
the cache without importing UP into the routes package. ``routes/common.py``
re-exports these names for backward compatibility.
"""

import hashlib
import json
import threading
import time

from lib.log import get_logger
from lib.utils import safe_json as _safe_json

logger = get_logger(__name__)

DEFAULT_USER_ID = 1

_meta_cache_lock = threading.Lock()
_meta_cache = {'data': None, 'etag': None, 'ts': 0, 'ttl': 120}
# ★ TTL set to 120s (was 5s). invalidate_meta_cache() is called on every
# mutation (create/update/delete), so the TTL is a safety net, not the primary
# freshness mechanism. This eliminates redundant DB queries during idle periods
# and reduces round-trips through VS Code port forwarding.


def invalidate_meta_cache():
    """Call after any conversation mutation (save / delete)."""
    with _meta_cache_lock:
        _meta_cache['ts'] = 0


def refresh_meta_cache_if_stale(db):
    """Return (json_bytes, etag). Re-query DB only if TTL expired."""
    now = time.monotonic()
    with _meta_cache_lock:
        if _meta_cache['data'] is not None and (now - _meta_cache['ts']) < _meta_cache['ttl']:
            return _meta_cache['data'], _meta_cache['etag']

    rows = db.execute(
        '''SELECT id, title, created_at, updated_at, settings, msg_count
           FROM conversations WHERE user_id=? ORDER BY updated_at DESC''',
        (DEFAULT_USER_ID,)
    ).fetchall()
    convs = []
    for r in rows:
        settings = _safe_json(r['settings'], default=None, label='settings')
        convs.append({
            'id': r['id'], 'title': r['title'],
            'messageCount': r['msg_count'] or 0,
            'createdAt': r['created_at'], 'created_at': r['created_at'],
            'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
            'settings': settings,
        })
    payload = json.dumps(convs, ensure_ascii=False).encode('utf-8')
    etag = hashlib.md5(payload).hexdigest()[:16]

    with _meta_cache_lock:
        _meta_cache['data'] = payload
        _meta_cache['etag'] = etag
        _meta_cache['ts'] = time.monotonic()
    return payload, etag


__all__ = ['invalidate_meta_cache', 'refresh_meta_cache_if_stale']
