# HOT_PATH — functions in this module are called per-request.
"""lib/model_info/_limits.py — Auto-learned model and route token limits.

CRITICAL SHARED STATE — this module owns the SINGLE process-wide instances of:
  • ``_LEARNED_MODEL_LIMITS`` — dict[model_id → max_tokens], mutated in place
    by ``_learn_model_limit`` and rebound exactly ONCE (at module load, below)
    to the dict returned by ``_load_learned_limits``.
  • ``_LEARNED_ROUTE_LIMITS`` — route-identity → max_tokens evidence.
  • ``_limits_lock`` — the threading.Lock guarding both dicts.

Both are re-exported from ``lib.model_info`` BY REFERENCE. The module-load
rebind (``_LEARNED_MODEL_LIMITS = _load_learned_limits()``) happens HERE and
nowhere else, so every caller — ``lib.model_info._LEARNED_MODEL_LIMITS`` and
``lib.model_info._limits._LEARNED_MODEL_LIMITS`` — sees the same object.

Depends on ._max_output (_MODEL_MAX_OUTPUT, _DEFAULT_UNKNOWN_MAX_OUTPUT), which
in turn depends only on ._family — the dependency direction stays acyclic.
"""

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass

from lib.log import get_logger
from lib.model_info._max_output import (
    _DEFAULT_UNKNOWN_MAX_OUTPUT,
    _MODEL_MAX_OUTPUT,
)

logger = get_logger(__name__)


# ── Auto-learned model limits (persisted to server_config.json) ──────────
_limits_lock = threading.Lock()
_LEARNED_MODEL_LIMITS: dict[str, int] = {}  # model_id → max_tokens
_LEARNED_ROUTE_LIMITS: dict[str, int] = {}
_MAX_MODEL_ID_CHARS = 256
_MAX_ROUTE_KEY_CHARS = 80
_MAX_LEARNED_MODEL_LIMIT = 1_000_000
_MAX_LEARNED_MODEL_ENTRIES = 2_048


def _sanitize_learned_limits(
    raw_limits,
    *,
    max_entries: int = _MAX_LEARNED_MODEL_ENTRIES,
    max_key_chars: int = _MAX_MODEL_ID_CHARS,
) -> tuple[dict[str, int], bool]:
    """Return a bounded plain-map projection of persisted model limits.

    ``model_limits`` predates per-entry metadata, so JSON/dict insertion order
    is its only recency signal. Writers move fresh evidence to the end and the
    sanitizer retains that newest tail when the shared capacity is reached.
    """
    source = raw_limits if isinstance(raw_limits, dict) else {}
    cleaned: dict[str, int] = {}
    for model, value in source.items():
        if not (
            isinstance(model, str)
            and model
            and model == model.strip()
            and len(model) <= max_key_chars
        ):
            continue
        try:
            limit = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 1 <= limit <= _MAX_LEARNED_MODEL_LIMIT:
            cleaned[model] = limit
    if len(cleaned) > max_entries:
        cleaned = dict(list(cleaned.items())[-max_entries:])
    return cleaned, not isinstance(raw_limits, dict) or cleaned != raw_limits


def _repair_persisted_limits(cfg_path: str) -> None:
    """Sanitize the file's current map under its cross-writer lock."""
    from lib.json_store import update_json_atomic

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        limits, _ = _sanitize_learned_limits(cfg.get('model_limits'))
        route_limits, _ = _sanitize_learned_limits(
            cfg.get('route_model_limits'),
            max_key_chars=_MAX_ROUTE_KEY_CHARS)
        cfg['model_limits'] = limits
        cfg['route_model_limits'] = route_limits
        return cfg

    update_json_atomic(cfg_path, _mutate, default={})


