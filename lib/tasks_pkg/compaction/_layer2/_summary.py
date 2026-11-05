"""Authorized query-aware L2 summary orchestration.

Entry point: _generate_query_aware_summary. This owner retains authority checks,
route selection, credential lifetime, budget settlement and usable-text recovery.
_summary_input prepares evidence; SummaryTransport records provider work, usage
and completion proof through the shared dispatch and stream-result boundaries.
"""

import hashlib
import json
import re

import sys
from contextlib import ExitStack

from lib.log import audit_log, get_logger

from lib.llm.stream_result import UnverifiedProviderStreamError
from lib.tasks_pkg.compaction._ownership import compaction_owner_key
from lib.tasks_pkg.compaction._layer2._summary_input import (
    SummaryInputPorts, build_summary_input, compose_summary_input,
    _archived_prior_receipt as _archived_prior_receipt,
)
from lib.tasks_pkg.compaction._layer2._summary_transport import (
    IncompleteSummaryError, SummaryTransport,
)
from lib.tasks_pkg.compaction._constants import (
    _SUMMARY_MAX_TOKENS, _SUMMARY_DISPATCH_MAX_TOKENS,
)

#: Advisory receipt size (chars ≈ tokens × 4), never a text-slicing limit.
_SUMMARY_RECEIPT_MAX_CHARS = _SUMMARY_MAX_TOKENS * 4
from lib.tasks_pkg.compaction._layer2._prompt import (
    _SUMMARY_SYSTEM_PROMPT,
    _build_summary_user_content,
    _ensure_summary_objective,
    _extract_latest_assistant_conclusions,
    _extract_prior_receipt,
    _format_messages_for_summary,
    _summary_input_char_budget,
)

logger = get_logger(__name__)



def _record_postprocess_degradation(
    *, conv_id: str, stage: str, exc: BaseException,
) -> None:
    """Emit content-free evidence that usable model output was recovered."""
    try:
        audit_log(
            'compaction_summary_postprocess_degraded',
            conv=str(conv_id or '')[:16],
            stage=stage,
            error_type=type(exc).__name__,
        )
    except Exception as audit_exc:
        logger.debug(
            '[Summary] postprocess degradation audit failed: %s', audit_exc)


def _summary_cache_affinity_id(conv_id: str, task: dict | None) -> str:
    """Return one opaque owner/conversation-scoped identity for L2 dispatch.

    Summary requests have a stable system prefix but a completely different
    wire prefix from the next ordinary agent round.  Reusing the parent
    conversation affinity makes the Codex settle gate wait behind unrelated
    writes in both directions, while message-list dispatch otherwise gives the
    summary a random subscription session id.  A separate stable identity lets
    summaries reuse their own prefix without perturbing the main conversation.

    Production tasks contribute their validated owner.  Legacy/direct callers
    without a task still get a conversation-scoped namespace; an explicitly
    malformed task owner fails closed to no affinity instead of merging owners.
    The returned value contains no conversation or owner text.
    """
    effective_conv_id = str(compaction_owner_key(task, conv_id)).strip()
    if not effective_conv_id:
        return ''

    owner_scope = 'unowned'
    if isinstance(task, dict) and task.get('_userId') is not None:
        try:
            from lib.tasks_pkg.manager import task_user_id
            owner_scope = str(task_user_id(task))
        except (TypeError, ValueError) as exc:
            logger.warning(
                '[Summary] refusing cache affinity for invalid task owner: %s',
                exc)
            return ''

    digest = hashlib.sha256(
        ('tofu-l2-summary\0' + owner_scope + '\0' + effective_conv_id)
        .encode('utf-8')
    ).hexdigest()[:24]
    return f'l2s-{digest}'


