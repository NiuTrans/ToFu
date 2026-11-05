"""Synchronous and asynchronous streaming dispatch orchestration.

Selection, body preparation, transport and cancellable waits stay in each lane;
DispatchFailurePolicy owns their common failed-attempt decisions and notifications.
"""

from lib.llm.stream_result import (
    ProviderStreamResult,
    ensure_provider_stream_result,
)
from .factory import get_dispatcher
import logging
import time

from ._attempt_timing import AttemptObserver
from ._api_failures import DispatchFailurePolicy, _notify_retry
from lib.log import get_logger

from lib.llm.diagnostic_timing import timed_call, timed_call_with_fields, timed_stage
from lib.llm_dispatch._api_budget import _gateway_outage_budget_secs, _raise_dispatch_exhausted, _saturation_budget_secs, _saturation_escalate, _validate_429_attempt_budget
from lib.llm_dispatch._api_contention import _admit_or_defer_shared_contention, _admit_or_defer_shared_contention_async
from ._api_retry_state import DispatchRetryState as _StreamRetryState, _cycling_can_ever_serve, responses_state_bound_pairs
from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot, _first_output_callbacks, _settle_stream_result, _sleep_and_record_queue_wait

logger = get_logger('lib.llm_dispatch.api')




def _dominant_cooldown_cause(dispatcher, capability, state) -> str:
    """Best-effort dominant cooldown cause for the wait HUD ('' on failure).

    Reads the dispatcher's slot-health view; must never break the wait loop.
    """
    try:
        return dispatcher.dominant_cooldown_cause(
            capability,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys(),
            exclude_pairs=state.eff_exclude_pairs())
    except Exception as error:
        logger.debug('dominant cooldown cause unavailable: %s', error)
        return ''


def _notify_attempt_restart(callback, reason, *, log_prefix):
    """Fence a discarded partial attempt without trusting caller callbacks."""
    if callback is None:
        return
    try:
        callback(reason=reason)
    except Exception as exc:
        logger.debug('%s on_attempt_restart raised: %s', log_prefix, exc,
                     exc_info=True)


class ProviderReportedNetworkError(Exception):
    """Provider ended a well-formed stream with finish_reason='network_error'."""


def _provider_reported_network_failure(stream_result, msg, finish) -> bool:
    """True only for the observed provider-signalled transport failure shape.

    zai-class providers can terminate an otherwise well-formed SSE stream with
    an explicit ``finish_reason='network_error'`` (observed in tb21 v11
    evidence: 230 bytes, 2 SSE frames, finish+DONE both seen, zero content).
    Accepting it promoted an upstream transport failure into a successful
    empty answer and silently ended the agent turn. Only the zero-actionable
    -content shape is retryable here: a turn that already streamed content or
    tool calls keeps its historical handling, since retrying partial output
    would change production semantics for a case not observed failing.
    """
    if str(finish or '') != 'network_error':
        return False
    if not isinstance(msg, dict):
        return False
    if msg.get('tool_calls'):
        return False
    content = msg.get('content')
    if isinstance(content, str) and content.strip():
        return False
    if isinstance(content, (list, tuple)) and any(
            str(part or '').strip() for part in content):
        return False
    return True


def _stream_wait_notifier(on_waiting, slot, tag, on_retry=None, strict_model=True):
    """Bounded on_waiting wrapper: a raising listener must never kill the stream."""
    if not on_waiting and not on_retry:
        return None

    def _notify(status, _slot=slot):
        try:
            if status.kind == 'network_recovered' and on_retry:
                _notify_retry(on_retry, _slot, strict_model=strict_model,
                    attempt=0, reason='Network recovered', status_code=0)
            elif status.kind == 'network_recovery' and on_retry:
                _notify_retry(on_retry, _slot, strict_model=strict_model,
                    attempt=0, reason='Waiting for network recovery', status_code=0)
            elif on_waiting:
                on_waiting(status=status, slot=_slot)
        except Exception as exc:
            logger.debug('%s on_waiting raised: %s', tag, exc)

    return _notify


def _rotate_slot_for_provider_network_failure(state, slot, log_prefix,
                                              max_retries, on_retry,
                                              strict_model,
                                              fire_attempt_restart) -> None:
    """Rotate the slot after a provider-reported network_error with zero
    actionable content: one bounded non-429 retry, no pair/model exclusion —
    the endpoint is reachable, the failure is transient.
    """
    state.hard_attempts += 1
    state.last_err = ProviderReportedNetworkError(
        'provider stream ended with finish_reason=network_error '
        'and zero actionable content')
    slot.record_error(is_rate_limit=False, error='provider network_error')
    logger.warning(
        '%s Provider reported network_error on %s:%s with no '
        'actionable content — rotating slot (attempt %d/%d)',
        log_prefix, slot.key_name, slot.model,
        state.hard_attempts, max_retries)
    _notify_retry(
        on_retry, slot, strict_model=strict_model,
        attempt=state.hard_attempts,
        reason='Provider network error', status_code=0)
    fire_attempt_restart('provider network_error — rotating slot')