def _load_learned_limits() -> dict:
    """Load auto-learned model token limits from server config."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
            limits, changed = _sanitize_learned_limits(
                cfg.get('model_limits'))
            if changed:
                try:
                    _repair_persisted_limits(cfg_path)
                except Exception as e:
                    # A read-only/transiently unavailable config must not
                    # discard the valid bounded projection we already loaded.
                    logger.warning(
                        '[ModelInfo] Failed to repair learned limits: %s', e)
            if limits:
                logger.info('[ModelInfo] Loaded %d auto-learned model limits',
                            len(limits))
            return limits
    except Exception as e:
        logger.warning('[ModelInfo] Failed to load learned model limits: %s', e)
    return {}


# Initialize on module load — this is the ONE AND ONLY rebind of
# _LEARNED_MODEL_LIMITS. __init__ re-exports the resulting object by reference;
# it must NOT rebind again (that would create a divergent dict).
_LEARNED_MODEL_LIMITS = _load_learned_limits()


def _load_learned_route_limits() -> dict[str, int]:
    """Load bounded route-scoped output limits from server config."""
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if os.path.isfile(cfg_path):
            with open(cfg_path) as handle:
                cfg = json.load(handle)
            limits, changed = _sanitize_learned_limits(
                cfg.get('route_model_limits'),
                max_key_chars=_MAX_ROUTE_KEY_CHARS)
            if changed:
                _repair_persisted_limits(cfg_path)
            return limits
    except Exception as error:
        logger.warning(
            '[ModelInfo] Failed to load learned route limits: %s', error)
    return {}


_LEARNED_ROUTE_LIMITS = _load_learned_route_limits()


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


def _clamp_route_max_tokens(
    model: str,
    max_tokens: int,
    *,
    route_key: str = '',
    declared_limit: int = 0,
) -> int:
    """Clamp against family, declared Deployment and learned route limits."""
    limit = _clamp_max_tokens(model, max_tokens)
    if isinstance(declared_limit, int) and declared_limit > 0:
        limit = min(limit, declared_limit)
    learned = _LEARNED_ROUTE_LIMITS.get(str(route_key or ''))
    if learned:
        limit = min(limit, learned)
    return limit


def _route_output_limit_key(
    *,
    provider_id: str,
    offering_id: str,
    deployment_id: str,
    protocol: str,
    model: str,
) -> str:
    """Return a collision-free, bounded identity for route-local evidence.

    A versioned digest avoids delimiter collisions while bounding the
    persisted map. ``model`` is a compatibility discriminator for legacy
    slots without deployment ids.
    """
    identity = json.dumps(
        [provider_id, offering_id, deployment_id, protocol, model],
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')
    return 'route:v1:' + hashlib.sha256(identity).hexdigest()


def _learn_route_limit(route_key: str, limit: int) -> None:
    """Persist provider/Offering/Deployment/protocol-specific evidence."""
    if not (
        isinstance(route_key, str)
        and route_key
        and route_key == route_key.strip()
        and len(route_key) <= _MAX_ROUTE_KEY_CHARS
        and isinstance(limit, int)
        and not isinstance(limit, bool)
        and 1 <= limit <= _MAX_LEARNED_MODEL_LIMIT
    ):
        logger.warning('[ModelInfo] Ignoring invalid learned route limit')
        return
    with _limits_lock:
        old = _LEARNED_ROUTE_LIMITS.get(route_key)
        if old == limit:
            return
        _LEARNED_ROUTE_LIMITS.pop(route_key, None)
        _LEARNED_ROUTE_LIMITS[route_key] = limit
        sanitized, _ = _sanitize_learned_limits(
            _LEARNED_ROUTE_LIMITS, max_key_chars=_MAX_ROUTE_KEY_CHARS)
        _LEARNED_ROUTE_LIMITS.clear()
        _LEARNED_ROUTE_LIMITS.update(sanitized)
        try:
            from lib.config_dir import config_path
            from lib.json_store import update_json_atomic
            cfg_path = config_path('server_config.json')

            def _mutate(cfg):
                if not isinstance(cfg, dict):
                    cfg = {}
                limits, _ = _sanitize_learned_limits(
                    cfg.get('route_model_limits'),
                    max_key_chars=_MAX_ROUTE_KEY_CHARS)
                limits.pop(route_key, None)
                limits[route_key] = limit
                limits, _ = _sanitize_learned_limits(
                    limits, max_key_chars=_MAX_ROUTE_KEY_CHARS)
                cfg['route_model_limits'] = limits
                return cfg

            persisted = update_json_atomic(cfg_path, _mutate, default={})
            persisted_limits, _ = _sanitize_learned_limits(
                (persisted or {}).get('route_model_limits'),
                max_key_chars=_MAX_ROUTE_KEY_CHARS)
            _LEARNED_ROUTE_LIMITS.clear()
            _LEARNED_ROUTE_LIMITS.update(persisted_limits)
        except Exception as error:
            logger.error(
                '[ModelInfo] Failed to persist route limit: %s', error,
                exc_info=True)
    try:
        from lib.log import audit_log
        audit_log(
            'route_model_limit_learned', route_key=route_key,
            max_tokens=limit, previous=old)
    except Exception as audit_error:
        logger.debug(
            '[ModelInfo] route limit audit failed: %s', audit_error)


def _learn_model_limit(model: str, limit: int, *, route_key: str = ''):
    """Auto-learn and persist a model's max_tokens limit.

    Updates the in-memory dict and writes to data/config/server_config.json
    so the limit survives server restarts.

    Args:
        model: Model identifier (e.g. 'gpt-4.1-mini').
        limit: Detected max_tokens upper bound.
    """
    if route_key:
        _learn_route_limit(route_key, limit)
        return
    if not (
        isinstance(model, str)
        and model
        and model == model.strip()
        and len(model) <= _MAX_MODEL_ID_CHARS
        and isinstance(limit, int)
        and not isinstance(limit, bool)
        and 1 <= limit <= _MAX_LEARNED_MODEL_LIMIT
    ):
        logger.warning(
            '[ModelInfo] Ignoring invalid learned model limit identity/value')
        return

    with _limits_lock:
        old = _LEARNED_MODEL_LIMITS.get(model)
        if old == limit:
            return  # already known
        _LEARNED_MODEL_LIMITS.pop(model, None)
        _LEARNED_MODEL_LIMITS[model] = limit
        sanitized, _ = _sanitize_learned_limits(_LEARNED_MODEL_LIMITS)
        _LEARNED_MODEL_LIMITS.clear()
        _LEARNED_MODEL_LIMITS.update(sanitized)
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
                limits, _ = _sanitize_learned_limits(
                    cfg.get('model_limits'))
                # Refresh insertion order so finite-capacity eviction follows
                # actual evidence rather than the model's first-ever sighting.
                limits.pop(model, None)
                limits[model] = limit
                limits, _ = _sanitize_learned_limits(limits)
                cfg['model_limits'] = limits
                return cfg

            persisted = update_json_atomic(cfg_path, _mutate, default={})
            persisted_limits, _ = _sanitize_learned_limits(
                (persisted or {}).get('model_limits'))
            _LEARNED_MODEL_LIMITS.clear()
            _LEARNED_MODEL_LIMITS.update(persisted_limits)
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


@dataclass(frozen=True)
class TokenLimitEvidence:
    """Normalized provider evidence for an output-token boundary."""

    field: str
    boundary: int
    inclusive: bool

    @property
    def maximum_allowed(self) -> int:
        return self.boundary if self.inclusive else self.boundary - 1


def _parse_token_limit_evidence(
    error_text: str,
    model: str,
) -> TokenLimitEvidence | None:
    """Parse inclusive/exclusive output-token bounds from provider text."""
    token_field = (
        r'(?P<field>max_tokens|max_output_tokens|maxOutputTokens)'
    )
    exclusive_patterns = [
        token_field
        + r'.*?range\s+is\s+from\s+\d+\s*\(?\s*inclusive\s*\)?'
          r'\s+to\s+(?P<limit>\d+)\s*\(?\s*exclusive\s*\)?',
        token_field
        + r'.*?(?:less\s+than|below)\s+(?P<limit>\d+)',
    ]
    inclusive_patterns = [
        token_field + r'.*?\[\s*\d+\s*,\s*(?P<limit>\d+)\s*\]',
        token_field
        + r'.*?(?:at\s+most|less\s+than\s+or\s+equal\s+to|no\s+more\s+than|'
          r'cannot\s+exceed|must\s+not\s+exceed|up\s+to|maximum\s+of|'
          r'maximum\s+is)\s+(?P<limit>\d+)',
        token_field
        + r'.*?between\s+\d+\s+and\s+(?P<limit>\d+)',
    ]
    for inclusive, patterns in (
        (False, exclusive_patterns),
        (True, inclusive_patterns),
    ):
        for pattern in patterns:
            match = re.search(pattern, error_text, re.IGNORECASE)
            if not match:
                continue
            boundary = int(match.group('limit'))
            evidence = TokenLimitEvidence(
                field=match.group('field'),
                boundary=boundary,
                inclusive=inclusive,
            )
            maximum = evidence.maximum_allowed
            if 1 <= maximum <= _MAX_LEARNED_MODEL_LIMIT:
                logger.debug(
                    '[ModelInfo] Parsed output-token boundary=%d '
                    'inclusive=%s maximum=%d for model=%s',
                    boundary, inclusive, maximum, model)
                return evidence
    return None


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
    evidence = _parse_token_limit_evidence(error_text, model)
    return evidence.maximum_allowed if evidence is not None else None
