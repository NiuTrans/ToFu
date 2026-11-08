"""lib/key_stats/_state.py — Shared singletons, cache helpers, persistence.

This module owns the ONE-AND-ONLY copies of the mutable module-level state
for the whole ``lib.key_stats`` package:

    _cache, _lock, _last_resort_logged, _STATS_PATH, _pending_mutations

Every other submodule imports these BY REFERENCE from here (and the package
``__init__`` re-exports them), so there is exactly one ``_cache`` / ``_lock``
per process.  Never duplicate this state anywhere else.

Persistence (docs/modules/auth_providers_billing.md, "Key-health
persistence") separates two classes of data:

  * Health telemetry — success / failure / credential-failure, 429 and
    gateway counters and their last errors — is reconstructible.
    ``_record_telemetry`` applies it to ``_cache`` at once and coalesces it
    per pair key; one background flush per
    ``KEY_STATS_FLUSH_INTERVAL_SECONDS`` merges it into the store, and
    ``shutdown_key_stats`` (production shutdown, interpreter exit) waits
    out a commit in progress and flushes the rest. Recording never touches
    disk or waits for a writer.
  * Admission decisions — manual overrides and billing stops — are durable:
    ``_commit_decision`` commits them, after all earlier telemetry, before
    returning.

Every store write is one ``_commit_batch`` transaction against the latest
document under the JSON store's sidecar flock, so separate Tofu processes
sharing the config directory merge instead of overwriting each other. The
flock covers only that read-modify-write; the namespace-fold map (a config
read) is resolved before it.
``_lock`` guards only memory and is never held across disk writes;
``_persist_lock`` serialises this process's transactions. Readers load the
opened inode without the writer lock and never write.

The low-level helpers that read/mutate the cache under ``_lock`` also live
here: ``_today``, ``_pair_key``, ``_list_siblings``, ``_load_unlocked``,
``_namespace_fold_map``, ``_fold_namespaces``, ``_ensure_fresh_unlocked``,
``_new_entry``.
"""

import atexit
import json
import os
import sys as _sys
import threading
from contextvars import ContextVar
from datetime import date
from typing import Callable

from lib.config_dir import config_path
from lib.json_store import (
    JsonStoreReadError,
    locked_path,
    read_json,
    write_json_atomic,
)
from lib.log import get_logger

logger = get_logger(__name__)


def _pkg():
    """Return the (possibly partially initialised) ``lib.key_stats`` module.

    Late-bound so internal calls to ``_today`` / ``_list_siblings`` and reads
    of ``_STATS_PATH`` resolve through ``lib.key_stats`` at call time.  This
    preserves the historical monkeypatch contract: test/debug code that does
    ``import lib.key_stats as ks; ks._today = ...`` (or
    ``ks._list_siblings = ...`` / ``ks._STATS_PATH = ...``) still steers the
    behaviour of the low-level helpers below.
    """
    return _sys.modules.get('lib.key_stats')


# ── Auto-disable thresholds ──
# A key is auto-disabled for the rest of the day when BOTH:
#   1. successes + attributed credential failures today >= MIN_ATTEMPTS
#   2. success rate within those credential-health outcomes < MIN_SUCCESS_RATE
# Generic failures (including legacy history) are telemetry, never evidence.
MIN_ATTEMPTS = 5
MIN_SUCCESS_RATE = 0.5

# NOTE (owner policy 2026-07-29): a 429 streak NEVER auto-disables a key.
# 429 means backpressure — the slot-local steering cooldown + RPM decay are
# the whole answer; only an explicit billing-stop (HTTP 402 /
# quota-exhausted) may disable a key for the day. The consecutive_429
# counter survives as pure UI telemetry.

_STATS_PATH = config_path('key_stats.json')
_lock = threading.Lock()
_cache = {
    'day': '',        # YYYY-MM-DD of currently loaded data
    'stats': {},      # {pair_key: {'success': int, 'failure': int, 'last_error': str}}
    'overrides': {},  # {pair_key: bool}  # explicit user overrides (PERSISTENT
                      # across day rollovers and restarts)
    'loaded': False,
}

