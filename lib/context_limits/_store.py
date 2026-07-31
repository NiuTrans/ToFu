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


def _fold_absorbed_namespaces(limits: dict, meta: dict,
                              mapping: dict) -> tuple[dict, dict, bool]:
    """Fold ``<absorbed_ns>::model`` keys into ``<account_ns>::model``.

    Pure transform — returns ``(new_limits, new_meta, changed)``. On a key
    collision the NEWER evidence (``meta.ts``) wins; a tie keeps the
    account entry (it is the surviving namespace — the one new learns
    write today). Bare-model keys and namespaces the map does not know
    pass through untouched: the fold converges what it knows, it never
    deletes history. Keys split on the FIRST ``::`` only — provider parts
    may themselves contain a single ':' (ephemeral:local).
    """
    if not mapping:
        return limits, meta, False
    new_limits = dict(limits)
    new_meta = dict(meta)
    changed = False
    for k in list(new_limits):
        if '::' not in k:
            continue
        ns, model = k.split('::', 1)
        dst_ns = mapping.get(ns)
        if not dst_ns or dst_ns == ns:
            continue
        dst_k = f'{dst_ns}::{model}'
        src_v = new_limits.pop(k)
        src_m = new_meta.pop(k, None)
        if dst_k not in new_limits:
            new_limits[dst_k] = src_v
            if src_m is not None:
                new_meta[dst_k] = src_m
        else:
            src_ts = float((src_m or {}).get('ts', 0) or 0)
            dst_ts = float((new_meta.get(dst_k) or {}).get('ts', 0) or 0)
            if src_ts > dst_ts:
                new_limits[dst_k] = src_v
                if src_m is not None:
                    new_meta[dst_k] = src_m
        changed = True
    return new_limits, new_meta, changed


def _persist_namespace_fold(cfg_path: str, mapping: dict) -> None:
    """Write the namespace fold back to server_config.json, race-safe.

    The mutate folds the file's CURRENT maps (another process may have
    learned something between our read and this write) rather than
    blindly overwriting with our earlier snapshot.
    """
    from lib.json_store import update_json_atomic

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        limits = cfg.get('model_context_limits') or {}
        meta = cfg.get('model_context_limits_meta') or {}
        limits, meta, _ = _fold_absorbed_namespaces(limits, meta, mapping)
        cfg['model_context_limits'] = limits
        # Same contract as _persist: meta only for keys with a value.
        cfg['model_context_limits_meta'] = {
            k: v for k, v in meta.items() if k in limits}
        return cfg

    try:
        update_json_atomic(cfg_path, _mutate, default={})
    except Exception as e:
        logger.warning('[CtxLimits] failed to persist namespace fold: %s', e)


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
            # Fold learned entries recorded under absorbed FACE
            # namespaces (the duplicate anthropic CARD era) into their
            # ACCOUNT namespace (charter #23). Without this, the
            # account/face merge orphans every pre-merge learning —
            # measured 2026-07-29: sankuai_anthropic::claude-opus-5 held
            # tonight's 1.1M expand; post-merge slots ask for
            # sankuai::claude-opus-5 and would silently lose it. The map
            # lives once in provider_face — never re-derived here.
            try:
                from lib.llm_dispatch.provider_face import (
                    account_namespace_map)
                mapping = account_namespace_map(cfg.get('providers') or [])
            except Exception as e:
                logger.debug('[CtxLimits] namespace fold map unavailable '
                             '(non-fatal): %s', e)
                mapping = {}
            cleaned, meta, folded = _fold_absorbed_namespaces(
                cleaned, meta, mapping)
            if folded:
                logger.info('[CtxLimits] folded absorbed face namespace(s) '
                            'in learned limits: %s', sorted(mapping))
                _persist_namespace_fold(cfg_path, mapping)
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
