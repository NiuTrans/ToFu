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

# The sidebar is a metadata index, not a conversation-settings transport.  A
# full settings value can contain project summaries, paths, autopilot history
# and run reports (tens of KiB for one conversation).  Sending all of that for
# every row made a bounded 500-row poll exceed 1 MiB on the live single-user
# database.  Keep only fields that affect the unopened sidebar shell; opening a
# conversation still loads its complete settings through the body endpoint.
_SIDEBAR_SETTINGS_KEYS = frozenset({
    'pinned', 'pinnedAt', 'folderId', 'source',
    'lastMsgRole', 'lastMsgTimestamp', 'lastFinishReason',
    'lastMsgError', 'lastMsgHasOutput', '_reconciledAt',
    'activeTaskId',
})


def _sidebar_settings(value):
    """Return the bounded settings projection needed by sidebar-only shells."""
    settings = _safe_json(value, default={}, label='settings') or {}
    if not isinstance(settings, dict):
        return {}
    return {key: settings[key] for key in _SIDEBAR_SETTINGS_KEYS
            if key in settings}

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
            # A rebuild performs DB I/O without holding the global lock.  The
            # generation prevents an invalidation that lands during that I/O
            # from being overwritten by the older snapshot on return.
            e['generation'] = e.get('generation', 0) + 1


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
        e = {
            'data': None,
            'etag': None,
            'ts': 0,
            'ttl': _META_TTL,
            'generation': 0,
            # Serialize only this user's cache misses.  DB work still happens
            # outside the global lock, so hits and invalidations stay cheap.
            'rebuild_lock': threading.Lock(),
        }
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


# ── pt_conv_state_ssot P1 / pt_781ae072d6ee4e84: monotonic rev tuple ──
#
# runningTaskIdsRev on every notify frame is a two-element ``[ns, replica_id]``
# JSON array so the client can idempotent-gate with a plain lex compare and
# never accept a reordered stale frame:
#
#   * ns          — BOOT-EPOCH-ANCHORED nanoseconds: a wall-clock anchor
#                   sampled EXACTLY ONCE at import, advanced only by a
#                   ``time.monotonic_ns()`` delta. See _BOOT_EPOCH_NS below.
#   * replica_id  — reuses the ``TOFU_REPLICA_ID or pid`` convention already
#                   in ``lib.agent_core.push.PushHub._replica_id`` so we do
#                   NOT introduce a second replica-identity source. Two
#                   frames from DIFFERENT replicas are ordered by (ns, rid)
#                   lex — an arbitrary but stable tiebreak.
#
# ⚠️  WHY NOT RAW ``time.monotonic_ns()`` (the original P1 choice, pt_781ae072)
# ----------------------------------------------------------------------------
# monotonic_ns counts from THIS PROCESS's start. The client compares revs with
# ``>``, so a process-relative value is only meaningful against another value
# from the SAME process. It produced three shipped failures, mirror images of
# one another (all reproduced against the real reducer):
#
#   * RESTART → permanent FALSE-BUSY. monotonic_ns resets to ~0, so every
#     post-restart frame compares SMALLER than what connected tabs already
#     hold and is dropped forever. The client keeps the DEAD pre-restart task
#     id: the Stop button never clears and click-open attaches to a 404.
#     Measured on this host pre-fix: 3.93e16 (~455 days of uptime) — a fresh
#     process starting at ~1e9 loses by seven orders of magnitude.
#   * MULTI-REPLICA → starvation. A replica booted 10 days ago mints ~8.6e14;
#     one booted an hour ago ~3.6e12, so the younger replica's frames can
#     never land however recent they are.
#   * Combined with the client's old wall-clock CLEAR stamp (~1.78e18) →
#     permanent FALSE-IDLE: a conv that went busy then idle across a
#     reconnect became deaf on BOTH transports until F5.
#
# The anchor fixes all three at once because it puts every rev the fleet mints
# — across processes and hosts — into ONE comparable domain: wall time.
#
# REJECTED ALTERNATIVE: a persistent per-conv counter in the DB. Correct, but
# it puts a write+read on the hot path of every notify frame (measured 135 us
# median / 315 us p95 vs 0.19 us for the arithmetic below — ~720x) and makes
# the DB a hard dependency of the one signal that most needs to survive
# degraded conditions. This scheme needs no storage at all.