# Fingerprint of the exact inode loaded into ``_cache``. Atomic writers replace
# the inode, so this detects another process's update even when size/mtime are
# coincidentally unchanged. ``_FINGERPRINT_UNKNOWN`` forces one disk refresh.
_FINGERPRINT_UNKNOWN = object()
_disk_fingerprint = _FINGERPRINT_UNKNOWN
# Store path whose latest known document (plus pending mutations) ``_cache``
# holds. Tests and debug code may repoint ``lib.key_stats._STATS_PATH``.
_cache_path: str | None = None

# Resource class ``fixed`` (docs/RESOURCE_CONTROL.md): a hardware-independent
# bound on how long reconstructible telemetry stays unwritten — the
# cross-process staleness and crash-loss window. It never delays, sizes or
# refuses a request.
KEY_STATS_FLUSH_INTERVAL_SECONDS = 2.0


class _TelemetryBatch:
    """Coalesced health telemetry for one store path and calendar day."""

    __slots__ = ('path', 'day', 'by_key')

    def __init__(self, path: str, day: str):
        self.path = path
        self.day = day
        self.by_key: dict[str, dict] = {}


class _Decision:
    """One admission decision (override or billing stop) awaiting disk."""

    __slots__ = ('path', 'day', 'day_scoped', 'mutate')

    def __init__(self, path: str, day: str, day_scoped: bool,
                 mutate: Callable[[dict, dict], None]):
        self.path = path
        self.day = day
        self.day_scoped = day_scoped
        self.mutate = mutate


# Mutations not yet on disk, oldest first. Semantic items (never a document
# snapshot) let a commit replay them onto whatever another process wrote in
# the meantime, and every (re)load replays them onto the fresh snapshot, so
# ``_cache`` is always "latest known store + this process's unwritten
# changes". Telemetry coalesces per pair key, so this stays bounded by keys
# and decisions even while storage is unavailable.
_pending_mutations: list = []
# This process's commit in progress (guarded by ``_lock``): its store path,
# the batch it took from ``_pending_mutations``, and the fingerprint of the
# document it read under the flock (``None`` until read). A snapshot with
# that base fingerprint predates the commit and still needs the batch.
_inflight_path: str | None = None
_inflight_batch: list = []
_inflight_base = None
# Serialises this process's store transactions; acquired before ``_lock``.
_persist_lock = threading.Lock()
# Demand-driven flush worker (guarded by ``_lock``); retires when idle.
_flusher: threading.Thread | None = None
_flusher_stop = threading.Event()

# Track which (day, pk) combinations have already emitted the "last-resort"
# info log so we don't spam the log on every dispatch call.
_last_resort_logged: set[tuple[str, str]] = set()


def _stats_path() -> str:
    """Resolve the on-disk stats path, honouring a monkeypatched
    ``lib.key_stats._STATS_PATH`` (the historical test contract).

    Falls back to this module's own ``_STATS_PATH`` when the package attr is
    unavailable (e.g. during package initialisation).
    """
    _pk = _pkg()
    if _pk is not None:
        return getattr(_pk, '_STATS_PATH', _STATS_PATH)
    return _STATS_PATH


def _today() -> str:
    return date.today().isoformat()


def _pair_key(provider_id: str, key_name: str) -> str:
    return f'{provider_id or "default"}::{key_name or ""}'


# The selecting dispatcher's own credential index for one selection, set by
# ``key_admission_batch(credentials=...)``. A pick reads siblings while it
# holds the dispatcher lock; resolving the dispatcher there may bind its
# resource port, which samples the dispatcher under that same lock.
_SELECTION_CREDENTIALS: ContextVar[Callable[[str], list] | None] = ContextVar(
    'key_admission_credentials', default=None)


def _list_siblings(provider_id: str) -> list:
    """Return live credential pair-keys under one owner+Provider namespace.

    Slots already carry the owner, Provider and Credential identities chosen
    by model-routing v2. The dispatcher's slot pool indexes those identities
    as slots are published, withdrawn and reloaded
    (``SlotPool.key_stats_key_names``), so this is one lookup, exact at every
    moment, that avoids a second storage authority and cannot mix equal
    Provider IDs belonging to two owners. Sorted, so "the last key" is stable.

    Scope = same *provider_id* only.  Cross-provider "last key" counting is
    deliberately incorrect (a YourProvider key shouldn't be kept alive just because
    the user also has an OpenAI key).
    """
    namespace = provider_id or 'default'
    try:
        credentials = _SELECTION_CREDENTIALS.get()
        if credentials is None:
            from lib.llm_dispatch.factory import get_dispatcher

            credentials = get_dispatcher().slots.key_stats_key_names
        names = credentials(namespace)
    except Exception as e:
        logger.debug('[KeyStats] siblings lookup failed (non-fatal): %s', e)
        return []
    return [_pair_key(namespace, name) for name in names]


