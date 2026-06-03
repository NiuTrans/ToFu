"""lib/context_limits.py — Auto-learned per-(provider, model) context-window limits.

Tofu often routes the same logical model id through several providers (e.g.
``deepseek-v3.2`` may be served by Tencent, Baidu, Huawei, Doubao gateways).
Each provider may advertise a different context length even for an identical
upstream model — and our preset tables in :mod:`lib.tasks_pkg.compaction`
inevitably get some of these wrong.

This module corrects the preset error in BOTH directions, by learning from
real traffic:

* **Shrink** — when an LLM call fails with ``PromptTooLongError``, parse the
  upstream-reported max ("…N tokens > M maximum") and persist M as the new
  ceiling for that ``(provider_id, model)`` pair. The preset said 1M but the
  provider actually rejected at 200k? Use 200k from now on.

* **Expand** — when an LLM call succeeds, look at the actual ``prompt_tokens``
  it accepted. If the call sent more tokens than our currently-known limit
  (static preset OR prior learned value), bump the learned ceiling up to that
  observed count plus a small headroom. The preset said 128k but a 600k
  request just succeeded? Raise to 600k.

Both paths persist to ``data/config/server_config.json`` under a new key
``model_context_limits`` (so the value survives restarts), and surface a
single ``_context_limit_learned`` blob inside ``usage`` so the orchestrator
can show a one-line SSE notice to the user.

Key shape: ``"<provider_id>::<model>"`` — falls back to the bare model name
when no provider is known so older callers keep working.
"""

import json
import os
import threading

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


def _key(provider_id: str | None, model: str) -> str:
    """Compose the storage key. Empty provider_id collapses to bare model."""
    pid = (provider_id or '').strip()
    m = (model or '').strip()
    if not m:
        return ''
    return f'{pid}::{m}' if pid else m


def _load() -> dict[str, int]:
    """Load persisted learned limits from server_config.json."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
            limits = cfg.get('model_context_limits') or {}
            # Coerce values to int; drop anything bogus.
            cleaned = {}
            for k, v in limits.items():
                try:
                    iv = int(v)
                except (TypeError, ValueError) as e:
                    logger.debug('[CtxLimits] Dropping non-int value for %s: %r (%s)', k, v, e)
                    continue
                if _MIN_LEARNABLE <= iv <= _MAX_LEARNABLE:
                    cleaned[k] = iv
            if cleaned:
                logger.info('[CtxLimits] Loaded %d auto-learned context limits',
                            len(cleaned))
            return cleaned
    except Exception as e:
        logger.warning('[CtxLimits] Failed to load learned context limits: %s', e)
    return {}


_LEARNED = _load()


def _persist():
    """Write the in-memory dict to server_config.json. Caller holds _lock."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        cfg = {}
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning('[CtxLimits] server_config.json unreadable, '
                               'rewriting from scratch: %s', e)
                cfg = {}
        cfg['model_context_limits'] = dict(_LEARNED)
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        tmp_path = cfg_path + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, cfg_path)
    except Exception as e:
        logger.error('[CtxLimits] Failed to persist learned context limits: %s',
                     e, exc_info=True)


def lookup_learned_context_limit(provider_id: str | None, model: str) -> int | None:
    """Return the learned context limit for ``(provider_id, model)``, or None.

    Tries the per-provider key first, then falls back to the bare model id
    so legacy single-provider entries still apply.
    """
    if not model:
        return None
    with _lock:
        v = _LEARNED.get(_key(provider_id, model))
        if v is None and provider_id:
            v = _LEARNED.get(_key('', model))
        return int(v) if v else None


def learn_shrink_from_error(provider_id: str | None, model: str,
                            reported_tokens: int | None,
                            preset_limit: int | None = None) -> dict | None:
    """Persist a shrunk context limit based on an overflow error.

    Args:
        provider_id: Provider id from the dispatch slot (may be empty).
        model: Model id we just sent the rejected request to.
        reported_tokens: The N in ``"prompt is too long: N tokens > M maximum"``.
            We treat this as "the provider definitely cannot accept N tokens"
            and pick a ceiling slightly below it (95%) so rounding/header
            overhead doesn't push us over again.
        preset_limit: The currently-believed limit (static + prior-learned).
            Used to floor the shrink so a single anomalous error doesn't
            collapse the limit to almost zero.

    Returns:
        ``{'model': …, 'old_limit': old, 'new_limit': new}`` if a change was
        persisted, else None.
    """
    if not model or not reported_tokens or reported_tokens < _MIN_LEARNABLE:
        return None

    # Provider rejected at >= reported_tokens, so the true ceiling is
    # somewhere between the previous successful prompt size and reported_tokens.
    # We don't have the former here, so be conservative: take 95% of
    # reported_tokens as the new ceiling.
    candidate = int(reported_tokens * 0.95)

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
        # Only persist when the new ceiling is genuinely smaller than the
        # current known one (whether that's an old learned value or the
        # static preset). Otherwise we'd churn the file on every error.
        prior_known = old if old is not None else preset_limit
        if prior_known and candidate >= prior_known:
            logger.debug('[CtxLimits] Shrink skipped: candidate=%d >= prior=%d '
                         '(model=%s provider=%s)',
                         candidate, prior_known, model, provider_id or '?')
            return None
        _LEARNED[k] = candidate
        _persist()

    logger.warning('[CtxLimits] ⚙️ Learned SHRUNK context limit for '
                   'provider=%s model=%s: %d (was %s, reported overflow at %d tokens)',
                   provider_id or '?', model, candidate,
                   old if old is not None else (preset_limit or 'unknown'),
                   reported_tokens)
    try:
        audit_log('context_limit_learned',
                  direction='shrink',
                  provider_id=provider_id or '',
                  model=model,
                  new_limit=candidate,
                  old_limit=old,
                  reported_tokens=reported_tokens)
    except Exception as e:
        logger.debug('[CtxLimits] audit_log failed: %s', e)

    return {'model': model, 'provider_id': provider_id or '',
            'old_limit': old or preset_limit or 0,
            'new_limit': candidate, 'direction': 'shrink'}


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