def _codex_subscription_provider(task: dict | None) -> str:
    """Return the active Codex-subscription provider, if this task has one.

    Automatic L2 summaries historically used :func:`dispatch_chat`.  Managed
    Codex slots are intentionally ``stream_only``, so that path excludes them
    before picking a slot and can silently spend a different provider's API
    key for compaction.  Use the already-observed provider (or the
    conversation's sticky key on a newly resumed task) to keep the summary on
    the subscription that owns the conversation.

    A pre-existing hard provider pin always wins.  We never replace a
    non-Codex pin merely because stale task metadata mentions Codex.
    """
    if not isinstance(task, dict):
        return ''

    try:
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        pinned = get_pinned_provider() or ''
    except Exception as exc:
        logger.debug('[Summary] provider-pin lookup failed: %s', exc)
        pinned = ''

    cfg = task.get('config') or {}
    candidates = [
        pinned,
        task.get('provider_id') or '',
        cfg.get('providerId') or cfg.get('provider_id') or '',
    ]

    # A resumed conversation can compact before its first dispatch has copied
    # provider_id back onto the new task.  Its sticky key is the last slot that
    # actually served the conversation, so it is stronger evidence than model
    # name guessing (the same model id may exist on multiple providers).
    if not pinned and compaction_owner_key(task):
        try:
            from lib.llm_dispatch import get_dispatcher
            from lib.llm_dispatch.conv_affinity import get_preferred_key
            sticky_key = get_preferred_key(compaction_owner_key(task))
            if sticky_key:
                dispatcher = get_dispatcher()
                dispatcher.initialize()
                # The sticky value is the slot's stable affinity identity
                # (route credential+deployment), which outlives request-random
                # key_names; match on the same identity.
                candidates.extend(
                    slot.provider_id for slot in dispatcher.slots
                    if slot.affinity_key() == sticky_key
                    and slot.oauth == 'codex')
        except Exception as exc:
            logger.debug('[Summary] sticky Codex-provider lookup failed: %s',
                         exc)

    # Crash recovery can rebuild a task before it has dispatched once in this
    # process: no task.provider_id and no in-memory sticky key exist yet.  The
    # configured model is still useful evidence when the live dispatcher says
    # every matching slot belongs to ONE Codex provider.  Refuse to infer when
    # the model is shared across providers; that preserves the no-name-guessing
    # rule while closing the recovered-task hole.
    model_hint = str(
        task.get('model') or cfg.get('model') or cfg.get('preset') or '')
    if not pinned and model_hint:
        try:
            from lib.llm_dispatch import get_dispatcher
            dispatcher = get_dispatcher()
            dispatcher.initialize()
            matching = [
                slot for slot in dispatcher.slots
                if model_hint in {
                    str(slot.model or ''), str(slot.logical_model or '')
                }
            ]
            providers = {
                str(slot.provider_id or '') for slot in matching
                if slot.oauth == 'codex' and slot.provider_id
            }
            if (matching and len(providers) == 1
                    and all(slot.oauth == 'codex' for slot in matching)):
                candidates.extend(providers)
        except Exception as exc:
            logger.debug('[Summary] model-slot Codex lookup failed: %s', exc)

    def _is_codex_provider(provider_id: str) -> bool:
        if not provider_id:
            return False
        if provider_id == 'oauth_codex':
            return True
        try:
            from lib.llm_dispatch import get_dispatcher
            dispatcher = get_dispatcher()
            dispatcher.initialize()
            return any(slot.provider_id == provider_id and slot.oauth == 'codex'
                       for slot in dispatcher.slots)
        except Exception as exc:
            logger.debug('[Summary] Codex-provider classification failed: %s',
                         exc)
            return False

    if pinned:
        return pinned if _is_codex_provider(pinned) else ''
    for provider_id in candidates:
        if _is_codex_provider(provider_id):
            return provider_id
    return ''


def _summary_authority_evidence(task: dict | None) -> str:
    """Snapshot the owner, parent route and policy before any blocking discovery."""
    if not isinstance(task, dict):
        return ''
    view = dict(task)
    try:
        # The owner read stamps an identical compatibility principal onto
        # older tasks; compare that canonical form, not its presence.
        from lib.tasks_pkg.manager import task_user_id
        task_user_id(view)
    except (ValueError, TypeError, PermissionError):
        pass  # The ownership boundary below still refuses invalid identity.
    config = view.get('config') or {}
    # Capture before any discovery/accounting read can wait. Initial and
    # replacement routes share the same owner, parent pins and policy.
    fields = [view.get(key) for key in (
        '_userId', '_principalContext', '_tenant_id', 'model',
        '_pinned_provider_id', 'provider_id', 'disableModelFallback')]
    fields.append({key: config.get(key) for key in (
        'model', 'preset', '_pinned_provider_id', 'providerId',
        'provider_id', 'disableModelFallback', 'compaction')})
    return json.dumps(fields, sort_keys=True)


