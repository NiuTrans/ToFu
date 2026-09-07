"""lib/context_limits/_learn.py — Write path: shrink / expand learning + strike gate.

Contains the two public learners (:func:`learn_shrink_from_error`,
:func:`learn_expand_from_success`) plus the private strike-gate helpers
(:func:`_register_strike`, :func:`_clear_pending_strikes`) and every tunable
they use:

* ``_EXPAND_HEADROOM``      — headroom added when expanding.
* ``_MIN_SHRINK_FACTOR``    — floor a single shrink step at this fraction.
* ``_BIG_DROP_FACTOR``      — inferred drop below this fraction is "big".
* ``_REQUIRED_STRIKES``     — consecutive big-drop overflows before persisting.
* ``_STRIKE_WINDOW_SEC``    — strikes older than this reset the counter.

Bounds (``_MIN_LEARNABLE`` / ``_MAX_LEARNABLE``) come from ``_store``. Shared
mutable state (``_LEARNED`` / ``_META`` / ``_lock`` / ``_persist``) is reached
through the package facade at call time (single-instance invariant + test
monkeypatch support).
"""

import time

from lib.log import audit_log, get_logger

from lib.context_limits._store import (
    _has_pending_evidence,
    _key,
    _MAX_LEARNABLE,
    _MIN_LEARNABLE,
    _pending_timestamp,
)

logger = get_logger(__name__)


# When EXPANDING from a successful prompt_tokens observation, raise the
# learned ceiling to ``observed * (1 + _EXPAND_HEADROOM)`` so a single
# borderline call doesn't immediately re-trigger compaction next round.
_EXPAND_HEADROOM = 0.05  # 5%

# Don't shrink below this fraction of the prior known limit in a single
# step — protects against a one-off transient "prompt too long" from a
# provider that briefly reduced its window then restored it. The next
# overflow on the same provider will shrink further.
_MIN_SHRINK_FACTOR = 0.10  # never shrink below 10% of prior ceiling

# ── Anti-blip strike-gate tunables (2026-06-08, user-approved) ──
# An INFERRED shrink (no gateway-stated maximum) that would drop the prior
# known limit to below this fraction of it is a "big drop" and must be
# corroborated by _REQUIRED_STRIKES consecutive overflows before it sticks.
_BIG_DROP_FACTOR = 0.5      # candidate < prior * 0.5  ⇒ "more than 2× shrink"
_REQUIRED_STRIKES = 2       # consecutive big-drop overflows needed to persist
_STRIKE_WINDOW_SEC = 3600.0  # strikes older than this don't count as consecutive


def _facade():
    import lib.context_limits as _f
    return _f


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

    f = _facade()
    with f._lock:
        old = f._LEARNED.get(k)
        prior_known = old if old is not None else preset_limit
        now = time.time()

        # Only persist when the new ceiling is genuinely smaller than the
        # current known one. Otherwise we'd churn the file on every error.
        if prior_known and candidate >= prior_known:
            logger.debug('[CtxLimits] Shrink skipped: candidate=%d >= prior=%d '
                         '(model=%s provider=%s)',
                         candidate, prior_known, model, provider_id or '?')
            # A non-shrinking event clears any pending big-drop strikes.
            if _clear_pending_strikes(k):
                # Clearing only in memory lets a restart resurrect stale
                # corroboration and turn a later first overflow into strike 2.
                f._persist()
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
                f._persist()  # persist the pending-strike meta
                return None

        # Persist the shrink.
        f._LEARNED[k] = candidate
        f._META[k] = {'ts': now, 'source': 'shrink', 'strikes': 0}
        f._persist()

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
    _META = _facade()._META
    meta = _META.get(k)
    if (_has_pending_evidence(meta)
            and (now - _pending_timestamp(meta)) <= _STRIKE_WINDOW_SEC):
        meta['strikes'] = int(meta.get('strikes', 0)) + 1
        meta['pending'] = candidate
        if meta.get('source') == 'pending':
            meta['ts'] = now
        else:
            meta['pending_ts'] = now
        return meta['strikes']
    if k in _facade()._LEARNED and isinstance(meta, dict):
        # Preserve the learned value's source/timestamp: shrink TTL and expand
        # floor semantics must keep working while a new drop awaits strike 2.
        next_meta = dict(meta)
        next_meta.update({
            'strikes': 1,
            'pending': candidate,
            'pending_ts': now,
        })
        _META[k] = next_meta
    else:
        _META[k] = {
            'ts': now,
            'source': 'pending',
            'strikes': 1,
            'pending': candidate,
        }
    return 1


def _clear_pending_strikes(k: str) -> bool:
    """Clear pending evidence and report whether state changed.

    Caller holds ``_lock``. Learned provenance is preserved because it drives
    shrink expiry and expand resolution independently of the strike gate.
    """
    _META = _facade()._META
    meta = _META.get(k)
    if not _has_pending_evidence(meta):
        return False
    if meta.get('source') == 'pending':
        _META.pop(k, None)
    else:
        meta['strikes'] = 0
        meta.pop('pending', None)
        meta.pop('pending_ts', None)
    return True


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
    f = _facade()
    with f._lock:
        old = f._LEARNED.get(k)
        if old is not None and candidate <= old:
            return None
        f._LEARNED[k] = candidate
        # Expand entries are permanent (corroborated by a real accepted
        # prompt) — source='expand' is NOT subject to the shrink TTL.
        f._META[k] = {'ts': time.time(), 'source': 'expand', 'strikes': 0}
        f._persist()

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
