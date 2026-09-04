"""The dispatch_stream and async_dispatch_stream operations."""

from lib.llm.stream_result import (
    ProviderStreamResult,
    ensure_provider_stream_result,
)
from lib.llm_errors import (
    is_subscription_quota_error,
    parse_subscription_retry_after,
)
from .factory import get_dispatcher
import os
import time
from lib.log import get_logger
from lib.llm_dispatch._api_budget import _UNREACHABLE_COOLDOWN, _force_oauth_token_refresh, _gateway_outage_budget_secs, _raise_dispatch_exhausted, _raise_if_429_attempt_budget_exhausted, _record_route_missing_model, _saturation_budget_secs, _saturation_escalate, _validate_429_attempt_budget
from lib.llm_dispatch._api_contention import _admit_or_defer_shared_contention, _admit_or_defer_shared_contention_async, _shared_contention_retry_delay
from lib.llm_dispatch._api_errors import DispatchSharedContentionDeferred
from lib.llm_dispatch._api_stream_state import _StreamRetryState, _adapt_stream_body_for_slot, _cycling_can_ever_serve, _first_output_callbacks, _settle_stream_result, _sleep_and_record_queue_wait

logger = get_logger('lib.llm_dispatch.api')


def _notify_attempt_restart(callback, reason, *, log_prefix):
    """Fence a discarded partial attempt without trusting caller callbacks."""
    if callback is None:
        return
    try:
        callback(reason=reason)
    except Exception as exc:
        logger.debug('%s on_attempt_restart raised: %s', log_prefix, exc,
                     exc_info=True)


def _check_request_headroom(payload, *, log_prefix):
    """Reject only requests that exceed the cgroup's absolute headroom."""
    try:
        from lib import cgroup_guard
    except Exception as exc:
        logger.debug('%s cgroup guard unavailable: %s', log_prefix, exc,
                     exc_info=True)
        return
    try:
        approx_bytes = cgroup_guard.approx_body_bytes(payload)
        admitted, reason = cgroup_guard.check_request_headroom(
            ident=log_prefix or 'dispatch_stream', approx_bytes=approx_bytes)
        disabled = os.environ.get(
            'TOFU_CGROUP_REQUEST_GUARD', '1').lower() in {
                '0', 'off', 'false', 'no'}
        if not admitted and not disabled:
            raise cgroup_guard.MemoryPressureError(reason)
    except cgroup_guard.MemoryPressureError:
        raise
    except Exception as exc:
        logger.debug('%s cgroup headroom check skipped: %s', log_prefix, exc,
                     exc_info=True)


