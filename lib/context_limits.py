"""lib/context_limits.py — Auto-learned per-(provider, model) context-window limits.

Tofu often routes the same logical model id through several providers (e.g.
``deepseek-v3.2`` may be served by Tencent, Baidu, Huawei, Doubao gateways).
Each provider may advertise a different context length even for an identical
upstream model — and our preset tables in :mod:`lib.tasks_pkg.compaction`
inevitably get some of these wrong.

This module corrects the preset error in BOTH directions, by learning from
real traffic:

* **Shrink** — when an LLM call fails with ``PromptTooLongError``, learn a
  smaller ceiling for that ``(provider_id, model)`` pair. Two sources:
  - *authoritative* — the gateway stated its own ``maximum context length is M``;
    we learn M directly and immediately (a literal ceiling, not a guess).
  - *inferred* — we only know the rejected request size N; we guess
    ``N * 0.95``. Because a single transient blip (a momentary mis-route to a
    smaller backend behind the same model id) must not permanently collapse a
    genuine 1M window, an inferred shrink that drops the limit by more than
    ``_BIG_DROP_FACTOR`` requires ``_REQUIRED_STRIKES`` consecutive overflow
    events (within ``_STRIKE_WINDOW_SEC``) before it is persisted.

* **Expand** — when an LLM call succeeds, look at the actual ``prompt_tokens``
  it accepted. If the call sent more tokens than our currently-known limit,
  bump the learned ceiling up to that observed count plus a small headroom.

**TTL self-heal.** Shrink entries are inherently uncertain (the rejection may
have been a transient gateway/route hiccup, and once a limit shrinks our own
compaction caps every prompt below it — so the expand path can NEVER observe
tokens above the wrong ceiling to correct it; see the deadlock note below).
Each *shrink* entry therefore carries a timestamp and is ignored (and lazily
dropped) once older than ``_SHRINK_TTL_DAYS``. On expiry we fall back to the
static preset; if the smaller window is real, the next overflow re-learns it
within one request (which ``reactive_compact`` recovers gracefully). *Expand*
entries are permanent — they are only ever corroborated by a real accepted
prompt, so they cannot be wrong in a way that hurts the user.

    The expand-starvation deadlock (why TTL, not "expand harder"): when a
    shrink drops the learned limit L, ``_usable_context(L)`` and the
    force-compact trigger cap every outgoing prompt well below L. So
    ``observed_tokens`` is structurally < L forever, and the expand condition
    ``observed > preset`` is unsatisfiable. Expand can never rescue a wrongful
    shrink — only the shrink side (gate + TTL) can.

Both paths persist to ``data/config/server_config.json``:
  - ``model_context_limits``      → ``{"<provider_id>::<model>": int, ...}`` —
    the plain int map (public surface read by ``routes/config.py`` + frontend).
  - ``model_context_limits_meta`` → ``{"<key>": {"ts": float, "source": str,
    "strikes": int}}`` — sidecar metadata driving TTL + the strike gate.

A single ``_context_limit_learned`` blob is surfaced inside ``usage`` so the
orchestrator can show a one-line SSE notice to the user.

Key shape: ``"<provider_id>::<model>"`` — falls back to the bare model name
when no provider is known so older callers keep working.
"""

import json
import os
import threading
import time

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'lookup_learned_context_limit',
    'learn_shrink_from_error',
    'learn_expand_from_success',
]


# ── Storage ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
_LEARNED: dict[str, int] = {}
_META: dict[str, dict] = {}

# Sanity bounds. A real context window is at least a few thousand tokens
# (we never want to learn a 12-token "limit" from a malformed error) and
# at most 50M (no model in 2026 ships with more, even with infinite-context
# experiments).
_MIN_LEARNABLE = 4_000
_MAX_LEARNABLE = 50_000_000

# When EXPANDING from a successful prompt_tokens observation, raise the
# learned ceiling to ``observed * (1 + _EXPAND_HEADROOM)`` so a single
# borderline call doesn't immediately re-trigger compaction next round.
_EXPAND_HEADROOM = 0.05  # 5%

# Don't shrink below this fraction of the prior known limit in a single
# step — protects against a one-off transient "prompt too long" from a
# provider that briefly reduced its window then restored it. The next
# overflow on the same provider will shrink further.
_MIN_SHRINK_FACTOR = 0.10  # never shrink below 10% of prior ceiling

# ── Self-heal + anti-blip tunables (2026-06-08, user-approved) ──
# Shrink entries expire after this many days and revert to the static
# preset. Env override: TOFU_CTX_SHRINK_TTL_DAYS. Expand entries never
# expire. See module docstring for the expand-starvation rationale.
try:
    _SHRINK_TTL_DAYS = float(os.environ.get('TOFU_CTX_SHRINK_TTL_DAYS', '7') or 7)
except (TypeError, ValueError):
    _SHRINK_TTL_DAYS = 7.0
_SHRINK_TTL_SEC = _SHRINK_TTL_DAYS * 86400.0

