"""Feature-flag persistence + hot-reload.

Moved out of ``routes/common.py`` (2026-06). ``apply_feature_updates`` reads
``features.json``, merges the requested flag changes, writes it back,
audit-logs each change, and hot-reloads the live ``lib.*`` toggles (with the
``needs_restart`` caveat for plugin features that mount routes at import time).
No Flask
dependency — the ``POST /api/v1/features`` handler just parses the body,
calls this, and ``jsonify``s the result.
"""

import re
import threading

from lib.config_dir import config_path as _config_path
from lib.identity import PrincipalContext
from lib.json_store import JsonStoreReadError, read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

# Base boolean feature flags this store manages: (json key, lib attribute).
# Plugin flags (e.g. trading) are appended dynamically from the feature
# registry by _managed_flags() so core never names an optional feature.
_BASE_BOOL_FLAGS = [
    ('pptx_translate_enabled', 'PPTX_TRANSLATE_ENABLED'),
    ('cache_extended_ttl', 'CACHE_EXTENDED_TTL'),
    ('debug_mode', 'DEBUG_MODE'),
    ('optimizer_enabled', 'OPTIMIZER_ENABLED'),
]
_APPLY_LOCK = threading.Lock()
_PUBLIC_FLAG_KEY = re.compile(r'^[a-z][a-z0-9_]{0,79}$')
_PUBLIC_FLAG_LIMIT = 256
_PUBLIC_FLAG_RESERVED_KEYS = frozenset({'ok', 'request_id'})


def feature_flags_snapshot() -> dict[str, bool]:
    """Return the live deployment flags exposed to browser/API consumers."""
    import lib as _lib

    flags = {
        'pptx_translate_enabled': bool(getattr(
            _lib, 'PPTX_TRANSLATE_ENABLED', False)),
        'cache_extended_ttl': bool(getattr(
            _lib, 'CACHE_EXTENDED_TTL', False)),
        'debug_mode': bool(getattr(_lib, 'DEBUG_MODE', False)),
        'optimizer_enabled': bool(getattr(
            _lib, 'OPTIMIZER_ENABLED', True)),
        'artifacts_enabled': bool(getattr(
            _lib, 'ARTIFACTS_ENABLED', True)),
    }
    try:
        from lib.feature_registry import registered_flags
        for feature in registered_flags():
            if len(flags) >= _PUBLIC_FLAG_LIMIT:
                logger.debug(
                    '[Features] public flag projection capped at %d entries',
                    _PUBLIC_FLAG_LIMIT)
                break
            if (not _PUBLIC_FLAG_KEY.fullmatch(feature.json_key)
                    or feature.json_key in _PUBLIC_FLAG_RESERVED_KEYS
                    or feature.json_key in flags):
                logger.debug(
                    '[Features] invalid/reserved plugin flag omitted: %r',
                    feature.json_key)
                continue
            flags[feature.json_key] = bool(getattr(
                _lib, feature.env_key, feature.default))
    except Exception as exc:
        logger.debug('[Features] plugin flag projection failed: %s', exc)
    return flags


def _managed_flags():
    """Return (json_key, lib_attr) for base flags + registered plugin flags."""
    flags = list(_BASE_BOOL_FLAGS)
    try:
        from lib.feature_registry import registered_flags
        for f in registered_flags():
            flags.append((f.json_key, f.env_key))
    except Exception as e:
        logger.debug('[Features] plugin flag enumeration failed: %s', e)
    return flags


def read_features() -> dict:
    """Read features.json (empty dict on failure)."""
    features_path = _config_path('features.json')
    data = read_json(features_path, default={})
    if not isinstance(data, dict):
        logger.warning('[Features] Ignoring non-object features.json')
        return {}
    return data


def apply_feature_updates(data: dict, *, principal: PrincipalContext):
    """Merge requested flag changes into features.json + hot-reload lib toggles.

    Args:
        data: subset of the managed flag keys → new (bool) values.

    Returns:
        ``{saved, changed, needs_restart}`` on success, or
        ``{error: 'internal_error'}`` if the file write failed.
    """
    if not isinstance(principal, PrincipalContext):
        raise TypeError('feature update requires PrincipalContext')
    principal.require_scope('admin')
    owner_user_id = principal.require_owner(context='feature update')
    with _APPLY_LOCK:
        return _apply_feature_updates_locked(
            data, owner_user_id=owner_user_id)