def _namespace_fold_map() -> dict:
    """Return ``{absorbed face namespace: account namespace}`` for the fold.

    Reads the server config (and imports the face module on first use), so
    a store transaction resolves it before it takes the store's flock.
    """
    try:
        from lib import _load_server_config
        from lib.llm_dispatch.provider_face import account_namespace_map
        cfg = _load_server_config() or {}
        return account_namespace_map(cfg.get('providers') or [])
    except Exception as e:
        logger.debug('[KeyStats] namespace fold map unavailable (non-fatal): %s', e)
        return {}


def _fold_namespaces(stats: dict, overrides: dict, mapping: dict) -> bool:
    """Fold stats/overrides recorded under absorbed FACE namespaces into
    their ACCOUNT namespace, per *mapping* (:func:`_namespace_fold_map`).
    Pure memory work on the caller's maps. Returns True when the maps
    changed (the caller then persists).

    Why this exists (2026-07-29 invisible-total-outage): key-health history
    was recorded per duplicate face CARD (``example_corp_anthropic::…``) while the
    Settings UI renders one card per ACCOUNT (``example-corp::…``, charter #23).
    The account/face config merge stops NEW face-namespace recordings, but
    the state already on disk — day-scoped billing-stops and PERSISTENT
    manual overrides — would stay orphaned on a namespace nothing reads and
    nothing renders: stops silently lost (a live 402 re-burned per model),
    manual decisions silently inert.

    The face→account mapping is computed ONCE in
    ``lib.llm_dispatch.provider_face.account_namespace_map`` — never
    re-derived here (a second copy of the face rule would drift).

    Merge semantics:
      * counters (success/failure/rate_limited) SUM across faces — one
        physical key, one health history;
      * consecutive_429 takes MAX (a streak is a point-in-time value);
      * exhausted ORs, exhausted_models UNIONs (the account's own entries
        win per-model conflicts — it is the surviving namespace);
      * an empty account last_error inherits the face's;
      * overrides MOVE only when the account has none — the account's
        explicit user decision always wins.
      * unknown-shape key names (not ``<ns>_key_<i>``) are left untouched:
        the fold converges what it knows, it never deletes history.
    """
    if not mapping:
        return False
    changed = False

    def _dst_pk(src_ns, dst_ns, key_name):
        if key_name.startswith(src_ns + '_key_'):
            return f'{dst_ns}::{dst_ns}{key_name[len(src_ns):]}'
        return None

    for src_ns, dst_ns in mapping.items():
        if src_ns == dst_ns:
            continue
        for pk in [k for k in stats
                   if k.split('::', 1)[0] == src_ns]:
            dst_pk = _dst_pk(src_ns, dst_ns, pk.split('::', 1)[1])
            if dst_pk is None:
                logger.warning('[KeyStats] namespace fold: unrecognised key '
                               'name in %s — kept as-is', pk)
                continue
            src_entry = stats.pop(pk)
            dst_entry = stats.get(dst_pk)
            if dst_entry is None:
                stats[dst_pk] = src_entry
            else:
                for field in ('success', 'failure', 'credential_failures',
                              'rate_limited', 'gateway_errors'):
                    dst_entry[field] = (int(dst_entry.get(field) or 0)
                                        + int(src_entry.get(field) or 0))
                dst_entry['consecutive_429'] = max(
                    int(dst_entry.get('consecutive_429') or 0),
                    int(src_entry.get('consecutive_429') or 0))
                dst_entry['exhausted'] = (bool(dst_entry.get('exhausted'))
                                          or bool(src_entry.get('exhausted')))
                merged_models = dict(src_entry.get('exhausted_models') or {})
                merged_models.update(dst_entry.get('exhausted_models') or {})
                dst_entry['exhausted_models'] = merged_models
                if not dst_entry.get('last_error'):
                    dst_entry['last_error'] = src_entry.get('last_error') or ''
                if not dst_entry.get('last_credential_error'):
                    dst_entry['last_credential_error'] = (
                        src_entry.get('last_credential_error') or '')
            changed = True
        for pk in [k for k in overrides
                   if k.split('::', 1)[0] == src_ns]:
            dst_pk = _dst_pk(src_ns, dst_ns, pk.split('::', 1)[1])
            if dst_pk is None:
                continue
            if dst_pk not in overrides:
                overrides[dst_pk] = overrides[pk]
            del overrides[pk]
            changed = True

    if changed:
        logger.info('[KeyStats] folded absorbed face namespace(s) into '
                    'account namespace: %s', mapping)
    return changed


