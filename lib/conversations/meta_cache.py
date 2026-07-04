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

Epic D4 (2026-07): the cache is now **user-keyed** (was a single global blob —
a multi-tenant correctness bug: user B could read user A's cached sidebar), the
conversation-list query is **bounded** with a ``LIMIT`` (was an unbounded
full-user-scan), and invalidation is **cross-replica** — it publishes on the
shared push_bus so a mutation on replica A clears replica B's stale entry.
Both public functions default ``user_id=DEFAULT_USER_ID`` so the existing
single-user call sites are byte-identical; under the ``inproc`` backend the
cross-replica publish collapses to a local re-clear (no dependency, no change).
"""

import hashlib
import json
import os
import threading
import time

from lib.log import get_logger
from lib.utils import safe_json as _safe_json

logger = get_logger(__name__)

DEFAULT_USER_ID = 1

# Per-user cache entries: user_id -> {'data', 'etag', 'ts', 'ttl'}. Keying by
# user is what makes the cache multi-tenant-correct — a global blob would let
# one user's sidebar leak into another's response behind a shared process.
_meta_cache_lock = threading.Lock()
_meta_cache_by_user: dict = {}
_META_TTL = 120  # safety-net TTL; invalidate_meta_cache() is the primary path

# ── Cross-replica cache invalidation (Epic D4 §2.4) ──
# A mutation on replica A must clear replica B's cached sidebar entry, else B
# serves a stale list for up to the TTL. We reuse the EXISTING push_bus pub/sub
# (same Redis, already §10-signed-off) on a DISTINCT topic — NOT a new
# dependency and NOT a second bus module. Under the default inproc backend the
# bus publish just calls the local deliver → identical to a plain local
# invalidation (byte-identical single-box). Under redis the invalidation frame
# fans out to every replica's subscriber, which clears its own local entry.
_INVAL_TOPIC = 'tofu:cache:invalidate'
_inval_bus = None
_inval_bus_lock = threading.Lock()


def _local_invalidate(user_id) -> None:
    """Clear THIS replica's cached entry for ``user_id``.

    Pure local mutation — used both by the public ``invalidate_meta_cache``
    (local half) AND as the bus deliver callback when a PEER replica publishes
    an invalidation. Never publishes (no recursion)."""
    with _meta_cache_lock:
        e = _meta_cache_by_user.get(user_id)
        if e is not None:
            e['ts'] = 0


def _on_inval_frame(frame) -> None:
    """Bus subscriber callback: a peer replica published an invalidation."""
    try:
        uid = frame.get('userId') if isinstance(frame, dict) else None
    except Exception as _e:  # pragma: no cover
        logger.debug('[meta_cache] bad invalidation frame: %s', _e)
        return
    if uid is not None:
        _local_invalidate(uid)


def _get_inval_bus():
    global _inval_bus
    if _inval_bus is not None:
        return _inval_bus
    with _inval_bus_lock:
        if _inval_bus is not None:
            return _inval_bus
        try:
            from lib.agent_core.push_bus import make_push_bus
            bus = make_push_bus(_on_inval_frame, topic=_INVAL_TOPIC)
            bus.start()  # no-op for inproc; subscribes for redis
            _inval_bus = bus
        except Exception as e:
            logger.debug('[meta_cache] invalidation bus init failed (%s) — '
                         'local-only invalidation', e)
            _inval_bus = None
    return _inval_bus


def reset_inval_bus_for_test():
    """Test-only: drop the memoized invalidation bus so a fresh one is built."""
    global _inval_bus
    with _inval_bus_lock:
        _inval_bus = None


def _sidebar_limit() -> int:
    """Max conversations returned for the sidebar list (bounds the scan).

    A user with tens of thousands of conversations must not full-scan on every
    poll. Default 500 (well above any realistic visible sidebar); override via
    ``TOFU_SIDEBAR_MAX``. ``0`` disables the cap (legacy unbounded — not
    recommended)."""
    try:
        n = int(os.environ.get('TOFU_SIDEBAR_MAX', '') or '500')
    except (ValueError, TypeError):
        n = 500
    return max(0, n)


def _entry(user_id):
    e = _meta_cache_by_user.get(user_id)
    if e is None:
        e = {'data': None, 'etag': None, 'ts': 0, 'ttl': _META_TTL}
        _meta_cache_by_user[user_id] = e
    return e


def invalidate_meta_cache(user_id: int = DEFAULT_USER_ID):
    """Call after any conversation mutation (save / delete) for ``user_id``.

    Clears THIS replica's entry AND publishes a cross-replica invalidation on
    the shared bus so every peer replica clears its own entry (Epic D4 §2.4).
    Under the ``inproc`` backend the publish collapses to a local re-clear, so
    single-box behaviour is byte-identical. Best-effort: a bus failure never
    breaks the mutation path (the TTL remains the safety net)."""
    # Local half — always correct, byte-identical single-box path.
    _local_invalidate(user_id)
    # Cross-replica half — fan the invalidation out to peer replicas.
    bus = _get_inval_bus()
    if bus is not None:
        try:
            bus.publish({'channel': 'cache', 'type': 'invalidate', 'userId': user_id})
        except Exception as e:
            logger.debug('[meta_cache] cross-replica invalidation publish failed: %s', e)


def refresh_meta_cache_if_stale(db, user_id: int = DEFAULT_USER_ID):
    """Return (json_bytes, etag) for ``user_id``. Re-query DB only if TTL
    expired. The query is scoped to ``user_id`` AND bounded by a ``LIMIT`` so a
    huge history doesn't full-scan."""
    now = time.monotonic()
    with _meta_cache_lock:
        e = _entry(user_id)
        if e['data'] is not None and (now - e['ts']) < e['ttl']:
            return e['data'], e['etag']

    limit = _sidebar_limit()
    if limit > 0:
        rows = db.execute(
            '''SELECT id, title, created_at, updated_at, settings, msg_count
               FROM conversations WHERE user_id=? ORDER BY updated_at DESC
               LIMIT ?''',
            (user_id, limit)
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT id, title, created_at, updated_at, settings, msg_count
               FROM conversations WHERE user_id=? ORDER BY updated_at DESC''',
            (user_id,)
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
        e = _entry(user_id)
        e['data'] = payload
        e['etag'] = etag
        e['ts'] = time.monotonic()
    return payload, etag


__all__ = ['invalidate_meta_cache', 'refresh_meta_cache_if_stale']