def _apply_feature_updates_locked(data: dict, *, owner_user_id: int):
    """Persist and hot-apply one update while preserving commit order."""
    import lib as _lib

    features_path = _config_path('features.json')
    managed = _managed_flags()
    outcome = {'changed': [], 'saved': {}}

    def _merge(existing):
        if not isinstance(existing, dict):
            raise JsonStoreReadError('features.json is not a JSON object')
        changed = []
        for json_key, _attr in managed:
            if json_key not in data:
                continue
            new_val = bool(data[json_key])
            old_val = existing.get(json_key, None)
            existing[json_key] = new_val
            if old_val != new_val:
                changed.append(json_key)
                logger.info('[Features] %s: %s → %s',
                            json_key, old_val, new_val)
        outcome['changed'] = changed
        outcome['saved'] = existing
        return existing if changed else None

    try:
        updated = update_json_atomic(
            features_path, _merge, default={}, strict=True, indent=2)
    except Exception as e:
        logger.error('[Features] Failed to write features.json: %s', e, exc_info=True)
        return {'error': 'internal_error'}
    existing = updated if updated is not None else outcome['saved']
    changed = outcome['changed']

    # ── Audit trail for each flag that actually changed ──
    if changed:
        try:
            from lib.log import audit_log as _audit
            for _param in changed:
                _audit('feature_flag_change',
                       user_id=owner_user_id,
                       param=_param,
                       new=bool(existing.get(_param, False)))
        except Exception as _aerr:
            logger.debug('[Features] audit_log feature_flag_change failed: %s', _aerr)

    # Hot-reload plugin flags (tofu.flags) on the lib module. A flag whose
    # feature mounts blueprints at import time (needs_restart=True) cannot take
    # effect until restart if it was OFF at boot — generalises the former
    # trading-specific TRADING_ROUTES_REGISTERED check.
    needs_restart = False
    try:
        from lib.feature_registry import registered_flags, was_boot_enabled
        _plugin_flags = {f.json_key: f for f in registered_flags()}
    except Exception as _pe:
        logger.debug('[Features] plugin flag lookup failed: %s', _pe)
        _plugin_flags = {}
    for _jk, _flag in _plugin_flags.items():
        if _jk not in changed:
            continue
        _new = existing.get(_jk, False)
        setattr(_lib, _flag.env_key, _new)
        if _flag.needs_restart and _new and not was_boot_enabled(_jk):
            needs_restart = True
            logger.info('[Features] %s → True but feature not registered at '
                        'boot — needs_restart=True', _flag.env_key)
        else:
            logger.info('[Features] Hot-reloaded %s → %s', _flag.env_key, _new)
    if 'pptx_translate_enabled' in changed:
        _lib.PPTX_TRANSLATE_ENABLED = existing.get('pptx_translate_enabled', False)
        logger.info('[Features] Hot-reloaded PPTX_TRANSLATE_ENABLED → %s',
                    _lib.PPTX_TRANSLATE_ENABLED)
    # Hot-reload CACHE_EXTENDED_TTL — takes effect on next LLM request
    if 'cache_extended_ttl' in changed:
        _lib.CACHE_EXTENDED_TTL = existing.get('cache_extended_ttl', True)
        logger.info('[Features] Hot-reloaded CACHE_EXTENDED_TTL → %s', _lib.CACHE_EXTENDED_TTL)
    # Hot-reload DEBUG_MODE — takes effect on next page load (client-side flag)
    if 'debug_mode' in changed:
        _lib.DEBUG_MODE = existing.get('debug_mode', False)
        logger.info('[Features] Hot-reloaded DEBUG_MODE → %s', _lib.DEBUG_MODE)
    # Hot-reload OPTIMIZER_ENABLED. Also toggles the underlying scheduled
    # task's enabled flag so the cron tick won't fire `run_once` when off.
    if 'optimizer_enabled' in changed:
        _lib.OPTIMIZER_ENABLED = existing.get('optimizer_enabled', True)
        logger.info('[Features] Hot-reloaded OPTIMIZER_ENABLED → %s',
                    _lib.OPTIMIZER_ENABLED)
        try:
            from lib.scheduler.manager import get_scheduler
            mgr = get_scheduler()
            rows = [row for row in mgr.list_tasks(
                    user_id=owner_user_id, include_disabled=True)
                    if row.get('task_type') == 'optimizer'
                    and row.get('name') == 'Daily Optimizer']
            for r in rows:
                tid = r['id']
                mgr.toggle_task(
                    tid, user_id=owner_user_id,
                    enabled=_lib.OPTIMIZER_ENABLED)
        except Exception as _te:
            logger.warning('[Features] Could not toggle Daily Optimizer task: %s',
                           _te, exc_info=True)

    return {'saved': existing, 'needs_restart': needs_restart, 'changed': changed}


__all__ = [
    'apply_feature_updates', 'feature_flags_snapshot', 'read_features',
]
