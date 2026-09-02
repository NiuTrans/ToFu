"""lib/llm_dispatch/api.py — Module-level dispatch convenience functions.

High-level API for LLM dispatch: dispatch_chat, dispatch_stream,
dispatch_fastest, dispatch_parallel, smart_chat, smart_chat_batch, etc.
All functions call get_dispatcher() internally.

Usage:
    from lib.llm_dispatch import dispatch_chat, dispatch_stream, smart_chat
"""

import copy
from dataclasses import dataclass
import math
import os
import threading
import time
from collections import defaultdict

from lib.llm_errors import (
    RateLimitError,
    is_subscription_quota_error,
    parse_subscription_retry_after,
)
from lib.llm.stream_result import (
    ProviderStreamResult,
    ensure_provider_stream_result,
)
from lib.log import get_logger

from .factory import get_dispatcher

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchWaitStatus:
    """Typed status for a pool-capacity wait before an attempt is reserved."""

    kind: str
    request_elapsed_s: float
    transport_idle_s: float = 0.0
    semantic_idle_s: float = 0.0
    response_headers_seen: bool = False
    transport_byte_count: int = 0
    sse_event_count: int = 0
    reasoning_chars: int = 0
    content_chars: int = 0
    tool_call_count: int = 0

# A provider 429 is a waitable capacity signal, not evidence that the selected
# model is dead. Keep rotating until recovery or user cancellation by default;
# operators may opt into a positive bounded-escalation budget explicitly.
_DEFAULT_429_SATURATION_SECS = 0.0
_DEFAULT_429_RETRY_DELAY_S = 0.3

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


def _force_oauth_token_refresh(oauth: str, log_prefix: str = '') -> bool:
    """Force an OAuth-subscription token refresh after a mid-flight 401.

    ``resolve_oauth_request`` only refreshes near expiry, so a token
    revoked/refreshed ELSEWHERE yields a 401 with a locally-"valid" token.
    Bypass the near-expiry check by calling the provider's refresh function
    directly. Returns True when a fresh token was stored (caller retries
    the request ONCE with it). Scoped to subscription slots — plain API-key
    slots never reach here.
    """
    try:
        if oauth == 'claude':
            from lib.oauth.claude import claude_refresh_token
            refreshed = claude_refresh_token()
        elif oauth == 'codex':
            from lib.oauth.codex import codex_refresh_token
            refreshed = codex_refresh_token()
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
                              prefer_model=None, what='dispatch'):
    """Raise the terminal error when a dispatch loop runs out of slots.

    When the exhausting failure was an endpoint-unreachable error, every
    candidate slot was a dead host — re-raise a single clear message
    naming the model instead of letting an opaque urllib3 ``MaxRetryError``
    bubble to the user. Otherwise propagate ``last_err`` unchanged (or a
    generic RuntimeError when there was no captured error).
    """
    from lib.llm_errors import EndpointUnreachableError
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
    raise last_err or RuntimeError(
        'All %d %s attempts failed for capability=%s'
        % (max_retries, what, capability))


def _unix_time_ns() -> int:
    """Return wall time in nanoseconds through replaceable clock facades."""
    precise = getattr(time, 'time_ns', None)
    if callable(precise):
        return int(precise())
    return int(float(time.time()) * 1_000_000_000)


def _shared_contention_retry_delay(dispatcher, slot, is_contention,
                                   log_prefix) -> float:
    """Arm project admission, retaining legacy delay for old dispatchers."""
    if not is_contention:
        return _DEFAULT_429_RETRY_DELAY_S
    note_contention = getattr(dispatcher, 'note_shared_contention', None)
    if not callable(note_contention):
        return _DEFAULT_429_RETRY_DELAY_S
    try:
        retry_delay = float(note_contention(slot))
        # Current dispatchers apply the delay before the next network request,
        # where every task sees the same family gate. Older/fake dispatchers
        # have no admission hook, so preserve their post-failure delay.
        if callable(getattr(
                dispatcher, 'reserve_shared_contention_probe', None)):
            return 0.0
        if math.isfinite(retry_delay) and retry_delay >= 0:
            return max(_DEFAULT_429_RETRY_DELAY_S, retry_delay)
    except Exception as exc:
        logger.debug('%s note_shared_contention failed: %s', log_prefix, exc)
    return _DEFAULT_429_RETRY_DELAY_S


def _shared_contention_admission_decision(dispatcher, slot) -> tuple[float, bool]:
    """Return a validated ``(delay_s, admitted)`` family-gate decision."""
    reserve_probe = getattr(
        dispatcher, 'reserve_shared_contention_probe', None)
    if not callable(reserve_probe):
        return 0.0, True
    decision = reserve_probe(slot)
    delay_s = float(getattr(decision, 'delay_s'))
    admitted = bool(getattr(decision, 'admitted'))
    if not math.isfinite(delay_s) or delay_s < 0:
        raise ValueError(f'invalid shared-contention delay: {delay_s!r}')
    return delay_s, admitted


def _wait_for_shared_contention_admission(
        dispatcher, slot, sleep_fn, log_prefix) -> None:
    """Synchronously wait for one pre-request family probe reservation."""
    while True:
        try:
            delay_s, admitted = _shared_contention_admission_decision(
                dispatcher, slot)
        except Exception as exc:
            # Fail closed to the historical 0.3s retry rather than turning a
            # coordinator defect into an unbounded upstream request herd.
            logger.debug('%s shared-contention admission failed: %s',
                         log_prefix, exc)
            sleep_fn(_DEFAULT_429_RETRY_DELAY_S)
            return
        if delay_s > 0:
            sleep_fn(delay_s)
        if admitted:
            return


async def _wait_for_shared_contention_admission_async(
        dispatcher, slot, *, state, abort_check, async_sleep_fn,
        log_prefix) -> None:
    """Asynchronously wait for one pre-request family probe reservation."""
    while True:
        try:
            delay_s, admitted = _shared_contention_admission_decision(
                dispatcher, slot)
        except Exception as exc:
            logger.debug('%s shared-contention admission failed: %s',
                         log_prefix, exc)
            delay_s, admitted = _DEFAULT_429_RETRY_DELAY_S, True
        if delay_s > 0:
            wait_started = time.monotonic()
            await async_sleep_fn(delay_s, abort_check)
            state.record_queue_wait(wait_started)
        if admitted:
            return


def _note_shared_contention_recovered(dispatcher, slot, log_prefix) -> None:
    """Best-effort signal that a provider/model project admitted traffic."""
    note_success = getattr(dispatcher, 'note_shared_success', None)
    if not callable(note_success):
        return
    try:
        note_success(slot)
    except Exception as exc:
        logger.debug('%s note_shared_success failed: %s', log_prefix, exc)



def _cool_slot_on_premature_close(slot, usage, *, stream_state=''):
    """Settle an unusable provider stream once as a slot soft failure.

    ``lib/llm/_sse_core.py::SSEAccumulator.finalize`` retains
    ``usage['_missing_done']`` as a compatibility diagnostic, but a provider
    finish frame can prove completion without the optional trailing ``[DONE]``.
    The closed ``_stream_state`` therefore decides health. Only an unusable
    state (or a legacy missing-DONE tuple with no closed state) enters the same
    ``record_truncation`` path used by translation retries; after three
    consecutive soft failures, the existing exponential-backoff/300s cap cools
    the slot.

    The reservation is released by ``record_truncation`` itself. No provisional
    success is recorded and no consecutive-error rollback is needed.

    The over-cooling guard is the parser's closed state (plus legacy
    ``_missing_done``): verified completion and client abort are never cooled.
    """
    if not isinstance(usage, dict) and not stream_state:
        return
    usage = usage if isinstance(usage, dict) else {}
    stream_state = str(stream_state or usage.get('_stream_state') or '')
    unusable_states = {
        'premature_close', 'malformed_stream',
        'semantic_progress_timeout', 'no_actionable_output',
        'empty_response', 'tool_payload_missing', 'unknown',
    }
    if stream_state:
        # Closed typed state is authoritative. A provider may omit ``[DONE]``
        # after an otherwise authoritative finish frame; that is verified
        # completion and must not be cooled merely because the compatibility
        # diagnostic still records ``_missing_done``.
        if stream_state not in unusable_states:
            return
    elif not usage.get('_missing_done'):
        # Historical tuple/plugin seam: no closed state exists, so retain the
        # conservative missing-DONE health signal until that producer migrates.
        return
    try:
        _elapsed = usage.get('stream_elapsed_ms', 0)
        _chunks = usage.get('_chunks_received', 0)
        slot.record_truncation(
            error='unusable provider stream state=%s '
                  'elapsed_ms=%s chunks=%s' % (
                      stream_state or 'legacy_missing_done',
                      _elapsed,
                      _chunks,
                  ),
            release_reservation=True,
        )
        from lib.log import audit_log
        audit_log('premature_close_cooldown',
                  key_name=slot.key_name, model=slot.model,
                  provider_id=slot.provider_id,
                  consecutive_errors=slot.consecutive_errors,
                  cooldown_until=round(slot.cooldown_until, 1),
                  elapsed_ms=_elapsed, chunks=_chunks,
                  stream_state=stream_state or 'legacy_missing_done',
                  trace_id=usage.get('trace_id', ''))
        logger.warning('[Dispatch] Unusable provider stream on %s:%s '
                       '(state=%s) — '
                       'recorded soft failure (consecutive_errors=%d)',
                       slot.key_name, slot.model,
                       stream_state or 'legacy_missing_done',
                       slot.consecutive_errors)
    except Exception as e:
        logger.warning('[Dispatch] _cool_slot_on_premature_close failed '
                       'for %s:%s: %s', slot.key_name, slot.model, e)


# ═══════════════════════════════════════════════════════════
#  Config-surface audit trail (CLAUDE.md §10)
# ═══════════════════════════════════════════════════════════

_AUDITED_SEVERITY_DOWNGRADE = False


def _audit_severity_downgrade() -> None:
    """Emit a one-time audit_log entry for the 2026-05-05 per-cycle 429
    severity downgrade (WARNING→INFO). Only logged once per process.

    This keeps CLAUDE.md §10 ("config changes must leave a trace") happy
    without flooding audit.log with the same entry on every dispatch.
    """
    global _AUDITED_SEVERITY_DOWNGRADE
    if _AUDITED_SEVERITY_DOWNGRADE:
        return
    _AUDITED_SEVERITY_DOWNGRADE = True
    try:
        from lib.log import audit_log
        audit_log('config_change',
                  param='dispatch_429_severity',
                  old='warning',
                  new='info',
                  reason='log-noise audit 2026-05-05 — per-cycle 429 is '
                         'routine backpressure, rotation handled by '
                         'dispatcher; only final key exhaustion is warning',
                  approved_by='plan')
    except Exception as e:
        logger.debug('[Dispatch] audit_log unavailable: %s', e)



__all__ = [
    'pick_key_for_model',
    'dispatch_chat',
    'dispatch_stream',
    'async_dispatch_stream',
    'dispatch_fastest',
    'dispatch_parallel',
    'get_dispatch_status',
    '_group_by_capability',
    'smart_chat',
    'smart_chat_batch',
]


# ═══════════════════════════════════════════════════════════
#  Key rotation convenience
# ═══════════════════════════════════════════════════════════

def pick_key_for_model(model: str) -> tuple:
    """Public convenience: pick the best API key for *model*.

    Returns (api_key, key_name, slot_or_None).
    Callers who already choose their own model (orchestrator, route handlers)
    use this to **spread RPM across keys** instead of always using the same key.

    Example::

        from lib.llm_dispatch import pick_key_for_model
        api_key, key_name, slot = pick_key_for_model(model)
        body = build_body(model, messages, ...)
        result = stream_chat(body, ..., api_key=api_key)
        if slot:
            slot.record_request(success=True, latency_ms=..., ttft_ms=...)
    """
    return get_dispatcher().pick_key_for_model(model)


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_chat (non-streaming)
# ═══════════════════════════════════════════════════════════