def _file_fingerprint(path: str):
    try:
        stat = os.stat(path)
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except FileNotFoundError as error:
        logger.debug('[KeyStats] stats file is not present yet: %s (%s)',
                     path, error)
        return ('missing',)
    except OSError as error:
        logger.debug('[KeyStats] could not fingerprint %s: %s', path, error)
        return _FINGERPRINT_UNKNOWN


def _normalise_document(raw, today: str) -> dict:
    """Validate the store and apply the daily-reset contract in memory."""
    if raw is None:
        return {'day': today, 'stats': {}, 'overrides': {}}
    if not isinstance(raw, dict):
        raise JsonStoreReadError('key stats store is not an object')
    stored_day = raw.get('day') or ''
    if not isinstance(stored_day, str):
        raise JsonStoreReadError('key stats store has an invalid day')
    stats = raw.get('stats', {})
    overrides = raw.get('overrides', {})
    if not isinstance(stats, dict) or not isinstance(overrides, dict):
        raise JsonStoreReadError('key stats store has invalid nested maps')
    if any(not isinstance(key, str) or not isinstance(entry, dict)
           for key, entry in stats.items()):
        raise JsonStoreReadError('key stats store has an invalid stats entry')
    if any(not isinstance(key, str) or not isinstance(value, bool)
           for key, value in overrides.items()):
        raise JsonStoreReadError('key stats store has an invalid override')
    if any(
            entry.get('exhausted_models') is not None
            and not isinstance(entry.get('exhausted_models'), dict)
            for entry in stats.values()):
        raise JsonStoreReadError(
            'key stats store has invalid per-model exhaustion data')

    # Counter coercion preserves the historical tolerance for numeric strings,
    # while rejecting valid-JSON-but-unusable values (objects, NaN, etc.) as a
    # corrupt document. A hot-path record call must never crash halfway through
    # because ``int(entry['success'])`` discovered a malformed leaf.
    normalised_stats = {}
    for key, entry in stats.items():
        clean_entry = dict(entry)
        for field in ('success', 'failure', 'credential_failures',
                      'rate_limited', 'consecutive_429', 'gateway_errors'):
            if field not in clean_entry:
                continue
            try:
                value = int(clean_entry.get(field) or 0)
            except (TypeError, ValueError, OverflowError) as error:
                raise JsonStoreReadError(
                    f'key stats store has invalid {field!r} counter') from error
            if value < 0:
                raise JsonStoreReadError(
                    f'key stats store has negative {field!r} counter')
            clean_entry[field] = value
        normalised_stats[key] = clean_entry
    return {
        'day': today,
        'stats': normalised_stats if stored_day == today else {},
        'overrides': overrides,
    }


def _apply_document_unlocked(document: dict) -> None:
    previous_day = _cache.get('day')
    _cache['day'] = document['day']
    _cache['stats'] = document['stats']
    _cache['overrides'] = document['overrides']
    _cache['loaded'] = True
    if previous_day and previous_day != document['day']:
        _last_resort_logged.clear()


_TELEMETRY_COUNTERS = ('success', 'failure', 'credential_failures',
                       'rate_limited', 'gateway_errors')
_TELEMETRY_TEXT = ('last_error', 'last_credential_error')


