"""Upstream rate-limit attempt budgets, outage/saturation escalation and cooldown policy."""

from lib.llm_errors import RateLimitError
import os
import time
from lib.log import get_logger
from lib.llm_dispatch._api_errors import DispatchNoAdmissibleSlot, DispatchRateLimitBudgetExceeded

logger = get_logger('lib.llm_dispatch.api')


def _validate_429_attempt_budget(max_429_attempts: int | None) -> int | None:
    """Validate the explicit per-dispatch upstream-attempt ceiling."""
    if max_429_attempts is None:
        return None
    if isinstance(max_429_attempts, bool) or not isinstance(
            max_429_attempts, int):
        raise ValueError('max_429_attempts must be a positive integer or None')
    if max_429_attempts <= 0:
        raise ValueError('max_429_attempts must be a positive integer or None')
    return max_429_attempts


def _raise_if_429_attempt_budget_exhausted(
    *,
    max_429_attempts: int | None,
    upstream_attempts: int,
    last_error: Exception,
) -> None:
    if (max_429_attempts is not None
            and upstream_attempts >= max_429_attempts):
        raise DispatchRateLimitBudgetExceeded(
            last_error,
            attempts=upstream_attempts,
            limit=max_429_attempts,
        ) from last_error


# A provider 429 is a waitable capacity signal, not evidence that the selected
# model is dead. Keep rotating until recovery or user cancellation by default;
# operators may opt into a positive bounded-escalation budget explicitly.
_DEFAULT_429_SATURATION_SECS = 0.0


# How long (seconds) to cool a slot whose endpoint was unreachable at the
# connect phase, so the picker routes around the dead host instead of
# retrying it every cycle. The local health checker clears this early when
# the box recovers. Env-tunable for deployments with flaky self-hosted boxes.
try:
    _UNREACHABLE_COOLDOWN = float(os.environ.get('TOFU_UNREACHABLE_COOLDOWN', '30'))
    if _UNREACHABLE_COOLDOWN <= 0:
        _UNREACHABLE_COOLDOWN = 30.0
except (ValueError, TypeError) as e:
    logger.debug('[Dispatch] TOFU_UNREACHABLE_COOLDOWN parse failed, using default: %s', e)
    _UNREACHABLE_COOLDOWN = 30.0


def _gateway_outage_budget_secs() -> float:
    """Wall-clock ceiling (seconds) on a GATEWAY-OUTAGE streak in the
    streaming dispatch loops.

    ``TOFU_GATEWAY_OUTAGE_BUDGET_S`` (default 0 = DISABLED — wait forever).
    Owner directive 2026-08-20: a gateway 5xx (502/503/504) storm means the
    whole upstream is sick, and that is WAITABLE — never interrupt the turn
    on the user's behalf. The loop keeps rotating (0.3s cycles, abort_check
    every cycle) until the gateway recovers or the USER cancels; stopping
    is the user's call, never the system's. Unlike model-local 429 saturation,
    a gateway outage has no independently selectable fallback target here.

    A real per-key 429 or a success clears the outage streak, so genuine
    contention is never capped either way. Set a positive budget to restore
    the legacy bounded give-up (raise the streak error so the worker thread
    frees itself during a total outage — thread-pool-starvation guard).
    Read per call so tests / ops can toggle without a restart.
    """
    try:
        _v = float(os.environ.get('TOFU_GATEWAY_OUTAGE_BUDGET_S', '') or '0')
        return _v if _v > 0 else 0.0
    except (ValueError, TypeError) as e:
        logger.debug('[Dispatch] TOFU_GATEWAY_OUTAGE_BUDGET_S parse failed, '
                     'using default (disabled): %s', e)
        return 0.0


def _saturation_budget_secs() -> float:
    """Bounded-escalation budget (seconds) for continuous all-slot 429
    saturation ( 交付①).

    ``TOFU_429_SATURATION_SECS`` defaults to 0 (disabled): a 429 wall keeps
    rotating until it recovers or the user cancels. A positive value is an
    explicit operator policy; once exceeded, ``RateLimitError`` with
    ``is_saturation=True`` reaches ``llm_fallback`` so another model can make
    progress.
    Read per call so tests / ops can tune the policy without a restart.
    """
    try:
        raw = os.environ.get('TOFU_429_SATURATION_SECS')
        if raw is None or not raw.strip():
            return _DEFAULT_429_SATURATION_SECS
        parsed = float(raw)
        return parsed if parsed > 0 else 0.0
    except (ValueError, TypeError) as e:
        logger.debug('[Dispatch] TOFU_429_SATURATION_SECS parse failed, '
                     'using default: %s', e)
        return _DEFAULT_429_SATURATION_SECS


def _force_oauth_token_refresh(
    oauth: str,
    log_prefix: str = '',
    owner_user_id: int | None = None,
) -> bool:
    """Force an OAuth-subscription token refresh after a mid-flight 401.

    ``resolve_oauth_request`` only refreshes near expiry, so a token
    revoked/refreshed ELSEWHERE yields a 401 with a locally-"valid" token.
    Bypass the near-expiry check by calling the provider's refresh function
    directly. Returns True when a fresh token was stored (caller retries
    the request ONCE with it). Scoped to subscription slots — plain API-key
    slots never reach here.
    """
    try:
        from lib.llm._transport import transport_owner_scope
        owner_scope = transport_owner_scope(owner_user_id)
        if oauth == 'claude':
            from lib.oauth.claude import claude_refresh_token
            refreshed = claude_refresh_token(user_id=owner_scope)
        elif oauth == 'codex':
            from lib.oauth.codex import codex_refresh_token
            refreshed = codex_refresh_token(user_id=owner_scope)
        else:
            logger.warning('%s _force_oauth_token_refresh: unknown oauth '
                           'provider %r', log_prefix, oauth)
            return False
    except Exception as e:
        logger.warning('%s Forced %s token refresh raised: %s',
                       log_prefix, oauth, e, exc_info=True)
        return False
    if refreshed:
        logger.info('%s Forced %s token refresh succeeded — retrying '
                    'request once with the new token', log_prefix, oauth)
        return True
    logger.warning('%s Forced %s token refresh returned no token',
                   log_prefix, oauth)
    return False