def dispatch_chat(messages, *, max_tokens=4096, temperature=0,
                  thinking_enabled=False, preset='low', effort=None,
                  capability='text', prefer_model=None, tools=None,
                  extra=None, max_retries=3, log_prefix='',
                  timeout=None, strict_model=False,
                  exclude_models=None, abort_check=None):
    """Smart dispatch: pick the best available slot and send a non-streaming chat.

    Auto-retries on failure with fallback to different slots.

    Args:
        messages: List of chat messages
        max_tokens: Max output tokens
        temperature: Sampling temperature
        thinking_enabled: Enable extended thinking
        preset/effort: Thinking effort level
        capability: Required capability ('text', 'vision', 'thinking', 'cheap')
        prefer_model: Preferred model name
        tools: Tool definitions for function calling
        extra: Extra body parameters
        max_retries: Number of slots to try before giving up

    Returns:
        (content_text: str, usage_dict: dict)
    """
    from lib.llm import AbortedError, BadRequestError, ContentFilterError, InvalidImageError, PermissionError_, PromptTooLongError, RateLimitError, StreamOnlyError, chat
    from lib.llm._transport import abortable_sleep
    from lib.llm_errors import EndpointUnreachableError, RequestScopedError

    # 2026-05-05 config-surface change (CLAUDE.md §10): per-cycle 429
    # severity downgraded from WARNING → INFO (routine backpressure; the
    # dispatch handler already rotates to the next key). Only final key
    # exhaustion stays WARNING/ERROR. Audited here on first call so the
    # change is discoverable in audit.log without a code-search.
    _audit_severity_downgrade()
    dispatcher = get_dispatcher()
    exclude = set(exclude_models) if exclude_models else set()
    exclude_keys = set()      # keys to exclude entirely (transient classes)
    exclude_pairs = set()     # (key_name, model) pairs to exclude (transient classes)
    # Durable counterparts (permission/quota) — survive the 60s reset;
    # see _StreamRetryState.exclude_*_durable for the rationale.
    exclude_keys_durable = set()
    exclude_pairs_durable = set()
    last_err = None
    # Fruit 2 (E2): at most ONE forced OAuth token refresh-retry per
    # request — a 401 on a subscription slot whose token was revoked/
    # refreshed elsewhere gets one refresh + one retry before normal
    # failover applies.
    _oauth_refresh_used = False

    # Pre-exclude stream-only models from non-streaming dispatch.
    #   Models like qwq-plus only support stream=True and will reject
    #   non-streaming requests with HTTP 400.
    for slot in dispatcher.slots:
        if slot.stream_only and slot.model not in exclude:
            exclude.add(slot.model)
            logger.debug('%s Excluding stream-only model %s from non-streaming dispatch',
                        log_prefix, slot.model)

    # Caller exclusions AND protocol-required stream-only exclusions are
    # permanent for this dispatch call. Snapshot only after both sources have
    # landed so attempt one and 429 cycling can never re-introduce them.
    _initial_exclude_models = set(exclude)

    # NO total time budget. A non-streaming call is waited out for as long
    #   as the upstream needs — truncating a translate/summarize call at a
    #   deadline produced silently incomplete content, which is worse than
    #   waiting. ``timeout`` is honored when a caller explicitly passes one;
    #   otherwise None = no read timeout (connect phase stays bounded, so a
    #   dead host still fails over). Retries are bounded by max_retries, and
    #   an abort is honored by the transport's abort poll.
    _per_attempt_timeout = timeout

    # hard_attempts counts only non-429 failures; 429 loops forever.
    hard_attempts = 0
    _429_count = 0
    # Keep the historical aggregate for compatibility, but split it for
    # diagnostics: a locally unavailable/cooling slot is not evidence that an
    # upstream server returned HTTP 429.
    _slot_wait_cycles = 0
    _upstream_429_retries = 0
    _queue_wait_ms = 0.0

    def _queue_sleep(seconds: float) -> None:
        nonlocal _queue_wait_ms
        wait_started = time.monotonic()
        if abort_check is None:
            time.sleep(seconds)
        else:
            abortable_sleep(seconds, abort_check)
        _queue_wait_ms += max(
            0.0, (time.monotonic() - wait_started) * 1000)
    _last_exclusion_reset = time.monotonic()  # track when we last reset hard-error exclusions
    _EXCLUSION_RESET_INTERVAL = 60  # reset exclude_pairs every 60s during 429 cycling
    # 429-saturation clock — set on the first genuine-429 starvation signal.
    # Bounded escalation is disabled by default; a positive
    # TOFU_429_SATURATION_SECS opts into model fallback.
    _sat_start = None

    while hard_attempts < max_retries:
        if abort_check and abort_check():
            raise AbortedError('Aborted during non-stream dispatch retry')
        total_attempts = hard_attempts + _429_count

        # Bounded 429-saturation escalation (mirror of dispatch_stream).
        _sat_budget = _saturation_budget_secs()
        if _sat_budget > 0 and _sat_start is not None \
                and (time.monotonic() - _sat_start) > _sat_budget:
            _saturation_escalate(
                log_prefix, 'dispatch_chat',
                elapsed_s=time.monotonic() - _sat_start,
                budget_s=_sat_budget, cycles=_429_count,
                model=prefer_model)

        # Periodically reset hard-error exclusions during 429 cycling.
        #   502/timeout errors may be transient (gateway restart), but
        #   exclude_pairs is permanent per dispatch call.  After 60s,
        #   give excluded slots another chance.
        if _429_count > 0 and (time.monotonic() - _last_exclusion_reset) >= _EXCLUSION_RESET_INTERVAL:
            if exclude_pairs or exclude_keys:
                logger.info(
                    '%s dispatch_chat: resetting hard-error exclusions '
                    'after %ds of 429 cycling (cycle #%d) — '
                    'exclude_keys=%s exclude_pairs=%s',
                    log_prefix, _EXCLUSION_RESET_INTERVAL, _429_count,
                    exclude_keys, exclude_pairs)
                exclude_keys.clear()
                exclude_pairs.clear()
                # Note: dispatch_chat does NOT clear `exclude` here — that
                # set tracks model-level hard errors, which dispatch_chat
                # accumulates across attempts. Caller-provided
                # exclude_models is also preserved by virtue of being
                # added to `exclude` at construction time.
            _last_exclusion_reset = time.monotonic()

        # Caller-provided exclude_models must apply on attempt 1 too
        # (failure-driven exclusions only kick in after total_attempts>0).
        _eff_exclude = exclude if exclude else None
        slot = dispatcher.pick_and_reserve(
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=_eff_exclude,
            exclude_keys=(exclude_keys | exclude_keys_durable
                          if total_attempts > 0 else None),
            exclude_pairs=(exclude_pairs | exclude_pairs_durable
                           if total_attempts > 0 else None),
            strict_model=strict_model)
        if slot is None:
            # All slots in cooldown / excluded — wait briefly and retry.
            # Keep fast-polling when capable slots EXIST but are merely
            #   cooling (429-equivalent), even if THIS call hasn't caught a
            #   raw 429 yet — otherwise fresh concurrent requests bail on
            #   attempt 1 under heavy contention. Give up only when no
            #   capable slot exists at all. (Mirror of dispatch_stream.)
            _slots_exist = dispatcher.has_capable_slots(
                capability, exclude_models=exclude,
                exclude_keys=exclude_keys | exclude_keys_durable,
                exclude_pairs=exclude_pairs | exclude_pairs_durable,
                prefer_model=prefer_model if strict_model else None)
            if _slots_exist or _cycling_can_ever_serve(
                    dispatcher, capability,
                    initial_exclude_models=_initial_exclude_models,
                    durable_keys=exclude_keys_durable,
                    durable_pairs=exclude_pairs_durable,
                    strict_model=strict_model, prefer_model=prefer_model):
                _queue_sleep(0.3)
                _429_count += 1
                _slot_wait_cycles += 1
                if _sat_start is None:
                    _sat_start = time.monotonic()
                if _429_count % 20 == 0:
                    logger.info(
                        '%s dispatch_chat: still cycling (slots cooling, %d times), '
                        'waiting for cooldown to expire…',
                        log_prefix, _429_count)
                continue
            break

        tag = f'{log_prefix}[D:{slot.key_name}:{slot.model}]'
        try:
            _wait_for_shared_contention_admission(
                dispatcher, slot, _queue_sleep, tag)
        except AbortedError:
            slot.release()
            logger.debug('%s User aborted during contention admission', tag)
            raise
        t0 = time.time()

        try:
            # Merge tools into extra dict (chat() doesn't have a tools param)
            _extra = dict(extra) if extra else {}
            if tools:
                _extra['tools'] = tools

            # No budget clamp: pass the caller's timeout through unchanged
            # (None = no read timeout).
            _timeout = _per_attempt_timeout

            content, usage = chat(
                model=slot.model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                thinking_enabled=thinking_enabled,
                effort=effort or preset,
                api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                extra=_extra or None,
                log_prefix=tag,
                max_retries=0,  # fail fast — dispatcher handles retries
                timeout=_timeout,
                thinking_format=slot.thinking_format or '',
                provider_id=slot.provider_id or '',
                api_protocol=slot.protocol or 'openai',
                responses_feature_profile=(
                    getattr(slot, 'responses_profile', '') or 'compatible'),
                oauth=slot.oauth or '',
                adapter=slot.adapter or None,
            )
            latency = (time.time() - t0) * 1000
            _out_tokens = 0
            if isinstance(usage, dict):
                _out_tokens = (usage.get('completion_tokens')
                               or usage.get('output_tokens') or 0)
                try:
                    _out_tokens = int(_out_tokens)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[api] dispatch_chat caught %s: %s', type(_e_audit).__name__, _e_audit)
                    _out_tokens = 0
            slot.record_success(latency, output_tokens=_out_tokens)
            _note_shared_contention_recovered(dispatcher, slot, log_prefix)
            # Inject dispatch metadata so callers know which slot served this
            if isinstance(usage, dict):
                completed_at_unix_ns = _unix_time_ns()
                usage['_dispatch'] = {
                    'key': slot.key_name, 'model': slot.model,
                    'key_tail': (slot.api_key or '')[-4:],
                    'provider_id': slot.provider_id,
                    'protocol': slot.protocol or 'openai',
                    'responses_profile': getattr(
                        slot, 'responses_profile', '') or '',
                    'latency_ms': round(latency),
                    # Non-streaming chat cannot expose provider first-byte
                    # timing.  Record response completion as a conservative
                    # first-output bound so formal TTFT never favors Tofu.
                    'ttft_ms': round(latency, 3),
                    'stream_started_at_unix_ns': (
                        completed_at_unix_ns - round(latency * 1_000_000)),
                    'first_content_at_unix_ns': completed_at_unix_ns,
                    'stream_completed_at_unix_ns': completed_at_unix_ns,
                    'ttft_measurement': 'nonstream_response_complete_upper_bound',
                    'queue_wait_ms': round(_queue_wait_ms, 3),
                    'queue_wait_measurement': 'dispatcher_backpressure_only',
                    'attempt': hard_attempts + 1,
                    '429_retries': _429_count,
                    'slot_wait_cycles': _slot_wait_cycles,
                    'upstream_429_retries': _upstream_429_retries,
                }
            return content, usage

        except RateLimitError as e:
            _is_quota = bool(getattr(e, 'is_quota', False))
            _is_gateway = bool(getattr(e, 'is_gateway', False))
            _is_contention = bool(getattr(e, 'is_shared_contention', False))
            # HTTP 402 (account credit dead) → key-wide stop; 429-quota →
            # per-model (see Slot.record_error is_account_quota).
            _is_account_quota = bool(
                _is_quota
                and int(getattr(e, 'status_code', 0) or 0) == 402)
            _err_str = str(e)[:200]
            # Subscription-quota timed hold: the upstream named the reset
            #   time (resets_at/resets_in_seconds) — park the slot for that
            #   explicit duration instead of the 0.5s steering nudge.
            slot.record_error(is_rate_limit=True,
                              is_quota_exhausted=_is_quota,
                              is_account_quota=_is_account_quota,
                              is_gateway=_is_gateway,
                              is_shared_contention=_is_contention,
                              cooldown_s=getattr(e, 'retry_after_s', None),
                              error=_err_str if _is_quota else '')
            last_err = e
            _retry_delay_s = _shared_contention_retry_delay(
                dispatcher, slot, _is_contention, log_prefix)
            if _is_quota:
                # Persistent billing/quota exhaustion (HTTP 402 or
                #   429+insufficient_quota). Retrying on this key all day
                #   is pointless — exclude the entire key and move on.
                #   Durable: must survive the 60s 429-cycling reset.
                exclude_keys_durable.add(slot.key_name)
                hard_attempts += 1
                logger.warning(
                    '%s Quota exhausted on %s:%s — disabling key for '
                    'today: %s',
                    log_prefix, slot.key_name, slot.model, _err_str)
                continue
            _429_count += 1
            _upstream_429_retries += 1
            if _sat_start is None and not _is_gateway:
                _sat_start = time.monotonic()
            # Don't exclude anything — slot.record_error() sets a 0.5s
            #   cooldown which naturally steers pick_and_reserve to another
            #   slot.  After cooldown the slot is eligible again.
            # Generic 429s keep the 0.3s baseline. Typed project contention
            # consumes the provider/model probe reservation computed above.
            _err_body = str(e)[:300]
            # 2026-05-05 noise-reduction: per-cycle 429 is ROUTINE backpressure
              # (handler already rotates to the next key). Log at INFO, not
              # WARNING — only the final exhaustion path (all keys excluded)
              # remains WARNING/ERROR. See CLAUDE.md §10 / audit_log below.
            if _429_count <= 3 or _429_count % 100 == 0:
                # First few + every 100th kept informative (include body).
                # Everything between is DEBUG: even coordinated contention can
                # persist indefinitely, so per-cycle INFO remains unbounded.
                logger.info(
                    '%s 429 rate-limited on %s:%s (cycle #%d) — body: %s',
                    log_prefix, slot.key_name, slot.model, _429_count, _err_body)
            else:
                logger.debug(
                    '%s 429 rate-limited on %s:%s (cycle #%d)',
                    log_prefix, slot.key_name, slot.model, _429_count)
            if _retry_delay_s > 0:
                _queue_sleep(_retry_delay_s)
            # Don't increment hard_attempts — 429 retries are free
            continue

        except PermissionError_ as e:
            # OAuth-subscription slot 401: the stored token was revoked/
            #   refreshed elsewhere (resolve_oauth_request only refreshes
            #   near expiry). Force ONE refresh + ONE retry before the
            #   normal failover below applies. Scoped to oauth slots —
            #   plain API-key slots keep the pair-exclusion path.
            if slot.oauth and not _oauth_refresh_used:
                _oauth_refresh_used = True
                if _force_oauth_token_refresh(slot.oauth, log_prefix):
                    slot.release()  # refresh-retry — not a slot-health signal
                    logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) '
                                   '— token refreshed, retrying once',
                                   log_prefix, slot.key_name, slot.model,
                                   slot.oauth)
                    continue
                logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) — '
                               'forced refresh failed; normal failover',
                               log_prefix, slot.key_name, slot.model, slot.oauth)
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            last_err = e
            # Durable — an auth rejection cannot heal inside this call.
            exclude_pairs_durable.add((slot.key_name, slot.model))
            hard_attempts += 1
            # If ALL models for this key have been excluded (all got 401),
            #   exclude the entire key to avoid further wasted attempts.
            _key_pairs = {(kn, m) for kn, m in exclude_pairs_durable
                          if kn == slot.key_name}
            _key_models = {s.model for s in dispatcher.slots
                           if s.key_name == slot.key_name
                           and (not capability or capability in s.capabilities)}
            if _key_models and _key_models <= {m for _, m in _key_pairs}:
                exclude_keys_durable.add(slot.key_name)
                logger.warning(
                    '%s Permission denied on ALL models for key %s — '
                    'excluding entire key',
                    log_prefix, slot.key_name)
            else:
                logger.warning(
                    '%s Permission denied on %s:%s — excluding pair, '
                    'remaining slots: %s',
                    log_prefix, slot.key_name, slot.model,
                    dispatcher.summarize_slots(capability))

        except EndpointUnreachableError as e:
            # Endpoint host down (connect-phase failure). Cool the slot,
            #   exclude this (key, model) pair, and fail over to another
            #   slot — same handling as dispatch_stream.
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            slot.cooldown_until = time.time() + _UNREACHABLE_COOLDOWN
            slot.cooldown_reason = 'upstream'
            last_err = e
            exclude_pairs.add((slot.key_name, slot.model))
            hard_attempts += 1
            logger.warning(
                '%s Endpoint unreachable on %s:%s (%s) — cooled %ds + '
                'excluded pair, failing over: %s',
                log_prefix, slot.key_name, slot.model,
                getattr(e, 'base_url', '') or '?', _UNREACHABLE_COOLDOWN,
                str(e)[:160])

        except ContentFilterError as e:
            # HTTP 450 — content policy violation. No point retrying with
            # different model/key since the same content will be blocked.
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Content filter (HTTP 450) — not retrying: %s', tag, str(e)[:200], exc_info=True)
            raise

        except PromptTooLongError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Prompt/request too large — not retrying on other slots '
                           '(same payload = same rejection)', tag)
            raise   # Escape to orchestrator for reactive compaction

        except InvalidImageError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Image content error — not retrying on other slots '
                           '(same image = same rejection)', tag)
            raise   # Same payload = same rejection on all keys

        except StreamOnlyError as e:
            # Model only supports streaming — exclude entire model and
            #   try a different one. Mark the slot so future dispatches
            #   don't repeat this mistake.
            slot.stream_only = True
            slot.record_error(is_rate_limit=False)
            exclude.add(slot.model)
            last_err = e
            hard_attempts += 1
            logger.warning('%s Model %s only supports streaming — excluding '
                          'from non-streaming dispatch, trying next model',
                          log_prefix, slot.model)

        except RequestScopedError as e:
            # Request-scoped 4xx (404/422) — THIS request's semantics, not
            # slot/model health (CLIProxyAPI isRequestScopedResultError).
            # Surface untouched: release the inflight reservation, NO
            # cooldown, NO key_stats feed, NO fallback consumption.
            slot.release()
            logger.warning('%s Request-scoped error (HTTP %s) on %s:%s — '
                           'surfacing to caller: %.300s',
                           log_prefix, getattr(e, 'status_code', 0) or '?',
                           slot.key_name, slot.model, str(e))
            raise

        except BadRequestError as e:
            # Deterministic HTTP 400 — a PAYLOAD-level rejection, not slot
            # health. Release the slot (no consecutive_errors → no 300s
            # lockout, no key_stats feed — the ContentFilterError /
            # InvalidImageError precedent) and exclude only the PAIR: a 400
            # CAN be key-specific, so the remaining keys each get one try;
            # exhaustion falls through to the turn-level model fallback.
            slot.release()
            last_err = e
            exclude_pairs.add((slot.key_name, slot.model))
            hard_attempts += 1
            logger.warning('%s Bad request (HTTP 400, deterministic) on %s:%s '
                           '— released slot, excluded pair, trying next: %.500s',
                           log_prefix, slot.key_name, slot.model, str(e))

        except Exception as e:
            # Subscription-quota/capacity signal arriving via a non-429
            #   wrapper (SSE RetryableAPIError for "selected model is at
            #   capacity", generic SSE error for usage_limit_reached).
            #   Treat as rate-limit class with its parsed reset duration —
            #   never as slot-health damage.
            if is_subscription_quota_error(str(e)):
                _ra = parse_subscription_retry_after(str(e))
                slot.record_error(is_rate_limit=True, cooldown_s=_ra,
                                  error=str(e)[:200])
                last_err = e
                _429_count += 1
                _upstream_429_retries += 1
                logger.warning('%s Subscription quota/capacity signal on %s:%s '
                               '(cooldown %ss) — rotating slot: %s',
                               log_prefix, slot.key_name, slot.model,
                               f'{_ra:.0f}' if _ra else '0.5', str(e)[:160])
                _queue_sleep(0.3)
                continue
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False)
            last_err = e
            # Timeout errors → exclude only this (key, model) pair,
            # not the entire model — other backends for different models
            # may still be fast.  True model-level failures (4xx, etc.)
            # still exclude the whole model.
            _is_timeout = 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()
            if _is_timeout:
                exclude_pairs.add((slot.key_name, slot.model))
                logger.debug('%s Timeout (%.0fms) — excluding pair '
                             '%s:%s, trying next slot', tag, latency, slot.key_name, slot.model, exc_info=True)
            elif strict_model:
                # strict_model: only exclude pair, keep other keys
                exclude_pairs.add((slot.key_name, slot.model))
                logger.debug('%s Error (strict_model): %s — excluding pair '
                             '%s:%s, trying other keys', tag, str(e)[:200],
                             slot.key_name, slot.model, exc_info=True)
            else:
                exclude.add(slot.model)
                logger.debug('%s Error: %s — trying next slot', tag, str(e)[:200], exc_info=True)
            hard_attempts += 1

    _raise_dispatch_exhausted(last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              what='dispatch')