def _summary_input_ports() -> SummaryInputPorts:
    """Bind input policy at call time; quote pricing and dispatch share it."""
    return SummaryInputPorts(
        input_char_budget=_summary_input_char_budget,
        prior_receipt=_extract_prior_receipt, archived_receipt=_archived_prior_receipt,
        latest_assistant=_extract_latest_assistant_conclusions,
        format_history=_format_messages_for_summary,
        user_content=_build_summary_user_content, system_prompt=_SUMMARY_SYSTEM_PROMPT,
    )


def _summary_request_input_tokens(
    messages: list, current_query: str, *, task: dict | None,
    source_messages: list | None, anchor_text: str,
) -> int:
    """Estimated input of the request _generate_query_aware_summary dispatches."""
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    return _estimate_total_tokens(compose_summary_input(
        messages, current_query, task=task, source_messages=source_messages,
        anchor_text=anchor_text, ports=_summary_input_ports()).prompt)


def _summary_route_matches_quote(summary_route: dict, expected_route: dict) -> bool:
    """Keep route identity, capacity and the authorized spending ceiling."""
    expected_cost = expected_route.get('projectedCostUsd')
    actual_cost = summary_route.get('projectedCostUsd')
    same_route = all(summary_route.get(key) == expected_route.get(key)
                     for key in ('modelId', 'providerId', 'policy', 'routeIdentity'))
    subscription_quote = (
        summary_route.get('requestOwned') and expected_cost is None
        and actual_cost is None and summary_route.get('costBasis')
        == expected_route.get('costBasis'))
    if summary_route.get('requestOwned'):
        expected_max = expected_route.get('maximumCostUsd')
        actual_max = summary_route.get('maximumCostUsd')
        if expected_max is not None and (actual_max is None or actual_max > expected_max):
            same_route = False
        if (summary_route.get('capacityOutputTokens', 0)
                > expected_route.get('capacityOutputTokens', 0)):
            same_route = False
    if (not same_route or (not subscription_quote and (
            expected_cost is None or actual_cost is None
            or actual_cost > expected_cost))):
        return False
    return True


def _summary_request_key(
    messages: list, *, owner_user_id: int | None, summary_route: dict,
    model: str | None, pinned_provider: str | None, codex_provider: str, config: dict,
) -> str:
    """Digest the evidence, authority and original qualified route for deduplication."""
    # Retain only a digest, never a second transcript. Identical requests
    # share both successful elapsed time and failure across admission stages.
    # Learned route health may move the plan off a cooled credential;
    # the identity stays the health-blind leader so repeats still dedupe.
    key_route = summary_route.get('_requestRoute') or summary_route
    identity = [messages, owner_user_id, key_route.get('modelId') or model,
                pinned_provider or key_route.get('providerId') or codex_provider,
                config.get('compaction') or {}]
    if key_route.get('requestOwned'):
        identity.extend([key_route.get('routeIdentity'),
                         key_route.get('declaredPricing'),
                         key_route.get('capacityOutputTokens')])
    digest = hashlib.sha256()
    for chunk in json.JSONEncoder(sort_keys=True).iterencode(identity):
        digest.update(chunk.encode('utf-8'))
    return digest.hexdigest()


