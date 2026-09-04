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
import math
import os

from lib.log import get_logger

logger = get_logger(__name__)


# Sanity bounds. A real context window is at least a few thousand tokens
# (we never want to learn a 12-token "limit" from a malformed error) and
# at most 50M (no model in 2026 ships with more, even with infinite-context
# experiments).
_MIN_LEARNABLE = 4_000
_MAX_LEARNABLE = 50_000_000
_MAX_CONTEXT_LIMIT_COMPONENT_CHARS = 256
_MAX_CONTEXT_LIMIT_KEY_CHARS = (
    _MAX_CONTEXT_LIMIT_COMPONENT_CHARS * 2 + 2)
_MAX_CONTEXT_LIMIT_ENTRIES = 2048
_MAX_META_SOURCE_CHARS = 32
_MAX_PENDING_STRIKES = 1_000


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
    if (not m or len(m) > _MAX_CONTEXT_LIMIT_COMPONENT_CHARS
            or len(pid) > _MAX_CONTEXT_LIMIT_COMPONENT_CHARS):
        return ''
    return f'{pid}::{m}' if pid else m


def _valid_stored_key(key: object) -> bool:
    if not (
        isinstance(key, str)
        and key == key.strip()
        and key
        and len(key) <= _MAX_CONTEXT_LIMIT_KEY_CHARS
    ):
        return False
    if '::' not in key:
        return len(key) <= _MAX_CONTEXT_LIMIT_COMPONENT_CHARS
    provider_id, model = key.split('::', 1)
    return bool(
        provider_id
        and model
        and len(provider_id) <= _MAX_CONTEXT_LIMIT_COMPONENT_CHARS
        and len(model) <= _MAX_CONTEXT_LIMIT_COMPONENT_CHARS
    )


def _safe_timestamp(value: object) -> float:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return timestamp if math.isfinite(timestamp) and timestamp >= 0 else 0.0


def _pending_timestamp(meta: dict | None) -> float:
    if not isinstance(meta, dict):
        return 0.0
    if 'pending_ts' in meta:
        return _safe_timestamp(meta.get('pending_ts'))
    if meta.get('source') == 'pending':
        return _safe_timestamp(meta.get('ts'))
    return 0.0