def _merge_telemetry(total: dict, delta: dict) -> None:
    """Fold *delta* into the earlier coalesced *total*, preserving order.

    ``streak_reset`` records that a non-429 outcome ended the 429 streak;
    ``streak_add`` counts the 429s after the last reset.
    """
    for field in _TELEMETRY_COUNTERS:
        if delta.get(field):
            total[field] = total.get(field, 0) + delta[field]
    if delta.get('streak_reset'):
        total['streak_reset'] = True
        total['streak_add'] = delta.get('streak_add', 0)
    elif delta.get('streak_add'):
        total['streak_add'] = total.get('streak_add', 0) + delta['streak_add']
    for field in _TELEMETRY_TEXT:
        if field in delta:
            total[field] = delta[field]


def _apply_telemetry(entry: dict, delta: dict) -> None:
    for field in _TELEMETRY_COUNTERS:
        if delta.get(field):
            entry[field] = int(entry.get(field) or 0) + delta[field]
    if delta.get('streak_reset'):
        entry['consecutive_429'] = delta.get('streak_add', 0)
    elif delta.get('streak_add'):
        entry['consecutive_429'] = (
            int(entry.get('consecutive_429') or 0) + delta['streak_add'])
    for field in _TELEMETRY_TEXT:
        if field in delta:
            entry[field] = delta[field]


def _apply_pending(item, document: dict) -> None:
    """Apply one pending item to a normalised document of its day."""
    if isinstance(item, _TelemetryBatch):
        # Stats are day-scoped: a past day's telemetry never counts toward
        # the next day, exactly as if it had been flushed before midnight.
        if item.day != document['day']:
            return
        for pk, delta in item.by_key.items():
            _apply_telemetry(_entry_for_update(document['stats'], pk), delta)
    elif not item.day_scoped or item.day == document['day']:
        item.mutate(document['stats'], document['overrides'])


def _replay_pending_unlocked(document: dict, path: str, *,
                             include_inflight: bool = False) -> None:
    items = _pending_mutations
    if include_inflight and _inflight_path == path:
        items = _inflight_batch + _pending_mutations
    for item in items:
        if item.path == path:
            _apply_pending(item, document)


def _current_day() -> str:
    _pk = _pkg()
    return _pk._today() if _pk is not None else _today()


def _roll_cache_day_unlocked(today: str) -> None:
    """Reset day-scoped stats in memory; manual overrides persist."""
    if _cache['day'] == today:
        return
    logger.info(
        '[KeyStats] Day rollover (in-memory) %s -> %s '
        '(preserving %d manual override(s))',
        _cache['day'], today, len(_cache.get('overrides') or {}))
    _cache['day'] = today
    _cache['stats'] = {}
    _last_resort_logged.clear()


def _tail_batch_unlocked(path: str, day: str) -> _TelemetryBatch:
    tail = _pending_mutations[-1] if _pending_mutations else None
    if (isinstance(tail, _TelemetryBatch) and tail.path == path
            and tail.day == day):
        return tail
    tail = _TelemetryBatch(path, day)
    _pending_mutations.append(tail)
    return tail


def _requeue_failed_unlocked(path: str, batch: list) -> None:
    """Put a failed commit's *batch* back ahead of newer mutations.

    Adjacent telemetry of one day merges in order, so retries during a long
    outage keep at most one batch per decision instead of one per attempt.
    """
    merged: list = []
    for item in batch + [m for m in _pending_mutations if m.path == path]:
        previous = merged[-1] if merged else None
        if (isinstance(item, _TelemetryBatch)
                and isinstance(previous, _TelemetryBatch)
                and previous.day == item.day):
            for pk, delta in item.by_key.items():
                _merge_telemetry(previous.by_key.setdefault(pk, {}), delta)
            continue
        merged.append(item)
    _pending_mutations[:] = merged + [
        m for m in _pending_mutations if m.path != path]


def _claim_flusher_unlocked() -> threading.Thread | None:
    """Register a flush worker unless one exists. Caller holds ``_lock``.

    Returns the new, unstarted thread for :func:`_start_flusher`. Starting it
    here would hold ``_lock`` until the new thread first runs, which under GIL
    pressure stalls every admission read (and the dispatcher pick holding the
    global dispatcher lock while it reads key health).
    """
    global _flusher
    if _flusher is not None:
        return None
    _flusher = threading.Thread(
        target=_flush_loop, name='tofu-key-stats-flush', daemon=True)
    return _flusher