# ═══════════════════════════════════════════════════════════
#  Helpers — pre-built body adaptation for model swap
# ═══════════════════════════════════════════════════════════

def _readjust_thinking_params(body: dict, new_model: str, thinking_format: str):
    """Fix thinking parameters when a pre-built body is dispatched to a different model family.

    Different model families use incompatible thinking parameter formats:
      - Claude:   thinking.type = 'adaptive'
      - Doubao:   thinking.type = 'enabled' / 'disabled'
      - GLM:      thinking.type = 'enabled' / 'disabled'; 5.2+ adds
                  reasoning_effort (+ clear_thinking when history carries
                  reasoning_content); 5.3 is forced-thinking — 'disabled'
                  degrades to reasoning_effort='low'
      - MiniMax:  reasoning_split = True
      - Qwen/LongCat: enable_thinking = True/False
      - Gemini 3.x: reasoning_effort = 'minimal'/'low'/'medium'/'high'
      - Kimi K3:    reasoning_effort = 'low'/'high'/'max', NO temperature
      - Kimi K2:    thinking.type = 'enabled'/'disabled'

    When dispatch swaps the model (e.g. Claude → Doubao), the body may carry
    the wrong format, causing HTTP 400 errors. This function detects mismatches
    and converts to the correct format for the new model.
    """
    from lib.llm import (
        is_claude, is_doubao, is_gemini, is_glm, is_gpt5, is_kimi,
        is_kimi_k3, is_longcat, is_minimax, is_qwen,
    )

    # Detect current thinking state from the body
    thinking_dict = body.get('thinking')
    enable_thinking = body.get('enable_thinking')
    reasoning_split = body.get('reasoning_split')
    reasoning_effort = body.get('reasoning_effort')
    effort = body.get('effort')

    has_thinking_params = (thinking_dict is not None or
                           enable_thinking is not None or
                           reasoning_split is not None or
                           reasoning_effort is not None)
    if not has_thinking_params:
        return  # No thinking params to adjust

    # Determine if thinking is currently enabled
    is_enabled = False
    if isinstance(thinking_dict, dict):
        t = thinking_dict.get('type', '')
        is_enabled = t in ('enabled', 'adaptive')
    elif enable_thinking is not None:
        is_enabled = bool(enable_thinking)
    elif reasoning_split is not None:
        is_enabled = bool(reasoning_split)
    elif reasoning_effort is not None:
        # Gemini dialect: 'minimal' is the closest thing to "thinking off".
        is_enabled = reasoning_effort != 'minimal'

    # Carry the effort across the swap if no explicit effort was set — also
    # when thinking_dict/enable_thinking declared the state: GLM bodies carry
    # the rung in top-level reasoning_effort, and without this a GLM→GLM
    # slot swap (e.g. 5.2 slot 503 → 5.3 slot) would silently reset it to
    # the model default.
    if not effort and reasoning_effort is not None:
        effort = reasoning_effort

    # Clean ALL thinking-related keys before re-setting
    body.pop('thinking', None)
    body.pop('enable_thinking', None)
    body.pop('reasoning_split', None)
    body.pop('reasoning_effort', None)
    body.pop('effort', None)

    # Re-apply for the new model using the same logic as build_body
    _tf = thinking_format
    # ── Plugin dialect (tofu.providers) first; built-ins fall through ──
    _plugin_dialect = None
    if _tf:
        from lib.llm_dispatch.provider_registry import get_dialect
        _plugin_dialect = get_dialect(_tf)
    if _plugin_dialect is None and _tf == 'enable_thinking':
        # Lockstep with build_body(): GLM-5.2+ has a native thinking
        # contract; a provider-level legacy enable_thinking format must not
        # claim it (dead field on GLM-native gateways — see the build_body
        # branch for the live evidence and the engine-vs-family rationale).
        from lib.model_info import glm_line_version
        _gv = glm_line_version(new_model)
        if _gv is not None and _gv >= (5, 2):
            _tf = ''
    if _plugin_dialect is not None:
        _readjust = _plugin_dialect.apply_readjust
        try:
            if _readjust is not None:
                _readjust(body, is_enabled=is_enabled, model=new_model,
                          effort=effort)
            else:
                _plugin_dialect.apply_build(
                    body, thinking_enabled=is_enabled,
                    temperature=body.get('temperature'), model=new_model,
                    effort=effort)
        except Exception as e:
            logger.error('[_readjust_thinking_params] plugin dialect %r failed: '
                         '%s', _tf, e, exc_info=True)
    elif _tf == 'none':
        # Engine accepts no thinking flag — leave the body without one.
        # We already popped enable_thinking / reasoning_split / thinking
        # above, so this branch is intentionally a no-op.
        pass
    elif _tf == 'chat_template_kwargs':
        # sglang / vLLM dual-mode: thinking is gated through Jinja
        # template, exposed as ``chat_template_kwargs.enable_thinking``.
        # Top-level ``enable_thinking`` would be silently ignored.
        kw = body.get('chat_template_kwargs')
        if not isinstance(kw, dict):
            kw = {}
        kw['enable_thinking'] = bool(is_enabled)
        body['chat_template_kwargs'] = kw
    elif not _tf and is_gpt5(new_model):
        from lib.llm import gpt_reasoning_effort
        body['reasoning_effort'] = gpt_reasoning_effort(effort, is_enabled, new_model)
    elif _tf == 'reasoning_effort' or (not _tf and is_gemini(new_model)):
        from lib.llm import gemini_reasoning_effort
        body['reasoning_effort'] = gemini_reasoning_effort(effort, is_enabled)
    elif _tf == 'enable_thinking' or (not _tf and (is_longcat(new_model) or is_qwen(new_model))):
        body['enable_thinking'] = is_enabled
    elif _tf == 'thinking_type' or (not _tf and is_doubao(new_model)):
        body['thinking'] = {'type': 'enabled' if is_enabled else 'disabled'}
    elif not _tf and is_kimi(new_model):
        if is_kimi_k3(new_model):
            # K3: top-level reasoning_effort only; temperature stripped below.
            from lib.llm import kimi_k3_reasoning_effort
            body['reasoning_effort'] = kimi_k3_reasoning_effort(
                effort, is_enabled)
        else:
            body['thinking'] = {'type': 'enabled' if is_enabled else 'disabled'}
            body['temperature'] = 1.0 if is_enabled else 0.6
    elif not _tf and is_glm(new_model):
        # Kept in lockstep with the GLM branch in build_body() — GLM-5.3 is
        # forced-thinking (see that branch for the contract).
        from lib.llm import glm_reasoning_effort
        from lib.model_info import glm_line_version
        _glm_v = glm_line_version(new_model)
        _glm_forced = _glm_v is not None and _glm_v >= (5, 3)
        if is_enabled or _glm_forced:
            body['thinking'] = {'type': 'enabled'}
            body['temperature'] = 1.0
            if _glm_v is not None and _glm_v >= (5, 2):
                body['reasoning_effort'] = glm_reasoning_effort(
                    effort, is_enabled, new_model)
                if any(m.get('reasoning_content')
                       for m in (body.get('messages') or [])
                       if isinstance(m, dict) and m.get('role') == 'assistant'):
                    body['thinking']['clear_thinking'] = False
        else:
            body['thinking'] = {'type': 'disabled'}
            body['temperature'] = max(body.get('temperature', 0.7), 0.01)
    elif not _tf and is_minimax(new_model):
        if is_enabled:
            body['reasoning_split'] = True
    elif not _tf and is_claude(new_model):
        from lib.llm import is_claude_opus_47
        if is_enabled:
            # Keep this in sync with the Claude branch in build_body().
            #   • 4.6 and earlier: adaptive + temperature=1.0
            #   • 4.7+: adaptive + display='summarized' (required to see
            #           reasoning trace), and NO temperature (ignored today,
            #           may be rejected in a future revision).
            body['thinking'] = {'type': 'adaptive'}
            if is_claude_opus_47(new_model):
                body['thinking']['display'] = 'summarized'
            else:
                body['temperature'] = 1.0
            # Effort rung -- kept in lockstep with build_body(). The adaptive
            # generation states EVERY rung (its default is `high`, so an
            # omitted `medium` would be a silent upgrade); pre-4.7 Claude
            # still defaults to medium and keeps the omit wire. The two paths
            # diverged on exactly this rung before
            # tests/test_claude_effort_rung_parity.py pinned them together.
            _omit_medium = not is_claude_opus_47(new_model)
            if effort and not (_omit_medium and effort == 'medium'):
                # xhigh is Opus 4.7-only; downgrade on older Claude to avoid HTTP 400.
                if effort == 'xhigh' and not is_claude_opus_47(new_model):
                    effort = 'high'
                elif effort == 'ultra':
                    # Tofu's legacy 'ultra' label maps to Claude's top rung.
                    effort = 'max'
                body['effort'] = effort
        elif is_claude_opus_47(new_model):
            # Thinking OFF must be STATED on the adaptive generation — Opus 5
            # defaults adaptive thinking ON, so the popped-key state above
            # would silently re-enable it after a swap (live-measured ~1.93x
            # completion tokens; see the build_body branch for the numbers).
            # No effort is set: disabled + xhigh/max is HTTP 400.
            body['thinking'] = {'type': 'disabled'}
    # else: standard OpenAI-compatible — no thinking params needed

    # ── Claude Opus 4.7+ rejects sampling params (HTTP 400) ──
    # Strip unconditionally after re-setting, since non-4.7 branches above
    # may have assigned temperature=1.0 before a potential model swap.
    from lib.llm import is_claude_opus_47
    if is_claude_opus_47(new_model):
        for _k in ('temperature', 'top_p', 'top_k'):
            body.pop(_k, None)

    # ── Kimi K3 fixes temperature=1.0 — any other value is HTTP 400 ──
    # Strip unconditionally: a body swapped in from another family (e.g.
    # Qwen's temperature=0.7) would otherwise carry a rejected value.
    if is_kimi_k3(new_model):
        for _k in ('temperature', 'top_p', 'presence_penalty',
                   'frequency_penalty'):
            body.pop(_k, None)

    # Observability: which dialect actually landed on the wire? Debug
    # level so production logs stay quiet; flip the logger when
    # diagnosing dialect mismatches across slot swaps.
    logger.debug(
        '_readjust_thinking_params: model=%s thinking_format=%r '
        'is_enabled=%s has_chat_template_kwargs=%s has_enable_thinking=%s '
        'has_thinking=%s', new_model, thinking_format or '(auto)',
        is_enabled,
        'chat_template_kwargs' in body, 'enable_thinking' in body,
        'thinking' in body,
    )


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_stream (streaming)
# ═══════════════════════════════════════════════════════════

def _adapt_stream_body_for_slot(slot, body_or_messages, is_body, *,
                                tools, max_tokens, temperature,
                                thinking_enabled, preset, effort):
    """Build/adapt the request body for a specific dispatched slot.

    Pure CPU (no I/O) — shared by the sync ``dispatch_stream`` loop and the
    native-async ``async_dispatch_stream`` loop so the provider-specific body
    quirks (max_tokens re-clamp, thinking-param readjust, Claude prefill
    guard, Gemini thought-signature injection) live in ONE place.

    Returns the body dict ready to hand to ``stream_chat`` / ``async_stream_chat``.
    """
    from lib.llm import build_body
    if is_body:
        body = dict(body_or_messages)
        body['model'] = slot.model
        # dict() is a SHALLOW copy — body['messages'] would alias the caller's
        #   task-shared list, so the model-specific IN-PLACE rewrites below
        #   (_strip_trailing_assistant_for_claude / _downscale_oversized_images /
        #   _inject_gemini_thought_signatures) would MUTATE that shared list.
        #   On a 503/429 slot-swap retry the loop re-adapts the SAME shared body
        #   for a different slot: a Gemini attempt bakes extra_content.google.
        #   thought_signature into a prefix tool_call, then the recovered Claude
        #   attempt re-serializes the now-polluted prefix → different wire bytes
        #   → prompt-cache key shift → full prefix miss (conv mrne3bqe R4/R5,
        #   which recovered — "rebound" — once a clean round re-formed the prefix).
        #   Deep-copy the messages so each per-slot adaptation is idempotent and
        #   never leaks a model-specific rewrite back onto the caller / next slot.
        if isinstance(body.get('messages'), list):
            body['messages'] = copy.deepcopy(body['messages'])
        if tools is not None:
            body['tools'] = tools
        if 'max_tokens' in body:
            from lib.llm import _clamp_max_tokens
            from lib.llm.body import _clamp_completion_to_context_window
            body['max_tokens'] = _clamp_max_tokens(slot.model, body['max_tokens'])
            body['max_tokens'] = _clamp_completion_to_context_window(
                slot.model, body.get('messages'), body['max_tokens'],
                provider_id=slot.provider_id or '')
        _readjust_thinking_params(body, slot.model, slot.thinking_format or '')
        from lib.llm import _downscale_oversized_images, _strip_trailing_assistant_for_claude, is_claude
        from lib.llm.body import _validate_image_blocks
        if is_claude(slot.model) and body.get('messages'):
            _strip_trailing_assistant_for_claude(body['messages'], slot.model)
            # Reconcile any mislabeled image data-URI media type BEFORE the
            # downscale pass. On this pre-built-body swap path (dispatch
            # swapped the model onto Claude), build_body's _validate_image_blocks
            # never ran, so a stored data:image/jpeg URI holding PNG bytes would
            # reach the strict Anthropic Messages API and 400 the turn. Run it
            # here so the swap path has the same self-consistency guarantee as
            # the fresh-build path.
            _validate_image_blocks(body['messages'])
            _downscale_oversized_images(body['messages'], slot.model)
        from lib.llm.body import _inject_gemini_thought_signatures
        from lib.model_info import is_gemini as _is_gemini
        if _is_gemini(slot.model) and body.get('messages'):
            _inject_gemini_thought_signatures(body['messages'], slot.model)
        if (slot.protocol or '') == 'responses':
            body['_responses_feature_profile'] = (
                getattr(slot, 'responses_profile', '') or 'compatible')
        return body
    body = build_body(
        slot.model, body_or_messages,
        max_tokens=max_tokens, temperature=temperature,
        thinking_enabled=thinking_enabled, preset=effort or preset,
        tools=tools, stream=True,
        thinking_format=slot.thinking_format or '',
        provider_id=slot.provider_id or '',
    )
    if (slot.protocol or '') == 'responses':
        body['_responses_feature_profile'] = (
            getattr(slot, 'responses_profile', '') or 'compatible')
    return body


