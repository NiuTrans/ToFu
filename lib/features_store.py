"""Feature-flag persistence + hot-reload.

Moved out of ``routes/common.py`` (2026-06). ``apply_feature_updates`` reads
``features.json``, merges the requested flag changes, writes it back,
audit-logs each change, and hot-reloads the live ``lib.*`` toggles (with the
``needs_restart`` caveat for plugin features that mount routes at import time).
No Flask
dependency — the ``POST /api/v1/features`` handler just parses the body,
calls this, and ``jsonify``s the result.
"""

import json
import os

from lib.config_dir import config_path as _config_path
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
    try:
        if os.path.isfile(features_path):
            with open(features_path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning('[Features] Failed to read features.json: %s', e)
    return {}


def apply_feature_updates(data: dict):
    """Merge requested flag changes into features.json + hot-reload lib toggles.

    Args:
        data: subset of the managed flag keys → new (bool) values.

    Returns:
        ``{saved, changed, needs_restart}`` on success, or
        ``{error: 'internal_error'}`` if the file write failed.
    """
    import lib as _lib

    features_path = _config_path('features.json')
    existing = read_features()

    changed = []
    managed = _managed_flags()
    for json_key, _attr in managed:
        if json_key in data:
            new_val = bool(data[json_key])
            old_val = existing.get(json_key, None)
            existing[json_key] = new_val
            if old_val != new_val:
                changed.append(json_key)
                logger.info('[Features] %s: %s → %s', json_key, old_val, new_val)

    try:
        os.makedirs(os.path.dirname(features_path), exist_ok=True)
        with open(features_path, 'w') as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        logger.error('[Features] Failed to write features.json: %s', e, exc_info=True)
        return {'error': 'internal_error'}

    # ── Audit trail for each flag that actually changed ──
    if changed:
        try:
            from lib.log import audit_log as _audit
            for _param in changed:
                _audit('feature_flag_change',
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
            from lib.scheduler import get_scheduler
            mgr = get_scheduler()
            db = mgr._get_db()
            rows = db.execute(
                "SELECT id FROM scheduled_tasks WHERE task_type=? AND name=?",
                ['optimizer', 'Daily Optimizer']).fetchall()
            for r in rows:
                tid = r['id'] if isinstance(r, dict) else r[0]
                mgr.toggle_task(tid, enabled=_lib.OPTIMIZER_ENABLED)
        except Exception as _te:
            logger.warning('[Features] Could not toggle Daily Optimizer task: %s',
                           _te, exc_info=True)

    return {'saved': existing, 'needs_restart': needs_restart, 'changed': changed}


__all__ = ['apply_feature_updates', 'read_features']