def _start_flusher(thread: threading.Thread | None) -> None:
    """Start a worker claimed under ``_lock``; the caller has released it."""
    global _flusher
    if thread is None:
        return
    try:
        thread.start()
    except RuntimeError as error:
        with _lock:
            if _flusher is thread:
                _flusher = None
        logger.warning('[KeyStats] Could not start the flush worker: %s — '
                       'mutations stay pending until the next flush', error)


def _flush_loop() -> None:
    global _flusher
    me = threading.current_thread()
    retired = False
    try:
        while not retired:
            stopping = _flusher_stop.wait(KEY_STATS_FLUSH_INTERVAL_SECONDS)
            try:
                flush_key_stats()
            except Exception:
                logger.exception('[KeyStats] Flush failed; mutations stay '
                                 'pending for the next attempt')
            with _lock:
                # Retire in the same critical section that observes an empty
                # queue, so a concurrent producer either sees no worker and
                # starts one, or its mutation keeps this worker alive.
                if stopping or not _pending_mutations:
                    if _flusher is me:
                        _flusher = None
                    retired = True
    finally:
        if not retired:
            with _lock:
                if _flusher is me:
                    _flusher = None


def _commit_batch(path: str, batch: list, today: str):
    """Merge *batch* into the latest store document under its sidecar flock.

    Every process sharing the store queues on that flock, at interpreter exit
    all at once, so it covers only the read-modify-write of the document.
    """
    global _inflight_base
    fold_map = _namespace_fold_map()
    with locked_path(path):
        raw = read_json(path, default=None, strict=True)
        base = _file_fingerprint(path)
        # Readers never take the flock, so taking ``_lock`` here cannot
        # invert any lock order.
        with _lock:
            _inflight_base = base
        document = _normalise_document(raw, today)
        for item in batch:
            _apply_pending(item, document)
        _fold_namespaces(document['stats'], document['overrides'], fold_map)
        write_json_atomic(path, document, allow_nan=False)
        # Fingerprint our own inode while other writers are still excluded,
        # so the next read does not reload the document we just wrote.
        fingerprint = _file_fingerprint(path)
    return document, fingerprint


def _persist_path_locked(path: str) -> bool:
    """Commit this process's pending mutations for *path*.

    Caller holds ``_persist_lock`` and not ``_lock``. On storage failure the
    batch returns to the head of ``_pending_mutations`` (still applied in
    memory) and the existing file is left untouched for recovery/forensics.
    """
    global _inflight_path, _inflight_batch, _inflight_base
    global _disk_fingerprint, _cache_path
    with _lock:
        batch = [item for item in _pending_mutations if item.path == path]
        if not batch:
            return True
        _pending_mutations[:] = [
            item for item in _pending_mutations if item.path != path]
        _inflight_path, _inflight_batch, _inflight_base = path, batch, None
        today = _current_day()
    try:
        document, fingerprint = _commit_batch(path, batch, today)
    except (OSError, ValueError, JsonStoreReadError) as error:
        with _lock:
            _inflight_path, _inflight_batch, _inflight_base = None, [], None
            _requeue_failed_unlocked(path, batch)
        logger.warning('[KeyStats] Failed to update %s: %s — keeping %d '
                       'pending mutation(s) in memory without overwriting '
                       'disk', path, error, len(batch))
        return False
    except BaseException:
        with _lock:
            _inflight_path, _inflight_batch, _inflight_base = None, [], None
            _requeue_failed_unlocked(path, batch)
        raise
    with _lock:
        _inflight_path, _inflight_batch, _inflight_base = None, [], None
        if path == _stats_path():
            _replay_pending_unlocked(document, path)
            _apply_document_unlocked(document)
            _cache_path = path
            _disk_fingerprint = fingerprint
    return True


def flush_key_stats(timeout: float | None = None) -> bool:
    """Durably merge every mutation pending now into its store.

    Returns False when a commit failed (its mutations stay pending and
    applied in memory) or when ``timeout`` expired while another commit of
    this process held the store.
    """
    acquired = _persist_lock.acquire(
        timeout=-1 if timeout is None else max(0.0, timeout))
    if not acquired:
        return False
    try:
        with _lock:
            paths = list(dict.fromkeys(
                item.path for item in _pending_mutations))
        committed = True
        for path in paths:
            committed = _persist_path_locked(path) and committed
        return committed
    finally:
        _persist_lock.release()


