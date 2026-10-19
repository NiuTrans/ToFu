"""Layer 2 — query-aware LLM summary generation.

Wraps the cheap-model dispatch that turns the OLD conversation region into a
concise working-state snapshot (``_generate_query_aware_summary``).
"""

import hashlib
import json
import time
import re

import sys
from contextlib import ExitStack, nullcontext

from lib.log import audit_log, get_logger

from lib.llm.stream_result import UnverifiedProviderStreamError
from lib.tasks_pkg.compaction._ownership import compaction_owner_key
from lib.tasks_pkg.compaction._constants import (
    _SUMMARY_MAX_TOKENS, _SUMMARY_DISPATCH_MAX_TOKENS,
)

#: Advisory receipt size (chars ≈ tokens × 4), never a text-slicing limit.
_SUMMARY_RECEIPT_MAX_CHARS = _SUMMARY_MAX_TOKENS * 4
from lib.tasks_pkg.compaction._tokens import _human_size
from lib.tasks_pkg.compaction._layer2._prompt import (
    _SUMMARY_SYSTEM_PROMPT,
    _build_summary_user_content,
    _ensure_summary_objective,
    _extract_latest_assistant_conclusions,
    _extract_prior_receipt,
    _format_messages_for_summary,
    _receipt_body_from_text,
    _summary_input_char_budget,
)

logger = get_logger(__name__)



class IncompleteSummaryError(ValueError):
    """Observed provider output is not a verified normal completion."""

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
                candidates.extend(
                    slot.provider_id for slot in dispatcher.slots
                    if slot.key_name == sticky_key and slot.oauth == 'codex')
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


