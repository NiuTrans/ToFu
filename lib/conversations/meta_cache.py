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
    except (ValueError, TypeError) as e:
        logger.debug('[meta_cache] TOFU_SIDEBAR_MAX parse failed, using default: %s', e)
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


def notify_conv_changed(conv_id, *, rev=None, deleted: bool = False,
                        user_id: int = DEFAULT_USER_ID) -> None:
    """Invalidate the sidebar cache AND push a real-time change signal to clients.

    This is the single event-driven cross-device sync seam: every authoritative
    conversation mutation (task-result save, PUT, rename, settings/folder,
    message delete/edit, conversation delete) calls this so a sibling device
    reconciles WITHOUT a manual refresh or waiting for the periodic poll.

    The pushed frame is intentionally tiny — ``{type, convId, rev, userId}`` — a
    targeting HINT, not the conversation data. The client rev-gates on it (a
    frame whose ``rev`` is <= its known ``_serverRev`` for that conv is a no-op,
    which is what makes SELF-ECHO cheap) then does a targeted refetch of just
    that conversation. ``rev=None`` marks a metadata-only change (title / folder
    / activeTaskId) — the DB ``rev`` trigger only bumps on a messages change —
    so the client falls back to a debounced sidebar refresh, not a body refetch.

    ``user_id`` scopes the frame for multi-user forward-safety (Epic D4): the
    client ignores a frame whose ``userId`` is not its own, so once auth lands a
    fleet ``notify`` broadcast can't surface one user's conversation to another.

    Best-effort: a push failure never breaks the mutation path (the cache
    invalidation above + the periodic-poll fallback still reconcile).
    """
    # Cache invalidation (local + cross-replica) — unchanged existing behaviour.
    invalidate_meta_cache(user_id)
    # Real-time client signal — the new event-driven half.
    try:
        from lib.agent_core.push import push_event
        payload = {
            'type': 'conv_deleted' if deleted else 'conv_changed',
            'convId': conv_id,
            'userId': user_id,
        }
        if rev is not None:
            try:
                payload['rev'] = int(rev)
            except (TypeError, ValueError):
                logger.debug('[meta_cache] conv=%s non-int rev=%r dropped', conv_id, rev)
        push_event('notify', conv_id, payload)
    except Exception as e:
        logger.debug('[meta_cache] conv-changed push skipped conv=%s: %s', conv_id, e)


def refresh_meta_cache_if_stale(db, user_id: int = DEFAULT_USER_ID):
    """Return (json_bytes, etag) for ``user_id``. Re-query DB only if TTL
    expired. The query is scoped to ``user_id`` AND bounded by a ``LIMIT`` so a
    huge history doesn't full-scan."""
    now = time.monotonic()
    with _meta_cache_lock:
        e = _entry(user_id)
        if e['data'] is not None and (now - e['ts']) < e['ttl']:
            return e['data'], e['etag']

    # Authoritative total for the sidebar "N earlier not loaded" affordance
    # (C4). Computed ONLY here, at cache-rebuild — the 60s poll that hits the
    # warm cache never reaches this COUNT, so it stays off the hot path. A
    # COUNT(*) on the indexed user_id is cheap even for a huge history. Emitted
    # BEFORE the list SELECT so the bounded LIMIT query remains the LAST DB call
    # (the D4 bounded-scan guard inspects db.calls[-1]).
    total_count = None
    try:
        _tc = db.execute(
            'SELECT COUNT(*) AS c FROM conversations WHERE user_id=?',
            (user_id,)
        ).fetchone()
        total_count = (_tc['c'] if _tc else 0) or 0
    except Exception as _e:
        logger.debug('[meta_cache] total-count query failed: %s', _e)

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
    if total_count is None:
        total_count = len(rows)
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
        e['total'] = total_count
    return payload, etag


def get_cached_total(user_id: int = DEFAULT_USER_ID):
    """Return the authoritative conversation total captured at the last cache
    rebuild for ``user_id``, or ``None`` if the cache hasn't been populated yet.

    Read-only, lock-guarded, does NOT trigger a DB query — the route reads this
    right after ``refresh_meta_cache_if_stale`` (which populates it), so it is
    fresh without adding any cost to the poll path."""
    with _meta_cache_lock:
        e = _meta_cache_by_user.get(user_id)
        return e.get('total') if e else None


__all__ = ['invalidate_meta_cache', 'notify_conv_changed', 'refresh_meta_cache_if_stale',
           'get_cached_total']
