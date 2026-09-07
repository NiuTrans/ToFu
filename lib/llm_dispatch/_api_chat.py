"""Key-pick convenience and the non-streaming dispatch_chat operation."""

from lib.llm_errors import (
    is_subscription_quota_error,
    parse_subscription_retry_after,
)
from .factory import get_dispatcher
import time
from lib.log import get_logger
from lib.llm_dispatch._api_budget import _UNREACHABLE_COOLDOWN, _force_oauth_token_refresh, _raise_dispatch_exhausted, _raise_if_429_attempt_budget_exhausted, _record_route_missing_model, _remember_route_missing_error, _saturation_budget_secs, _saturation_escalate, _unix_time_ns, _validate_429_attempt_budget
from lib.llm_dispatch._api_contention import _admit_or_defer_shared_contention, _note_shared_contention_recovered, _shared_contention_retry_delay
from lib.llm_dispatch._api_errors import DispatchSharedContentionDeferred
from lib.llm_dispatch._api_hygiene import (
    _advance_credential_delivery_anomaly,
    _audit_severity_downgrade,
)
from lib.llm_dispatch._api_stream_state import _cycling_can_ever_serve

logger = get_logger('lib.llm_dispatch.api')


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


def dispatch_chat(messages, *, max_tokens=4096, temperature=0,
                  thinking_enabled=False, preset='low', effort=None,
                  capability='text', prefer_model=None, tools=None,
                  extra=None, max_retries=3, log_prefix='',
                  timeout=None, strict_model=False,
                  exclude_models=None, abort_check=None,
                  max_429_attempts: int | None = None,
                  defer_on_shared_contention: bool = False,
                  owner_user_id: int | None = None):
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
        defer_on_shared_contention: For reconstructible work, yield before
            transport when every eligible family is behind a known gate.

    Returns:
        (content_text: str, usage_dict: dict)
    """
    from lib.llm import AbortedError, BadRequestError, ContentFilterError, InvalidImageError, PermissionError_, PromptTooLongError, RateLimitError, StreamOnlyError, chat
    from lib.llm._transport import abortable_sleep
    from lib.llm_errors import EndpointUnreachableError, ModelRouteMissingError, RequestScopedError

    # 2026-05-05 config-surface change (CLAUDE.md §10): per-cycle 429
    # severity downgraded from WARNING → INFO (routine backpressure; the
    # dispatch handler already rotates to the next key). Only final key
    # exhaustion stays WARNING/ERROR. Audited here on first call so the
    # change is discoverable in audit.log without a code-search.
    _audit_severity_downgrade()
    max_429_attempts = _validate_429_attempt_budget(max_429_attempts)
    dispatcher = get_dispatcher()
    exclude = set(exclude_models) if exclude_models else set()
    exclude_keys = set()      # keys to exclude entirely (transient classes)
    exclude_pairs = set()     # (key_name, model) pairs to exclude (transient classes)
    # Durable counterparts (route/permission/quota) — survive the 60s reset;
    # see _StreamRetryState.exclude_*_durable for the rationale.
    exclude_keys_durable = set()
    exclude_pairs_durable = set()
    exclude_models_durable = set()
    last_err = None
    # First deterministic payload HTTP 400 (never route-missing) — see
    # _StreamRetryState.first_bad_request_err.
    first_bad_request_err = None
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
    _credential_delivery_anomaly_count = 0
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
                    durable_models=exclude_models_durable,
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
            _admit_or_defer_shared_contention(
                dispatcher,
                slot,
                _queue_sleep,
                tag,
                defer=defer_on_shared_contention,
            )
        except AbortedError:
            slot.release()
            logger.debug('%s User aborted during contention admission', tag)
            raise
        except DispatchSharedContentionDeferred:
            slot.release()
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
                provider_id=(getattr(slot, 'routing_provider_id', '')
                             or slot.provider_id or ''),
                api_protocol=slot.protocol or 'openai',
                responses_feature_profile=(
                    getattr(slot, 'responses_profile', '') or 'compatible'),
                oauth=slot.oauth or '',
                adapter=slot.adapter or None,
                owner_user_id=owner_user_id,
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
                    'provider_id': (getattr(slot, 'routing_provider_id', '')
                                    or slot.provider_id),
                    'route_snapshot': dict(
                        getattr(slot, 'route_snapshot', {}) or {}),
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
            (_credential_delivery_anomaly_count,
             _credential_anomaly_exhausted) = (
                _advance_credential_delivery_anomaly(
                    e, _credential_delivery_anomaly_count))
            _raise_if_429_attempt_budget_exhausted(
                max_429_attempts=max_429_attempts,
                upstream_attempts=_upstream_429_retries,
                last_error=e,
            )
            if _credential_anomaly_exhausted:
                logger.error(
                    '%s Credential-delivery anomaly persisted for %d actual '
                    'responses on %s — stopping bounded non-stream gateway '
                    'rotation without excluding the key or recording an auth '
                    'failure',
                    log_prefix, _credential_delivery_anomaly_count, slot.model)
                raise
            if _sat_start is None and not _is_gateway:
                _sat_start = time.monotonic()
            # Don't exclude anything. Generic per-key 429s receive the 0.5s
            # slot cooldown and keep the 0.3s baseline. Typed project/app
            # contention leaves the warm key eligible; its provider/model
            # admission gate exclusively owns pacing and cache-safe probes.
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
                if _force_oauth_token_refresh(
                        slot.oauth, log_prefix, owner_user_id):
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
            hard_attempts += 1
            if slot.oauth and getattr(e, 'status_code', 0) == 401:
                # Every model on an OAuth provider row shares this bearer
                # credential. A 401 is key-wide; sibling-model retries only
                # replay the same rejected token.
                exclude_keys_durable.add(slot.key_name)
                logger.warning(
                    '%s OAuth credential rejected on %s — excluding whole key',
                    log_prefix, slot.key_name)
                continue
            # A 403 can be a model entitlement, so keep pair-level handling.
            exclude_pairs_durable.add((slot.key_name, slot.model))
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

        except ModelRouteMissingError as e:
            # HTTP 400 route-missing (AIGC "不支持的模型类型") — the
            #   gateway does not serve this MODEL at all: every remaining
            #   key would reject identically, so pair-exclusion would burn
            #   attempts and risk this 400 masking the actionable one.
            #   Exclude the model durably for this dispatch and process-locally
            #   until dispatcher rebuild/catalog refresh. Release only — not a
            #   key-health signal.
            slot.release()
            last_err = _remember_route_missing_error(last_err, e)
            exclude.add(slot.model)
            exclude_models_durable.add(slot.model)
            _record_route_missing_model(dispatcher, slot, e)
            hard_attempts += 1
            logger.warning('%s Model %s has no route on this gateway '
                           '(HTTP 400) — excluded model, trying next: %.300s',
                           log_prefix, slot.model, str(e))

        except BadRequestError as e:
            # Deterministic HTTP 400 — a PAYLOAD-level rejection, not slot
            # health. Release the slot (no consecutive_errors → no 300s
            # lockout, no key_stats feed — the ContentFilterError /
            # InvalidImageError precedent) and exclude only the PAIR: a 400
            # CAN be key-specific, so the remaining keys each get one try;
            # exhaustion falls through to the turn-level model fallback.
            slot.release()
            last_err = e
            if first_bad_request_err is None:
                first_bad_request_err = e
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
                _raise_if_429_attempt_budget_exhausted(
                    max_429_attempts=max_429_attempts,
                    upstream_attempts=_upstream_429_retries,
                    last_error=e,
                )
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
                              first_err=first_bad_request_err,
                              what='dispatch')