def _check_stream_outage_policy(state, log_prefix, prefer_model):
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


def _hold_for_warm_sticky_key(state, dispatcher, *, prefer_model, abort_check,
                              log_prefix) -> None:
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


def _handle_empty_slot_pick(state, dispatcher, *, capability, prefer_model,
                            strict_model, abort_check, on_waiting, log_prefix,
                            max_retries) -> bool:
    """Return True to pick again, False when no capable slot remains."""
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
        return True
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
    _held_routes = state.route_unavailable.pairs
    _slots_exist = dispatcher.has_capable_slots(
        capability, exclude_models=state.exclude,
        exclude_keys=(state.exclude_keys | state.exclude_keys_durable
                      | state.credential_outage.keys),
        exclude_pairs=(state.exclude_pairs | state.exclude_pairs_durable
                       | _held_routes),
        prefer_model=prefer_model if strict_model else None)
    # Exit iff even the HEALABLE pool is empty (caller bans + durable
    # exclusions only). `_429_count > 0` used to keep this alive
    # forever — note_cooldown_cycle() increments it, so the condition
    # was self-sustaining once any cycle ran (233daa6 CI hang).
    if _slots_exist or _cycling_can_ever_serve(
            dispatcher, capability,
            initial_exclude_models=state._initial_exclude_models,
            durable_models=state.exclude_models_durable,
            durable_keys=(state.exclude_keys_durable
                          | state.credential_outage.keys),
            durable_pairs=state.exclude_pairs_durable,
            strict_model=strict_model, prefer_model=prefer_model):
        if not _slots_exist and _held_routes:
            # Only no-channel routes remain: probe them again on the
            # fixed backoff instead of polling the gateway.
            if state.credential_outage.retry_cooled():
                return True
            _sleep_and_record_queue_wait(
                state, state.route_unavailable.probe_delay_s(),
                abort_check=abort_check)
            return True
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
                on_waiting(
                    status=state.wait_status(
                        cooldown_cause=_dominant_cooldown_cause(
                            dispatcher, capability, state)),
                    slot=None)
            except Exception as _ore:
                logger.debug('%s on_waiting (cooldown) raised: %s',
                             log_prefix, _ore)
        if state._cooldown_cycles % 20 == 0:
            logger.info(
                '%s dispatch_stream: still cycling (slots cooling, %d times, '
                'strict=%s), waiting for cooldown to expire…',
                log_prefix, state._cooldown_cycles, strict_model)
        return True
    if state.credential_outage.retry_cooled():
        return True
    logger.warning(
        '%s dispatch_stream: NO CAPABLE SLOT on attempt %d/%d. '
        'exclude_models=%s exclude_keys=%s exclude_pairs=%s strict_model=%s. '
        'Available slots: %s',
        log_prefix, state.hard_attempts + 1, max_retries,
        state.exclude, state.exclude_keys, state.exclude_pairs, strict_model,
        dispatcher.summarize_slots(capability))
    return False