def _settle_stream_result(slot, usage, *, latency, ttft, state,
                          cache_conv_id, tag, dispatcher=None,
                          stream_result=None):
    """Settle one provider stream result exactly once for both dispatch loops.

    The sync ``dispatch_stream`` and async ``async_dispatch_stream`` loops
    differ in their transport call + the production-only pre-send gates the
    sync path carries (cgroup headroom, warm-key hold, big-prefix admission),
    so the LOOPS are intentionally NOT unified. Once a slot answers, its typed
    result selects exactly one mutually exclusive health action: verified
    completion records success, client abort releases neutrally, and every
    invalid stream records one provider/slot error. Both loops then stamp the
    same ``usage['_dispatch']`` metadata and cache-settle timestamp here.

    Mutates ``usage`` in place (stamps ``_dispatch``) and returns nothing.
    """
    _out_tokens = 0
    if isinstance(usage, dict):
        _out_tokens = (usage.get('completion_tokens')
                       or usage.get('output_tokens') or 0)
        try:
            _out_tokens = int(_out_tokens)
        except (ValueError, TypeError) as _e_audit:
            logger.debug('[api] _settle_stream_result caught %s: %s',
                         type(_e_audit).__name__, _e_audit)
            _out_tokens = 0
    raw_stream_state = ''
    if stream_result is not None:
        raw_stream_state = str(
            getattr(getattr(stream_result, 'state', None), 'value', '') or '')
    if not raw_stream_state and isinstance(usage, dict):
        raw_stream_state = str(usage.get('_stream_state') or '')
    verified_complete = raw_stream_state in {'', 'provider_finished'}
    client_aborted = raw_stream_state == 'client_aborted'
    if verified_complete:
        slot.record_success(
            latency, ttft_ms=ttft, output_tokens=_out_tokens)
        _note_shared_contention_recovered(dispatcher, slot, tag)
    elif client_aborted:
        slot.release()
    else:
        _cool_slot_on_premature_close(
            slot, usage, stream_state=raw_stream_state)
    if isinstance(usage, dict):
        completed_at_unix_ns = _unix_time_ns()
        latency_ms = max(0.0, float(latency or 0))
        ttft_ms = None
        if ttft is not None:
            candidate_ttft = float(ttft)
            if math.isfinite(candidate_ttft) and candidate_ttft >= 0:
                ttft_ms = candidate_ttft
        usage['_dispatch'] = {
            'key': slot.key_name, 'model': slot.model,
            'key_tail': (slot.api_key or '')[-4:],
            'provider_id': slot.provider_id,
            'protocol': slot.protocol or 'openai',
            'responses_profile': getattr(
                slot, 'responses_profile', '') or '',
            'latency_ms': round(latency_ms),
            'ttft_ms': round(ttft_ms, 3) if ttft_ms is not None else None,
            'stream_started_at_unix_ns': (
                completed_at_unix_ns - round(latency_ms * 1_000_000)),
            'first_content_at_unix_ns': (
                completed_at_unix_ns
                - round((latency_ms - ttft_ms) * 1_000_000)
                if ttft_ms is not None else None),
            'stream_completed_at_unix_ns': completed_at_unix_ns,
            'attempt': state.hard_attempts + 1,
            '429_retries': state._429_count,
            'queue_wait_ms': round(getattr(state, 'queue_wait_ms', 0.0), 3),
            'queue_wait_measurement': 'dispatcher_backpressure_only',
        }
    if cache_conv_id and verified_complete:
        try:
            from lib.llm_dispatch.cache_settle import (
                codex_cache_write_pending, is_cold_write,
                observe_codex_cache, record_stream_end,
            )
            _cache_profile = slot.oauth or ''
            if _cache_profile == 'codex':
                observe_codex_cache(cache_conv_id, usage)
            record_stream_end(
                cache_conv_id,
                cold_write=is_cold_write(usage),
                cache_profile=_cache_profile,
                pending_write=(
                    codex_cache_write_pending(usage)
                    if _cache_profile == 'codex' else False),
            )
        except ImportError as _cs_err:
            logger.debug('%s cache-settle record unavailable: %s', tag, _cs_err)


def _cycling_can_ever_serve(dispatcher, capability, *, initial_exclude_models,
                            durable_keys, durable_pairs, strict_model,
                            prefer_model):
    """True iff the slot-cooldown cycle can EVER yield a slot again.

    Applies ONLY the caller's own model bans + the durable (permission/quota)
    exclusions — the classes that cannot heal inside one dispatch call. An
    empty answer means every capable slot is dead on a non-healable class, so
    sleeping-and-retrying would cycle forever: the 2026-08-05 CI hang
    (233daa6 serial lane, insight second-pass) spun on
    ``_429_count > 0 or _slots_exist`` — self-sustaining, because
    ``note_cooldown_cycle()`` increments ``_429_count``, so after ANY single
    cooldown cycle the condition stayed true even with every key durably
    excluded by 401.

    Transient exclusions (unreachable / timeout / 502 / rate-limit cooldown)
    are deliberately IGNORED here: the 60s exclusion reset resurrects them
    and each resurrection burns a bounded hard attempt, so that cycling
    terminates on its own.
    """
    return dispatcher.has_capable_slots(
        capability,
        exclude_models=initial_exclude_models,
        exclude_keys=durable_keys,
        exclude_pairs=durable_pairs,
        prefer_model=prefer_model if strict_model else None)


class _StreamRetryState:
    """Shared retry/exclusion bookkeeping for the streaming dispatch loops.

    Both ``dispatch_stream`` (sync) and ``async_dispatch_stream`` (async) run
    the SAME three-tier exclusion state machine around their (different)
    transport calls:

      * ``exclude``       — models excluded entirely (hard model errors).
      * ``exclude_keys``  — keys excluded entirely (quota exhaustion / auto-
                            exhausted after consecutive 429s).
      * ``exclude_pairs`` — ``(key_name, model)`` pairs excluded (permission /
                            timeout / unreachable / strict-model errors), so
                            another key serving the same model is still tried.

    This class owns the state + the pure transitions that used to be hand-
    duplicated in both loops (init, the 60s periodic exclusion reset, the
    slot-pick exclusion kwargs, the avoid-set relaxation, and the per-error
    set mutations).  Loop control (``continue`` / ``break`` / sleep) and the
    transport call stay in each function — only the bookkeeping is shared, so
    the two can never drift on WHICH set a given error mutates.

    Counters:
      * ``hard_attempts`` counts genuine failures (capped by ``max_retries``).
      * ``_429_count`` counts completed upstream 429 attempts.
      * ``_cooldown_cycles`` counts slot-pool polling only; it is never exposed
        as a retry attempt.
    """

    _EXCLUSION_RESET_INTERVAL = 60  # reset hard-error exclusions every 60s of 429 cycling

    def __init__(self, exclude_models=None, avoid_pairs=None):
        self.exclude = set(exclude_models) if exclude_models else set()
        # Caller-provided model exclusions are permanent for this dispatch —
        # remembered so the periodic reset doesn't re-introduce them.
        self._initial_exclude_models = set(self.exclude)
        self.exclude_keys = set()
        self.exclude_pairs = set()
        # DURABLE exclusions — permission (401/403) pairs/keys and quota-dead
        # keys. The transient sets above are cleared every 60s of 429 cycling
        # (a 502/timeout may be a gateway restart that recovered);
        # rejection classes cannot heal inside one dispatch call, so
        # resurrecting them only re-burns a hard attempt on a guaranteed
        # failure (2026-08-03: a dead kimi-k3 key was re-tried every 60s,
        # consuming all 3 hard attempts over ~2min). They survive
        # maybe_reset_exclusions; the NEXT dispatch call still starts fresh,
        # so a fixed key self-heals between calls.
        self.exclude_keys_durable = set()
        self.exclude_pairs_durable = set()
        # Caller-provided avoid set seeds exclude_pairs so the first pick
        # already steers around it; tracked separately so it can be relaxed
        # as a last resort when every other slot is cooled/excluded.
        self._initial_avoid = set(avoid_pairs) if avoid_pairs else set()
        if self._initial_avoid:
            self.exclude_pairs |= self._initial_avoid
        self.last_err = None
        self.hard_attempts = 0
        self._429_count = 0
        self._cooldown_cycles = 0
        self._started_at = time.monotonic()
        self.queue_wait_ms = 0.0
        self._last_exclusion_reset = time.monotonic()
        # Monotonic timestamp when the current UNBROKEN gateway-5xx streak
        # began (None = no active streak). Set on the first gateway 502/503/504,
        # cleared by any real per-key 429 or a success. Used to bound a
        # whole-upstream outage without capping genuine per-key contention.
        self._gateway_streak_start = None
        # Monotonic timestamp of the FIRST genuine-429 starvation signal in
        # this dispatch call (None = not starving). Feeds the bounded
        # saturation escalation (): real per-key/contention 429s
        # and 429-equivalent cooldown cycles start the clock; gateway 5xx
        # has its own streak budget above and never starts this one.
        self._saturation_start = None

    def record_queue_wait(self, started_monotonic: float) -> None:
        """Accumulate only explicit dispatcher backpressure waits."""
        self.queue_wait_ms += max(
            0.0, (time.monotonic() - started_monotonic) * 1000)

    @property
    def total_attempts(self):
        return self.hard_attempts + self._429_count

    @property
    def capacity_wait_cycles(self):
        return self._429_count + self._cooldown_cycles

    def wait_status(self):
        elapsed = max(0.0, time.monotonic() - self._started_at)
        return DispatchWaitStatus(
            kind='waiting_slot',
            request_elapsed_s=elapsed,
            transport_idle_s=elapsed,
            semantic_idle_s=elapsed,
        )

    def gateway_outage_exceeded(self, budget_s):
        """True when nothing but gateway 5xx has come back for > ``budget_s``.

        ``budget_s <= 0`` disables the cap (legacy infinite-rotation behaviour).
        The streak must be active (a gateway 5xx was seen and no real 429 /
        success has cleared it since)."""
        if budget_s <= 0 or self._gateway_streak_start is None:
            return False
        return (time.monotonic() - self._gateway_streak_start) > budget_s

    def maybe_reset_exclusions(self, log_prefix, label):
        """Periodically clear hard-error exclusions during 429 cycling.

        502/timeout exclusions may be transient (gateway restart) but are
        otherwise permanent for the dispatch call.  After 60s of 429 cycling
        give excluded slots another chance — still-broken ones get re-excluded
        quickly.  Caller-provided ``exclude_models`` are preserved.  Returns
        the set of currently-active exclusions (for the caller's log line)
        before the reset, or None when nothing was reset.
        """
        if self.capacity_wait_cycles > 0 and (
                time.monotonic() - self._last_exclusion_reset) >= self._EXCLUSION_RESET_INTERVAL:
            if self.exclude or self.exclude_keys or self.exclude_pairs:
                logger.info(
                    '%s %s: resetting hard-error exclusions after %ds of 429 '
                    'cycling (cycle #%d) — exclude_models=%s exclude_keys=%s '
                    'exclude_pairs=%s',
                    log_prefix, label, self._EXCLUSION_RESET_INTERVAL,
                    self.capacity_wait_cycles, self.exclude, self.exclude_keys,
                    self.exclude_pairs)
                self.exclude.clear()
                self.exclude |= self._initial_exclude_models
                self.exclude_keys.clear()
                self.exclude_pairs.clear()
            if self.exclude_keys_durable or self.exclude_pairs_durable:
                logger.debug(
                    '%s %s: durable exclusions survive the reset '
                    '(permission/quota cannot heal mid-call): keys=%s pairs=%s',
                    log_prefix, label, self.exclude_keys_durable,
                    self.exclude_pairs_durable)
            self._last_exclusion_reset = time.monotonic()

    def eff_exclude_models(self):
        """``exclude_models`` to pass to pick_and_reserve.

        Caller-provided exclusions apply from attempt 1; failure-driven ones
        only after the first attempt.
        """
        return self.exclude if (self.total_attempts > 0
                                or self._initial_exclude_models) else None

    def eff_exclude_keys(self):
        if self.total_attempts <= 0:
            return None
        return self.exclude_keys | self.exclude_keys_durable

    def eff_exclude_pairs(self):
        # Caller-provided avoid pairs are specifically a *first-pick* hint:
        # the previous semantic attempt already proved that slot unhealthy.
        # Waiting for this fresh dispatch call to fail once before exposing the
        # set silently routes the retry straight back to the poisoned slot.
        if self.total_attempts <= 0 and not self._initial_avoid:
            return None
        return self.exclude_pairs | self.exclude_pairs_durable

    def relax_avoid_if_exhausted(self):
        """Drop the caller-provided avoid set when every other slot is gone.

        Zero-byte force-rotate uses ``avoid_pairs`` to steer away from a
        freshly-poisoned slot; if the rest of the pool is exhausted we'd
        rather retry the bad slot than fail outright.  Relaxes at most once.
        Returns True if it relaxed (caller should ``continue``).
        """
        if self._initial_avoid and self._initial_avoid <= self.exclude_pairs:
            self.exclude_pairs -= self._initial_avoid
            self._initial_avoid = set()
            return True
        return False

    def note_free_429(self, *, is_gateway=False):
        """A routine (non-quota) 429 — free retry, does not count toward the cap.

        ``is_gateway`` distinguishes a genuine per-key 429 (contention — rotate
        forever) from a gateway 5xx mapped onto this path (whole-upstream
        outage — bounded by ``gateway_outage_exceeded``). A gateway 5xx opens /
        extends the outage streak; a REAL 429 (or a success) clears it, because
        it proves the upstream is answering and only this key is throttled."""
        self._429_count += 1
        if is_gateway:
            if self._gateway_streak_start is None:
                self._gateway_streak_start = time.monotonic()
        else:
            self._gateway_streak_start = None
            if self._saturation_start is None:
                self._saturation_start = time.monotonic()

    def note_success(self):
        """A slot answered — clear any active gateway-outage streak."""
        self._gateway_streak_start = None
        self._saturation_start = None

    def saturation_exceeded(self, budget_s):
        """True when every slot has been 429-starved for > ``budget_s``.

        ``budget_s <= 0`` disables the cap (legacy infinite-rotation
        behaviour). Requires an active starvation clock — a dispatch that
        never saw a genuine 429 (e.g. hard-error churn) is not 'saturated'.
        """
        if budget_s <= 0 or self._saturation_start is None:
            return False
        return (time.monotonic() - self._saturation_start) > budget_s

    def note_cooldown_cycle(self):
        """Record one pool poll with no upstream request attempt."""
        self._cooldown_cycles += 1
        if self._saturation_start is None:
            self._saturation_start = time.monotonic()

    def note_quota_key(self, slot):
        """Quota-exhausted 429 — disable the key for this dispatch; hard attempt.

        Durable: a dead balance cannot recover inside one dispatch call, so
        the periodic 429-cycling reset must not resurrect it."""
        self.exclude_keys_durable.add(slot.key_name)
        self.hard_attempts += 1

    def note_permission_pair(self, slot, dispatcher, capability, log_prefix):
        """Permission denied — exclude the (key, model) PAIR; escalate to a
        whole-key exclusion only if EVERY model for this key is now excluded.

        Durable: an auth rejection cannot heal inside one dispatch call (see
        ``exclude_pairs_durable``)."""
        self.exclude_pairs_durable.add((slot.key_name, slot.model))
        self.hard_attempts += 1
        _key_pairs = {(kn, m) for kn, m in self.exclude_pairs_durable
                      if kn == slot.key_name}
        _key_models = {s.model for s in dispatcher.slots
                       if s.key_name == slot.key_name
                       and (not capability or capability in s.capabilities)}
        if _key_models and _key_models <= {m for _, m in _key_pairs}:
            self.exclude_keys_durable.add(slot.key_name)
            logger.warning('%s Permission denied on ALL models for key %s — '
                           'excluding entire key', log_prefix, slot.key_name)
            return True
        return False

    def note_unreachable_pair(self, slot):
        """Endpoint unreachable — exclude the pair; hard attempt.  (The caller
        sets the slot cooldown — that's slot mutation, not retry bookkeeping.)"""
        self.exclude_pairs.add((slot.key_name, slot.model))
        self.hard_attempts += 1

    def note_generic_error(self, slot, *, is_timeout, strict_model):
        """Generic stream error: a timeout or a strict-model error excludes the
        PAIR (other keys of the model still tried); anything else excludes the
        whole MODEL.  Hard attempt either way."""
        if is_timeout or strict_model:
            self.exclude_pairs.add((slot.key_name, slot.model))
        else:
            self.exclude.add(slot.model)
        self.hard_attempts += 1