# An INFERRED shrink (no gateway-stated maximum) that would drop the prior
# known limit to below this fraction of it is a "big drop" and must be
# corroborated by _REQUIRED_STRIKES consecutive overflows before it sticks.
_BIG_DROP_FACTOR = 0.5      # candidate < prior * 0.5  ⇒ "more than 2× shrink"
_REQUIRED_STRIKES = 2       # consecutive big-drop overflows needed to persist
_STRIKE_WINDOW_SEC = 3600.0  # strikes older than this don't count as consecutive


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


_LEARNED, _META = _load()


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

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        cfg['model_context_limits'] = dict(_LEARNED)
        # Only persist metadata for keys that still have a learned value.
        cfg['model_context_limits_meta'] = {
            k: v for k, v in _META.items() if k in _LEARNED
        }
        return cfg

    try:
        update_json_atomic(cfg_path, _mutate, default={})
    except Exception as e:
        logger.error('[CtxLimits] Failed to persist learned context limits: %s',
                     e, exc_info=True)


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
    with _lock:
        k = _key(provider_id, model)
        v = _LEARNED.get(k)
        if v is None and provider_id:
            k = _key('', model)
            v = _LEARNED.get(k)
        if v is None:
            return None
        meta = _META.get(k)
        if meta and meta.get('source') == 'shrink':
            ts = meta.get('ts', 0) or 0
            age = time.time() - ts
            if ts and age > _SHRINK_TTL_SEC:
                logger.warning('[CtxLimits] ♻️ Shrink entry expired (age %.1fd > '
                               '%.1fd TTL) — dropping %s=%d, reverting to preset',
                               age / 86400.0, _SHRINK_TTL_DAYS, k, v)
                _LEARNED.pop(k, None)
                _META.pop(k, None)
                _persist()
                try:
                    audit_log('context_limit_learned', direction='expire',
                              key=k, old_limit=v, age_days=round(age / 86400.0, 2))
                except Exception as e:
                    logger.debug('[CtxLimits] audit_log expire failed: %s', e)
                return None
        return int(v)


def learn_shrink_from_error(provider_id: str | None, model: str,
                            reported_tokens: int | None,
                            preset_limit: int | None = None,
                            stated_max: int | None = None) -> dict | None:
    """Persist a shrunk context limit based on an overflow error.

    Args:
        provider_id: Provider id from the dispatch slot (may be empty).
        model: Model id we just sent the rejected request to.
        reported_tokens: The size N of the rejected request
            ("you requested N tokens" / "N tokens > M maximum"). Used as an
            *inferred* ceiling (N * 0.95) only when ``stated_max`` is absent.
        preset_limit: The currently-believed limit (static + prior-learned).
            Used to floor the shrink and to size the big-drop strike gate.
        stated_max: The gateway-stated maximum M ("maximum context length is
            M tokens") when present. This is authoritative — learned directly
            and immediately (bypasses the strike gate, but is still TTL'd).

    Returns:
        ``{'model': …, 'old_limit': old, 'new_limit': new, 'direction': 'shrink'}``
        if a change was persisted, else None (including when a big inferred
        drop is awaiting more strikes).
    """
    authoritative = bool(stated_max and stated_max >= _MIN_LEARNABLE)
    if authoritative:
        candidate = int(stated_max)
    else:
        if not reported_tokens or reported_tokens < _MIN_LEARNABLE:
            return None
        # Provider rejected at >= reported_tokens, so the true ceiling is
        # somewhere below it. Be conservative: take 95% of reported_tokens.
        candidate = int(reported_tokens * 0.95)

    if not model:
        return None

    if preset_limit and preset_limit > 0:
        floor = max(_MIN_LEARNABLE, int(preset_limit * _MIN_SHRINK_FACTOR))
        if candidate < floor:
            logger.info('[CtxLimits] Shrink candidate %d below floor %d '
                        '(prior limit %d) — clamping for model=%s provider=%s',
                        candidate, floor, preset_limit, model, provider_id or '?')
            candidate = floor

    candidate = max(_MIN_LEARNABLE, min(candidate, _MAX_LEARNABLE))

    k = _key(provider_id, model)
    if not k:
        return None

    with _lock:
        old = _LEARNED.get(k)
        prior_known = old if old is not None else preset_limit
        now = time.time()

        # Only persist when the new ceiling is genuinely smaller than the
        # current known one. Otherwise we'd churn the file on every error.
        if prior_known and candidate >= prior_known:
            logger.debug('[CtxLimits] Shrink skipped: candidate=%d >= prior=%d '
                         '(model=%s provider=%s)',
                         candidate, prior_known, model, provider_id or '?')
            # A non-shrinking event clears any pending big-drop strikes.
            _clear_pending_strikes(k)
            return None

        # ── Big-drop strike gate (inferred shrinks only) ──
        # An authoritative gateway-stated max is trusted immediately.
        if (not authoritative and prior_known
                and candidate < prior_known * _BIG_DROP_FACTOR):
            strikes = _register_strike(k, candidate, now)
            if strikes < _REQUIRED_STRIKES:
                logger.warning('[CtxLimits] ⏳ Big inferred shrink for '
                               'provider=%s model=%s (%s → %d) held: strike %d/%d '
                               '(needs %d consecutive overflows within %.0fs)',
                               provider_id or '?', model, prior_known, candidate,
                               strikes, _REQUIRED_STRIKES, _REQUIRED_STRIKES,
                               _STRIKE_WINDOW_SEC)
                _persist()  # persist the pending-strike meta
                return None

        # Persist the shrink.
        _LEARNED[k] = candidate
        _META[k] = {'ts': now, 'source': 'shrink', 'strikes': 0}
        _persist()

    logger.warning('[CtxLimits] ⚙️ Learned SHRUNK context limit for '
                   'provider=%s model=%s: %d (was %s, %s)',
                   provider_id or '?', model, candidate,
                   old if old is not None else (preset_limit or 'unknown'),
                   (f'gateway-stated max {stated_max}' if authoritative
                    else f'reported overflow at {reported_tokens} tokens'))
    try:
        audit_log('context_limit_learned',
                  direction='shrink',
                  provider_id=provider_id or '',
                  model=model,
                  new_limit=candidate,
                  old_limit=old,
                  reported_tokens=reported_tokens,
                  stated_max=stated_max,
                  authoritative=authoritative)
    except Exception as e:
        logger.debug('[CtxLimits] audit_log failed: %s', e)

    return {'model': model, 'provider_id': provider_id or '',
            'old_limit': old or preset_limit or 0,
            'new_limit': candidate, 'direction': 'shrink'}


