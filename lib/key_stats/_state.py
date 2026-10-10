"""lib/key_stats/_state.py — Shared singletons + low-level cache helpers.

This module owns the ONE-AND-ONLY copies of the mutable module-level state
for the whole ``lib.key_stats`` package:

    _cache, _lock, _siblings_cache, _siblings_lock, _last_resort_logged,
    _STATS_PATH, _SIBLINGS_TTL_SEC

Every other submodule imports these BY REFERENCE from here (and the package
``__init__`` re-exports them), so there is exactly one ``_cache`` / ``_lock``
per process.  Never duplicate this state anywhere else.

The low-level helpers that read/mutate the cache under ``_lock`` also live
here: ``_today``, ``_pair_key``, ``_list_siblings``, ``_load_unlocked``,
``_fold_namespaces_unlocked``,
``_update_store_unlocked``, ``_ensure_fresh_unlocked``, ``_new_entry``.
"""

import os
import sys as _sys
import threading
import time
from datetime import date
from typing import Callable

from lib.config_dir import config_path
from lib.json_store import (
    JsonStoreReadError,
    locked_path,
    read_json,
    update_json_atomic,
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
#   1. total attempts today >= MIN_ATTEMPTS  (avoid flapping on 1-2 failures)
#   2. success rate today < MIN_SUCCESS_RATE
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
# coincidentally unchanged. ``_FINGERPRINT_UNKNOWN`` forces one disk refresh
# after our own transactional write (the returned document can become stale
# immediately after the helper releases its cross-process lock).
_FINGERPRINT_UNKNOWN = object()
_disk_fingerprint = _FINGERPRINT_UNKNOWN
# Semantic writes that could not reach disk. Keeping callables (rather than a
# stale document snapshot) lets a later successful transaction replay them on
# top of whatever another process committed while storage was unavailable.
_pending_mutations: list[Callable[[dict, dict], None]] = []

# ── Siblings lookup cache ──
# Cached list of pair-keys (provider_id::credential_id) per owner+Provider,
# projected from live v2 Slots every _SIBLINGS_TTL_SEC seconds. Held under a
# dedicated lock so the siblings lookup never contends with the hot-path
# stats lock above (the hot path reads siblings OUTSIDE _lock and only
# passes the already-computed list into the locked block).
_SIBLINGS_TTL_SEC = 30.0
_siblings_lock = threading.Lock()
_siblings_cache = {
    'ts': 0.0,
    'by_provider': {},   # {provider_id: [pair_key, ...]}
}

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


def _list_siblings(provider_id: str) -> list:
    """Return live credential pair-keys under one owner+Provider namespace.

    Slots already carry the owner, Provider and Credential identities chosen
    by model-routing v2. Reading that projection avoids a second storage
    authority and cannot mix equal Provider IDs belonging to two owners.

    Scope = same *provider_id* only.  Cross-provider "last key" counting is
    deliberately incorrect (a Meituan key shouldn't be kept alive just because
    the user also has an OpenAI key).
    """
    now = time.monotonic()
    with _siblings_lock:
        if (now - _siblings_cache['ts']) < _SIBLINGS_TTL_SEC:
            cached = _siblings_cache['by_provider'].get(provider_id or 'default')
            if cached is not None:
                return list(cached)

    by_provider: dict[str, set[str]] = {}
    try:
        from lib.llm_dispatch.factory import get_dispatcher

        for slot in list(get_dispatcher().slots):
            slot_provider_id = slot.key_stats_provider_id()
            key_name = slot.key_stats_key_name()
            if key_name:
                by_provider.setdefault(slot_provider_id, set()).add(
                    _pair_key(slot_provider_id, key_name))
    except Exception as e:
        logger.debug('[KeyStats] siblings lookup failed (non-fatal): %s', e)
        by_provider = {}

    normalized = {
        key: sorted(values) for key, values in by_provider.items()}

    with _siblings_lock:
        _siblings_cache['ts'] = now
        _siblings_cache['by_provider'] = normalized

    return list(normalized.get(provider_id or 'default', []))


def _fold_namespaces_unlocked(stats: dict | None = None,
                              overrides: dict | None = None) -> bool:
    """Fold stats/overrides recorded under absorbed FACE namespaces into
    their ACCOUNT namespace. Caller must hold _lock. Returns True when the
    cache changed (the caller then persists).

    Why this exists (2026-07-29 invisible-total-outage): key-health history
    was recorded per duplicate face CARD (``sankuai_anthropic::…``) while the
    Settings UI renders one card per ACCOUNT (``sankuai::…``, charter #23).
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
    try:
        from lib import _load_server_config
        from lib.llm_dispatch.provider_face import account_namespace_map
        cfg = _load_server_config() or {}
        mapping = account_namespace_map(cfg.get('providers') or [])
    except Exception as e:
        logger.debug('[KeyStats] namespace fold map unavailable (non-fatal): %s', e)
        return False
    if not mapping:
        return False

    stats = _cache['stats'] if stats is None else stats
    overrides = _cache['overrides'] if overrides is None else overrides
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
                for field in ('success', 'failure', 'rate_limited',
                              'gateway_errors'):
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
        for field in ('success', 'failure', 'rate_limited',
                      'consecutive_429', 'gateway_errors'):
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


def _update_store_unlocked(
        mutator: Callable[[dict, dict], None]) -> bool:
    """Apply one semantic mutation to the latest on-disk document.

    Caller holds ``_lock``. The JSON-store transaction adds a stable sidecar
    flock, so separate Tofu processes sharing the config directory cannot
    overwrite each other's counters or manual decisions. On storage failure
    the same mutation is retained in this process's cache, while the existing
    file is left untouched for recovery/forensics.
    """
    global _disk_fingerprint
    _pk = _pkg()
    today = (_pk._today() if _pk is not None else _today())
    stats_path = _stats_path()
    candidate = None

    def _mutate(raw):
        nonlocal candidate
        document = _normalise_document(raw, today)
        for pending in _pending_mutations:
            pending(document['stats'], document['overrides'])
        mutator(document['stats'], document['overrides'])
        _fold_namespaces_unlocked(
            document['stats'], document['overrides'])
        candidate = document
        return document

    try:
        document = update_json_atomic(
            stats_path, _mutate, default=None, strict=True)
    except (OSError, JsonStoreReadError) as error:
        logger.warning('[KeyStats] Failed to update %s: %s — keeping the '
                       'mutation in memory without overwriting disk',
                       stats_path, error)
        if candidate is not None:
            # The locked read succeeded and only persistence failed. Preserve
            # the exact latest document that the transaction had mutated.
            _apply_document_unlocked(candidate)
        elif _cache.get('day') != today:
            _cache['day'] = today
            _cache['stats'] = {}
            if not isinstance(_cache.get('overrides'), dict):
                _cache['overrides'] = {}
            _last_resort_logged.clear()
        if candidate is None:
            mutator(_cache['stats'], _cache['overrides'])
            _fold_namespaces_unlocked()
            _cache['loaded'] = True
        _pending_mutations.append(mutator)
        # Keep serving the in-memory fallback while the same broken document
        # remains in place. An atomic repair/replacement changes the inode and
        # is detected by the next read.
        _disk_fingerprint = _file_fingerprint(stats_path)
        return False

    _apply_document_unlocked(document)
    _pending_mutations.clear()
    # Another process may commit immediately after update_json_atomic releases
    # its lock. Force the next read/decision to load the newest inode instead
    # of attaching a potentially newer fingerprint to this returned snapshot.
    _disk_fingerprint = _FINGERPRINT_UNKNOWN
    return True


def _load_unlocked():
    """Load a stable disk snapshot. Caller must hold ``_lock``."""
    global _disk_fingerprint
    _pk = _pkg()
    today = (_pk._today() if _pk is not None else _today())
    stats_path = _stats_path()
    try:
        # Read and fingerprint while holding the same stable sidecar lock used
        # by writers. The fingerprint therefore names the document we parsed.
        with locked_path(stats_path):
            raw = read_json(stats_path, default=None, strict=True)
            fingerprint = _file_fingerprint(stats_path)
        document = _normalise_document(raw, today)
    except (OSError, JsonStoreReadError) as error:
        logger.warning('[KeyStats] Failed to read %s: %s — using empty '
                       'in-memory stats without changing the file',
                       stats_path, error)
        if not _pending_mutations:
            _apply_document_unlocked(
                {'day': today, 'stats': {}, 'overrides': {}})
        _disk_fingerprint = _file_fingerprint(stats_path)
        return

    # Replay unsaved semantic writes onto this stable external snapshot before
    # exposing it. This also flushes them when an operator repairs/replaces a
    # previously corrupt store and the next operation is only a read.
    if _pending_mutations:
        _update_store_unlocked(lambda _stats, _overrides: None)
        return

    stored_day = (raw or {}).get('day') if isinstance(raw, dict) else ''
    if stored_day and stored_day != today:
        logger.info('[KeyStats] Day rollover %s -> %s — resetting stats '
                    '(preserving %d manual override(s))',
                    stored_day, today, len(document['overrides']))
    _apply_document_unlocked(document)
    _disk_fingerprint = fingerprint
    # Persist a rollover or namespace fold against the latest locked state.
    if stored_day != today or _fold_namespaces_unlocked():
        _update_store_unlocked(lambda _stats, _overrides: None)


def _ensure_fresh_unlocked():
    """Make sure cache is loaded and reset if the calendar day has changed.

    Stats (counters + ``exhausted`` flag) reset at each calendar-day
    boundary, but manual overrides are PERSISTENT — a key the user
    explicitly disabled (or enabled) stays that way until they clear
    the override via the Settings UI.
    """
    if not _cache['loaded']:
        _load_unlocked()
        return
    _pk = _pkg()
    today = (_pk._today() if _pk is not None else _today())
    if _cache['day'] != today:
        logger.info(
            '[KeyStats] Day rollover (in-memory) %s -> %s '
            '(preserving %d manual override(s))',
            _cache['day'], today, len(_cache.get('overrides') or {}))
        _cache['day'] = today
        _cache['stats'] = {}
        # DO NOT touch _cache['overrides'] — manual decisions persist.
        _last_resort_logged.clear()
        _update_store_unlocked(lambda _stats, _overrides: None)
        return
    fingerprint = _file_fingerprint(_stats_path())
    if (_disk_fingerprint is _FINGERPRINT_UNKNOWN
            or fingerprint != _disk_fingerprint):
        _load_unlocked()


def _new_entry() -> dict:
    return {
        'success': 0,
        'failure': 0,
        'rate_limited': 0,       # count of 429s today (informational)
        'consecutive_429': 0,    # current streak of 429s with no success
        # Gateway-class failures (502/503/504, upstream-vendor transients,
        # mid-stream SSE errors): counted on their OWN counter so a gateway
        # outage never enters the success-rate denominator nor the
        # auto-disable gate (2026-08-03 sankuai 502 storm auto-disabled 2 of
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