def _first_output_callbacks(
    started_at,
    on_thinking,
    on_content,
    on_tool_call_ready,
    on_before_tool_call_ready=None,
):
    """Wrap every output channel around one per-attempt TTFT latch."""
    ttft_value = [None]
    recorded = False

    def _forward(callback, value, before=None):
        nonlocal recorded
        if not recorded:
            ttft_value[0] = (time.time() - started_at) * 1000
            recorded = True
        if before:
            before()
        if callback:
            callback(value)

    def _thinking(value):
        _forward(on_thinking, value)

    def _content(value):
        _forward(on_content, value)

    def _tool_call(value):
        _forward(
            on_tool_call_ready, value, before=on_before_tool_call_ready)

    return ttft_value, _thinking, _content, _tool_call


def _sleep_and_record_queue_wait(state, seconds, *, abort_check=None):
    """Sleep abortably for sync backpressure and account for the whole wait."""
    if seconds <= 0:
        return
    started_at = time.monotonic()
    if abort_check is None:
        time.sleep(seconds)
    else:
        from lib.llm._transport import abortable_sleep
        abortable_sleep(seconds, abort_check)
    state.record_queue_wait(started_at)


def dispatch_stream(body_or_messages, *, on_thinking=None, on_content=None,
                    on_tool_call_ready=None, on_before_tool_call_ready=None,
                    abort_check=None, max_tokens=4096, temperature=0,
                    thinking_enabled=False, preset='low', effort=None,
                    capability='text', prefer_model=None, tools=None,
                    max_retries=3, log_prefix='', strict_model=False,
                    on_retry=None, avoid_pairs=None,
                    exclude_models=None, on_attempt_restart=None,
                    on_waiting=None) -> ProviderStreamResult:
    """Smart dispatch for streaming requests.

    Accepts either:
      - A pre-built body dict (with 'messages' key) — legacy path; the
        body is re-adapted to each dispatched slot via
        ``_readjust_thinking_params`` / ``_clamp_max_tokens``.
      - Raw messages list — PREFERRED path: ``build_body`` is called for
        the actual slot picked, so provider-specific quirks (Claude Opus 4.7
        sampling param rejection, GLM temperature clamp, Qwen enable_thinking
        shape, …) are always correct for the final target model.

    The raw-messages path accepts ``tools=`` so callers doing tool loops
    (e.g. paper report, swarm agents) no longer need to pre-build the body.

    429 handling:
      - Does NOT count toward max_retries — retries indefinitely.
      - Does NOT exclude any pair — the 0.5s slot cooldown naturally
        steers the picker to the next slot.  After cooldown expires the
        slot becomes eligible again, so slots rotate automatically.
      - Generic 429s sleep 0.3s; typed shared-project retries reserve bounded
        provider/model probe slots (one per second, at most three seconds).
      - Respects abort_check so the user can still cancel.

    strict_model:
      When True AND prefer_model is set, the dispatcher will NEVER silently
      fall back to a different model.  429 retries stay within the preferred
      model's slots (different keys / alias group members).  Use this for
      user-facing requests where the frontend explicitly chose a model.

    on_waiting:
      Optional ``on_waiting(status=…, slot=…)`` callback. Capacity polling
      reports a typed ``DispatchWaitStatus``; an active transport reports its
      typed stream-progress status. This channel refreshes the waiting HUD and
      liveness clock without manufacturing a retry attempt. ``on_retry`` is
      reserved for a request that actually failed and will be attempted again.

    avoid_pairs:
      Optional set of ``(key_name, model)`` tuples the caller wants the
      dispatcher to avoid for THIS call (in addition to whatever
      ``exclude_pairs`` accumulates internally).  Used by zero-byte retry
      logic in ``analyse_stream_result`` → ``stream_llm_response`` to
      force slot rotation away from a poisoned upstream pool, mirroring
      the ``gateway-5xx-treated-as-429`` pattern.  When the dispatcher
      runs out of non-avoided slots it falls back to the avoided pair
      rather than failing — better to retry the bad slot than to fail
      the whole task.

    Returns:
        (msg: str, finish_reason: str, usage: dict)
    """
    from lib.llm import (
        AbortedError,
        BadRequestError,
        ContentFilterError,
        InvalidImageError,
        PermissionError_,
        PromptTooLongError,
        RateLimitError,
        stream_chat,
    )
    from lib.llm_errors import EndpointUnreachableError, RequestScopedError

    def _fire_attempt_restart(reason: str) -> None:
        """Notify the callee an in-flight attempt is being discarded and the
        request restarts from scratch (its partial stream will be re-sent).
        Distinct from on_retry: this callback specifically tells the content
        boundary that a partially emitted request is being replaced."""
        if on_attempt_restart is None:
            return
        try:
            on_attempt_restart(reason=reason)
        except Exception as _oar_e:
            logger.debug('%s on_attempt_restart raised: %s', log_prefix, _oar_e)

    dispatcher = get_dispatcher()
    state = _StreamRetryState(exclude_models=exclude_models, avoid_pairs=avoid_pairs)
    # Fruit 2 (E2): at most ONE forced OAuth token refresh-retry per
    # request (see dispatch_chat).
    _oauth_refresh_used = False

    # Detect if it's a pre-built body or raw messages
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

    # ③ Large-request headroom guard: pressure percentage triggers an
    # absolute request-envelope check; it is never a refusal by itself. If the
    # envelope still cannot fit after one trim, raise a typed local-pressure
    # signal so orchestration can shrink the derived context and retry the same
    # model. No-op off-cgroup / for small bodies / when the guard is disabled.
    try:
        from lib import cgroup_guard
        _approx = cgroup_guard.approx_body_bytes(body_or_messages)
        _ok_hr, _hr_reason = cgroup_guard.check_request_headroom(
            ident=log_prefix or 'dispatch_stream', approx_bytes=_approx)
        if not _ok_hr and os.environ.get('TOFU_CGROUP_REQUEST_GUARD', '1') not in ('0', 'off', 'false', 'no'):
            raise cgroup_guard.MemoryPressureError(_hr_reason)
    except cgroup_guard.MemoryPressureError:
        raise
    except Exception as _hr_e:
        logger.debug('%s cgroup headroom check skipped: %s', log_prefix, _hr_e)

    # hard_attempts counts only non-429 failures; 429 loops forever
    #   (the abort_check runs every cycle so the user can always cancel).
    #   A blanket 429-cycle cap was intentionally removed: the meaningful
    #   bound is the gateway-outage cap below (whole-upstream 5xx storm),
    #   which frees the worker without capping genuine per-key contention.

    while state.hard_attempts < max_retries:
        # Abort check — let the user cancel during 429 cycling
        if abort_check and abort_check():
            from lib.llm import AbortedError as _AE
            raise _AE('Aborted during dispatch retry')

        # Gateway-outage cap — DISABLED by default (budget 0, owner
        #   directive 2026-08-20: a whole-upstream 5xx storm is WAITABLE —
        #   keep rotating until the gateway recovers or the user cancels;
        #   abort_check runs every cycle, so stopping is always the user's
        #   call, never ours). When TOFU_GATEWAY_OUTAGE_BUDGET_S > 0, an
        #   unbroken gateway-5xx streak past the budget raises so the worker
        #   thread is freed (legacy thread-pool-starvation guard). A real
        #   per-key 429 or a success clears the streak, so genuine
        #   contention is never capped.
        _gw_budget = _gateway_outage_budget_secs()
        if state.gateway_outage_exceeded(_gw_budget):
            logger.error(
                '%s dispatch_stream: gateway 5xx outage exceeded %.0fs budget '
                '(only gateway errors from every slot) — giving up so the '
                'worker thread is freed. Last error: %s',
                log_prefix, _gw_budget, str(state.last_err)[:200])
            raise state.last_err or RuntimeError(
                'Gateway outage: no slot reachable for %.0fs'
                % _gw_budget)

        # 429-saturation escalation is opt-in. By default every-slot
        # saturation remains waitable until recovery or user cancellation.
        _sat_budget = _saturation_budget_secs()
        if state.saturation_exceeded(_sat_budget):
            _saturation_escalate(
                log_prefix, 'dispatch_stream',
                elapsed_s=time.monotonic() - state._saturation_start,
                budget_s=_sat_budget, cycles=state.capacity_wait_cycles,
                model=prefer_model)

        # Log available slots at start of each attempt for debugging
        logger.debug(
            '%s dispatch_stream attempt hard=%d/%d 429=%d cooldown_polls=%d: '
            'slots: %s',
            log_prefix, state.hard_attempts + 1, max_retries, state._429_count,
            state._cooldown_cycles,
            dispatcher.summarize_slots(capability))

        # Periodically reset hard-error exclusions during 429 cycling
        #   (502/timeout may be transient; give recovered slots another chance).
        state.maybe_reset_exclusions(log_prefix, 'dispatch_stream')

        # Warm-key hold (pre-pick): if this conversation has a sticky key
        #   whose prompt-cache prefix is warm but that key is in a SHORT
        #   rate-limit cooldown, briefly wait it out BEFORE picking — otherwise
        #   the picker rebinds to a cold key and re-bills a full cache_creation
        #   (~100K tokens) to dodge a sub-second per-minute throttle. The
        #   warm-cache saving dwarfs the wait. Bounded by the hold budget and
        #   capped to ONE hold per dispatch call (``_warm_held``) so a key stuck
        #   in a longer cooldown can't stall the loop — after one hold it falls
        #   through to the normal cold-key rebind. Flag-gated + reversible.
        if not getattr(state, '_warm_held', False):
            try:
                from lib.llm_dispatch.conv_affinity import (
                    get_conv_affinity,
                    sticky_hold_budget_ms,
                    sticky_hold_enabled,
                    sticky_hold_max_ms,
                    sticky_routing_enabled,
                )
                if sticky_routing_enabled() and sticky_hold_enabled():
                    _conv = get_conv_affinity()
                    if _conv:
                        _hold = dispatcher.sticky_cooldown_remaining_s(
                            _conv, prefer_model=prefer_model,
                            exclude_keys=state.eff_exclude_keys() or set(),
                            exclude_pairs=state.eff_exclude_pairs() or set())
                        if _hold is not None:
                            _remaining_s, _warm_key = _hold
                            # Escalating ceiling: the flat budget (default 1.5s)
                            # only covers a transient sub-second 429 nudge. A
                            # concurrent sibling can cool the conv's SOLE warm
                            # key for longer; wait that contention window out
                            # (up to sticky_hold_max_ms, default 8s) rather than
                            # cold-rebind and destroy the prefix. A genuinely
                            # long error/quota backoff (remaining > ceiling, e.g.
                            # 300s) still falls through to the cold rebind — and
                            # sticky_cooldown_remaining_s already returns None
                            # for an excluded/disabled key, so failover is intact
                            # (this is NOT a hard pin). One hold per dispatch
                            # call (_warm_held), and we wait the FULL remaining
                            # so the next pick actually finds the warm key
                            # eligible.
                            _budget_s = sticky_hold_budget_ms() / 1000.0
                            _ceiling_s = sticky_hold_max_ms() / 1000.0
                            if 0 < _remaining_s <= _ceiling_s:
                                _wait = min(_remaining_s + 0.05, _ceiling_s)
                                _kind = ('short 429 cooldown'
                                         if _remaining_s <= _budget_s
                                         else 'contention on sole warm key '
                                              '(escalated hold)')
                                logger.info(
                                    '%s dispatch_stream: holding %.2fs for '
                                    'conv=%s warm key %s (%s) to keep prompt '
                                    'cache warm',
                                    log_prefix, _wait, _conv[:8], _warm_key, _kind)
                                if abort_check and abort_check():
                                    from lib.llm import AbortedError as _AE
                                    raise _AE('Aborted during warm-key hold')
                                _sleep_and_record_queue_wait(
                                    state, _wait, abort_check=abort_check)
                                state._warm_held = True
                            else:
                                logger.debug(
                                    '%s dispatch_stream: warm key %s cooldown '
                                    '%.1fs exceeds hold ceiling %.1fs — genuine '
                                    'backoff, rebinding cold', log_prefix,
                                    _warm_key, _remaining_s, _ceiling_s)
            except ImportError as _imp_err:
                logger.debug('%s warm-key hold unavailable: %s',
                             log_prefix, _imp_err)

        slot = dispatcher.pick_and_reserve(
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys(),
            exclude_pairs=state.eff_exclude_pairs(),
            strict_model=strict_model)
        if slot is None:
            # ── Last-resort: drop the caller-provided avoid set ──
            # Zero-byte force-rotate uses ``avoid_pairs`` to steer away
            # from a freshly-poisoned slot. If the rest of the pool is
            # already exhausted we'd rather retry the bad slot than fail
            # outright — failure here means the task aborts, while a
            # retry on the original slot might still succeed.
            if state.relax_avoid_if_exhausted():
                logger.info(
                    '%s dispatch_stream: relaxing avoid_pairs — every '
                    'other slot is in cooldown/excluded', log_prefix)
                continue
            # All slots in cooldown / excluded — wait briefly and retry.
            # Two cases produce slot=None and need OPPOSITE handling:
            #   (a) capable slots EXIST but are all in transient 0.5s
            #       rate-limit cooldown — this is a 429-equivalent; keep
            #       fast-polling regardless of whether THIS call has yet
            #       caught a raw 429. Under high concurrency a fresh
            #       request routinely arrives while every slot is cooling
            #       (from OTHER requests' 429s); bailing here on attempt 1
            #       was the cause of spurious "All N attempts failed".
            #   (b) no capable slot exists at all → genuinely unservable.
            _slots_exist = dispatcher.has_capable_slots(
                capability, exclude_models=state.exclude,
                exclude_keys=state.exclude_keys | state.exclude_keys_durable,
                exclude_pairs=state.exclude_pairs | state.exclude_pairs_durable,
                prefer_model=prefer_model if strict_model else None)
            # Exit iff even the HEALABLE pool is empty (caller bans + durable
            # exclusions only). `_429_count > 0` used to keep this alive
            # forever — note_cooldown_cycle() increments it, so the condition
            # was self-sustaining once any cycle ran (233daa6 CI hang).
            if _slots_exist or _cycling_can_ever_serve(
                    dispatcher, capability,
                    initial_exclude_models=state._initial_exclude_models,
                    durable_keys=state.exclude_keys_durable,
                    durable_pairs=state.exclude_pairs_durable,
                    strict_model=strict_model, prefer_model=prefer_model):
                _sleep_and_record_queue_wait(
                    state, 0.3, abort_check=abort_check)
                state.note_cooldown_cycle()
                # Notify the caller the FIRST time we enter the cooldown
                #   wait and then sparsely (every 20 cycles ≈ 6s). Under
                #   contention every
                #   capable slot is cooling from OTHER requests' 429s, so this
                #   call returns slot=None WITHOUT ever catching a raw 429 of
                #   its own — yet it can wait minutes for a rate-limited
                #   strict_model. A flow/swarm worker in this state showed a
                #   bare "Waiting…" pulse with no signal. This is a WAITING
                #   status, not a retry: no request has run in this cycle.
                if on_waiting and (
                        state._cooldown_cycles == 1
                        or state._cooldown_cycles % 20 == 0):
                    try:
                        on_waiting(status=state.wait_status(), slot=None)
                    except Exception as _ore:
                        logger.debug('%s on_waiting (cooldown) raised: %s',
                                     log_prefix, _ore)
                if state._cooldown_cycles % 20 == 0:
                    logger.info(
                        '%s dispatch_stream: still cycling (slots cooling, %d times, '
                        'strict=%s), waiting for cooldown to expire…',
                        log_prefix, state._cooldown_cycles, strict_model)
                continue
            logger.warning(
                '%s dispatch_stream: NO CAPABLE SLOT on attempt %d/%d. '
                'exclude_models=%s exclude_keys=%s exclude_pairs=%s strict_model=%s. '
                'Available slots: %s',
                log_prefix, state.hard_attempts + 1, max_retries,
                state.exclude, state.exclude_keys, state.exclude_pairs, strict_model,
                dispatcher.summarize_slots(capability))
            break

        tag = f'{log_prefix}[D:{slot.key_name}:{slot.model}]'

        # Build this slot's provider-specific body through the shared adapter.
        body = _adapt_stream_body_for_slot(
            slot, body_or_messages, is_body,
            tools=tools, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, preset=preset, effort=effort)

        # ── Per-key big-prefix admission control ──
        # Bound the key's cache WORKING SET so several big conversations don't
        # LRU-evict each other's prompt cache in the key's shared namespace (the
        # mutual-eviction / rebound miss). This is a SUB-DOMINANT residual — the
        # dominant miss cause is client-side prefix re-serialization, caught in
        # cache_tracking; this gate only bounds the concurrency residual.
        # Residency-aware: admission counts the
        # distinct big prefixes RESIDENT on the key within the cache-TTL window
        # (not just concurrent streams), so a warm same-conv re-run passes free
        # while a NEW distinct big prefix waits when the working set is full.
        # Passing conv_id (the residency identity) is what makes it catch the
        # flow-non-overlap-but-residency-overlap case the old stream-only gate
        # was blind to. Soft + env-gated (see big_prefix_gate.py).
        import contextlib as _contextlib
        _cache_conv_id = ''
        _est_tok = 0
        try:
            from lib.llm_dispatch.big_prefix_gate import (
                big_prefix_slot, estimate_prefix_tokens,
            )
            from lib.llm_dispatch.conv_affinity import get_conv_affinity
            _cache_conv_id = get_conv_affinity() or ''
            _est_tok = estimate_prefix_tokens(body)
            # Distinct keys serving THIS model. Gating only helps with ≥2 keys
            # (a held big prefix can route to another key's cache namespace);
            # on a single-key model the gate would only serialize one pool and
            # add latency, so big_prefix_slot no-ops when key_count <= 1.
            _model_key_count = len({s.key_name for s in dispatcher.slots
                                    if s.model == slot.model})
            _big_gate = big_prefix_slot(
                slot.key_name, _est_tok, conv_id=_cache_conv_id,
                log_prefix=tag, key_count=_model_key_count)
        except ImportError as _bpg_err:
            logger.debug('%s big-prefix gate unavailable: %s', tag, _bpg_err)
            _big_gate = _contextlib.nullcontext()

        # ── Cache write-visibility settle gate (Anthropic SDK #1451 race) ──
        # If THIS conversation's prior big round's stream ended less than the
        # settle window ago, briefly wait so its cache WRITE is visible upstream
        # before this round tries to read the prefix back — otherwise the read
        # misses and the whole prefix is re-billed (the dominant floor-miss in
        # fast tool-loop / autopilot conversations). The wait sits inside the
        # agent's own tool loop (never delays a turn's FIRST request), is
        # adaptive (only the remainder of the window), abort-aware, and
        # env-gated. See cache_settle.py.
        try:
            try:
                from lib.llm_dispatch.cache_settle import settle_before_send
                settle_before_send(
                    _cache_conv_id,
                    _est_tok,
                    abort_check=abort_check,
                    log_prefix=tag,
                    cache_profile=slot.oauth or '',
                )
            except ImportError as _cs_err:
                logger.debug(
                    '%s cache-settle gate unavailable: %s', tag, _cs_err)

            with _big_gate:
                # Reserve shared-project capacity only after all local cache
                # gates have finished; the one-second spacing therefore
                # measures actual upstream request starts, not local waits.
                _wait_for_shared_contention_admission(
                    dispatcher,
                    slot,
                    lambda seconds: _sleep_and_record_queue_wait(
                        state, seconds, abort_check=abort_check),
                    tag,
                )
                t0 = time.time()
                (ttft_value, _on_thinking_wrapper, _on_content_wrapper,
                 _on_tool_call_ready_wrapper) = _first_output_callbacks(
                    t0,
                    on_thinking,
                    on_content,
                    on_tool_call_ready,
                    on_before_tool_call_ready,
                )
                # Waiting-heartbeat seam: the transport reports the current
                # attempt's typed progress while it awaits headers/model output
                # or while a started stream is semantically stalled. The slot
                # is only current-attempt identity; stale errors are never UI.
                _on_stream_wait = None
                if on_waiting:
                    def _on_stream_wait(status, _slot=slot):
                        try:
                            on_waiting(status=status, slot=_slot)
                        except Exception as _owe:
                            logger.debug(
                                '%s on_waiting raised: %s', tag, _owe)
                stream_result = msg, finish, usage = \
                    ensure_provider_stream_result(stream_chat(
                        body, api_key=slot.api_key,
                        base_url=slot.base_url or None,
                        extra_headers=slot.extra_headers or None,
                        oauth=slot.oauth or '',
                        adapter=slot.adapter or None,
                        on_thinking=_on_thinking_wrapper,
                        on_content=_on_content_wrapper,
                        on_tool_call_ready=_on_tool_call_ready_wrapper,
                        abort_check=abort_check,
                        log_prefix=tag,
                        on_attempt_restart=on_attempt_restart,
                        on_stream_wait=_on_stream_wait,
                        api_protocol=slot.protocol or 'openai',
                    ))
                latency = (time.time() - t0) * 1000
                _settle_stream_result(
                    slot, usage, latency=latency, ttft=ttft_value[0],
                    state=state, cache_conv_id=_cache_conv_id, tag=tag,
                    dispatcher=dispatcher, stream_result=stream_result)
                settled_state = stream_result.state.value
                if state._429_count > 0:
                    logger.info(
                        '%s dispatch_stream settled after %d 429-retries: '
                        'state=%s finish_reason=%s model=%s provider=%s '
                        'latency=%.0fms', log_prefix, state._429_count,
                        settled_state, finish, slot.model, slot.provider_id,
                        latency)
                else:
                    logger.debug(
                        '%s dispatch_stream settled: state=%s '
                        'finish_reason=%s model=%s provider=%s '
                        'latency=%.0fms attempt=%d/%d',
                        log_prefix, settled_state, finish, slot.model,
                        slot.provider_id, latency, state.hard_attempts + 1,
                        max_retries)
                return stream_result

        except RateLimitError as e:
            _is_quota = bool(getattr(e, 'is_quota', False))
            _is_gateway = bool(getattr(e, 'is_gateway', False))
            _is_contention = bool(getattr(e, 'is_shared_contention', False))
            # HTTP 402 (account credit dead) → key-wide stop; 429-quota →
            # per-model (see Slot.record_error is_account_quota).
            _is_account_quota = bool(
                _is_quota
                and int(getattr(e, 'status_code', 0) or 0) == 402)
            _err_str = str(e)[:200]
            # Subscription-quota timed hold (resets_at/resets_in_seconds)
            #   — explicit cooldown instead of the 0.5s steering nudge.
            slot.record_error(is_rate_limit=True,
                              is_quota_exhausted=_is_quota,
                              is_account_quota=_is_account_quota,
                              is_gateway=_is_gateway,
                              is_shared_contention=_is_contention,
                              cooldown_s=getattr(e, 'retry_after_s', None),
                              error=_err_str if _is_quota else '')
            state.last_err = e
            _retry_delay_s = _shared_contention_retry_delay(
                dispatcher, slot, _is_contention, log_prefix)
            if _is_quota:
                # Persistent billing/quota exhaustion — disable this key
                #   for the remainder of this dispatch and flag it so the
                #   user sees "out of balance" in Settings.
                state.note_quota_key(slot)
                logger.warning(
                    '%s Quota exhausted on %s:%s — disabling key for '
                    'today: %s',
                    log_prefix, slot.key_name, slot.model, _err_str)
                if on_retry:
                    on_retry(attempt=state.hard_attempts,
                             reason='Key balance exhausted', status_code=429)
                _fire_attempt_restart('key balance exhausted')
                continue
            state.note_free_429(is_gateway=bool(getattr(e, 'is_gateway', False)))
            # Don't exclude anything — slot.record_error() sets a 0.5s
            #   cooldown which naturally steers pick_and_reserve to another
            #   slot.  After cooldown expires the slot is eligible again,
            #   so all slots rotate automatically.
            # Generic 429s keep the 0.3s baseline; typed project contention
            # uses the provider/model probe reservation computed above.
            # Log response body periodically to diagnose persistent 429s
            _err_body = str(e)[:300]
            # 2026-05-05 noise-reduction: per-cycle 429 is ROUTINE backpressure
              # (handler already rotates to the next key). Log at INFO, not
              # WARNING — only the final exhaustion path (all keys excluded)
              # remains WARNING/ERROR.
            if state._429_count <= 3 or state._429_count % 100 == 0:
                logger.info(
                    '%s 429 rate-limited on %s:%s (cycle #%d) — body: %s',
                    log_prefix, slot.key_name, slot.model, state._429_count,
                    _err_body)
            else:
                # DEBUG between sparse INFO beats keeps a long outage bounded.
                logger.debug(
                    '%s 429 rate-limited on %s:%s (cycle #%d)',
                    log_prefix, slot.key_name, slot.model, state._429_count)
            if on_retry:
                if _is_gateway:
                    # Upstream-outage class (gateway 5xx / vendor transient
                    # wrapped in 4xx) — NOT 限流; the cause is a sick
                    # upstream, not per-key contention.
                    on_retry(attempt=state._429_count,
                             reason='Upstream error',
                             status_code=int(getattr(e, 'status_code', 0) or 0))
                else:
                    on_retry(attempt=state._429_count, reason='Rate limited (429)', status_code=429)
            _fire_attempt_restart('rate limited (429) — rotating slot')
            if _retry_delay_s > 0:
                _sleep_and_record_queue_wait(
                    state, _retry_delay_s, abort_check=abort_check)
            # Don't increment hard_attempts — 429 retries are free
            continue

        except PermissionError_ as e:
            # OAuth-subscription slot 401: force ONE token refresh + ONE
            #   retry before the normal failover below (see dispatch_chat).
            #   Scoped to oauth slots — API-key slots keep pair exclusion.
            if slot.oauth and not _oauth_refresh_used:
                _oauth_refresh_used = True
                if _force_oauth_token_refresh(slot.oauth, log_prefix):
                    slot.release()  # refresh-retry — not a slot-health signal
                    logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) '
                                   '— token refreshed, retrying once',
                                   log_prefix, slot.key_name, slot.model,
                                   slot.oauth)
                    _fire_attempt_restart('oauth token refreshed — retrying')
                    continue
                logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) — '
                               'forced refresh failed; normal failover',
                               log_prefix, slot.key_name, slot.model, slot.oauth)
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            # Exclude the (key, model) PAIR; escalate to a whole-key
            #   exclusion only if ALL models for this key are now excluded.
            if not state.note_permission_pair(slot, dispatcher, capability, log_prefix):
                logger.warning(
                    '%s Permission denied on %s:%s — excluding pair, '
                    'remaining slots: %s',
                    log_prefix, slot.key_name, slot.model,
                    dispatcher.summarize_slots(capability))

        except EndpointUnreachableError as e:
            # The endpoint host is down (connect-phase failure). The
            #   transport already escaped its same-key retry loop, so here
            #   we cool the slot down and exclude this (key, model) pair,
            #   then immediately pick another slot — this is the failover.
            #   A whole-endpoint cooldown (not just 0.5s) keeps the picker
            #   off the dead host while we route around it; the local
            #   health checker clears the cooldown when the box recovers.
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            slot.cooldown_until = time.time() + _UNREACHABLE_COOLDOWN
            slot.cooldown_reason = 'upstream'
            state.last_err = e
            state.note_unreachable_pair(slot)
            logger.warning(
                '%s Endpoint unreachable on %s:%s (%s) — cooled %ds + '
                'excluded pair, failing over: %s',
                log_prefix, slot.key_name, slot.model,
                getattr(e, 'base_url', '') or '?', _UNREACHABLE_COOLDOWN,
                str(e)[:160])
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Endpoint unreachable', status_code=0)

        except AbortedError:
            slot.release()  # user abort — not a slot-health signal
            logger.debug('%s User aborted — stopping dispatch immediately', tag)
            raise   # Don't retry on other slots, user wants to stop

        except ContentFilterError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Content filter (HTTP 450) — not retrying', tag, exc_info=True)
            raise   # Same content = same filter, no point retrying

        except PromptTooLongError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Prompt/request too large — not retrying on other slots '
                           '(same payload = same rejection)', tag)
            raise   # Escape to orchestrator for reactive compaction

        except InvalidImageError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Image content error — not retrying on other slots '
                           '(same image = same rejection)', tag)
            raise   # Same payload = same rejection on all keys

        except RequestScopedError as e:
            # Request-scoped 4xx (404/422) — THIS request's semantics, not
            # slot/model health (CLIProxyAPI isRequestScopedResultError).
            # Surface untouched: no cooldown, no key_stats, no fallback.
            slot.release()
            logger.warning('%s Request-scoped error (HTTP %s) on %s:%s — '
                           'surfacing to caller: %.300s',
                           log_prefix, getattr(e, 'status_code', 0) or '?',
                           slot.key_name, slot.model, str(e))
            raise

        except BadRequestError as e:
            # Deterministic HTTP 400 — PAYLOAD-level, not slot health (see
            # the sync dispatch_chat branch). Release + pair-exclude only.
            slot.release()
            state.last_err = e
            state.exclude_pairs.add((slot.key_name, slot.model))
            state.hard_attempts += 1
            logger.warning('%s Bad request (HTTP 400, deterministic) on %s:%s '
                           '— released slot, excluded pair, trying next: %.500s',
                           log_prefix, slot.key_name, slot.model, str(e))
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Upstream error', status_code=400)

        except Exception as e:
            # Subscription-quota/capacity signal via a non-429 wrapper
            #   (SSE RetryableAPIError / generic SSE error) — rate-limit
            #   class with parsed reset duration, NOT slot-health damage.
            if is_subscription_quota_error(str(e)):
                _ra = parse_subscription_retry_after(str(e))
                slot.record_error(is_rate_limit=True, cooldown_s=_ra,
                                  error=str(e)[:200])
                state.last_err = e
                state.note_free_429()
                logger.warning('%s Subscription quota/capacity signal on %s:%s '
                               '(cooldown %ss) — rotating slot: %s',
                               log_prefix, slot.key_name, slot.model,
                               f'{_ra:.0f}' if _ra else '0.5', str(e)[:160])
                if on_retry:
                    on_retry(attempt=state._429_count,
                             reason='Subscription quota reached',
                             status_code=429)
                _fire_attempt_restart('subscription quota reached — rotating slot')
                _sleep_and_record_queue_wait(
                    state, 0.3, abort_check=abort_check)
                continue
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            _is_timeout = 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()
            # Notify frontend about retry so user sees status instead of "Waiting…"
            if on_retry:
                _status = getattr(e, 'status_code', 0) or 0
                _reason = str(e)[:120]
                if _is_timeout:
                    _reason = 'Request timed out'
                elif _status:
                    _reason = f'HTTP {_status}'
                on_retry(attempt=state.hard_attempts + 1, reason=_reason, status_code=_status)
            # Timeout / strict-model → exclude the PAIR (other keys of the
            # model still tried); otherwise exclude the whole MODEL.
            state.note_generic_error(slot, is_timeout=_is_timeout, strict_model=strict_model)
            if _is_timeout:
                logger.debug('%s Timeout (%.0fms) — excluding pair '
                             '%s:%s, trying next slot', tag, latency, slot.key_name, slot.model, exc_info=True)
            elif strict_model:
                logger.debug('%s Stream error (strict_model): %s — excluding pair '
                             '%s:%s, trying other keys', tag, str(e)[:200],
                             slot.key_name, slot.model, exc_info=True)
            else:
                logger.debug('%s Stream error: %s — trying next slot', tag, str(e)[:200], exc_info=True)

    # All retries exhausted or no slot available — raise the last error
    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              what='dispatch_stream')