# The mint itself lives in lib.agent_core.rev_clock (2026-08-05) — it is a
# pure clock utility with no persistence dependency, and CORE modules
# (lib.agent_core.push) must not import lib.conversations.* (core/persistence
# boundary ratchet). Re-exported here so existing callers and the clock-domain
# test keep working unchanged.
from lib.agent_core.rev_clock import (  # noqa: F401  (re-export)
    _BOOT_EPOCH_NS,
    _replica_id,
    _running_task_ids_rev,
)


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
        # ── pt_conv_state_ssot P1: server-authoritative busy signal ──
        # Every notify frame carries a fresh (monotonic_ns, replica_id) tuple
        # so the client can idempotent-gate on strictly-increasing time. The
        # runningTaskIds list is a SNAPSHOT projection of the task registry
        # for THIS conv (the SSOT for "who is running"), never derived from
        # settings.activeTaskId. Deleted frames omit the list — a gone conv
        # has no busy concept — but keep the rev tuple so the client's gate
        # has a uniform key across conv_changed and conv_deleted.
        payload['runningTaskIdsRev'] = _running_task_ids_rev()
        if not deleted:
            try:
                from lib.tasks_pkg.manager._registry import snapshot_running_by_conv
                # pt_ab42421158214591: pass user_id through so a mutation
                # in user A's namespace does not surface user B's running
                # tasks in the projection. Only scope when the caller
                # passed a NON-DEFAULT user_id — DEFAULT_USER_ID=1
                # (single-user personal-install today) is coerced to
                # empty-string = unscoped, preserving the current
                # all-registry behaviour verbatim until auth is landed
                # and callers start passing real per-request user_ids.
                # A caller passing a string (AuthContext.user_id shape)
                # or any int != DEFAULT_USER_ID is treated as a real
                # tenant scope.
                if isinstance(user_id, int) and user_id == DEFAULT_USER_ID:
                    _snap_scope = ''
                else:
                    _snap_scope = str(user_id or '')
                snap = snapshot_running_by_conv(user_id=_snap_scope)
            except Exception as _re:
                logger.debug('[meta_cache] conv=%s registry snapshot failed: %s',
                             conv_id, _re)
                snap = {}
            payload['runningTaskIds'] = list(snap.get(conv_id, []))
        push_event('notify', conv_id, payload)
    except Exception as e:
        logger.debug('[meta_cache] conv-changed push skipped conv=%s: %s', conv_id, e)


def _query_meta_snapshot(db, user_id):
    """Build one uncached sidebar snapshot and its authoritative total."""
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
            '''SELECT id, title, created_at, updated_at, settings, msg_count, rev
               FROM conversations WHERE user_id=? ORDER BY updated_at DESC
               LIMIT ?''',
            (user_id, limit)
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT id, title, created_at, updated_at, settings, msg_count, rev
               FROM conversations WHERE user_id=? ORDER BY updated_at DESC''',
            (user_id,)
        ).fetchall()
    if total_count is None:
        total_count = len(rows)
    convs = []
    for r in rows:
        settings = _sidebar_settings(r['settings'])
        convs.append({
            'id': r['id'], 'title': r['title'],
            'messageCount': r['msg_count'] or 0,
            # The browser uses this DB-issued monotonic version as both its
            # staleness referee and the baseRev for the next CAS write.  Do
            # not make it fall back to cross-device wall-clock comparisons.
            'rev': int(r['rev'] or 0),
            'createdAt': r['created_at'], 'created_at': r['created_at'],
            'updatedAt': r['updated_at'], 'updated_at': r['updated_at'],
            'settings': settings,
        })
    payload = json.dumps(convs, ensure_ascii=False).encode('utf-8')
    etag = hashlib.md5(payload).hexdigest()[:16]

    return payload, etag, total_count


def refresh_meta_cache_if_stale(db, user_id: int = DEFAULT_USER_ID):
    """Return (json_bytes, etag) for ``user_id``.

    Cache misses are single-flight per user. A generation fence also prevents
    a DB snapshot started before a mutation from being installed as fresh
    after that mutation invalidates the cache.
    """
    now = time.monotonic()
    with _meta_cache_lock:
        e = _entry(user_id)
        if e['data'] is not None and (now - e['ts']) < e['ttl']:
            return e['data'], e['etag']
        rebuild_lock = e['rebuild_lock']

    with rebuild_lock:
        # Another request may have rebuilt while this one waited.
        with _meta_cache_lock:
            e = _entry(user_id)
            now = time.monotonic()
            if e['data'] is not None and (now - e['ts']) < e['ttl']:
                return e['data'], e['etag']

        # A mutation may race any individual DB read. Retry a bounded number
        # of times until the generation is stable; under continuous writes the
        # last snapshot is returned uncached, so the next poll retries instead
        # of blessing potentially stale data for the full TTL.
        for _attempt in range(3):
            with _meta_cache_lock:
                generation = _entry(user_id)['generation']
            payload, etag, total_count = _query_meta_snapshot(db, user_id)
            with _meta_cache_lock:
                e = _entry(user_id)
                if e['generation'] == generation:
                    e['data'] = payload
                    e['etag'] = etag
                    e['ts'] = time.monotonic()
                    e['total'] = total_count
                    return payload, etag

        logger.debug('[meta_cache] user=%s changed during three rebuilds; '
                     'returning uncached snapshot', user_id)
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
