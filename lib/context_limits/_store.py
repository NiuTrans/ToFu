"""lib/context_limits/_store.py — Persistence + key composition for learned limits.

Holds the storage-facing helpers (``_key``, ``_load``, ``_persist``) and the
sanity bounds (``_MIN_LEARNABLE`` / ``_MAX_LEARNABLE``) used when loading and
clamping learned values.

**Shared mutable state (single-instance invariant).** The three mutable
objects ``_LEARNED`` (dict), ``_META`` (dict) and ``_lock`` (threading.Lock)
are the *authoritative process-wide* store. They are defined ONCE in the
package facade (:mod:`lib.context_limits` ``__init__``) and every function
here — and in the sibling ``_lookup`` / ``_learn`` modules — reaches them
through the facade module object at CALL time (``_facade()._LEARNED`` …).
This guarantees there is exactly one ``_LEARNED`` / ``_META`` / ``_lock`` per
process AND lets the self-heal test monkeypatch ``lib.context_limits._LEARNED``
/ ``_META`` / ``_persist`` and have every code path observe the patched value.
"""

import json
import os

from lib.log import get_logger

logger = get_logger(__name__)


# Sanity bounds. A real context window is at least a few thousand tokens
# (we never want to learn a 12-token "limit" from a malformed error) and
# at most 50M (no model in 2026 ships with more, even with infinite-context
# experiments).
_MIN_LEARNABLE = 4_000
_MAX_LEARNABLE = 50_000_000


def _facade():
    """Return the package facade module holding the shared mutable state.

    Resolved lazily (and re-resolved on every access) so that a test which
    does ``monkeypatch.setattr(lib.context_limits, '_LEARNED', {})`` is
    honoured by every store/lookup/learn code path.
    """
    import lib.context_limits as _f
    return _f


def _key(provider_id: str | None, model: str) -> str:
    """Compose the storage key. Empty provider_id collapses to bare model."""
    pid = (provider_id or '').strip()
    m = (model or '').strip()
    if not m:
        return ''
    return f'{pid}::{m}' if pid else m


def _load() -> tuple[dict[str, int], dict[str, dict]]:
    """Load persisted learned limits + metadata from server_config.json."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
            limits = cfg.get('model_context_limits') or {}
            meta_raw = cfg.get('model_context_limits_meta') or {}
            # Coerce values to int; drop anything bogus.
            cleaned: dict[str, int] = {}
            for k, v in limits.items():
                try:
                    iv = int(v)
                except (TypeError, ValueError) as e:
                    logger.debug('[CtxLimits] Dropping non-int value for %s: %r (%s)', k, v, e)
                    continue
                if _MIN_LEARNABLE <= iv <= _MAX_LEARNABLE:
                    cleaned[k] = iv
            meta: dict[str, dict] = {}
            for k, mv in meta_raw.items():
                if isinstance(mv, dict) and k in cleaned:
                    meta[k] = {
                        'ts': float(mv.get('ts', 0) or 0),
                        'source': str(mv.get('source', '') or ''),
                        'strikes': int(mv.get('strikes', 0) or 0),
                    }
            if cleaned:
                logger.info('[CtxLimits] Loaded %d auto-learned context limits '
                            '(%d with metadata)', len(cleaned), len(meta))
            return cleaned, meta
    except Exception as e:
        logger.warning('[CtxLimits] Failed to load learned context limits: %s', e)
    return {}, {}


def _persist():
    """Write the in-memory dicts to server_config.json. Caller holds _lock.

    Uses ``update_json_atomic`` so this read-modify-write is serialised
    (per-path thread lock + cross-process flock) against the OTHER
    concurrent writers of this shared file (routes/config.py save,
    model_info._learn_model_limit, dispatcher discovery, health_local).
    A plain atomic write still loses updates when two writers touch
    different keys of the same file at once.
    """
    from lib.config_dir import config_path
    from lib.json_store import update_json_atomic
    cfg_path = config_path('server_config.json')

    f = _facade()

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        cfg['model_context_limits'] = dict(f._LEARNED)
        # Only persist metadata for keys that still have a learned value.
        cfg['model_context_limits_meta'] = {
            k: v for k, v in f._META.items() if k in f._LEARNED
        }
        return cfg

    try:
        update_json_atomic(cfg_path, _mutate, default={})
    except Exception as e:
        logger.error('[CtxLimits] Failed to persist learned context limits: %s',
                     e, exc_info=True)