# ═══════════════════════════════════════════════════════════
#  Public API — async_dispatch_stream (async wrapper)
# ═══════════════════════════════════════════════════════════

async def async_dispatch_stream(body_or_messages, *, on_thinking=None,
                                on_content=None, on_tool_call_ready=None,
                                abort_check=None, max_tokens=4096, temperature=0,
                                thinking_enabled=False, preset='low', effort=None,
                                capability='text', prefer_model=None, tools=None,
                                max_retries=3, log_prefix='', strict_model=False,
                                on_retry=None, avoid_pairs=None,
                                exclude_models=None,
                                on_waiting=None) -> ProviderStreamResult:
    """Native-async streaming dispatch — non-blocking on the event loop.

    Unlike the previous ``to_thread(dispatch_stream)`` stopgap, this drives the
    genuinely-async ``async_stream_chat`` (httpx) transport, so the streaming
    HTTP call runs ON the event loop without occupying a thread-pool worker.
    Slot selection, retry/429 cycling, and exclusion logic mirror the sync
    ``dispatch_stream`` loop; body adaptation is shared via
    ``_adapt_stream_body_for_slot``.

    Same signature and return shape as ``dispatch_stream``:
        (msg: str, finish_reason: str, usage: dict)

    This is the preferred entry point for async callers (the orchestrator once
    converted to native async, or any ``async def`` route handler).

    ⚠️ RESERVED-BY-DESIGN — NO PRODUCTION CALLER TODAY (verified 2026-06-25).
    Every streaming path in the app (the UI chat worker AND the
    ``/v1/chat/completions`` + ``/v1/messages`` compat endpoints) runs the task
    via ``spawn_task`` → ``asyncio.to_thread`` on an OFF-loop worker thread, then
    the ``async def`` route merely tails the task's event buffer. A worker thread
    is sync and cannot ``await`` this; it correctly uses the sync
    ``dispatch_stream`` (``requests``) instead. Making this go live would require
    EITHER converting ``run_task`` to native async — explicitly rejected (see
    docs/API_CONTRACT.md §1: it's off-loop already, so async gives
    no benefit and risks starving the loop with CPU-bound work) — OR a genuinely
    on-loop streaming endpoint that bypasses the thread worker (a real feature,
    not a wiring change). So this is NOT dead code: it's the tested substrate for
    a future native-async streaming caller, kept in lockstep with
    ``dispatch_stream`` (both share ``_StreamRetryState`` +
    ``_adapt_stream_body_for_slot``). It IS exercised by
    ``tests/test_async_dispatch_stream.py``. Do NOT "wire it to the worker" by
    wrapping it in ``asyncio.run()`` inside the thread — that spins a throwaway
    loop per stream and is a pure regression over the sync path.
    """
    from lib.llm import (
        AbortedError,
        BadRequestError,
        ContentFilterError,
        InvalidImageError,
        PermissionError_,
        PromptTooLongError,
        RateLimitError,
    )
    from lib.llm._transport import async_abortable_sleep
    from lib.llm.astream import async_stream_chat
    from lib.llm_errors import EndpointUnreachableError, RequestScopedError

    dispatcher = get_dispatcher()
    state = _StreamRetryState(exclude_models=exclude_models, avoid_pairs=avoid_pairs)
    # Fruit 2 (E2): at most ONE forced OAuth token refresh-retry per
    # request (see dispatch_chat).
    _oauth_refresh_used = False
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

    while state.hard_attempts < max_retries:
        if abort_check and abort_check():
            raise AbortedError('Aborted during dispatch retry')

        # Gateway-outage cap (lockstep with sync dispatch_stream) —
        #   DISABLED by default (budget 0, owner directive 2026-08-20: wait
        #   out a total upstream 5xx storm; the user cancels, we don't).
        #   Set TOFU_GATEWAY_OUTAGE_BUDGET_S > 0 to restore the bounded
        #   give-up so the event loop / worker isn't pinned forever.
        _gw_budget = _gateway_outage_budget_secs()
        if state.gateway_outage_exceeded(_gw_budget):
            logger.error(
                '%s async_dispatch_stream: gateway 5xx outage exceeded %.0fs '
                'budget — giving up. Last error: %s',
                log_prefix, _gw_budget, str(state.last_err)[:200])
            raise state.last_err or RuntimeError(
                'Gateway outage: no slot reachable for %.0fs'
                % _gw_budget)

        state.maybe_reset_exclusions(log_prefix, 'async_dispatch_stream')

        # 429-saturation escalation (lockstep with sync dispatch_stream
        #   —  交付①; disabled by default since 2026-08-03).
        _sat_budget = _saturation_budget_secs()
        if state.saturation_exceeded(_sat_budget):
            _saturation_escalate(
                log_prefix, 'async_dispatch_stream',
                elapsed_s=time.monotonic() - state._saturation_start,
                budget_s=_sat_budget, cycles=state.capacity_wait_cycles,
                model=prefer_model)

        slot = dispatcher.pick_and_reserve(
            capability=capability, prefer_model=prefer_model,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys(),
            exclude_pairs=state.eff_exclude_pairs(),
            strict_model=strict_model)
        if slot is None:
            if state.relax_avoid_if_exhausted():
                continue
            _slots_exist = dispatcher.has_capable_slots(
                capability, exclude_models=state.exclude,
                exclude_keys=state.exclude_keys | state.exclude_keys_durable,
                exclude_pairs=state.exclude_pairs | state.exclude_pairs_durable,
                prefer_model=prefer_model if strict_model else None)
            if _slots_exist or _cycling_can_ever_serve(
                    dispatcher, capability,
                    initial_exclude_models=state._initial_exclude_models,
                    durable_keys=state.exclude_keys_durable,
                    durable_pairs=state.exclude_pairs_durable,
                    strict_model=strict_model, prefer_model=prefer_model):
                _wait_started = time.monotonic()
                await async_abortable_sleep(0.3, abort_check)
                state.record_queue_wait(_wait_started)
                state.note_cooldown_cycle()
                if on_waiting and (
                        state._cooldown_cycles == 1
                        or state._cooldown_cycles % 20 == 0):
                    try:
                        on_waiting(status=state.wait_status(), slot=None)
                    except Exception as _owe:
                        logger.debug(
                            '%s async on_waiting (cooldown) raised: %s',
                            log_prefix, _owe)
                continue
            logger.warning('%s async_dispatch_stream: NO CAPABLE SLOT on '
                           'attempt %d/%d', log_prefix, state.hard_attempts + 1, max_retries)
            break

        tag = f'{log_prefix}[aD:{slot.key_name}:{slot.model}]'

        body = _adapt_stream_body_for_slot(
            slot, body_or_messages, is_body,
            tools=tools, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, preset=preset, effort=effort)

        # ── Cache write-visibility settle gate (async path) — see the sync
        #    dispatch_stream branch + cache_settle.py for the rationale. Waits
        #    (event-loop-friendly) so this conv's prior big round's cache write
        #    is visible before this round reads the prefix back.
        _cache_conv_id = ''
        try:
            try:
                from lib.llm_dispatch.conv_affinity import get_conv_affinity
                from lib.llm_dispatch.big_prefix_gate import (
                    estimate_prefix_tokens,
                )
                from lib.llm_dispatch.cache_settle import (
                    async_settle_before_send,
                )
                _cache_conv_id = get_conv_affinity() or ''
                await async_settle_before_send(
                    _cache_conv_id, estimate_prefix_tokens(body),
                    abort_check=abort_check, log_prefix=tag,
                    cache_profile=slot.oauth or '')
            except ImportError as _cs_err:
                logger.debug(
                    '%s cache-settle (async) unavailable: %s', tag, _cs_err)

            # Match the sync ordering: local cache waits finish before this
            # request reserves shared-project capacity.
            await _wait_for_shared_contention_admission_async(
                dispatcher,
                slot,
                state=state,
                abort_check=abort_check,
                async_sleep_fn=async_abortable_sleep,
                log_prefix=tag,
            )
            t0 = time.time()
            (ttft_value, _on_thinking_wrapper, _on_content_wrapper,
             _on_tool_call_ready_wrapper) = _first_output_callbacks(
                t0, on_thinking, on_content, on_tool_call_ready)

            # Waiting-heartbeat seam — see the sync dispatch_stream branch.
            _on_stream_wait = None
            if on_waiting:
                def _on_stream_wait(status, _slot=slot):
                    try:
                        on_waiting(status=status, slot=_slot)
                    except Exception as _owe:
                        logger.debug('%s on_waiting raised: %s', tag, _owe)
            stream_result = msg, finish, usage = ensure_provider_stream_result(
                await async_stream_chat(
                body, api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                oauth=slot.oauth or '',
                adapter=slot.adapter or None,
                on_thinking=_on_thinking_wrapper,
                on_content=_on_content_wrapper,
                on_tool_call_ready=_on_tool_call_ready_wrapper,
                abort_check=abort_check, log_prefix=tag,
                on_stream_wait=_on_stream_wait,
                api_protocol=slot.protocol or 'openai'))
            latency = (time.time() - t0) * 1000
            _settle_stream_result(
                slot, usage, latency=latency, ttft=ttft_value[0], state=state,
                cache_conv_id=_cache_conv_id, tag=tag, dispatcher=dispatcher,
                stream_result=stream_result)
            logger.debug('%s async_dispatch_stream settled: state=%s '
                         'finish=%s model=%s latency=%.0fms', log_prefix,
                         stream_result.state.value, finish, slot.model, latency)
            return stream_result

        except RateLimitError as e:
            _is_quota = bool(getattr(e, 'is_quota', False))
            _is_gateway = bool(getattr(e, 'is_gateway', False))
            _is_contention = bool(getattr(e, 'is_shared_contention', False))
            # HTTP 402 (account credit dead) → key-wide stop; 429-quota →
            # per-model (see Slot.record_error is_account_quota).
            _is_account_quota = bool(
                _is_quota
                and int(getattr(e, 'status_code', 0) or 0) == 402)
            # Subscription-quota timed hold (resets_at/resets_in_seconds)
            #   — explicit cooldown instead of the 0.5s steering nudge.
            slot.record_error(is_rate_limit=True, is_quota_exhausted=_is_quota,
                              is_account_quota=_is_account_quota,
                              is_gateway=_is_gateway,
                              is_shared_contention=_is_contention,
                              cooldown_s=getattr(e, 'retry_after_s', None),
                              error=str(e)[:200] if _is_quota else '')
            state.last_err = e
            _retry_delay_s = _shared_contention_retry_delay(
                dispatcher, slot, _is_contention, log_prefix)
            if _is_quota:
                state.note_quota_key(slot)
                if on_retry:
                    on_retry(attempt=state.hard_attempts, reason='Key balance exhausted',
                             status_code=429)
                continue
            state.note_free_429(is_gateway=bool(getattr(e, 'is_gateway', False)))
            if on_retry:
                if _is_gateway:
                    on_retry(attempt=state._429_count,
                             reason='Upstream error',
                             status_code=int(getattr(e, 'status_code', 0) or 0))
                else:
                    on_retry(attempt=state._429_count, reason='Rate limited (429)', status_code=429)
            if _retry_delay_s > 0:
                _wait_started = time.monotonic()
                await async_abortable_sleep(_retry_delay_s, abort_check)
                state.record_queue_wait(_wait_started)
            continue

        except PermissionError_ as e:
            # OAuth-subscription slot 401: force ONE token refresh + ONE
            #   retry before normal failover (see dispatch_chat).
            if slot.oauth and not _oauth_refresh_used:
                _oauth_refresh_used = True
                if _force_oauth_token_refresh(slot.oauth, log_prefix):
                    slot.release()  # refresh-retry — not a slot-health signal
                    logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) '
                                   '— token refreshed, retrying once',
                                   log_prefix, slot.key_name, slot.model,
                                   slot.oauth)
                    continue
                logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) — '
                               'forced refresh failed; normal failover',
                               log_prefix, slot.key_name, slot.model, slot.oauth)
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            if not state.note_permission_pair(slot, dispatcher, capability, log_prefix):
                logger.warning('%s Permission denied on %s:%s — excluding pair',
                               log_prefix, slot.key_name, slot.model)

        except EndpointUnreachableError as e:
            # Endpoint host down — cool the slot, exclude the pair, fail
            #   over. Mirrors the sync dispatch_stream handler.
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            slot.cooldown_until = time.time() + _UNREACHABLE_COOLDOWN
            slot.cooldown_reason = 'upstream'
            state.last_err = e
            state.note_unreachable_pair(slot)
            logger.warning(
                '%s Endpoint unreachable on %s:%s (%s) — cooled %ds + '
                'excluded pair, failing over',
                log_prefix, slot.key_name, slot.model,
                getattr(e, 'base_url', '') or '?', _UNREACHABLE_COOLDOWN)
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Endpoint unreachable', status_code=0)

        except AbortedError:
            slot.release()
            logger.debug('%s User aborted — stopping dispatch immediately', tag)
            raise

        except ContentFilterError:
            slot.release()
            logger.warning('%s Content filter (HTTP 450) — not retrying', tag, exc_info=True)
            raise

        except PromptTooLongError:
            slot.release()
            logger.warning('%s Prompt/request too large — not retrying', tag)
            raise

        except InvalidImageError:
            slot.release()
            logger.warning('%s Image content error — not retrying', tag)
            raise

        except RequestScopedError as e:
            # Request-scoped 4xx (404/422) — THIS request's semantics, not
            # slot/model health (CLIProxyAPI isRequestScopedResultError).
            slot.release()
            logger.warning('%s Request-scoped error (HTTP %s) on %s:%s — '
                           'surfacing to caller: %.300s',
                           log_prefix, getattr(e, 'status_code', 0) or '?',
                           slot.key_name, slot.model, str(e))
            raise

        except BadRequestError as e:
            # Deterministic HTTP 400 — PAYLOAD-level, not slot health (see
            # the sync dispatch_chat branch). Release + pair-exclude only.
            slot.release()
            state.last_err = e
            state.exclude_pairs.add((slot.key_name, slot.model))
            state.hard_attempts += 1
            logger.warning('%s Bad request (HTTP 400, deterministic) on %s:%s '
                           '— released slot, excluded pair, trying next: %.500s',
                           log_prefix, slot.key_name, slot.model, str(e))
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Upstream error', status_code=400)

        except Exception as e:
            # Subscription-quota/capacity signal via a non-429 wrapper —
            #   rate-limit class with parsed reset duration.
            if is_subscription_quota_error(str(e)):
                _ra = parse_subscription_retry_after(str(e))
                slot.record_error(is_rate_limit=True, cooldown_s=_ra,
                                  error=str(e)[:200])
                state.last_err = e
                state.note_free_429()
                logger.warning('%s Subscription quota/capacity signal on %s:%s '
                               '(cooldown %ss) — rotating slot: %s',
                               log_prefix, slot.key_name, slot.model,
                               f'{_ra:.0f}' if _ra else '0.5', str(e)[:160])
                if on_retry:
                    on_retry(attempt=state._429_count,
                             reason='Subscription quota reached',
                             status_code=429)
                _wait_started = time.monotonic()
                await async_abortable_sleep(0.3, abort_check)
                state.record_queue_wait(_wait_started)
                continue
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            _is_timeout = 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()
            if on_retry:
                _status = getattr(e, 'status_code', 0) or 0
                on_retry(attempt=state.hard_attempts + 1,
                         reason='Request timed out' if _is_timeout else (f'HTTP {_status}' if _status else str(e)[:120]),
                         status_code=_status)
            state.note_generic_error(slot, is_timeout=_is_timeout, strict_model=strict_model)
            logger.debug('%s async_dispatch_stream error: %s — next slot',
                         tag, str(e)[:200], exc_info=True)

    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              what='async_dispatch_stream')


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_fastest (race N slots)
# ═══════════════════════════════════════════════════════════

