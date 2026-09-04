"""Layer 2 — query-aware LLM summary generation.

Wraps the cheap-model dispatch that turns the OLD conversation region into a
concise working-state snapshot (``_generate_query_aware_summary``).
"""

import hashlib
import re
from contextlib import nullcontext

from lib.log import audit_log, get_logger
from lib.tasks_pkg.compaction._constants import _SUMMARY_MAX_TOKENS
from lib.tasks_pkg.compaction._tokens import _human_size
from lib.tasks_pkg.compaction._layer2._prompt import (
    _SUMMARY_SYSTEM_PROMPT,
    _build_summary_user_content,
    _ensure_summary_objective,
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
    effective_conv_id = str(
        conv_id or ((task or {}).get('convId') if isinstance(task, dict) else '')
        or '').strip()
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
    if not pinned and task.get('convId'):
        try:
            from lib.llm_dispatch import get_dispatcher
            from lib.llm_dispatch.conv_affinity import get_preferred_key
            sticky_key = get_preferred_key(task.get('convId') or '')
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


def _generate_query_aware_summary(messages: list, current_query: str,
                                   log_prefix: str = '',
                                   conv_id: str = '',
                                   task: dict | None = None,
                                   on_delta=None,
                                   usage_out: dict | None = None,
                                   anchor_text: str = '') -> str | None:
    """Call a cheap model to generate a query-aware summary.

    Degrades gracefully so the proactive path actually works on a
    vanilla/exported deploy: the input is capped to the model's real token
    window (see ``_summary_input_char_budget``), and if the preferred
    ``capability='cheap'`` dispatch fails (no model tagged cheap, or the
    single model is momentarily exhausted) it retries once against any
    text-capable slot before giving up.

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
    mutable dict populated with the successful summary call's raw usage so a
    proactive caller can account for the cost before committing a rewrite.
    ``anchor_text`` is the bounded verbatim view of the earliest-request
    anchor (pulled out of the lossy region for live re-insertion, so the
    model would not see it otherwise); ``current_query`` is the newest user
    message, which lives in the preserved region rather than the summary
    input. Both reach the model as VERBATIM EVIDENCE — the model authors the
    receipt's Objective itself (see ``_ensure_summary_objective``), so the
    receipt can track goal replacement across a long conversation.
    """
    from lib.llm_dispatch import dispatch_chat, dispatch_stream

    tag = f'{log_prefix}[Summary]' if log_prefix else '[Summary]'

    _char_budget = _summary_input_char_budget(task)
    formatted_ledger = ''
    if isinstance(task, dict):
        try:
            from lib.tasks_pkg.compaction._evidence import (
                bound_evidence_ledger, build_evidence_ledger,
                format_evidence_ledger)
            # Always provide a small in-memory working-state ledger. Coding
            # turns are dominated by tool-call-only assistant messages and
            # tool results, both intentionally excluded from prose history;
            # without this ledger the summary model cannot see what was read,
            # changed or tested. ``compaction.evidenceLedger=true`` remains
            # the opt-in that additionally persists exact cold results to disk.
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
        - len(anchor_text or '') - 900,
    )
    _full = _format_messages_for_summary(messages)
    formatted = _format_messages_for_summary(
        messages, char_budget=history_budget)

    logger.info('%s Formatting %d messages for summary (%s), query=%.80s',
                tag, len(messages), _human_size(len(formatted)), current_query)
    if len(formatted) < len(_full):
        logger.info('%s Input elided to budget (message-aware, all user msgs '
                    'kept): %s → %s (budget %s)',
                    tag, _human_size(len(_full)), _human_size(len(formatted)),
                    _human_size(history_budget))

    user_content = _build_summary_user_content(
        anchor_text=anchor_text,
        latest_user_message=current_query,
        formatted_history=formatted,
        formatted_ledger=formatted_ledger,
    )

    _summary_messages = [
        {'role': 'system', 'content': _SUMMARY_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]

    codex_provider = _codex_subscription_provider(task)
    summary_affinity_id = _summary_cache_affinity_id(conv_id, task)
    from lib.tasks_pkg.manager import task_user_id
    owner_user_id = task_user_id(task) if isinstance(task, dict) else None

    def _dispatch(capability: str):
        """Return ``(content, usage)`` for either path.

        With ``on_delta`` set we STREAM and forward each delta live; otherwise
        we use the non-streaming ``dispatch_chat`` (byte-identical to before).
        The streaming branch adopts content only after the typed provider
        result proves completion; a half-stream can never become the durable
        compaction summary. Both branches then yield ``(content, usage)``."""
        # Codex subscription slots are streaming-only.  A non-streaming
        # dispatch excludes them and may silently buy the summary from another
        # API provider.  Stream internally even when no UI delta callback was
        # requested, and hard-pin the picker to the subscription provider.
        use_stream = on_delta is not None or bool(codex_provider)

        # Keep this auxiliary request out of the parent conversation's sticky
        # routing + cache-settle namespace.  The context manager restores the
        # worker thread's parent affinity even when dispatch raises.  Streaming
        # also carries the same identity in the body so Codex's session headers
        # (and prompt_cache_key on profiles that emit it) are stable; dispatching
        # a bare message list would otherwise create a random subscription
        # session.
        from lib.llm_dispatch.conv_affinity import conv_affinity
        affinity_scope = conv_affinity(summary_affinity_id)
        with affinity_scope:
            if not use_stream:
                return dispatch_chat(
                    _summary_messages,
                    max_tokens=_SUMMARY_MAX_TOKENS, temperature=0,
                    capability=capability, log_prefix=tag,
                    owner_user_id=owner_user_id)

            summary_body = {
                'messages': _summary_messages,
                'max_tokens': _SUMMARY_MAX_TOKENS,
                'temperature': 0,
                'stream': True,
            }
            if summary_affinity_id:
                summary_body['_conv_id'] = summary_affinity_id

            def _on_content(chunk: str):
                if not chunk:
                    return
                try:
                    on_delta(chunk)
                except Exception as _cb_e:
                    # Best-effort live UI — a push failure must never abort the
                    # summary generation (the DB rewrite is the source of truth).
                    logger.debug('%s on_delta callback failed: %s', tag, _cb_e)

            try:
                from lib.llm_dispatch.provider_pin import (
                    get_pinned_provider, provider_pin)
                pin = (provider_pin(codex_provider)
                       if codex_provider and not get_pinned_provider()
                       else nullcontext())
            except Exception as exc:
                logger.debug('%s provider pin unavailable: %s', tag, exc)
                pin = nullcontext()

            with pin:
                from lib.llm.stream_result import (
                    require_verified_provider_stream_result,
                )
                stream_result = require_verified_provider_stream_result(
                    dispatch_stream(
                    summary_body,
                    on_content=_on_content if on_delta is not None else None,
                    max_tokens=_SUMMARY_MAX_TOKENS, temperature=0,
                    capability=capability, log_prefix=tag,
                    owner_user_id=owner_user_id),
                    context='L2 compaction summary')
                msg = stream_result.message
                usage = stream_result.usage
            # dispatch_stream returns the assistant message as a dict
            # ({'role': 'assistant', 'content': '...'}), NOT a bare string like
            # dispatch_chat — unwrap it so the shared post-processing (re.sub /
            # .strip below) receives a str. Same canonical unwrap as
            # lib/translate/engine/_engine.py.
            content = msg.get('content', '') if isinstance(msg, dict) else msg
            return content, usage

    try:
        try:
            content, usage = _dispatch('cheap')
        except Exception as _cheap_e:
            # Preferred cheap tier failed — retry once against ANY text slot
            # (a deploy may have no model tagged 'cheap' at all). The
            # dispatcher already widens 'cheap'→'text' internally when no
            # cheap slot exists, so this mainly covers a transient cheap-slot
            # exhaustion; it's cheap insurance and makes the intent explicit.
            logger.warning('%s cheap-tier summary dispatch failed (%s: %s) — '
                           'retrying on any text-capable slot',
                           tag, type(_cheap_e).__name__, _cheap_e)
            content, usage = _dispatch('text')

    except Exception as e:
        logger.warning('%s Summary dispatch failed (will keep messages intact): %s: %s',
                       tag, type(e).__name__, e)
        return None

    if not isinstance(content, str) or not content.strip():
        logger.warning('%s Summary model returned empty or non-text content', tag)
        return None

    usage = usage if isinstance(usage, dict) else {}
    if isinstance(usage_out, dict):
        usage_out.clear()
        usage_out.update(usage)
    in_tok = usage.get('prompt_tokens', 0)
    out_tok = usage.get('completion_tokens', 0)
    # Count this summary call's tokens toward the conversation's compaction
    # cost. Accounting is advisory and cannot invalidate usable model output.
    try:
        from lib.tasks_pkg.compaction._compaction_usage import (
            record_compaction_usage)
        record_compaction_usage(conv_id, usage, kind='L2')
    except Exception as exc:
        logger.debug('%s record_compaction_usage failed: %s', tag, exc)

    # Dispatch and receipt shaping are separate fault domains. Once a complete
    # provider result exists, a local sanitizer/objective bug must never turn
    # it into ``None`` and force the original oversized prompt back through
    # admission. Each stage keeps the last usable text on failure.
    processed = content
    try:
        processed = re.sub(
            r'<analysis>.*?</analysis>\s*',
            '', processed, flags=re.DOTALL,
        )
    except Exception as exc:
        logger.warning(
            '%s Summary post-processing degraded at analysis stripping '
            '(%s); retaining generated content', tag, type(exc).__name__)
        _record_postprocess_degradation(
            conv_id=conv_id, stage='analysis_strip', exc=exc)

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
    logger.info('%s Summary generated: %d chars  in=%d  out=%d tokens',
                tag, len(processed), in_tok, out_tok)
    return processed