def dispatch_stream(body_or_messages, *, on_thinking=None, on_content=None,
                    on_tool_call_ready=None, on_before_tool_call_ready=None,
                    abort_check=None, max_tokens=4096, temperature=0,
                    thinking_enabled=False, preset='low', effort=None,
                    capability='text', prefer_model=None, tools=None,
                    max_retries=3, log_prefix='', strict_model=False,
                    on_retry=None, avoid_pairs=None,
                    exclude_models=None, on_attempt_restart=None,
                    on_waiting=None,
                    max_429_attempts: int | None = None,
                    defer_on_shared_contention: bool = False,
                    owner_user_id: int | None = None,
                    ) -> ProviderStreamResult:
    """Stream through the provider pool and return ``ProviderStreamResult``.

    Raw messages are built for the selected slot; a pre-built body is adapted
    per retry. 429s rotate cooled slots without consuming ``max_retries`` but
    may use the caller's explicit upstream-response budget. ``strict_model``
    keeps rotation inside the requested model, while ``avoid_pairs`` is a
    best-effort preference that relaxes rather than falsely reporting no slot.
    Waiting callbacks are liveness-only; retry callbacks represent a real
    failed request, and attempt-restart fences any partial streamed text.
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
    from lib.llm_errors import EndpointUnreachableError, ModelRouteMissingError, RequestScopedError

    def _fire_attempt_restart(reason: str) -> None:
        _notify_attempt_restart(
            on_attempt_restart, reason, log_prefix=log_prefix)

    max_429_attempts = _validate_429_attempt_budget(max_429_attempts)
    dispatcher = get_dispatcher()
    state = _StreamRetryState(exclude_models=exclude_models, avoid_pairs=avoid_pairs)
    # Fruit 2 (E2): at most ONE forced OAuth token refresh-retry per
    # request (see dispatch_chat).
    _oauth_refresh_used = False

    # Detect if it's a pre-built body or raw messages
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

    _check_request_headroom(body_or_messages, log_prefix=log_prefix)

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
                    durable_models=state.exclude_models_durable,
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

        # Prefix size is needed only by the same-conversation cache-write
        # visibility guard below. The retired cross-conversation admission gate
        # ran after a slot was already reserved, so it could not reroute work;
        # its waits only delayed the selected key without improving cache hits.
        _cache_conv_id = ''
        _est_tok = 0

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
                from lib.llm_dispatch.cache_settle import (
                    estimate_prefix_tokens,
                    settle_before_send,
                )
                from lib.llm_dispatch.conv_affinity import get_conv_affinity
                _cache_conv_id = get_conv_affinity() or ''
                _est_tok = estimate_prefix_tokens(body)
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

            # Reserve shared-project capacity only after the local cache wait;
            # spacing therefore measures upstream starts, not local delays.
            _admit_or_defer_shared_contention(
                dispatcher,
                slot,
                lambda seconds: _sleep_and_record_queue_wait(
                    state, seconds, abort_check=abort_check),
                tag,
                defer=defer_on_shared_contention,
            )
            t0 = time.time()
            (ttft_value, _on_thinking_wrapper,
             _on_content_wrapper,
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
                    owner_user_id=owner_user_id,
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

        except DispatchSharedContentionDeferred:
            slot.release()
            raise

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
            _credential_anomaly_exhausted = (
                state.note_credential_delivery_anomaly(e))
            _raise_if_429_attempt_budget_exhausted(
                max_429_attempts=max_429_attempts,
                upstream_attempts=state._429_count,
                last_error=e,
            )
            if _credential_anomaly_exhausted:
                logger.error(
                    '%s Credential-delivery anomaly persisted for %d actual '
                    'responses on %s — stopping bounded gateway rotation '
                    'without excluding the key or recording an auth failure',
                    log_prefix,
                    getattr(e, 'credential_delivery_anomaly_attempts', 0),
                    slot.model)
                raise
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
                if _force_oauth_token_refresh(
                        slot.oauth, log_prefix, owner_user_id):
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
            if slot.oauth and getattr(e, 'status_code', 0) == 401:
                state.note_permission_key(slot)
                logger.warning(
                    '%s OAuth credential rejected on %s — excluding whole key',
                    log_prefix, slot.key_name)
            # A 403 may be model-scoped. Preserve pair-level rotation and
            # escalate only after every model on the key rejects it.
            elif not state.note_permission_pair(
                    slot, dispatcher, capability, log_prefix):
                logger.warning(
                    '%s Permission denied on %s:%s — excluding pair, '
                    'remaining slots: %s',
                    log_prefix, slot.key_name, slot.model,
                    dispatcher.summarize_slots(capability))
            _fire_attempt_restart('permission denied — rotating slot')

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
            _fire_attempt_restart('endpoint unreachable — rotating slot')

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

        except ModelRouteMissingError as e:
            # HTTP 400 route-missing (AIGC "不支持的模型类型") — the
            #   gateway does not serve this MODEL at all: every remaining
            #   key would reject identically, so pair-exclusion would burn
            #   attempts and risk this 400 masking the actionable one.
            #   Exclude the model durably for this dispatch and process-locally
            #   until dispatcher rebuild/catalog refresh. Release only — not a
            #   key-health signal.
            slot.release()
            state.note_route_missing_model(slot.model, e)
            _record_route_missing_model(dispatcher, slot, e)
            logger.warning('%s Model %s has no route on this gateway '
                           '(HTTP 400) — excluded model, trying next: %.300s',
                           log_prefix, slot.model, str(e))
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Upstream error', status_code=400)
            _fire_attempt_restart('model route missing — rotating slot')

        except BadRequestError as e:
            # Deterministic HTTP 400 — PAYLOAD-level, not slot health (see
            # the sync dispatch_chat branch). Release + pair-exclude only.
            slot.release()
            state.last_err = e
            if state.first_bad_request_err is None:
                state.first_bad_request_err = e
            state.exclude_pairs.add((slot.key_name, slot.model))
            state.hard_attempts += 1
            logger.warning('%s Bad request (HTTP 400, deterministic) on %s:%s '
                           '— released slot, excluded pair, trying next: %.500s',
                           log_prefix, slot.key_name, slot.model, str(e))
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Upstream error', status_code=400)
            _fire_attempt_restart('bad request — rotating slot')

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
                _raise_if_429_attempt_budget_exhausted(
                    max_429_attempts=max_429_attempts,
                    upstream_attempts=state._429_count,
                    last_error=e,
                )
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
            _fire_attempt_restart('stream failure — rotating slot')
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
                              first_err=state.first_bad_request_err,
                              what='dispatch_stream')


async def async_dispatch_stream(body_or_messages, *, on_thinking=None,
                                on_content=None, on_tool_call_ready=None,
                                on_attempt_restart=None,
                                abort_check=None, max_tokens=4096, temperature=0,
                                thinking_enabled=False, preset='low', effort=None,
                                capability='text', prefer_model=None, tools=None,
                                max_retries=3, log_prefix='', strict_model=False,
                                on_retry=None, avoid_pairs=None,
                                exclude_models=None,
                                on_waiting=None,
                                max_429_attempts: int | None = None,
                                defer_on_shared_contention: bool = False,
                                owner_user_id: int | None = None,
                                ) -> ProviderStreamResult:
    """Native-async streaming dispatch — non-blocking on the event loop.

    Unlike the previous ``to_thread(dispatch_stream)`` stopgap, this drives the
    genuinely-async ``async_stream_chat`` (httpx) transport, so the streaming
    HTTP call runs ON the event loop without occupying a thread-pool worker.
    Slot selection, retry/429 cycling, and exclusion logic mirror the sync
    ``dispatch_stream`` loop; body adaptation is shared via
    ``_adapt_stream_body_for_slot``.

    Same signature and return shape as ``dispatch_stream``:
        (msg: str, finish_reason: str, usage: dict)

    This is the production transport for the on-loop
    ``/api/v1/chat/stream-direct`` relay. Task-backed chat remains off-loop and
    correctly uses the synchronous sibling. Both paths share retry state, body
    adaptation, headroom admission, and attempt-restart fencing.
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
    from lib.llm_errors import EndpointUnreachableError, ModelRouteMissingError, RequestScopedError

    def _fire_attempt_restart(reason: str) -> None:
        _notify_attempt_restart(
            on_attempt_restart, reason, log_prefix=log_prefix)

    max_429_attempts = _validate_429_attempt_budget(max_429_attempts)
    dispatcher = get_dispatcher()
    state = _StreamRetryState(exclude_models=exclude_models, avoid_pairs=avoid_pairs)
    # Fruit 2 (E2): at most ONE forced OAuth token refresh-retry per
    # request (see dispatch_chat).
    _oauth_refresh_used = False
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

    _check_request_headroom(body_or_messages, log_prefix=log_prefix)

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

        # Async parity for the bounded warm-key hold in dispatch_stream.
        # Waiting is event-loop friendly and happens at most once per dispatch;
        # long provider/quota cooldowns still fall through to cold-key rebind.
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
                            _budget_s = sticky_hold_budget_ms() / 1000.0
                            _ceiling_s = sticky_hold_max_ms() / 1000.0
                            if 0 < _remaining_s <= _ceiling_s:
                                _wait = min(
                                    _remaining_s + 0.05, _ceiling_s)
                                _kind = (
                                    'short 429 cooldown'
                                    if _remaining_s <= _budget_s
                                    else 'contention on sole warm key '
                                         '(escalated hold)'
                                )
                                logger.info(
                                    '%s async_dispatch_stream: holding %.2fs '
                                    'for conv=%s warm key %s (%s) to keep '
                                    'prompt cache warm',
                                    log_prefix, _wait, _conv[:8], _warm_key,
                                    _kind)
                                if abort_check and abort_check():
                                    raise AbortedError(
                                        'Aborted during warm-key hold')
                                _wait_started = time.monotonic()
                                await async_abortable_sleep(
                                    _wait, abort_check)
                                state.record_queue_wait(_wait_started)
                                state._warm_held = True
                            else:
                                logger.debug(
                                    '%s async_dispatch_stream: warm key %s '
                                    'cooldown %.1fs exceeds hold ceiling %.1fs '
                                    '— genuine backoff, rebinding cold',
                                    log_prefix, _warm_key, _remaining_s,
                                    _ceiling_s)
            except ImportError as error:
                logger.debug('%s async warm-key hold unavailable: %s',
                             log_prefix, error)

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
                    durable_models=state.exclude_models_durable,
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
                from lib.llm_dispatch.cache_settle import (
                    async_settle_before_send,
                    estimate_prefix_tokens,
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
            await _admit_or_defer_shared_contention_async(
                dispatcher,
                slot,
                state=state,
                abort_check=abort_check,
                async_sleep_fn=async_abortable_sleep,
                log_prefix=tag,
                defer=defer_on_shared_contention,
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
                on_attempt_restart=on_attempt_restart,
                abort_check=abort_check, log_prefix=tag,
                on_stream_wait=_on_stream_wait,
                api_protocol=slot.protocol or 'openai',
                owner_user_id=owner_user_id))
            latency = (time.time() - t0) * 1000
            _settle_stream_result(
                slot, usage, latency=latency, ttft=ttft_value[0], state=state,
                cache_conv_id=_cache_conv_id, tag=tag, dispatcher=dispatcher,
                stream_result=stream_result)
            logger.debug('%s async_dispatch_stream settled: state=%s '
                         'finish=%s model=%s latency=%.0fms', log_prefix,
                         stream_result.state.value, finish, slot.model, latency)
            return stream_result

        except DispatchSharedContentionDeferred:
            slot.release()
            raise

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
                _fire_attempt_restart('key balance exhausted')
                continue
            state.note_free_429(is_gateway=bool(getattr(e, 'is_gateway', False)))
            _credential_anomaly_exhausted = (
                state.note_credential_delivery_anomaly(e))
            _raise_if_429_attempt_budget_exhausted(
                max_429_attempts=max_429_attempts,
                upstream_attempts=state._429_count,
                last_error=e,
            )
            if _credential_anomaly_exhausted:
                logger.error(
                    '%s Credential-delivery anomaly persisted for %d actual '
                    'responses on %s — stopping bounded async gateway '
                    'rotation without excluding the key or recording an auth '
                    'failure',
                    log_prefix,
                    getattr(e, 'credential_delivery_anomaly_attempts', 0),
                    slot.model)
                raise
            if on_retry:
                if _is_gateway:
                    on_retry(attempt=state._429_count,
                             reason='Upstream error',
                             status_code=int(getattr(e, 'status_code', 0) or 0))
                else:
                    on_retry(attempt=state._429_count, reason='Rate limited (429)', status_code=429)
            _fire_attempt_restart('rate limited (429) — rotating slot')
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
                if _force_oauth_token_refresh(
                        slot.oauth, log_prefix, owner_user_id):
                    slot.release()  # refresh-retry — not a slot-health signal
                    logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) '
                                   '— token refreshed, retrying once',
                                   log_prefix, slot.key_name, slot.model,
                                   slot.oauth)
                    _fire_attempt_restart(
                        'oauth token refreshed — retrying')
                    continue
                logger.warning('%s Auth rejected on OAuth slot %s:%s (%s) — '
                               'forced refresh failed; normal failover',
                               log_prefix, slot.key_name, slot.model, slot.oauth)
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            if slot.oauth and getattr(e, 'status_code', 0) == 401:
                state.note_permission_key(slot)
                logger.warning(
                    '%s OAuth credential rejected on %s — excluding whole key',
                    log_prefix, slot.key_name)
            elif not state.note_permission_pair(
                    slot, dispatcher, capability, log_prefix):
                logger.warning('%s Permission denied on %s:%s — excluding pair',
                               log_prefix, slot.key_name, slot.model)
            _fire_attempt_restart('permission denied — rotating slot')

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
            _fire_attempt_restart('endpoint unreachable — rotating slot')

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

        except ModelRouteMissingError as e:
            # HTTP 400 route-missing (AIGC "不支持的模型类型") — the
            #   gateway does not serve this MODEL at all: every remaining
            #   key would reject identically, so pair-exclusion would burn
            #   attempts and risk this 400 masking the actionable one.
            #   Exclude the model durably for this dispatch and process-locally
            #   until dispatcher rebuild/catalog refresh. Release only — not a
            #   key-health signal.
            slot.release()
            state.note_route_missing_model(slot.model, e)
            _record_route_missing_model(dispatcher, slot, e)
            logger.warning('%s Model %s has no route on this gateway '
                           '(HTTP 400) — excluded model, trying next: %.300s',
                           log_prefix, slot.model, str(e))
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Upstream error', status_code=400)
            _fire_attempt_restart('model route missing — rotating slot')

        except BadRequestError as e:
            # Deterministic HTTP 400 — PAYLOAD-level, not slot health (see
            # the sync dispatch_chat branch). Release + pair-exclude only.
            slot.release()
            state.last_err = e
            if state.first_bad_request_err is None:
                state.first_bad_request_err = e
            state.exclude_pairs.add((slot.key_name, slot.model))
            state.hard_attempts += 1
            logger.warning('%s Bad request (HTTP 400, deterministic) on %s:%s '
                           '— released slot, excluded pair, trying next: %.500s',
                           log_prefix, slot.key_name, slot.model, str(e))
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Upstream error', status_code=400)
            _fire_attempt_restart('bad request — rotating slot')

        except Exception as e:
            # Subscription-quota/capacity signal via a non-429 wrapper —
            #   rate-limit class with parsed reset duration.
            if is_subscription_quota_error(str(e)):
                _ra = parse_subscription_retry_after(str(e))
                slot.record_error(is_rate_limit=True, cooldown_s=_ra,
                                  error=str(e)[:200])
                state.last_err = e
                state.note_free_429()
                _raise_if_429_attempt_budget_exhausted(
                    max_429_attempts=max_429_attempts,
                    upstream_attempts=state._429_count,
                    last_error=e,
                )
                logger.warning('%s Subscription quota/capacity signal on %s:%s '
                               '(cooldown %ss) — rotating slot: %s',
                               log_prefix, slot.key_name, slot.model,
                               f'{_ra:.0f}' if _ra else '0.5', str(e)[:160])
                if on_retry:
                    on_retry(attempt=state._429_count,
                             reason='Subscription quota reached',
                             status_code=429)
                _fire_attempt_restart(
                    'subscription quota reached — rotating slot')
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
            _fire_attempt_restart('stream failure — rotating slot')
            logger.debug('%s async_dispatch_stream error: %s — next slot',
                         tag, str(e)[:200], exc_info=True)

    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              first_err=state.first_bad_request_err,
                              what='async_dispatch_stream')