def dispatch_fastest(messages, *, max_tokens=4096, temperature=0,
                     thinking_enabled=False, preset='low', effort=None,
                     capability='text', prefer_model=None,
                     n_race=2, tools=None, extra=None,
                     log_prefix=''):
    """Fire requests to N slots simultaneously, return the first successful result.

    This wastes some API quota but guarantees the fastest possible response.
    Best for latency-critical tasks.

    Args:
        n_race: Number of slots to race (default 2)
        (other args same as dispatch_chat)

    Returns:
        (content_text: str, usage_dict: dict)
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from lib.llm import chat

    dispatcher = get_dispatcher()
    slots = dispatcher.pick_top_n(n=n_race, capability=capability,
                                   prefer_model=prefer_model)

    if not slots:
        raise RuntimeError(f'No slots available for capability={capability}')

    if len(slots) == 1:
        # Only one slot — just call it directly
        return dispatch_chat(messages, max_tokens=max_tokens, temperature=temperature,
                             thinking_enabled=thinking_enabled, preset=preset,
                             effort=effort, capability=capability,
                             prefer_model=slots[0].model,
                             tools=tools, extra=extra, log_prefix=log_prefix)

    cancel_event = threading.Event()

    def _race_worker(slot):
        if cancel_event.is_set():
            return None
        # Note: record_request() was already called atomically in pick_top_n(reserve=True)
        t0 = time.time()
        tag = f'{log_prefix}[Race:{slot.key_name}:{slot.model}]'
        try:
            _extra = dict(extra) if extra else {}
            if tools:
                _extra['tools'] = tools

            content, usage = chat(
                model=slot.model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                thinking_enabled=thinking_enabled,
                effort=effort or preset,
                api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                extra=_extra or None,
                log_prefix=tag,
                max_retries=0,
                thinking_format=slot.thinking_format or '',
                provider_id=slot.provider_id or '',
                api_protocol=slot.protocol or 'openai',
                responses_feature_profile=(
                    getattr(slot, 'responses_profile', '') or 'compatible'),
                oauth=slot.oauth or '',
                adapter=slot.adapter or None,
            )
            latency = (time.time() - t0) * 1000
            _out_tokens = 0
            if isinstance(usage, dict):
                _out_tokens = (usage.get('completion_tokens')
                               or usage.get('output_tokens') or 0)
                try:
                    _out_tokens = int(_out_tokens)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[api] _race_worker caught %s: %s', type(_e_audit).__name__, _e_audit)
                    _out_tokens = 0
            slot.record_success(latency, output_tokens=_out_tokens)
            _note_shared_contention_recovered(dispatcher, slot, tag)
            return (content, usage, slot)
        except Exception as e:
            latency = (time.time() - t0) * 1000
            err_str = str(e)
            is_429 = '429' in err_str or 'rate' in err_str.lower()
            slot.record_error(is_rate_limit=is_429)
            raise

    with ThreadPoolExecutor(max_workers=n_race) as pool:
        futures = {pool.submit(_race_worker, s): s for s in slots}

        # Wait for the first successful result
        last_err = None
        done, pending = wait(futures, return_when=FIRST_COMPLETED)

        while done:
            for fut in done:
                try:
                    result = fut.result()
                    if result is not None:
                        content, usage, winner = result
                        cancel_event.set()
                        # Cancel pending futures
                        for p in pending:
                            p.cancel()
                        # Inject dispatch metadata
                        if isinstance(usage, dict):
                            usage['_dispatch'] = {
                                'key': winner.key_name,
                                'model': winner.model,
                                'key_tail': (winner.api_key or '')[-4:],
                                'provider_id': winner.provider_id,
                                'protocol': winner.protocol or 'openai',
                                'responses_profile': (
                                    winner.responses_profile or ''),
                                'latency_ms': round(winner.latency_ema),
                                'mode': 'race',
                            }
                        logger.debug('%s[Race] Winner: %s:%s', log_prefix, winner.key_name, winner.model)
                        return content, usage
                except Exception as e:
                    logger.debug('[Dispatch] race candidate failed: %s', e, exc_info=True)
                    last_err = e

            if pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
            else:
                break

        raise last_err or RuntimeError(
            'All %d race participants failed for capability=%s' % (n_race, capability))


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_parallel (fan-out for multiple tasks)
# ═══════════════════════════════════════════════════════════

def dispatch_parallel(tasks, *, capability='text', max_workers=4, log_prefix=''):
    """Execute multiple LLM tasks in parallel, distributing across slots.

    Args:
        tasks: List of dicts, each with:
            - 'messages': chat messages
            - 'max_tokens': (optional, default 4096)
            - 'temperature': (optional, default 0)
            - 'prefer_model': (optional)
            - 'extra': (optional)
        capability: Required capability for all tasks
        max_workers: Max concurrent requests

    Returns:
        List of (content, usage) tuples in the same order as tasks.
    """
    results = [None] * len(tasks)

    def _do_task(idx, task):
        try:
            content, usage = dispatch_chat(
                task['messages'],
                max_tokens=task.get('max_tokens', 4096),
                temperature=task.get('temperature', 0),
                capability=capability,
                prefer_model=task.get('prefer_model'),
                extra=task.get('extra'),
                log_prefix=f'{log_prefix}[P{idx}]',
            )
            return idx, (content, usage)
        except Exception as e:
            logger.debug('[Dispatch] parallel task[%d] failed (model=%s): %s', idx, task.get('prefer_model', '?'), e, exc_info=True)
            return idx, (None, {'error': str(e)})

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_do_task, i, t) for i, t in enumerate(tasks)]
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result

    return results


# ═══════════════════════════════════════════════════════════
#  Monitoring endpoint helpers
# ═══════════════════════════════════════════════════════════

def get_dispatch_status() -> dict:
    """Return current dispatcher status for monitoring/debugging."""
    d = get_dispatcher()
    slots_info = d.get_slots_info()
    return {
        'slots': slots_info,
        'total_slots': len(d.slots),
        'available_slots': sum(1 for s in d.slots if s.is_available),
        'by_capability': _group_by_capability(slots_info),
    }


def _group_by_capability(slots_info):
    """Group slot info by capability for easy overview."""
    caps = defaultdict(list)
    for s in slots_info:
        for c in s.get('capabilities', []):
            caps[c].append({
                'slot': f"{s['key']}:{s['model']}",
                'score': s['score'],
                'available': s['available'],
                'rpm_headroom_pct': s['rpm_headroom_pct'],
            })
    return dict(caps)


# ═══════════════════════════════════════════════════════════
#  Convenience — drop-in replacement for chat()
# ═══════════════════════════════════════════════════════════

def smart_chat(messages, *, model=None, max_tokens=4096, temperature=0,
               thinking_enabled=False, preset='low', effort=None,
               capability='text', tools=None, extra=None,
               log_prefix='', max_retries=3, timeout=None,
               exclude_models=None, abort_check=None, **_kw):
    """Drop-in replacement for ``lib.llm.chat()`` with auto dispatch.

    Uses the fastest available (key, model) slot across ALL keys.
    Falls back to direct ``chat()`` if dispatch fails entirely.

    Signature is intentionally close to ``chat()`` so call sites only
    need to change ``from lib.llm import chat`` →
    ``from lib.llm_dispatch import smart_chat as chat``.

    Extra kwargs (api_key, etc.) are silently ignored so callers that
    sometimes pass api_key don't break.
    """
    try:
        return dispatch_chat(
            messages, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, preset=preset,
            effort=effort, capability=capability,
            prefer_model=model, tools=tools, extra=extra,
            max_retries=max_retries, log_prefix=log_prefix,
            timeout=timeout,
            exclude_models=exclude_models,
            abort_check=abort_check,
        )
    except Exception as e:
        from lib.llm import AbortedError
        if isinstance(e, AbortedError):
            # Cancellation is terminal for this caller. Falling through to
            # direct chat would resurrect a timed-out background request and
            # keep consuming the shared model pool after its owner returned.
            raise
        # Ultimate fallback — direct call with default key
        logger.warning('%s[Dispatch] All slots exhausted (%s), '
                    'falling back to direct chat()', log_prefix, e, exc_info=True)
        from lib.llm import chat
        _fb_timeout = timeout
        # For 'cheap' tasks (translate etc.), fall back to a cheap model,
        #   NOT the default LLM_MODEL (which is Opus — way too slow/expensive).
        _fb_model = model
        if not _fb_model and capability == 'cheap':
            from lib import GEMINI_MODEL
            _fb_model = GEMINI_MODEL   # gemini-2.5-flash — fast & cheap
            logger.info('%s Fallback using cheap model: %s', log_prefix, _fb_model)
        # ── Audit trail: record the model switch on exhaustion fallback ──
        try:
            from lib.log import audit_log as _audit
            _audit('model_switch',
                   old=(model or '(dispatch-auto)'),
                   new=(_fb_model or '(lib.llm default)'),
                   reason='dispatch_exhausted',
                   capability=capability,
                   error=str(e)[:200])
        except Exception as _aerr:
            logger.debug('%s audit_log model_switch failed: %s', log_prefix, _aerr)
        return chat(messages=messages, model=_fb_model,
                    max_tokens=max_tokens, temperature=temperature,
                    thinking_enabled=thinking_enabled, effort=effort or preset,
                    log_prefix=log_prefix, timeout=_fb_timeout)


def smart_chat_batch(prompts, *, max_tokens=4096, temperature=0,
                     capability='text', log_prefix='',
                     max_concurrent=8, **kw):
    """Send multiple independent prompts concurrently via dispatch.

    Each prompt is dispatched to a different slot (potentially different
    keys and models), maximising throughput across all available RPM.

    Args:
        prompts: list of str | list of list[dict]  (raw text or messages)
        max_concurrent: max parallel workers (default 8)
        **kw: forwarded to dispatch_chat

    Returns:
        list of (content, usage) tuples — same order as input
    """
    import concurrent.futures

    def _to_messages(p):
        if isinstance(p, str):
            return [{'role': 'user', 'content': p}]
        return p  # already messages list

    results = [None] * len(prompts)

    def _worker(idx, msgs):
        try:
            return idx, dispatch_chat(
                msgs, max_tokens=max_tokens, temperature=temperature,
                capability=capability,
                log_prefix=f'{log_prefix}[batch:{idx}]', **kw)
        except Exception as e:
            logger.warning('%s[batch:%d] Failed: %s', log_prefix, idx, e, exc_info=True)
            return idx, (f'[Error] {e}', {})

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(prompts), max_concurrent)) as pool:
        futures = [pool.submit(_worker, i, _to_messages(p))
                   for i, p in enumerate(prompts)]
        for f in concurrent.futures.as_completed(futures):
            idx, result = f.result()
            results[idx] = result

    return results
