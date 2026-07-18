"""lib/context_limits/_lookup.py — Read path for learned context limits.

Contains :func:`lookup_learned_context_limit` and the shrink-TTL tunables
(``_SHRINK_TTL_DAYS`` / ``_SHRINK_TTL_SEC``) that the read path enforces:
a *shrink* entry older than the TTL is lazily dropped so the caller reverts
to the static preset (the expand-starvation self-heal — see the package
docstring). Expand entries and metadata-less legacy entries are permanent.

Shared mutable state (``_LEARNED`` / ``_META`` / ``_lock`` / ``_persist``) is
reached through the package facade at call time so there is exactly one copy
per process and the self-heal test's monkeypatches are honoured.
"""

import os
import time

from lib.log import audit_log, get_logger

from lib.context_limits._store import _key

logger = get_logger(__name__)


# ── Self-heal TTL tunable (2026-06-08, user-approved) ──
# Shrink entries expire after this many days and revert to the static
# preset. Env override: TOFU_CTX_SHRINK_TTL_DAYS. Expand entries never
# expire. See the package docstring for the expand-starvation rationale.
try:
    _SHRINK_TTL_DAYS = float(os.environ.get('TOFU_CTX_SHRINK_TTL_DAYS', '7') or 7)
except (TypeError, ValueError) as e:
    logger.debug('[CtxLimits] TOFU_CTX_SHRINK_TTL_DAYS parse failed, using default: %s', e)
    _SHRINK_TTL_DAYS = 7.0
_SHRINK_TTL_SEC = _SHRINK_TTL_DAYS * 86400.0


def _facade():
    import lib.context_limits as _f
    return _f


def lookup_learned_context_limit(provider_id: str | None, model: str) -> int | None:
    """Return the learned context limit for ``(provider_id, model)``, or None.

    Tries the per-provider key first, then falls back to the bare model id
    so legacy single-provider entries still apply. A *shrink* entry older
    than ``_SHRINK_TTL_DAYS`` is treated as expired: it is lazily dropped
    and ``None`` is returned so the caller reverts to the static preset.
    Entries with no metadata (e.g. hand-edited or pre-TTL legacy values)
    are treated as permanent.
    """
    if not model:
        return None
    f = _facade()
    with f._lock:
        k = _key(provider_id, model)
        v = f._LEARNED.get(k)
        if v is None and provider_id:
            k = _key('', model)
            v = f._LEARNED.get(k)
        if v is None:
            return None
        meta = f._META.get(k)
        if meta and meta.get('source') == 'shrink':
            ts = meta.get('ts', 0) or 0
            age = time.time() - ts
            if ts and age > _SHRINK_TTL_SEC:
                logger.warning('[CtxLimits] ♻️ Shrink entry expired (age %.1fd > '
                               '%.1fd TTL) — dropping %s=%d, reverting to preset',
                               age / 86400.0, _SHRINK_TTL_DAYS, k, v)
                f._LEARNED.pop(k, None)
                f._META.pop(k, None)
                f._persist()
                try:
                    audit_log('context_limit_learned', direction='expire',
                              key=k, old_limit=v, age_days=round(age / 86400.0, 2))
                except Exception as e:
                    logger.debug('[CtxLimits] audit_log expire failed: %s', e)
                return None
        return int(v)
