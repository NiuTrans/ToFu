"""Non-streaming selection, transport execution and successful settlement.

ChatRetryState and ChatFailurePolicy share the dispatch retry/failure owners;
this module owns native waits, the physical chat call and response metadata.
"""

from .factory import get_dispatcher
import time
from lib.log import get_logger
from lib.secret_envelope import secret_hint
from lib.llm_dispatch._api_budget import _raise_dispatch_exhausted, _saturation_budget_secs, _saturation_escalate, _unix_time_ns, _validate_429_attempt_budget
from lib.llm_dispatch._api_contention import _admit_or_defer_shared_contention, _note_shared_contention_recovered
from lib.llm_dispatch._api_hygiene import (
    _audit_severity_downgrade,
)
from ._api_retry_state import ChatRetryState, _cycling_can_ever_serve, responses_state_bound_pairs
from ._api_failures import ChatFailurePolicy
from ._route_trace import RouteAttemptTrace

from lib.llm.diagnostic_timing import timed_call, timed_call_with_fields, timed_stage

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


@timed_stage('dispatch_total')
def dispatch_chat(messages, *, max_tokens=4096, temperature=0,
                  thinking_enabled=None, preset='low', effort=None,
                  thinking_depth=None,
                  capability='text', prefer_model=None, tools=None,
                  extra=None, max_retries=3, log_prefix='',
                  timeout=None, strict_model=False,
                  exclude_models=None, abort_check=None,
                  max_429_attempts: int | None = None,
                  defer_on_shared_contention: bool = False,
                  owner_user_id: int | None = None, model_budget_admit=None):
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
    if model_budget_admit is not None and not callable(model_budget_admit):
        raise ValueError('model_budget_admit must be callable')
    from lib.llm import AbortedError, chat
    from lib.llm._transport import abortable_sleep

    # 2026-05-05 config-surface change (CLAUDE.md §10): per-cycle 429
    # severity downgraded from WARNING → INFO (routine backpressure; the
    # dispatch handler already rotates to the next key). Only final key
    # exhaustion stays WARNING/ERROR. Audited here on first call so the
    # change is discoverable in audit.log without a code-search.
    _audit_severity_downgrade()
    max_429_attempts = _validate_429_attempt_budget(max_429_attempts)
    dispatcher = timed_call('route_resolution', get_dispatcher)
    excluded_models = set(exclude_models) if exclude_models else set()

    # Pre-exclude stream-only models from non-streaming dispatch.
    #   Models like qwq-plus only support stream=True and will reject
    #   non-streaming requests with HTTP 400.
    for slot in dispatcher.slots:
        # chat aggregates a verified Codex Responses stream while preserving
        # this exact slot. Other stream-only transports retain their exclusion.
        collected_subscription = slot.protocol == 'responses' and slot.oauth == 'codex'
        if slot.stream_only and not collected_subscription and slot.model not in excluded_models:
            excluded_models.add(slot.model)
            logger.debug('%s Excluding stream-only model %s from non-streaming dispatch',
                        log_prefix, slot.model)

    # Capture caller and protocol exclusions before the first selection.
    state = ChatRetryState(excluded_models, dispatcher=dispatcher,
                           owner_user_id=owner_user_id,
                           state_bound_pairs=responses_state_bound_pairs(
                               dispatcher, messages, prefer_model=prefer_model,
                               log_prefix=log_prefix))

    # NO total time budget. A non-streaming call is waited out for as long
    #   as the upstream needs — truncating a translate/summarize call at a
    #   deadline produced silently incomplete content, which is worse than
    #   waiting. ``timeout`` is honored when a caller explicitly passes one;
    #   otherwise None = no read timeout (connect phase stays bounded, so a
    #   dead host still fails over). Retries are bounded by max_retries, and
    #   an abort is honored by the transport's abort poll.
    route_trace = RouteAttemptTrace()
    failure_policy = ChatFailurePolicy(
        state, dispatcher, capability=capability, strict_model=strict_model,
        max_429_attempts=max_429_attempts, owner_user_id=owner_user_id,
        log_prefix=log_prefix, on_failure=route_trace.note_cause,
    )

    @timed_stage('dispatcher_wait')
    def _queue_sleep(seconds: float) -> None:
        wait_started = time.monotonic()
        if abort_check is None:
            time.sleep(seconds)
        else:
            abortable_sleep(seconds, abort_check)
        state.record_queue_wait(wait_started)

    while state.hard_attempts < max_retries:
        if abort_check and abort_check():
            raise AbortedError('Aborted during non-stream dispatch retry')
        total_attempts = state.hard_attempts + state.capacity_wait_cycles

        _sat_budget = _saturation_budget_secs()
        if state.saturation_exceeded(_sat_budget):
            _saturation_escalate(
                log_prefix, 'dispatch_chat',
                elapsed_s=time.monotonic() - state._saturation_start,
                budget_s=_sat_budget, cycles=state.capacity_wait_cycles,
                model=prefer_model)
        state.maybe_reset_exclusions(log_prefix, 'dispatch_chat')

        pick_started = time.monotonic()
        slot = timed_call('route_selection', dispatcher.pick_and_reserve,
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys() or None,
            exclude_pairs=state.eff_exclude_pairs(),
            strict_model=strict_model)
        state.record_route_selection(pick_started)
        if slot is None:
            # All slots in cooldown / excluded — wait briefly and retry.
            # Keep fast-polling when capable slots EXIST but are merely
            #   cooling (429-equivalent), even if THIS call hasn't caught a
            #   raw 429 yet — otherwise fresh concurrent requests bail on
            #   attempt 1 under heavy contention. Give up only when no
            #   capable slot exists at all. (Mirror of dispatch_stream.)
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
                    durable_keys=state.exclude_keys_durable | state.credential_outage.keys,
                    durable_pairs=state.exclude_pairs_durable,
                    strict_model=strict_model, prefer_model=prefer_model):
                if not _slots_exist and _held_routes:
                    if state.credential_outage.retry_cooled():
                        continue
                    _queue_sleep(state.route_unavailable.probe_delay_s())
                    continue
                _queue_sleep(0.3)
                state.note_cooldown_cycle()
                if state.capacity_wait_cycles % 20 == 0:
                    logger.info(
                        '%s dispatch_chat: still cycling (slots cooling, %d times), '
                        'waiting for cooldown to expire…',
                        log_prefix, state.capacity_wait_cycles)
                continue
            if state.credential_outage.retry_cooled():
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
        except BaseException as error:
            slot.release()
            if isinstance(error, AbortedError):
                logger.debug('%s User aborted during contention admission', tag)
            raise
        t0 = time.time()
        slot_settled = False

        try:
            # Merge tools into extra dict (chat() doesn't have a tools param)
            _extra = dict(extra) if extra else {}
            if tools:
                _extra['tools'] = tools

            # No budget clamp: pass the caller's timeout through unchanged
            # (None = no read timeout).
            # Non-first attempt with no failure-driven cause recorded = a
            # scheduler re-pick (healthy-slot retry / re-score).
            if total_attempts > 0 and not route_trace._pending_cause:
                route_trace.note_cause('route_rescore')
            route_trace.start(slot)
            from lib.llm_dispatch.context_policy import slot_context_window
            from lib.model_info import _route_output_limit_key
            # Same Deployment identity the stream adapter stamps, so learned
            # request facts carry across stream and non-stream dispatch.
            route_limit_key = _route_output_limit_key(
                provider_id=str(getattr(slot, 'routing_provider_id', '')
                                or slot.provider_id or ''),
                offering_id=str(getattr(slot, 'route_offering_id', '') or ''),
                deployment_id=str(getattr(slot, 'route_deployment_id', '') or ''),
                protocol=str(slot.protocol or 'openai'),
                model=str(slot.model or ''),
            )
            content, usage = timed_call_with_fields('provider_call', {
                'model': slot.model,
                'provider_id': getattr(slot, 'routing_provider_id', '') or slot.provider_id,
                'protocol': slot.protocol or 'openai',
            }, chat,
                context_window=slot_context_window(slot),
                model=slot.model, messages=messages,
                knowledge_model=getattr(slot, 'logical_model', '') or slot.model,
                route_output_limit_key=route_limit_key,
                max_tokens=max_tokens, temperature=temperature,
                thinking_enabled=thinking_enabled,
                thinking_depth=thinking_depth,
                effort=effort, preset=preset,
                api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                extra=_extra or None,
                log_prefix=tag,
                max_retries=0,  # fail fast — dispatcher handles retries
                timeout=timeout,
                thinking_format=slot.thinking_format or '',
                inference_profile=getattr(slot, 'inference_profile', None),
                provider_id=(getattr(slot, 'routing_provider_id', '')
                             or slot.provider_id or ''),
                api_protocol=slot.protocol or 'openai',
                responses_feature_profile=(
                    getattr(slot, 'responses_profile', '') or 'compatible'),
                oauth=slot.oauth or '',
                adapter=slot.adapter or None,
                owner_user_id=owner_user_id, abort_check=abort_check,
                **({'model_budget_admit': model_budget_admit} if model_budget_admit is not None else {}),
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
            slot_settled = True
            state.credential_outage.note_success(slot)
            state.route_unavailable.note_success(slot)
            _note_shared_contention_recovered(dispatcher, slot, log_prefix)
            # Inject dispatch metadata so callers know which slot served this
            if isinstance(usage, dict):
                completed_at_unix_ns = _unix_time_ns()
                usage['_dispatch'] = {
                    'key': slot.key_name, 'model': slot.model,
                    'logical_model': getattr(slot, 'logical_model', '') or slot.model,
                    'key_tail': (slot.api_key or '')[-4:],
                    'key_hint': secret_hint(slot.api_key or '')[:64],
                    'provider_id': (getattr(slot, 'routing_provider_id', '')
                                    or slot.provider_id),
                    'route_snapshot': route_trace.snapshot(),
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
                    'queue_wait_ms': round(state.queue_wait_ms, 3),
                    'queue_wait_measurement': 'dispatcher_backpressure_only',
                    'route_selection_ms': round(state.route_selection_ms, 3),
                    'attempt': state.hard_attempts + 1,
                    '429_retries': state.capacity_wait_cycles,
                    'slot_wait_cycles': state._cooldown_cycles,
                    'upstream_429_retries': state._429_count,
                }
            return content, usage

        except BaseException as error:
            if slot_settled:
                # Completed work already returned this reservation. A later
                # bookkeeping failure must not release another request's slot.
                raise
            retry_delay = failure_policy.handle(error, slot, tag=tag)
            if retry_delay > 0:
                _queue_sleep(retry_delay)

    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              first_err=state.first_bad_request_err,
                              what='dispatch')