def _generate_query_aware_summary(messages: list, current_query: str,
                                   log_prefix: str = '',
                                   conv_id: str = '',
                                   task: dict | None = None,
                                   on_delta=None,
                                   usage_out: dict | None = None,
                                   anchor_text: str = '',
                                   source_messages: list | None = None,
                                   expected_route: dict | None = None,
                                   output_budget_tokens: int | None = None,
                                   ) -> str | None:
    """Generate a complete query-aware summary on an authorized route.

    The main-model policy is the default. Opt-in qualified policies require
    verified capability, capacity and route pricing before selecting a cheaper
    model; strict qualification refuses dispatch rather than crossing the pin.
    Transport completion and critical-evidence validation precede adoption.
    Only the explicit legacy utility path can retry a text-capable slot.

    ``on_delta``: optional ``fn(text_chunk)`` callback. When given, the summary
    is STREAMED (``dispatch_stream``) and every content delta is forwarded to
    ``on_delta`` as it arrives, so a caller can push the growing summary to a
    live UI (the manual /compact card). The full accumulated text is still
    returned — identical result to the non-streaming path — so callers that
    also want the final string are unaffected. ``on_delta`` exceptions are
    swallowed (best-effort UI) and never abort generation. When ``on_delta`` is
    ``None`` the original non-streaming ``dispatch_chat`` path is retained for
    ordinary providers; Codex subscriptions stream internally because all of
    their managed slots are streaming-only. ``usage_out`` is an optional
    mutable dict populated with the observed summary call's raw usage so a
    proactive caller can account for the cost before committing a rewrite.
    ``anchor_text`` is the literal text of the earliest-request
    anchor (pulled out of the lossy region for live re-insertion, so the
    model would not see it otherwise); ``current_query`` is the newest user
    message, which lives in the preserved region rather than the summary
    input. ``source_messages`` is the full pre-fold transcript used to
    extract the latest wrap-up and prior receipt (those usually sit in the
    preserved tail or an earlier compact tool pair, not in fold-only
    history). Both reach the model as VERBATIM EVIDENCE — the model authors
    the receipt's Objective itself (see ``_ensure_summary_objective``), so
    the receipt can track goal replacement across a long conversation.
    """
    authority_evidence = _summary_authority_evidence(task)
    from lib.tasks_pkg.compaction._summary_accounting import summary_model_admission
    model_admission = summary_model_admission(task)
    from lib.llm_dispatch import dispatch_chat, dispatch_stream
    from lib.llm.llm_errors import AbortedError, LocalRequestPreparationError
    dispatch_output_tokens = _SUMMARY_DISPATCH_MAX_TOKENS
    if output_budget_tokens is not None:
        if (not isinstance(output_budget_tokens, int)
                or isinstance(output_budget_tokens, bool) or output_budget_tokens <= 0):
            raise ValueError('summary output budget must be a positive integer')
        dispatch_output_tokens = min(dispatch_output_tokens, output_budget_tokens)

    original_output_allowance = dispatch_output_tokens
    conv_id = compaction_owner_key(task, conv_id)
    tag = f'{log_prefix}[Summary]' if log_prefix else '[Summary]'

    # Stop-priority wire: a summary running inside a LIVE task turn shares
    # the turn's abort signal. Without it the cheap-model call (seconds to a
    # minute on a near-full window) invisible-walls a user Stop until it
    # completes — the "pause stuck behind compaction" report. Only live
    # runtime task dicts (which always carry the 'aborted' key —
    # handle_abort_at_round_start indexes it directly) get the wire;
    # synthetic dicts (manual /compact, standalone callers) keep the
    # historical unabortable behavior.
    abort_check = None
    if isinstance(task, dict) and 'aborted' in task:
        from lib.agent_loop.agent_loop import AbortSignal
        abort_check = AbortSignal.from_task_flag(task).is_set
        if abort_check():
            logger.info('%s Summary skipped — task aborted before dispatch',
                        tag)
            return None

    from lib.tasks_pkg.compaction._summary_budget import (
        SummaryBudget, bounded_summary_task)
    _summary_messages = build_summary_input(
        messages, current_query, task=task, source_messages=source_messages,
        anchor_text=anchor_text, log_prefix=tag, ports=_summary_input_ports(),
    )

    codex_provider = _codex_subscription_provider(task)
    summary_affinity_id = _summary_cache_affinity_id(conv_id, task)
    from lib.tasks_pkg.manager import task_user_id
    owner_user_id = task_user_id(task) if isinstance(task, dict) else None

    from lib.llm_dispatch.native_context_scope import native_compaction_route_ready
    if isinstance(task, dict) and not native_compaction_route_ready(task):
        logger.info('%s Native summary route unavailable; preserving source', tag)
        return None

    budget = None
    summary_route = {}
    summary_input_tokens = 0
    summary_attempts = []
    failure_evidence = None
    dispatch_options = {'thinking_enabled': False}
    if isinstance(task, dict):
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        cfg = task.get('config') or {}
        model = task.get('model') or cfg.get('model') or cfg.get('preset')

        from lib.tasks_pkg.compaction._summary_routing import select_summary_route
        from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
        summary_input_tokens = _estimate_total_tokens(_summary_messages)
        summary_route = select_summary_route(
            task, input_tokens=summary_input_tokens,
            output_tokens=_SUMMARY_MAX_TOKENS,
            capacity_output_tokens=dispatch_output_tokens)
        model = summary_route.get('modelId') or model

        if summary_route.get('requestOwned'):
            dispatch_output_tokens = min(dispatch_output_tokens, summary_route['capacityOutputTokens'])
        if isinstance(usage_out, dict):
            usage_out['_summaryRoute'] = summary_route
        request_key = None
        if bounded_summary_task(task):
            request_key = _summary_request_key(
                _summary_messages, owner_user_id=owner_user_id, summary_route=summary_route,
                model=model, pinned_provider=get_pinned_provider(),
                codex_provider=codex_provider, config=cfg,
            )
        if bounded_summary_task(task) or 'aborted' in task:
            budget = SummaryBudget(task, abort_check, request_key=request_key)
            abort_check = budget.is_set
            if abort_check() or not model:
                budget.finish(False)
                budget.project_usage(usage_out)
                return None
        if _summary_authority_evidence(task) != authority_evidence:
            if budget is not None:
                budget.finish(False, failure_reason='summary_route_revalidation_failed')
                budget.project_usage(usage_out)
            logger.info('%s Summary authority changed during initial selection; '
                        'preserving source without dispatch', tag)
            return None
        if expected_route is not None:
            # Revalidate the exact request, but never buy a different route or
            # exceed the quote authorizing this optional cache rewrite.
            if not _summary_route_matches_quote(summary_route, expected_route):
                if budget is not None:
                    budget.finish(
                        False, failure_reason='summary_route_revalidation_failed')
                logger.info('%s Summary route/quote changed since preflight; '
                            'preserving source without dispatch', tag)
                return None
        if summary_route.get('reason') in {
                'conflicting_provider_pins', 'conflicting_tenant_identity'}:
            if budget is not None:
                budget.finish(False, failure_reason='summary_route_revalidation_failed')
                budget.project_usage(usage_out)
            logger.warning('%s Summary route identity conflict (%s); keeping source intact',
                           tag, summary_route['reason'])
            return None
        if model:
            # Keep waitable upstream failures inside the common dispatcher.
            # A summary-only 429 ceiling turns temporary TPM/gateway pressure
            # into a failed compaction before the authorized route can recover.
            dispatch_options = dict(prefer_model=model, strict_model=True,
                                    thinking_enabled=False)

    if model_admission is not None:
        dispatch_options['model_budget_admit'] = model_admission

    transport = SummaryTransport(
        messages=_summary_messages, conv_id=conv_id, affinity_id=summary_affinity_id,
        owner_user_id=owner_user_id, log_prefix=tag, abort_check=abort_check,
        on_delta=on_delta, usage_out=usage_out,
        dispatch_chat=dispatch_chat, dispatch_stream=dispatch_stream,
    )

    def _dispatch(capability: str):
        return transport.dispatch(
            capability, summary_route=summary_route, codex_provider=codex_provider,
            auxiliary_pin=auxiliary_pin, dispatch_output_tokens=dispatch_output_tokens,
            dispatch_options=dispatch_options,
        )

    from lib.tasks_pkg.compaction._summary_failure import (
        MAX_SUMMARY_ATTEMPTS, validate_summary_failure)
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError, SubscriptionLoginRequiredError
    from lib.tasks_pkg.compaction._auxiliary_route import (
        credential_route_exclusion, group_credential_excluded,
        mint_auxiliary_route, record_auxiliary_route_success)

    # Main identity is fixed before any auxiliary group pins its own provider.
    main_model = ((task.get('model') or (task.get('config') or {}).get('model')
                   or (task.get('config') or {}).get('preset'))
                  if isinstance(task, dict) else None)
    # Same resolution as select_summary_route: active thread pin, else the
    # task's own route group. A missing thread pin must not disable fallback.
    main_pin = ((get_pinned_provider() or str(task.get('_pinned_provider_id')
                 or (task.get('config') or {}).get('_pinned_provider_id') or ''))
                if isinstance(task, dict) else '')
    main_codex_provider = codex_provider  # Streaming-only subscription mains keep streaming.
    auxiliary_lifecycle = ExitStack()
    auxiliary_pin = ''
    dispatch_succeeded = False
    failure_reason = 'summary_dispatch_failed'
    initial_route = summary_route
    excluded_routes = []
    principal = (task.get('_principalContext') or {}) if isinstance(task, dict) else {}
    tenant = principal.get('tenant_id', task.get('_tenant_id')) if isinstance(task, dict) else None

    # Alias checks are secret-local, not provider attempts. Bound them by the
    # initial finite compiler supply at one revision, never a global slot cap.
    eligible_checks = max(1, int((initial_route.get('selectionBasis') or {}).get('eligibleRoutes') or 1))
    checked_groups = 0
    cleanup_failed = False

    def _close_route():
        nonlocal auxiliary_lifecycle, auxiliary_pin, cleanup_failed
        nonlocal failure_evidence, failure_reason
        closing = auxiliary_lifecycle
        auxiliary_lifecycle = ExitStack()
        auxiliary_pin = ''
        try:
            closing.close()
        except BaseException:
            cleanup_failed = True
            failure_evidence = None
            failure_reason = 'summary_dispatch_failed'
            raise

    main_fallback_used = False

    def _fall_back_to_main_route():
        """Use the task's own route once after auxiliary credentials are unsent.

        No new catalog, model or provider authority: only the strict main
        model under the parent pin selected before discovery. A main whose
        every slot shares the failed Codex resolver cannot help; one independent
        grant can, and dispatch's credential-outage routing skips the shared
        slots. A quote, opt-out or authority/abort change refuses rather than
        buying an unquoted call.
        """
        nonlocal summary_route, model, dispatch_options, dispatch_output_tokens
        nonlocal main_fallback_used, codex_provider
        if (main_fallback_used or expected_route is not None
                or not initial_route.get('requestOwned')
                or initial_route.get('policy') != 'aa-cheapest'
                or not main_model or not main_pin
                or len(summary_attempts) >= MAX_SUMMARY_ATTEMPTS
                or (abort_check and abort_check())
                or _summary_authority_evidence(task) != authority_evidence):
            return False
        from lib.llm_dispatch import get_dispatcher
        main_slots = [slot for slot in list(get_dispatcher().slots)
                      if getattr(slot, 'provider_id', '') == main_pin
                      and main_model in (getattr(slot, 'logical_model', ''),
                                         getattr(slot, 'model', ''))]
        if not main_slots:
            return False
        if any(exclusion['resolver'] == 'codex' for exclusion in excluded_routes) and all(
                getattr(slot, 'oauth', '') == 'codex' for slot in main_slots):
            return False
        main_fallback_used = True
        summary_route = {'modelId': main_model, 'providerId': main_pin,
                         'policy': initial_route.get('policy'),
                         'reason': 'main_after_auxiliary_credential_outage',
                         'projectedCostUsd': None, 'costBasis': {'source': 'unknown'},
                         'selectionBasis': {}}
        model = main_model
        codex_provider = main_codex_provider
        dispatch_output_tokens = original_output_allowance
        dispatch_options = {**dispatch_options, 'prefer_model': main_model, 'strict_model': True}
        if isinstance(usage_out, dict):
            usage_out['_summaryRoute'] = summary_route
        return True

    def _reselect_route():
        nonlocal summary_route, model, dispatch_options, dispatch_output_tokens
        if (expected_route is not None or not initial_route.get('requestOwned')
                or initial_route.get('policy') != 'aa-cheapest'
                or len(summary_attempts) >= MAX_SUMMARY_ATTEMPTS
                or checked_groups >= eligible_checks
                or (abort_check and abort_check())
                or _summary_authority_evidence(task) != authority_evidence):
            return False
        from lib.tasks_pkg.compaction._summary_routing import select_summary_route
        candidate = select_summary_route(
            task, input_tokens=summary_input_tokens, output_tokens=_SUMMARY_MAX_TOKENS,
            capacity_output_tokens=original_output_allowance,
            excluded_routes=excluded_routes)
        # Selection may block on owner/profile/repository reads; recheck task
        # authority before publishing a candidate or minting its credentials.
        if (not candidate.get('requestOwned')
                or candidate.get('policy') != initial_route.get('policy')
                or candidate['routeIdentity'].get('revision')
                    != initial_route['routeIdentity'].get('revision')
                or candidate.get('_parentRouteIdentities')
                    != initial_route.get('_parentRouteIdentities')
                or _summary_authority_evidence(task) != authority_evidence
                or (abort_check and abort_check())):
            return False
        summary_route = candidate
        model = candidate['modelId']
        dispatch_output_tokens = min(original_output_allowance, candidate['capacityOutputTokens'])
        dispatch_options = {**dispatch_options, 'prefer_model': model, 'strict_model': True}
        if isinstance(usage_out, dict):
            usage_out['_summaryRoute'] = summary_route
        return True

    try:
        while True:
            if abort_check and abort_check():
                return None
            if _summary_authority_evidence(task) != authority_evidence:
                failure_evidence = None
                failure_reason = 'summary_route_revalidation_failed'
                return None
            group = None
            if summary_route.get('requestOwned'):
                group = mint_auxiliary_route(summary_route, owner=owner_user_id,
                                              tenant=tenant, lifecycle=auxiliary_lifecycle)
                checked_groups += 1
                auxiliary_pin = group.pin_id
                # Transport selection follows the materialized credential,
                # never a provider brand or the parent OAuth slot.
                codex_provider = group.pin_id if group.primary.slot.oauth == 'codex' else ''
                if group_credential_excluded(group, excluded_routes, owner=owner_user_id, tenant=tenant):
                    excluded_routes.append(credential_route_exclusion(
                        group, owner=owner_user_id, tenant=tenant))
                    _close_route()
                    if _reselect_route() or _fall_back_to_main_route():
                        continue
                    return None
            # A new qualified transport is not proof that the previous
            # credential outage remains the overall outcome. Only its own
            # proven unsent failure may publish that cause again.
            failure_evidence = None
            failure_reason = 'summary_dispatch_failed'
            if _summary_authority_evidence(task) != authority_evidence:
                failure_reason = 'summary_route_revalidation_failed'
                return None  # Secret materialization can wait too; cleanup stays owned.
            try:
                content, usage = _dispatch('text' if dispatch_options.get('strict_model') else 'cheap')
            except LocalRequestPreparationError as error:
                cause = error.__cause__
                unsent = (not error.provider_reached and not transport.observed_provider_work
                          and not transport.transport_completed)
                # Sign-in expiry is not availability evidence and retrying the
                # same login cannot satisfy it, but an independently qualified
                # credential can still serve this unchanged, unsent request.
                sign_in_required = unsent and isinstance(cause, SubscriptionLoginRequiredError)
                safe = None
                if unsent and isinstance(cause, SubscriptionCredentialUnavailableError):
                    safe = validate_summary_failure({
                        'code': 'summary_credentials_unavailable',
                        'authIssue': cause.reason, 'credentialSource': cause.credential_source,
                        'providerReached': False})
                if safe is None and not sign_in_required:
                    failure_evidence = None
                    raise
                failure_evidence = safe
                attempt = {
                    'modelId': summary_route.get('modelId') or model,
                    'providerId': summary_route.get('providerId') or '',
                    'status': 'failed', 'providerReached': False}
                if safe is not None:
                    failure_reason = 'summary_credentials_unavailable'
                    attempt.update(authIssue=safe['authIssue'],
                                   credentialSource=safe['credentialSource'])
                summary_attempts.append(attempt)
                if group is not None:
                    # Dispatch already taught owner route health once (sign-in
                    # included), so later summaries skip a cooling credential;
                    # this exclusion keeps the rest of this summary off it.
                    excluded_routes.append(credential_route_exclusion(
                        group, owner=owner_user_id, tenant=tenant))
                    _close_route()
                reroutable = sign_in_required or cause.transient
                if reroutable and _reselect_route():
                    logger.info('%s Requalified independent summary credential route', tag)
                    continue
                if reroutable and _fall_back_to_main_route():
                    logger.info('%s Summary credential unavailable; using authorized main route', tag)
                    continue
                if sign_in_required:
                    logger.warning('%s Summary route requires subscription sign-in and no '
                                   'independent route qualified (will keep messages intact)', tag)
                return None
            except AbortedError:
                raise
            except (IncompleteSummaryError, UnverifiedProviderStreamError):
                raise  # Observed work never buys an unqualified second summary.
            except Exception as error:
                if (transport.transport_completed or budget is not None
                        or dispatch_options.get('strict_model')):
                    raise
                logger.warning('%s cheap-tier summary dispatch failed (%s) — '
                               'retrying on any text-capable slot', tag, type(error).__name__)
                content, usage = _dispatch('text')
            if group is not None:
                record_auxiliary_route_success(group, owner=owner_user_id, tenant=tenant)
            if summary_route.get('requestOwned') or main_fallback_used:
                summary_attempts.append({
                    'modelId': summary_route['modelId'], 'providerId': summary_route['providerId'],
                    'status': 'succeeded', 'providerReached': True})
            failure_evidence = None
            failure_reason = 'summary_dispatch_failed'
            break

        # Reject scratchpad-only output before objective injection can make it
        # appear useful. A local sanitizer failure keeps the last usable text.
        if isinstance(content, str):
            try:
                content = re.sub(
                    r'<analysis>.*?</analysis>\s*',
                    '', content, flags=re.DOTALL,
                )
            except Exception as exc:
                logger.warning(
                    '%s Summary post-processing degraded at analysis stripping '
                    '(%s); retaining generated content', tag, type(exc).__name__)
                _record_postprocess_degradation(
                    conv_id=conv_id, stage='analysis_strip', exc=exc)
        dispatch_succeeded = isinstance(content, str) and bool(content.strip())
        if budget is not None and budget.is_set():
            dispatch_succeeded = False
            return None  # User cancellation wins a race with completion.
    except AbortedError:
        failure_evidence = None
        failure_reason = 'summary_cancelled'
        dispatch_succeeded = False
        logger.info('%s Summary cancelled; messages kept intact', tag)
        return None
    except Exception as e:
        if cleanup_failed:
            raise  # Teardown failure is not credential recovery authority.
        failure_evidence = None
        failure_reason = ('summary_incomplete' if isinstance(
            e, (IncompleteSummaryError, UnverifiedProviderStreamError)) else 'summary_dispatch_failed')
        logger.warning('%s Summary dispatch failed (will keep messages intact): %s',
                       tag, type(e).__name__)
        return None
    except BaseException:
        # Cancellation/fatal exits need not be reflected in task.aborted.
        # Never settle an earlier unsent cause over unknown or observed work.
        failure_evidence = None
        failure_reason = 'summary_dispatch_failed'
        dispatch_succeeded = False
        raise

    finally:
        primary_error_active = sys.exc_info()[0] is not None
        try:
            auxiliary_lifecycle.close()
        except BaseException as cleanup_error:
            # Cleanup is part of adoption. It cannot publish a succeeded budget
            # or leave a credential cause masking an internal teardown defect.
            dispatch_succeeded = False
            failure_evidence = None
            failure_reason = 'summary_dispatch_failed'
            logger.warning('%s Auxiliary route teardown failed: %s',
                           tag, type(cleanup_error).__name__)
            if not primary_error_active:
                raise
        finally:
            if budget is not None:
                budget.finish(dispatch_succeeded, failure_reason=failure_reason,
                              failure_evidence=failure_evidence, attempts=summary_attempts)
                budget.project_usage(usage_out)
            elif isinstance(usage_out, dict):
                if summary_attempts:
                    usage_out['_summaryAttempts'] = list(summary_attempts)
                if failure_evidence is not None:
                    usage_out['_summaryFailure'] = failure_evidence

    if not isinstance(content, str) or not content.strip():
        logger.warning('%s Summary model returned empty or non-text content', tag)
        return None

    usage = usage if isinstance(usage, dict) else {}
    in_tok = usage.get('prompt_tokens', 0)
    out_tok = usage.get('completion_tokens', 0)

    # Dispatch and receipt shaping are separate fault domains. Once a complete
    # provider result exists, a local sanitizer/objective bug must never turn
    # it into ``None`` and force the original oversized prompt back through
    # admission. Each stage keeps the last usable text on failure.
    processed = content
    try:
        shaped = _ensure_summary_objective(
            processed,
            anchor_text=anchor_text,
        )
        if not isinstance(shaped, str):
            raise TypeError('objective injection returned non-text content')
        processed = shaped
    except Exception as exc:
        logger.warning(
            '%s Summary post-processing degraded at objective injection '
            '(%s); retaining generated content', tag, type(exc).__name__)
        _record_postprocess_degradation(
            conv_id=conv_id, stage='objective_injection', exc=exc)

    processed = processed.strip()
    if not processed:
        logger.warning('%s Summary became empty after post-processing', tag)
        return None
    # The prompt target is advisory. Its tail may contain a binding instruction
    # or unfinished action, so never slice completed output. Candidate adoption
    # separately checks savings, required state and final prompt admission.
    if len(processed) > _SUMMARY_RECEIPT_MAX_CHARS:
        logger.info('%s Complete summary exceeds advisory target (%d chars); '
                    'preserving tail for candidate validation', tag, len(processed))
    logger.info('%s Summary generated: %d chars  in=%d  out=%d tokens',
                tag, len(processed), in_tok, out_tok)
    return processed