def _has_pending_evidence(meta: object) -> bool:
    if not isinstance(meta, dict):
        return False
    try:
        candidate = int(meta.get('pending'))
        strikes = int(meta.get('strikes', 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        _MIN_LEARNABLE <= candidate <= _MAX_LEARNABLE
        and 0 < strikes <= _MAX_PENDING_STRIKES
    )


def _normalize_meta_row(value: object, *, has_limit: bool) -> dict | None:
    if not isinstance(value, dict):
        return None
    source = str(value.get('source', '') or '')
    if len(source) > _MAX_META_SOURCE_CHARS:
        source = ''
    pending = _has_pending_evidence(value)
    # A metadata-only row is meaningful only while it represents the first
    # corroboration strike. Ordinary dangling metadata is reconstructible
    # bookkeeping, not durable state.
    if not has_limit and (source != 'pending' or not pending):
        return None
    if source == 'pending' and not pending:
        return None

    normalized = {
        'ts': _safe_timestamp(value.get('ts')),
        'source': source,
        'strikes': int(value.get('strikes', 0) or 0) if pending else 0,
    }
    if pending:
        normalized['pending'] = int(value['pending'])
        if source != 'pending':
            normalized['pending_ts'] = _pending_timestamp(value)
    return normalized


def _prune_learned_state(
    limits: dict[str, int],
    meta: dict[str, dict],
    *,
    max_entries: int = _MAX_CONTEXT_LIMIT_ENTRIES,
) -> tuple[dict[str, int], dict[str, dict], bool]:
    """Bound learned values plus pending strikes by newest evidence.

    Pending strike rows intentionally have no learned value yet, but they are
    part of the same persistent growth budget. Legacy values without metadata
    rank oldest and are retained only while capacity remains.
    """
    keys = set(limits)
    keys.update(
        key for key, value in meta.items()
        if _has_pending_evidence(value))
    if len(keys) <= max_entries:
        return limits, meta, False
    newest = sorted(
        keys,
        key=lambda key: (
            max(
                _safe_timestamp((meta.get(key) or {}).get('ts')),
                _pending_timestamp(meta.get(key)),
            ),
            key,
        ),
        reverse=True,
    )[:max_entries]
    keep = set(newest)
    return (
        {key: value for key, value in limits.items() if key in keep},
        {key: value for key, value in meta.items() if key in keep},
        True,
    )


def _sanitize_learned_state(
    limits_raw: object,
    meta_raw: object,
    mapping: dict,
) -> tuple[dict[str, int], dict[str, dict], bool]:
    """Validate, namespace-fold and cap untrusted persisted state."""
    limits_input = limits_raw if isinstance(limits_raw, dict) else {}
    meta_input = meta_raw if isinstance(meta_raw, dict) else {}

    limits: dict[str, int] = {}
    for key, value in limits_input.items():
        if not _valid_stored_key(key):
            continue
        try:
            limit = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if _MIN_LEARNABLE <= limit <= _MAX_LEARNABLE:
            limits[key] = limit

    meta: dict[str, dict] = {}
    for key, value in meta_input.items():
        if not _valid_stored_key(key):
            continue
        normalized = _normalize_meta_row(value, has_limit=key in limits)
        if normalized is not None:
            meta[key] = normalized

    limits, meta, _ = _fold_absorbed_namespaces(limits, meta, mapping)
    limits, meta, _ = _prune_learned_state(limits, meta)
    changed = (
        not isinstance(limits_raw, dict)
        or not isinstance(meta_raw, dict)
        or limits != limits_raw
        or meta != meta_raw
    )
    return limits, meta, changed


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
            src_ts = _safe_timestamp((src_m or {}).get('ts'))
            dst_ts = _safe_timestamp((new_meta.get(dst_k) or {}).get('ts'))
            if src_ts > dst_ts:
                new_limits[dst_k] = src_v
                if src_m is not None:
                    new_meta[dst_k] = src_m
        changed = True
    # A first inferred big-drop strike has metadata but deliberately no
    # learned value. Fold those pending rows too; otherwise a provider-card
    # merge resets the corroboration gate even though its evidence is fresh.
    for k in list(new_meta):
        if k in new_limits or '::' not in k:
            continue
        row = new_meta.get(k)
        if not _has_pending_evidence(row):
            continue
        ns, model = k.split('::', 1)
        dst_ns = mapping.get(ns)
        if not dst_ns or dst_ns == ns:
            continue
        dst_k = f'{dst_ns}::{model}'
        src_m = new_meta.pop(k)
        dst_m = new_meta.get(dst_k)
        src_ts = _pending_timestamp(src_m)
        dst_ts = _pending_timestamp(dst_m)
        if dst_m is None or src_ts > dst_ts:
            if dst_k in new_limits and isinstance(dst_m, dict):
                # Keep the destination learned value's provenance/TTL while
                # carrying the newer corroboration strike across the merge.
                merged = dict(dst_m)
                merged.update({
                    'strikes': src_m['strikes'],
                    'pending': src_m['pending'],
                    'pending_ts': src_ts,
                })
                new_meta[dst_k] = merged
            else:
                new_meta[dst_k] = src_m
        changed = True
    return new_limits, new_meta, changed


def _persist_sanitized_state(cfg_path: str, mapping: dict) -> None:
    """Sanitize CURRENT on-disk context-limit maps, race-safe.

    Another process may learn between ``_load`` and this repair. The mutator
    therefore validates, folds and prunes the file's current maps instead of
    writing the earlier snapshot back over newer evidence.
    """
    from lib.json_store import update_json_atomic

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        limits, meta, _ = _sanitize_learned_state(
            cfg.get('model_context_limits'),
            cfg.get('model_context_limits_meta'),
            mapping,
        )
        cfg['model_context_limits'] = limits
        cfg['model_context_limits_meta'] = meta
        return cfg

    try:
        update_json_atomic(cfg_path, _mutate, default={})
    except Exception as e:
        logger.warning('[CtxLimits] failed to persist sanitized state: %s', e)


def _load() -> tuple[dict[str, int], dict[str, dict]]:
    """Load persisted learned limits + metadata from server_config.json."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
            # Fold learned entries recorded under absorbed FACE
            # namespaces (the duplicate anthropic CARD era) into their
            # ACCOUNT namespace (charter #23). Without this, the
            # account/face merge orphans every pre-merge learning —
            # measured 2026-07-29: the face-namespaced claude-opus-5 entry
            # held tonight's 1.1M expand; post-merge slots ask for the
            # account-namespaced id and would silently lose it. The map
            # lives once in provider_face — never re-derived here.
            try:
                from lib.llm_dispatch.provider_face import (
                    account_namespace_map)
                mapping = account_namespace_map(cfg.get('providers') or [])
            except Exception as e:
                logger.debug('[CtxLimits] namespace fold map unavailable '
                             '(non-fatal): %s', e)
                mapping = {}
            cleaned, meta, changed = _sanitize_learned_state(
                cfg.get('model_context_limits'),
                cfg.get('model_context_limits_meta'),
                mapping,
            )
            if changed:
                logger.info('[CtxLimits] repaired/folded persisted learned '
                            'context-limit state')
                _persist_sanitized_state(cfg_path, mapping)
            if cleaned:
                logger.info('[CtxLimits] Loaded %d auto-learned context limits '
                            '(%d metadata rows)', len(cleaned), len(meta))
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
    learned, meta, _ = _sanitize_learned_state(
        f._LEARNED, f._META, {})
    # Mutate rather than rebind so any caller holding the facade-owned dict
    # object continues to observe the single authoritative store.
    f._LEARNED.clear()
    f._LEARNED.update(learned)
    f._META.clear()
    f._META.update(meta)

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        cfg['model_context_limits'] = dict(learned)
        # A first big-drop strike may precede any learned value. It is durable
        # evidence too; otherwise restart silently resets the two-strike gate.
        cfg['model_context_limits_meta'] = dict(meta)
        return cfg

    try:
        update_json_atomic(cfg_path, _mutate, default={})
    except Exception as e:
        logger.error('[CtxLimits] Failed to persist learned context limits: %s',
                     e, exc_info=True)