def shutdown_key_stats() -> bool:
    """Join the flush worker and persist everything pending.

    Production shutdown calls this after dispatch producers stop; interpreter
    exit calls it for every other process. It never abandons work it can
    still commit: it waits out the worker's commit in progress, however long
    other processes hold the store's flock, then commits what was recorded
    after that commit took its batch. Only the production shutdown's hard
    deadline bounds it (docs/modules/auth_providers_billing.md, "Key-health
    persistence"). A later mutation starts a new demand-driven worker.
    Returns True when every commit succeeded.
    """
    with _lock:
        thread = _flusher
    _flusher_stop.set()
    try:
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join()
            except RuntimeError:
                # Claimed by a producer that has not started it yet; the
                # flush below persists everything that worker would have.
                logger.debug('[KeyStats] flush worker not started at shutdown')
        return flush_key_stats()
    finally:
        _flusher_stop.clear()


def _record_telemetry(pk: str, delta: dict) -> None:
    """Apply reconstructible telemetry now; persist it with the next flush.

    Never touches disk and never waits for a writer: the caller's request
    thread holds ``_lock`` only for this in-memory update.
    """
    today = _current_day()
    path = _stats_path()
    with _lock:
        if _cache['loaded'] and _cache_path == path:
            _roll_cache_day_unlocked(today)
            _apply_telemetry(_entry_for_update(_cache['stats'], pk), delta)
        tail = _tail_batch_unlocked(path, today)
        _merge_telemetry(tail.by_key.setdefault(pk, {}), delta)
        worker = _claim_flusher_unlocked()
    _start_flusher(worker)


def _commit_decision(mutate: Callable[[dict, dict], None], *,
                     day_scoped: bool) -> bool:
    """Apply an admission decision and make it durable before returning.

    Telemetry recorded earlier for the same store commits first in the same
    transaction, so a decision's resets (a re-enable clearing the 429
    streak) keep their order. On storage failure the decision stays applied
    in memory and pending, and the flush worker retries it. Never call with
    ``_lock`` held.
    """
    today = _current_day()
    path = _stats_path()
    with _persist_lock:
        with _lock:
            if _cache['loaded'] and _cache_path == path:
                _roll_cache_day_unlocked(today)
                mutate(_cache['stats'], _cache['overrides'])
            _pending_mutations.append(
                _Decision(path, today, day_scoped, mutate))
        committed = _persist_path_locked(path)
    if not committed:
        with _lock:
            worker = _claim_flusher_unlocked()
        _start_flusher(worker)
    return committed


def _read_store_snapshot(path: str):
    """Read one complete store inode without taking the writer lock.

    Writers publish by atomic replace, so an opened inode never changes and
    its ``fstat`` names exactly the bytes parsed. Readers therefore never
    wait for another process's commit.
    """
    try:
        with open(path, 'rb') as handle:
            stat = os.fstat(handle.fileno())
            payload = handle.read()
    except FileNotFoundError:
        return None, ('missing',)
    except OSError as error:
        raise JsonStoreReadError(
            f'failed to read key stats store {path}') from error
    try:
        raw = json.loads(payload.decode('utf-8'))
    except ValueError as error:
        raise JsonStoreReadError(
            f'invalid JSON in key stats store {path}') from error
    return raw, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _load_unlocked():
    """(Re)load the latest store snapshot. Caller must hold ``_lock``.

    Never writes and never waits for a writer. Pending mutations replay onto
    the snapshot; a rollover or namespace fold is persisted by the next flush.
    """
    global _disk_fingerprint, _cache_path
    today = _current_day()
    stats_path = _stats_path()
    try:
        raw, fingerprint = _read_store_snapshot(stats_path)
        document = _normalise_document(raw, today)
    except JsonStoreReadError as error:
        logger.warning('[KeyStats] Failed to read %s: %s — serving the last '
                       'loaded state and pending mutations without changing '
                       'the file', stats_path, error)
        if not (_cache['loaded'] and _cache_path == stats_path):
            document = {'day': today, 'stats': {}, 'overrides': {}}
            _replay_pending_unlocked(
                document, stats_path, include_inflight=True)
            _apply_document_unlocked(document)
            _cache_path = stats_path
        # Keep serving memory while the same broken document remains in
        # place; an atomic repair changes the inode and the next read sees it.
        _disk_fingerprint = _file_fingerprint(stats_path)
        return

    stored_day = (raw or {}).get('day') if isinstance(raw, dict) else ''
    if stored_day and stored_day != today:
        logger.info('[KeyStats] Day rollover %s -> %s — resetting stats '
                    '(preserving %d manual override(s))',
                    stored_day, today, len(document['overrides']))
    folded = _fold_namespaces(
        document['stats'], document['overrides'], _namespace_fold_map())
    _replay_pending_unlocked(
        document, stats_path,
        include_inflight=_inflight_base in (None, fingerprint))
    _apply_document_unlocked(document)
    _cache_path = stats_path
    _disk_fingerprint = fingerprint
    if stored_day != today or folded:
        _tail_batch_unlocked(stats_path, today)