@timed_stage('dispatch_total')
def dispatch_stream(body_or_messages, *, on_thinking=None, on_content=None,
                    on_tool_call_ready=None, on_before_tool_call_ready=None,
                    abort_check=None, max_tokens=4096, temperature=0,
                    thinking_enabled=None, preset='low', effort=None,
                    thinking_depth=None,
                    capability='text', prefer_model=None, tools=None, tool_choice=None, generation_options=None,
                    max_retries=3, log_prefix='', strict_model=False,
                    on_retry=None, avoid_pairs=None,
                    exclude_models=None, on_attempt_restart=None, on_provider_attempt=None,
                    on_waiting=None,
                    max_429_attempts: int | None = None,
                    defer_on_shared_contention: bool = False,
                    owner_user_id: int | None = None,
                    model_budget_admit=None,
                    attempt_timeout=None,
                    first_byte_timeout=None,
                    ) -> ProviderStreamResult:
    """Stream through the provider pool and return ``ProviderStreamResult``.

    Raw messages are built for the selected slot; a pre-built body is adapted
    per retry. 429s rotate cooled slots without consuming ``max_retries`` but
    may use the caller's explicit upstream-response budget. ``strict_model``
    keeps rotation inside the requested model, while ``avoid_pairs`` is a
    best-effort preference that relaxes rather than falsely reporting no slot.
    Waiting callbacks are liveness-only; retry callbacks represent a real
    failed request, and attempt-restart fences any partial streamed text.
    ``attempt_timeout`` / ``first_byte_timeout`` are the caller's own hard
    bounds on one physical attempt (see ``stream_chat``); they are orthogonal
    to ``max_retries`` rotation and keep a silent slot from consuming a
    bounded caller's entire budget.
    """
    from lib.llm import (
        stream_chat,
    )

    def _fire_attempt_restart(reason: str) -> None:
        _notify_attempt_restart(
            on_attempt_restart, reason, log_prefix=log_prefix)

    attempt_observer = AttemptObserver(on_provider_attempt)
    max_429_attempts = _validate_429_attempt_budget(max_429_attempts)
    dispatcher = timed_call('route_resolution', get_dispatcher)
    state = _StreamRetryState(
        exclude_models=exclude_models, avoid_pairs=avoid_pairs,
        dispatcher=dispatcher, owner_user_id=owner_user_id,
        state_bound_pairs=responses_state_bound_pairs(
            dispatcher, body_or_messages, prefer_model=prefer_model,
            log_prefix=log_prefix))
    # Fruit 2 (E2): at most ONE forced OAuth token refresh-retry per
    # request (see dispatch_chat).
    failure_policy = DispatchFailurePolicy(
        state, dispatcher, capability=capability, strict_model=strict_model,
        max_429_attempts=max_429_attempts, owner_user_id=owner_user_id,
        log_prefix=log_prefix, on_retry=on_retry, restart=_fire_attempt_restart,
        attempt_observer=attempt_observer)

    # Detect if it's a pre-built body or raw messages
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

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

        _check_stream_outage_policy(state, log_prefix, prefer_model)

        # Log available slots at start of each attempt for debugging. The
        # summary scores every selectable slot under the dispatcher lock, so
        # it is built only when this debug line is actually emitted.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                '%s dispatch_stream attempt hard=%d/%d 429=%d cooldown_polls=%d: '
                'slots: %s',
                log_prefix, state.hard_attempts + 1, max_retries,
                state._429_count, state._cooldown_cycles,
                dispatcher.summarize_slots(capability))

        # Periodically reset hard-error exclusions during 429 cycling
        #   (502/timeout may be transient; give recovered slots another chance).
        state.maybe_reset_exclusions(log_prefix, 'dispatch_stream')

        _hold_for_warm_sticky_key(
            state, dispatcher, prefer_model=prefer_model,
            abort_check=abort_check, log_prefix=log_prefix)

        pick_started = time.monotonic()
        slot = timed_call('route_selection', dispatcher.pick_and_reserve,
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys(),
            exclude_pairs=state.eff_exclude_pairs(),
            strict_model=strict_model)
        state.record_route_selection(pick_started)
        if slot is None:
            if _handle_empty_slot_pick(
                    state, dispatcher, capability=capability,
                    prefer_model=prefer_model, strict_model=strict_model,
                    abort_check=abort_check, on_waiting=on_waiting,
                    log_prefix=log_prefix, max_retries=max_retries):
                continue
            break

        tag = f'{log_prefix}[D:{slot.key_name}:{slot.model}]'

        # A re-pick with no recorded failure cause is a scheduler re-route
        # (re-score / retry on a healthy slot). Record it so the banner can
        # say WHY the route changed instead of showing bare internal IDs.
        if attempt_observer.ordinal > 0 and not attempt_observer.route_trace._pending_cause:
            attempt_observer.note_cause('route_rescore')

        # Build this slot's provider-specific body through the shared adapter.
        from lib.llm.request import request_preparation
        try:
            with request_preparation(stage='slot_body_adaptation'):
                body = _adapt_stream_body_for_slot(
                    slot, body_or_messages, is_body,
                    tools=tools, tool_choice=tool_choice,
                    generation_options=generation_options,
                    max_tokens=max_tokens, temperature=temperature,
                    thinking_enabled=thinking_enabled, preset=preset,
                    effort=effort, thinking_depth=thinking_depth,
                    model_budget_admit=model_budget_admit)
        except BaseException:
            slot.release()
            raise

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
            _on_stream_wait = _stream_wait_notifier(
                on_waiting, slot, tag, on_retry, strict_model)
            stream_result = msg, finish, usage = \
                ensure_provider_stream_result(timed_call_with_fields('provider_call', {
                    'model': slot.model,
                    'provider_id': getattr(slot, 'routing_provider_id', '') or slot.provider_id,
                    'protocol': slot.protocol or 'openai',
                }, attempt_observer.call, stream_chat, slot,
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
                    # Only forwarded when set: interactive callers (and every
                    # transport double that mirrors `stream_chat`) must see the
                    # historical call shape unchanged.
                    **({'attempt_timeout': attempt_timeout}
                       if attempt_timeout is not None else {}),
                    **({'first_byte_timeout': first_byte_timeout}
                       if first_byte_timeout is not None else {}),
                ))
            if _provider_reported_network_failure(stream_result, msg, finish):
                _rotate_slot_for_provider_network_failure(
                    state, slot, log_prefix, max_retries, on_retry,
                    strict_model, _fire_attempt_restart)
                continue
            latency = (time.time() - t0) * 1000
            _settle_stream_result(
                slot, usage, latency=latency, ttft=ttft_value[0],
                state=state, cache_conv_id=_cache_conv_id, tag=tag,
                dispatcher=dispatcher, stream_result=stream_result,
                route_snapshot=attempt_observer.route_trace.snapshot())
            if stream_result.is_verified_complete:
                state.credential_outage.note_success(slot)
            state.route_unavailable.note_success(slot)
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

        except BaseException as error:
            retry_delay = failure_policy.handle(error, slot, tag=tag)
            if retry_delay > 0:
                _sleep_and_record_queue_wait(state, retry_delay, abort_check=abort_check)

    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              first_err=state.first_bad_request_err,
                              what='dispatch_stream')