def _register_strike(k: str, candidate: int, now: float) -> int:
    """Record a pending big-drop strike for *k*. Caller holds ``_lock``.

    Strikes older than ``_STRIKE_WINDOW_SEC`` do not count as consecutive —
    the counter resets to 1. Returns the current consecutive strike count.
    """
    meta = _META.get(k)
    if (meta and meta.get('source') == 'pending'
            and (now - (meta.get('ts') or 0)) <= _STRIKE_WINDOW_SEC):
        meta['strikes'] = int(meta.get('strikes', 0)) + 1
        meta['ts'] = now
        meta['pending'] = candidate
        return meta['strikes']
    _META[k] = {'ts': now, 'source': 'pending', 'strikes': 1, 'pending': candidate}
    return 1


def _clear_pending_strikes(k: str):
    """Drop a pending (not-yet-persisted) strike record. Caller holds ``_lock``."""
    meta = _META.get(k)
    if meta and meta.get('source') == 'pending':
        _META.pop(k, None)


def learn_expand_from_success(provider_id: str | None, model: str,
                              observed_tokens: int,
                              preset_limit: int | None = None) -> dict | None:
    """Raise the learned context limit when a request bigger than our
    presumed ceiling actually succeeded.

    Args:
        provider_id: Provider id from the dispatch slot.
        model: Model id used.
        observed_tokens: The ``prompt_tokens`` value the provider returned in
            ``usage`` for the just-succeeded call.
        preset_limit: The currently-believed limit (static + prior-learned).
            We only expand when ``observed_tokens > preset_limit``.

    Returns:
        ``{'model': …, 'old_limit': old, 'new_limit': new}`` on a change,
        else None.
    """
    if not model or not observed_tokens or observed_tokens < _MIN_LEARNABLE:
        return None
    if not preset_limit or observed_tokens <= preset_limit:
        return None

    # Headroom so we don't immediately re-trigger compaction next turn.
    candidate = int(observed_tokens * (1 + _EXPAND_HEADROOM))
    candidate = max(_MIN_LEARNABLE, min(candidate, _MAX_LEARNABLE))

    k = _key(provider_id, model)
    if not k:
        return None
    with _lock:
        old = _LEARNED.get(k)
        if old is not None and candidate <= old:
            return None
        _LEARNED[k] = candidate
        # Expand entries are permanent (corroborated by a real accepted
        # prompt) — source='expand' is NOT subject to the shrink TTL.
        _META[k] = {'ts': time.time(), 'source': 'expand', 'strikes': 0}
        _persist()

    logger.warning('[CtxLimits] ⚙️ Learned EXPANDED context limit for '
                   'provider=%s model=%s: %d (was %s, observed accepted prompt=%d)',
                   provider_id or '?', model, candidate,
                   old if old is not None else (preset_limit or 'unknown'),
                   observed_tokens)
    try:
        audit_log('context_limit_learned',
                  direction='expand',
                  provider_id=provider_id or '',
                  model=model,
                  new_limit=candidate,
                  old_limit=old,
                  observed_tokens=observed_tokens)
    except Exception as e:
        logger.debug('[CtxLimits] audit_log failed: %s', e)

    return {'model': model, 'provider_id': provider_id or '',
            'old_limit': old or preset_limit or 0,
            'new_limit': candidate, 'direction': 'expand'}