def _archived_prior_receipt(task: dict | None) -> str:
    """Newest owner-scoped archive summary with structured receipt sections.

    Used only when live messages have no compact tool result (typical after a
    cold rebuild that dropped the synthetic pair). Loads metadata only —
    never the archived transcript payload. Fail-closed: any store error
    returns ''.
    """
    if not isinstance(task, dict):
        return ''
    conv_id = compaction_owner_key(task)
    user_id = task.get('_userId')
    if (not conv_id or not isinstance(user_id, int)
            or isinstance(user_id, bool) or user_id <= 0):
        return ''
    archive_scope = task.get('_archiveScope', '')
    archive_conv_id = task.get('convId') if archive_scope else conv_id
    scoped = {'archive_scope': archive_scope} if archive_scope else {}
    try:
        from lib.agent_core.store import get_conversation_store
        store = get_conversation_store()
        archives = store.list_compaction_archives(
            archive_conv_id, user_id=user_id, limit=8, **scoped)
    except Exception as exc:
        logger.debug('%s archive receipt lookup failed: %s',
                     '[Summary]', exc)
        return ''
    if not isinstance(archives, list) or not archives:
        return ''
    for meta in reversed(archives):
        if not isinstance(meta, dict):
            continue
        archive_id = meta.get('id') or meta.get('archiveId') or meta.get('archive_id')
        if not archive_id:
            continue
        try:
            payload = store.get_compaction_archive(
                archive_conv_id, archive_id, user_id=user_id,
                include_messages=False, **scoped)
        except Exception as exc:
            logger.debug('%s archive %s load failed: %s',
                         '[Summary]', archive_id, exc)
            continue
        if not isinstance(payload, dict):
            continue
        archive = payload.get('archive')
        if not isinstance(archive, dict):
            archive = payload
        body = _receipt_body_from_text(archive.get('summary'))
        if body:
            return body
    return ''


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
    _char_budget = _summary_input_char_budget(task)
    receipt_messages = (source_messages if source_messages is not None
                        else messages)
    prior_receipt = _extract_prior_receipt(receipt_messages)
    if not prior_receipt:
        prior_receipt = _archived_prior_receipt(task)
        if prior_receipt:
            logger.info('%s Prior receipt recovered from compaction archive',
                        tag)
    latest_assistant = _extract_latest_assistant_conclusions(receipt_messages)
    formatted_ledger = ''
    from lib.context_experiment_flags import normalize_context_experiment_flags
    compaction_flags = normalize_context_experiment_flags(
        (task.get('config') or {}) if isinstance(task, dict) else {}
    )['compaction']
    # Default input has ONE evidence authority: the formatter's bounded,
    # occurrence-local observations. The legacy ledger exists only for the
    # explicitly enabled runtime persistence/adaptive validation contracts.
    if isinstance(task, dict) and (
            compaction_flags['evidenceLedger']
            or compaction_flags['strategy'] == 'adaptive'):
        try:
            from lib.tasks_pkg.compaction._evidence import (
                bound_evidence_ledger, build_evidence_ledger,
                format_evidence_ledger)
            ledger = bound_evidence_ledger(
                build_evidence_ledger(messages, task),
                max_chars=max(2_000, min(16_000, _char_budget // 4)))
            task['_contextEvidenceLedger'] = ledger
            formatted_ledger = format_evidence_ledger(ledger)
        except Exception as exc:
            logger.warning('%s evidence ledger generation failed: %s', tag, exc)

    # MESSAGE-AWARE elision: pass the budget INTO the formatter so it trims by
    # dropping middle ASSISTANT/context content while keeping EVERY real user
    # message. The old code sliced the joined string blindly,
    # which could cut a user turn in half or drop it entirely — losing user
    # instructions. See _format_messages_for_summary / _elide_to_budget.
    history_budget = max(
        4_000,
        _char_budget - len(formatted_ledger) - len(current_query or '')
        - len(anchor_text or '') - len(prior_receipt)
        - len(latest_assistant) - 900,
    )
    format_measurement = {}
    formatted = _format_messages_for_summary(
        messages, char_budget=history_budget, measurement_out=format_measurement)
    full_chars = format_measurement['fullChars']

    logger.info('%s Formatting %d messages for summary (%s; '
                'prior_receipt=%s latest_assistant=%s)',
                tag, len(messages), _human_size(len(formatted)),
                _human_size(len(prior_receipt)),
                _human_size(len(latest_assistant)))
    if len(formatted) < full_chars:
        logger.info('%s Input elided to budget (message-aware, all user msgs '
                    'kept): %s → %s (budget %s)',
                    tag, _human_size(full_chars), _human_size(len(formatted)),
                    _human_size(history_budget))

    user_content = _build_summary_user_content(
        anchor_text=anchor_text,
        latest_user_message=current_query,
        formatted_history=formatted,
        formatted_ledger=formatted_ledger,
        prior_receipt=prior_receipt,
        latest_assistant_conclusions=latest_assistant,
    )

    _summary_messages = [
        {'role': 'system', 'content': _SUMMARY_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]

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
    dispatch_options = {'thinking_enabled': False}
    if isinstance(task, dict):
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        cfg = task.get('config') or {}
        model = task.get('model') or cfg.get('model') or cfg.get('preset')

        from lib.tasks_pkg.compaction._summary_routing import select_summary_route
        from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
        summary_route = select_summary_route(
            task, input_tokens=_estimate_total_tokens(_summary_messages),
            output_tokens=_SUMMARY_MAX_TOKENS,
            capacity_output_tokens=dispatch_output_tokens)
        model = summary_route.get('modelId') or model

        if summary_route.get('requestOwned'):
            dispatch_output_tokens = min(dispatch_output_tokens, summary_route['capacityOutputTokens'])
        if isinstance(usage_out, dict):
            usage_out['_summaryRoute'] = summary_route
        request_key = None
        if bounded_summary_task(task):
            # Retain only a digest, never a second transcript. Identical requests
            # share both successful elapsed time and failure across admission stages.
            identity = [_summary_messages, owner_user_id, model,
                        get_pinned_provider() or summary_route.get('providerId') or codex_provider,
                        cfg.get('compaction') or {}]
            if summary_route.get('requestOwned'):
                identity.extend([summary_route.get('routeIdentity'),
                                 summary_route.get('declaredPricing'),
                                 summary_route.get('capacityOutputTokens')])
            digest = hashlib.sha256()
            for chunk in json.JSONEncoder(sort_keys=True).iterencode(identity):
                digest.update(chunk.encode('utf-8'))
            request_key = digest.hexdigest()
        if bounded_summary_task(task) or 'aborted' in task:
            budget = SummaryBudget(task, abort_check, request_key=request_key)
            abort_check = budget.is_set
            if abort_check() or not model:
                budget.finish(False)
                return None
        if expected_route is not None:
            # Revalidate the exact request, but never buy a different route or
            # exceed the quote authorizing this optional cache rewrite.
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
                if budget is not None:
                    budget.finish(
                        False, failure_reason='summary_route_revalidation_failed')
                logger.info('%s Summary route/quote changed since preflight; '
                            'preserving source without dispatch', tag)
                return None
        if summary_route.get('reason') in {
                'conflicting_provider_pins', 'conflicting_tenant_identity'}:
            logger.warning('%s Summary route identity conflict; keeping source intact', tag)
            return None
        if model:
            dispatch_options = dict(prefer_model=model, strict_model=True,
                                    max_429_attempts=2, thinking_enabled=False)

    if model_admission is not None:
        dispatch_options['model_budget_admit'] = model_admission

    call_rows = []
    transport_completed = False

    def _dispatch_transport(call, *args, retry_allowed=None, **kwargs):
        from lib.tasks_pkg.llm_fallback._credential_recovery import call_with_credential_recovery

        def report_retry(attempt, cause):
            logger.warning('%s Summary credential preparation retry attempt=%d source=%s issue=%s',
                           tag, attempt, cause.credential_source, cause.reason)

        return call_with_credential_recovery(
            lambda: call(*args, **kwargs), abort_check=abort_check,
            on_retry=report_retry, retry_allowed=retry_allowed)

    def _record_usage(usage):
        usage = dict(usage) if isinstance(usage, dict) else {}
        from lib.cost import merge_usage_totals
        from lib.tasks_pkg.compaction._summary_cost import price_summary_call
        metadata = usage.get('_dispatch') or {}
        model_id = metadata.get('model') or summary_route.get('modelId') or ''
        provider_id = metadata.get('provider_id') or summary_route.get('providerId')
        call_rows.append({'model': model_id, 'provider_id': provider_id,
                          'usage': dict(usage), 'cost': price_summary_call(
                              usage, model=model_id, provider_id=provider_id,
                              declared_pricing=summary_route.get('declaredPricing'))})
        if isinstance(usage_out, dict):
            merged = merge_usage_totals(usage_out, usage)
            usage_out.clear()
            usage_out.update(merged)
            usage_out['_summaryCalls'] = list(call_rows)
            usage_out['_summaryRoute'] = summary_route
        try:
            from lib.tasks_pkg.compaction._compaction_usage import record_compaction_usage
            record_compaction_usage(conv_id, usage, kind='L2')
        except Exception as exc:
            logger.debug('%s record_compaction_usage failed: %s', tag, exc)

    def _dispatch(capability: str):
        """Return ``(content, usage)`` for either path.

        With ``on_delta`` set we STREAM and forward each delta live; otherwise
        we use the non-streaming ``dispatch_chat`` (byte-identical to before).
        The streaming branch adopts content only after the typed provider
        result proves completion; a half-stream can never become the durable
        compaction summary. Both branches then yield ``(content, usage)``."""
        nonlocal transport_completed
        # Codex subscription slots are streaming-only.  A non-streaming
        # dispatch excludes them and may silently buy the summary from another
        # API provider.  Stream internally even when no UI delta callback was
        # requested, and hard-pin the picker to the subscription provider.
        # A live-task summary streams even without a UI delta callback: the
        # streaming transport's watchdog polls abort_check every 0.5s and
        # interrupts generation mid-flight, while non-streaming chat() is a
        # bare blocking http_post that can only honor abort between retries.
        use_stream = (on_delta is not None or bool(codex_provider)
                      or abort_check is not None)

        # Keep this auxiliary request out of the parent conversation's sticky
        # routing + cache-settle namespace.  The context manager restores the
        # worker thread's parent affinity even when dispatch raises.  Streaming
        # also carries the same identity in the body so Codex's session headers
        # (and prompt_cache_key on profiles that emit it) are stable; dispatching
        # a bare message list would otherwise create a random subscription
        # session.
        from lib.llm_dispatch.conv_affinity import conv_affinity
        affinity_scope = conv_affinity(summary_affinity_id)
        from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
        selected_provider = auxiliary_pin or summary_route.get('providerId') or codex_provider
        existing_pin = get_pinned_provider()
        if selected_provider and existing_pin and selected_provider != existing_pin:
            raise RuntimeError('summary_provider_pin_conflict')
        pin = provider_pin(selected_provider) if selected_provider else nullcontext()
        with affinity_scope, pin:
            if not use_stream:
                chat_content, chat_usage = _dispatch_transport(dispatch_chat,
                    _summary_messages,
                    max_tokens=dispatch_output_tokens, temperature=0,
                    capability=capability, log_prefix=tag,
                    owner_user_id=owner_user_id, abort_check=abort_check,
                    **dispatch_options)
                transport_completed = True
                _record_usage(chat_usage)

                finish = str((chat_usage or {}).get('finish_reason') or '').lower()
                if finish not in {'stop', 'end_turn', 'stop_sequence', 'completed'}:
                    raise IncompleteSummaryError('summary_not_complete:' + (finish or 'missing_terminal'))
                return chat_content, chat_usage

            summary_body = {
                'messages': _summary_messages,
                'max_tokens': dispatch_output_tokens,
                'temperature': 0,
                'stream': True,
            }
            from lib.llm.reasoning import capture_intent
            summary_body['_reasoning_intent'] = capture_intent(
                thinking_enabled=False, temperature=0)
            dispatch_started = time.monotonic()
            first_text_ms = None
            if summary_affinity_id:
                summary_body['_conv_id'] = summary_affinity_id

            def _on_content(chunk: str):
                nonlocal first_text_ms
                if not chunk:
                    return
                if first_text_ms is None:
                    first_text_ms = (time.monotonic() - dispatch_started) * 1000
                if on_delta is None:
                    return
                try:
                    on_delta(chunk)
                except Exception as _cb_e:
                    # Best-effort live UI — a push failure must never abort the
                    # summary generation (the DB rewrite is the source of truth).
                    logger.debug('%s on_delta callback failed: %s', tag, _cb_e)

            with nullcontext():
                from lib.llm.stream_result import (
                    ensure_provider_stream_result, require_verified_provider_stream_result,
                )
                raw_result = _dispatch_transport(dispatch_stream,
                    summary_body,
                    retry_allowed=lambda: first_text_ms is None,
                    on_content=_on_content,
                    max_tokens=dispatch_output_tokens, temperature=0,
                    capability=capability, log_prefix=tag,
                    owner_user_id=owner_user_id, abort_check=abort_check,
                    **dispatch_options)
                transport_completed = True
                stream_result = ensure_provider_stream_result(raw_result)
                # Account observed usage even when completion cannot be adopted.
                observed_usage = getattr(stream_result, 'usage', {})
                _record_usage(observed_usage)
                observed_message = getattr(stream_result, 'message', {})
                metadata = observed_usage.get('_dispatch', {}) if isinstance(observed_usage, dict) else {}
                metadata = metadata if isinstance(metadata, dict) else {}
                thinking = observed_message.get('reasoning_content', '') if isinstance(observed_message, dict) else ''
                logger.info('%s Summary attempt model=%s provider=%s thinking_chars=%d first_text_ms=%s',
                            tag, metadata.get('model', ''), metadata.get('provider_id', ''),
                            len(thinking) if isinstance(thinking, str) else 0, first_text_ms)
                stream_result = require_verified_provider_stream_result(
                    stream_result, context='L2 compaction summary')

                finish_reason = (stream_result.provider_finish_reason
                                 or stream_result.compatibility_finish_reason)
                if finish_reason not in {'stop', 'end_turn', 'stop_sequence', 'completed'}:
                    raise IncompleteSummaryError('summary_not_complete:' + str(finish_reason))
                msg = stream_result.message
                usage = stream_result.usage
            # dispatch_stream returns the assistant message as a dict
            # ({'role': 'assistant', 'content': '...'}), NOT a bare string like
            # dispatch_chat — unwrap it so the shared post-processing (re.sub /
            # .strip below) receives a str. Same canonical unwrap as
            # lib/translate/engine/_engine.py.
            content = msg.get('content', '') if isinstance(msg, dict) else msg
            return content, usage

    auxiliary_lifecycle = ExitStack()
    auxiliary_pin = ''
    dispatch_succeeded = False
    failure_reason = 'summary_dispatch_failed'
    try:

        if summary_route.get('requestOwned'):
            if abort_check and abort_check():
                return None
            from lib.tasks_pkg.compaction._auxiliary_route import mint_auxiliary_route
            principal = task.get('_principalContext') or {}
            group = mint_auxiliary_route(
                summary_route, owner=owner_user_id,
                tenant=principal.get('tenant_id', task.get('_tenant_id')),
                lifecycle=auxiliary_lifecycle)
            auxiliary_pin = group.pin_id
            # Transport selection follows the minted route, never parent OAuth.
            codex_provider = group.pin_id if group.primary.slot.oauth == 'codex' else ''
        try:
            content, usage = _dispatch('text' if dispatch_options.get('strict_model') else 'cheap')
        except LocalRequestPreparationError:
            # Shared recovery has exhausted proven pre-ingress retries. A
            # credential outage never authorizes an unqualified route change.
            raise
        except AbortedError:
            # User Stop — do NOT burn the text-tier fallback; the turn is
            # already unwinding. Decline gracefully (messages stay intact).
            logger.info('%s Summary aborted by user; messages kept intact',
                        tag)
            return None
        except (IncompleteSummaryError, UnverifiedProviderStreamError):
            # A terminal failure is not slot exhaustion. Preserve the source
            # and charge the observed attempt; do not retry unqualified text.
            raise
        except Exception as _cheap_e:
            if (transport_completed or budget is not None
                    or dispatch_options.get('strict_model')):
                # Local validation/accounting failures after returned provider
                # work never authorize purchasing a second summary.
                raise  # A selected model never widens to an unqualified route.
            # Preferred cheap tier failed — retry once against ANY text slot
            # (a deploy may have no model tagged 'cheap' at all). The
            # dispatcher already widens 'cheap'→'text' internally when no
            # cheap slot exists, so this mainly covers a transient cheap-slot
            # exhaustion; it's cheap insurance and makes the intent explicit.
            logger.warning('%s cheap-tier summary dispatch failed (%s: %s) — '
                           'retrying on any text-capable slot',
                           tag, type(_cheap_e).__name__, _cheap_e)
            content, usage = _dispatch('text')

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
        logger.info('%s Summary aborted by user; messages kept intact', tag)
        return None
    except Exception as e:
        if isinstance(e, (IncompleteSummaryError, UnverifiedProviderStreamError)):
            failure_reason = 'summary_incomplete'
        logger.warning('%s Summary dispatch failed (will keep messages intact): %s: %s',
                       tag, type(e).__name__, e)
        return None

    finally:
        try:
            if budget is not None:
                budget.finish(dispatch_succeeded, failure_reason=failure_reason)
        finally:
            primary_error_active = sys.exc_info()[0] is not None
            try:
                auxiliary_lifecycle.close()
            except BaseException as cleanup_error:
                # Preserve an already-unwinding primary failure, but never
                # adopt a summary after a new cleanup failure/cancellation.
                logger.warning('%s Auxiliary route teardown failed: %s',
                               tag, type(cleanup_error).__name__)
                if not primary_error_active:
                    raise

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