async def async_dispatch_stream(body_or_messages, *, on_thinking=None,
                                on_content=None, on_tool_call_ready=None,
                                on_attempt_restart=None, on_provider_attempt=None,
                                abort_check=None, max_tokens=4096, temperature=0,
                                thinking_enabled=None, preset='low', effort=None,
                                thinking_depth=None,
                                capability='text', prefer_model=None, tools=None, tool_choice=None, generation_options=None,
                                max_retries=3, log_prefix='', strict_model=False,
                                on_retry=None, avoid_pairs=None,
                                exclude_models=None,
                                on_waiting=None,
                                max_429_attempts: int | None = None,
                                defer_on_shared_contention: bool = False,
                                owner_user_id: int | None = None,
                                model_budget_admit=None,
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
    adaptation, and attempt-restart fencing.
    """
    from lib.llm import (
        AbortedError,
    )
    from lib.llm._transport import async_abortable_sleep
    from lib.llm.astream import async_stream_chat


    async def _observed_transport(*args, **kwargs):
        return ensure_provider_stream_result(await async_stream_chat(*args, **kwargs))

    def _fire_attempt_restart(reason: str) -> None:
        _notify_attempt_restart(
            on_attempt_restart, reason, log_prefix=log_prefix)

    attempt_observer = AttemptObserver(on_provider_attempt)
    max_429_attempts = _validate_429_attempt_budget(max_429_attempts)
    dispatcher = timed_call('route_resolution', get_dispatcher)
    state = _StreamRetryState(
        exclude_models=exclude_models, avoid_pairs=avoid_pairs,
        dispatcher=dispatcher, owner_user_id=owner_user_id,
        state_bound_pairs=responses_state_bound_pairs(
            dispatcher, body_or_messages, prefer_model=prefer_model,
            log_prefix=log_prefix))
    # Fruit 2 (E2): at most ONE forced OAuth token refresh-retry per
    # request (see dispatch_chat).
    failure_policy = DispatchFailurePolicy(
        state, dispatcher, capability=capability, strict_model=strict_model,
        max_429_attempts=max_429_attempts, owner_user_id=owner_user_id,
        log_prefix=log_prefix, on_retry=on_retry, restart=_fire_attempt_restart,
        attempt_observer=attempt_observer)
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

        pick_started = time.monotonic()
        slot = dispatcher.pick_and_reserve(
            capability=capability, prefer_model=prefer_model,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys(),
            exclude_pairs=state.eff_exclude_pairs(),
            strict_model=strict_model)
        state.record_route_selection(pick_started)
        if slot is None:
            if state.relax_avoid_if_exhausted():
                continue
            _held_routes = state.route_unavailable.pairs
            _slots_exist = dispatcher.has_capable_slots(
                capability, exclude_models=state.exclude,
                exclude_keys=(state.exclude_keys | state.exclude_keys_durable
                              | state.credential_outage.keys),
                exclude_pairs=(state.exclude_pairs | state.exclude_pairs_durable
                               | _held_routes),
                prefer_model=prefer_model if strict_model else None)
            if _slots_exist or _cycling_can_ever_serve(
                    dispatcher, capability,
                    initial_exclude_models=state._initial_exclude_models,
                    durable_models=state.exclude_models_durable,
                    durable_keys=(state.exclude_keys_durable
                                  | state.credential_outage.keys),
                    durable_pairs=state.exclude_pairs_durable,
                    strict_model=strict_model, prefer_model=prefer_model):
                if not _slots_exist and _held_routes:
                    if state.credential_outage.retry_cooled():
                        continue
                    _wait_started = time.monotonic()
                    await async_abortable_sleep(
                        state.route_unavailable.probe_delay_s(), abort_check)
                    state.record_queue_wait(_wait_started)
                    continue
                _wait_started = time.monotonic()
                await async_abortable_sleep(0.3, abort_check)
                state.record_queue_wait(_wait_started)
                state.note_cooldown_cycle()
                if on_waiting and (
                        state._cooldown_cycles == 1
                        or state._cooldown_cycles % 20 == 0):
                    try:
                        on_waiting(
                            status=state.wait_status(
                                cooldown_cause=_dominant_cooldown_cause(
                                    dispatcher, capability, state)),
                            slot=None)
                    except Exception as _owe:
                        logger.debug(
                            '%s async on_waiting (cooldown) raised: %s',
                            log_prefix, _owe)
                continue
            if state.credential_outage.retry_cooled():
                continue
            logger.warning('%s async_dispatch_stream: NO CAPABLE SLOT on '
                           'attempt %d/%d', log_prefix, state.hard_attempts + 1, max_retries)
            break

        tag = f'{log_prefix}[aD:{slot.key_name}:{slot.model}]'

        # Non-failure re-pick = scheduler re-route; record the cause so the
        # banner can explain WHY the route changed (see sync branch above).
        if attempt_observer.ordinal > 0 and not attempt_observer.route_trace._pending_cause:
            attempt_observer.note_cause('route_rescore')

        from lib.llm.request import request_preparation
        try:
            with request_preparation(stage='slot_body_adaptation'):
                body = _adapt_stream_body_for_slot(
                    slot, body_or_messages, is_body,
                    tools=tools, tool_choice=tool_choice,
                    generation_options=generation_options,
                    max_tokens=max_tokens, temperature=temperature,
                    thinking_enabled=thinking_enabled, preset=preset,
                    effort=effort, thinking_depth=thinking_depth,
                    model_budget_admit=model_budget_admit)
        except BaseException:
            slot.release()
            raise

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
            _on_stream_wait = _stream_wait_notifier(
                on_waiting, slot, tag, on_retry, strict_model)
            stream_result = msg, finish, usage = ensure_provider_stream_result(
                await attempt_observer.acall(_observed_transport, slot,
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
            if _provider_reported_network_failure(stream_result, msg, finish):
                # Same provider-signalled transport failure as the sync loop:
                # bounded non-429 retry, no pair/model exclusion.
                state.hard_attempts += 1
                state.last_err = ProviderReportedNetworkError(
                    'provider stream ended with finish_reason=network_error '
                    'and zero actionable content')
                slot.record_error(is_rate_limit=False,
                                  error='provider network_error')
                logger.warning(
                    '%s Provider reported network_error on %s:%s with no '
                    'actionable content — rotating slot (attempt %d/%d)',
                    log_prefix, slot.key_name, slot.model,
                    state.hard_attempts, max_retries)
                _notify_retry(
                    on_retry, slot, strict_model=strict_model,
                    attempt=state.hard_attempts,
                    reason='Provider network error', status_code=0)
                _notify_attempt_restart(
                    on_attempt_restart,
                    'provider network_error — rotating slot',
                    log_prefix=log_prefix)
                continue
            latency = (time.time() - t0) * 1000
            _settle_stream_result(
                slot, usage, latency=latency, ttft=ttft_value[0], state=state,
                cache_conv_id=_cache_conv_id, tag=tag, dispatcher=dispatcher,
                stream_result=stream_result,
                route_snapshot=attempt_observer.route_trace.snapshot())
            if stream_result.is_verified_complete:
                state.credential_outage.note_success(slot)
            state.route_unavailable.note_success(slot)
            logger.debug('%s async_dispatch_stream settled: state=%s '
                         'finish=%s model=%s latency=%.0fms', log_prefix,
                         stream_result.state.value, finish, slot.model, latency)
            return stream_result

        except BaseException as error:
            retry_delay = failure_policy.handle(error, slot, tag=tag)
            if retry_delay > 0:
                wait_started = time.monotonic()
                await async_abortable_sleep(retry_delay, abort_check)
                state.record_queue_wait(wait_started)

    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              first_err=state.first_bad_request_err,
                              what='async_dispatch_stream')