def _observe_store() -> tuple[str, object]:
    """Fingerprint the store for :func:`_ensure_fresh_unlocked`.

    Call it before acquiring ``_lock``: the ``stat`` releases the GIL, and a
    holder of ``_lock`` waiting to get the GIL back stalls every reader.
    """
    path = _stats_path()
    return path, _file_fingerprint(path)


def _ensure_fresh_unlocked(observed: tuple[str, object] | None = None):
    """Make sure cache is loaded and reset if the calendar day has changed.

    Stats (counters + ``exhausted`` flag) reset at each calendar-day
    boundary, but manual overrides are PERSISTENT — a key the user
    explicitly disabled (or enabled) stays that way until they clear
    the override via the Settings UI. Costs one ``stat`` when nothing
    changed; never writes. *observed* is a fingerprint the caller took with
    :func:`_observe_store` just before acquiring ``_lock``; it replaces this
    call's own ``stat``.
    """
    stats_path = _stats_path()
    if not _cache['loaded'] or _cache_path != stats_path:
        _load_unlocked()
        return
    today = _current_day()
    if _cache['day'] != today:
        _roll_cache_day_unlocked(today)
        # DO NOT touch _cache['overrides'] — manual decisions persist.
        _tail_batch_unlocked(stats_path, today)
        return
    if _inflight_path == stats_path:
        # This process is replacing the file; its commit installs the result.
        return
    if observed is not None and observed[0] == stats_path:
        fingerprint = observed[1]
    else:
        fingerprint = _file_fingerprint(stats_path)
    if (_disk_fingerprint is _FINGERPRINT_UNKNOWN
            or fingerprint != _disk_fingerprint):
        _load_unlocked()


def _after_fork_in_child() -> None:
    """A forked child must never replay its parent's unwritten mutations."""
    global _flusher, _inflight_path, _inflight_batch, _inflight_base
    _lock._at_fork_reinit()
    _persist_lock._at_fork_reinit()
    _pending_mutations.clear()
    _flusher = None
    _inflight_path, _inflight_batch, _inflight_base = None, [], None
    _cache['loaded'] = False


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork_in_child)
atexit.register(shutdown_key_stats)


def _new_entry() -> dict:
    return {
        'success': 0,
        'failure': 0,
        'credential_failures': 0,
        'last_credential_error': '',
        'rate_limited': 0,       # count of 429s today (informational)
        'consecutive_429': 0,    # current streak of 429s with no success
        # Gateway-class failures (502/503/504, upstream-vendor transients,
        # mid-stream SSE errors): counted on their OWN counter so a gateway
        # outage never enters the success-rate denominator nor the
        # auto-disable gate (2026-08-03 example-corp 502 storm auto-disabled 2 of
        # 3 healthy keys for the day).
        'gateway_errors': 0,
        'last_error': '',
        'exhausted': False,
        # Per-model billing-stops: {model: reason}. A quota error carries a
        # model dimension (the slot that observed it) — on an aggregating
        # gateway one key proxies SEVERAL upstream vendors (kimi→Moonshot,
        # qwen→Aliyun), so a billing-stop on one model says nothing about
        # the others routed through the same key. Key-wide ``exhausted`` is
        # reserved for callers that genuinely cannot name a model.
        'exhausted_models': {},
    }


def _entry_for_update(stats: dict, pk: str) -> dict:
    """Return a mutable entry, repairing a malformed leaf defensively."""
    entry = stats.get(pk)
    if not isinstance(entry, dict):
        entry = _new_entry()
        stats[pk] = entry
    return entry