def _saturation_escalate(log_prefix, label, *, elapsed_s, budget_s, cycles,
                         model):
    """Raise the bounded 429-saturation escalation ( 交付①).

    Fires once per dispatch call when EVERY candidate slot has been
    continuously rate-limited past the budget — the caller (llm_fallback)
    treats it as a model-swap trigger. ``is_saturation`` keeps it OUT of
    the key-exhausted-for-today channel: the keys are healthy, merely
    contended (2026-08-01 incident: a VU carrier spun 3900+ cycles / 75min
    on a saturated shared-key model while a fallback model sat idle).
    """
    logger.warning(
        '%s %s: 429 saturation — every candidate slot continuously '
        'rate-limited for %.0fs (budget %.0fs, %d cycles, model=%s) — '
        'escalating to caller for model fallback',
        log_prefix, label, elapsed_s, budget_s, cycles, model or '?')
    try:
        from lib.log import audit_log as _audit
        _audit('llm_429_saturation', model=model or '',
               elapsed_s=round(elapsed_s, 1), cycles=cycles,
               budget_s=budget_s)
    except Exception as _ae:
        logger.debug('%s saturation audit_log failed: %s', log_prefix, _ae)
    raise RateLimitError(
        f'429 saturation: all candidate slots continuously rate-limited for '
        f'{elapsed_s:.0f}s (budget {budget_s:.0f}s, {cycles} cycles)',
        is_saturation=True, status_code=429,
        reason=f'saturation:{elapsed_s:.0f}s')


def _raise_dispatch_exhausted(last_err, *, max_retries, capability,
                              prefer_model=None, first_err=None,
                              what='dispatch'):
    """Raise the terminal error when a dispatch loop runs out of slots.

    When the exhausting failure was an endpoint-unreachable error, every
    candidate slot was a dead host — re-raise a single clear message
    naming the model instead of letting an opaque urllib3 ``MaxRetryError``
    bubble to the user. Otherwise propagate ``last_err`` unchanged (or a
    generic RuntimeError when there was no captured error).
    """
    from lib.llm_errors import BadRequestError, EndpointUnreachableError
    if last_err is None:
        from lib.key_stats import is_strict_billing_stop_admission
        if is_strict_billing_stop_admission():
            target = f'model={prefer_model}' if prefer_model else (
                f'capability={capability}')
            raise DispatchNoAdmissibleSlot(
                f'No policy-admissible slot for {target}; '
                'no upstream request was sent')
    if isinstance(last_err, EndpointUnreachableError):
        _target = prefer_model or capability
        _url = getattr(last_err, 'base_url', '') or ''
        msg = ('All endpoints for %s are unreachable — the model server(s) '
               'appear to be down or not accepting connections. '
               'Check that the endpoint is running and reachable.'
               % (('model %r' % _target) if prefer_model else
                  ('capability %r' % capability)))
        if _url:
            msg += ' (last tried: %s)' % _url
        raise EndpointUnreachableError(msg, base_url=_url) from last_err
    if isinstance(first_err, BadRequestError) \
            and isinstance(last_err, BadRequestError) \
            and first_err is not last_err:
        # Every tried pair failed with a DIFFERENT deterministic 400: the
        # first (usually the preferred model's payload rejection) names the
        # actionable cause; the last-ditch fallback's 400 would mask it.
        raise first_err from last_err
    raise last_err or RuntimeError(
        'All %d %s attempts failed for capability=%s'
        % (max_retries, what, capability))


def _remember_route_missing_error(previous, route_missing_error):
    """Keep an actionable provider failure ahead of routing-catalog noise.

    A logical model can have several wire IDs.  One stale ID returning
    ``ModelRouteMissingError`` is useful for route suppression, but must not
    replace a 401/403, payload rejection, timeout, or upstream error already
    observed on another wire ID that actually reaches the model.
    """
    from lib.llm_errors import ModelRouteMissingError
    if previous is not None and not isinstance(
            previous, ModelRouteMissingError):
        return previous
    return route_missing_error


def _record_route_missing_model(dispatcher, slot, error) -> None:
    """Process-suppress one unserved provider/wire-model route if supported.

    Test doubles and older embedders may expose only the picker protocol, so
    this remains a capability call.  The dispatch-local durable exclusion is
    still authoritative for the current request.
    """
    recorder = getattr(dispatcher, 'mark_model_route_missing', None)
    if not callable(recorder):
        return
    try:
        recorder(
            provider_id=str(getattr(slot, 'provider_id', '') or ''),
            model=str(getattr(slot, 'model', '') or ''),
            error=str(error),
        )
    except Exception as exc:
        logger.warning(
            '[Dispatch] Failed to suppress route-missing model %s/%s: %s',
            getattr(slot, 'provider_id', '') or '?',
            getattr(slot, 'model', '') or '?', exc)


def _unix_time_ns() -> int:
    """Return wall time in nanoseconds through replaceable clock facades."""
    precise = getattr(time, 'time_ns', None)
    if callable(precise):
        return int(precise())
    return int(float(time.time()) * 1_000_000_000)
