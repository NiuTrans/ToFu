# HOT_PATH — functions in this module are called per-request.
"""lib/model_info/_limits.py — Auto-learned per-model token limit management.

CRITICAL SHARED STATE — this module owns the SINGLE process-wide instances of:
  • ``_LEARNED_MODEL_LIMITS`` — dict[model_id → max_tokens], mutated in place
    by ``_learn_model_limit`` and rebound exactly ONCE (at module load, below)
    to the dict returned by ``_load_learned_limits``.
  • ``_limits_lock`` — the threading.Lock guarding that dict.

Both are re-exported from ``lib.model_info`` BY REFERENCE. The module-load
rebind (``_LEARNED_MODEL_LIMITS = _load_learned_limits()``) happens HERE and
nowhere else, so every caller — ``lib.model_info._LEARNED_MODEL_LIMITS`` and
``lib.model_info._limits._LEARNED_MODEL_LIMITS`` — sees the same object.

Depends on ._max_output (_MODEL_MAX_OUTPUT, _DEFAULT_UNKNOWN_MAX_OUTPUT), which
in turn depends only on ._family — the dependency direction stays acyclic.
"""

import json
import os
import re
import threading

from lib.log import get_logger
from lib.model_info._max_output import (
    _DEFAULT_UNKNOWN_MAX_OUTPUT,
    _MODEL_MAX_OUTPUT,
)

logger = get_logger(__name__)


# ── Auto-learned model limits (persisted to server_config.json) ──────────
_limits_lock = threading.Lock()
_LEARNED_MODEL_LIMITS: dict[str, int] = {}  # model_id → max_tokens


def _load_learned_limits() -> dict:
    """Load auto-learned model token limits from server config."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
            limits = cfg.get('model_limits', {})
            if limits:
                logger.info('[ModelInfo] Loaded %d auto-learned model limits: %s',
                            len(limits), ', '.join(f'{m}={v}' for m, v in limits.items()))
            return limits
    except Exception as e:
        logger.warning('[ModelInfo] Failed to load learned model limits: %s', e)
    return {}


# Initialize on module load — this is the ONE AND ONLY rebind of
# _LEARNED_MODEL_LIMITS. __init__ re-exports the resulting object by reference;
# it must NOT rebind again (that would create a divergent dict).
_LEARNED_MODEL_LIMITS = _load_learned_limits()


def _clamp_max_tokens(model: str, max_tokens: int) -> int:
    """Clamp max_tokens to the model-specific API limit.

    Checks both family-level limits (_MODEL_MAX_OUTPUT) and
    auto-learned per-model limits (_LEARNED_MODEL_LIMITS).
    Takes the minimum of all applicable limits. An unrecognised family is
    clamped to _DEFAULT_UNKNOWN_MAX_OUTPUT so the first request doesn't
    over-ask and get rejected.
    """
    # Defense-in-depth: a caller that passes a missing/None/invalid max_tokens
    # must never crash the clamp (``min(None, int)`` raises TypeError). Fall
    # back to the conservative unknown-family ceiling and let the family/learned
    # limits below refine it. The upstream config resolver is the primary guard;
    # this keeps _clamp_max_tokens total for every caller.
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = _DEFAULT_UNKNOWN_MAX_OUTPUT
    limit = max_tokens
    matched_family = False
    # Check family-level limits
    for _name, (check_fn, family_limit) in _MODEL_MAX_OUTPUT.items():
        if check_fn(model):
            # family_limit can be an int or a callable(model) → int
            effective_limit = family_limit(model) if callable(family_limit) else family_limit
            limit = min(limit, effective_limit)
            matched_family = True
            break
    # Unknown family — apply the conservative default ceiling so we don't ship
    # an over-large max_tokens and eat a guaranteed 400 on the first call.
    if not matched_family:
        limit = min(limit, _DEFAULT_UNKNOWN_MAX_OUTPUT)
    # Check auto-learned model-specific limits (may lower the limit further)
    learned = _LEARNED_MODEL_LIMITS.get(model)
    if learned:
        limit = min(limit, learned)
    return limit


def _learn_model_limit(model: str, limit: int):
    """Auto-learn and persist a model's max_tokens limit.

    Updates the in-memory dict and writes to data/config/server_config.json
    so the limit survives server restarts.

    Args:
        model: Model identifier (e.g. 'gpt-4.1-mini').
        limit: Detected max_tokens upper bound.
    """
    with _limits_lock:
        old = _LEARNED_MODEL_LIMITS.get(model)
        if old == limit:
            return  # already known
        _LEARNED_MODEL_LIMITS[model] = limit
        logger.warning('[ModelInfo] ⚙️ Auto-learned max_tokens for model=%s: %d (was: %s). '
                       'Persisting to config.', model, limit, old or 'unknown')
        # Persist to server_config.json via the locked read-modify-write so
        # a concurrent Settings save / context-limit learn doesn't clobber
        # this model_limits update (and vice-versa).
        try:
            from lib.config_dir import config_path
            from lib.json_store import update_json_atomic
            cfg_path = config_path('server_config.json')

            def _mutate(cfg):
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg.setdefault('model_limits', {})[model] = limit
                return cfg

            update_json_atomic(cfg_path, _mutate, default={})
            logger.info('[ModelInfo] Persisted model limit to %s', cfg_path)
        except Exception as e:
            logger.error('[ModelInfo] Failed to persist model limit for %s: %s',
                         model, e, exc_info=True)
    # Audit trail
    try:
        from lib.log import audit_log
        audit_log('model_limit_learned', model=model, max_tokens=limit, previous=old)
    except Exception as _audit_err:
        logger.debug('[ModelInfo] audit_log for model_limit_learned failed: %s', _audit_err)


def _parse_token_limit_from_error(error_text: str, model: str):
    """Parse max_tokens upper bound from an API error message.

    Recognizes common error message formats from various LLM API providers:
      - "Range of max_tokens should be [1, 65536]"
      - "max_tokens must be at most 65536"
      - "max_tokens value must be between 1 and 65536"
      - "max_output_tokens must be at most 65536"

    Args:
        error_text: The raw error response text (may include JSON wrapping).
        model: Model identifier (for logging).

    Returns:
        Detected max_tokens limit (int), or None if not a token-limit error.
    """
    patterns = [
        # "[1, 65536]" or "[1,65536]" style ranges
        r'max_tokens.*?\[\s*\d+\s*,\s*(\d+)\s*\]',
        # "max_tokens must be at most/less than/no more than N"
        r'max_tokens.*?(?:at most|less than or equal to|no more than|cannot exceed|'
        r'must not exceed|up to|maximum of|maximum is)\s+(\d+)',
        # "max_tokens ... between 1 and N"
        r'max_tokens.*?between\s+\d+\s+and\s+(\d+)',
        # max_output_tokens variants
        r'max_output_tokens.*?\[\s*\d+\s*,\s*(\d+)\s*\]',
        r'max_output_tokens.*?(?:at most|less than or equal to|maximum)\s+(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, error_text, re.IGNORECASE)
        if m:
            detected = int(m.group(1))
            if 1 <= detected <= 1_000_000:  # sanity: must be a plausible token count
                logger.debug('[ModelInfo] Parsed max_tokens limit=%d from error for model=%s',
                            detected, model)
                return detected
    return None
