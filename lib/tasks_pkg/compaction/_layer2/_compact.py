"""Layer 2 — public entrypoints for query-aware context compaction.

  * ``execute_compact_tool``    — generates the summary, mutates messages.
  * ``force_compact_if_needed`` — gates on threshold + records accepted handoff.
"""

import json
import math
import re
import time

from copy import deepcopy
from lib.log import get_logger
from lib.tasks_pkg.compaction._summary_budget import summary_failure_reason
from lib.tasks_pkg.compaction._archive import _archive_transcript, summary_dispatch_calls
from lib.tasks_pkg.compaction._constants import (
    _cooldown_lock,
    _MAX_PRESERVE_TURNS,
    _PRESERVE_BUDGET_RATIO,
    _SUMMARY_MAX_TOKENS,
    _AUTO_COMPACT_MIN_PAYBACK_ROUNDS,
    _AUTO_COMPACT_MIN_REDUCTION_RATIO,
    _summary_cooldowns,
)
from lib.tasks_pkg.compaction._tokens import (
    _compaction_trigger_threshold,
    _estimate_total_tokens,
    _get_context_limit,
    _record_compaction_cadence,
    _should_force_compact,
    _usable_context,
)
from lib.tasks_pkg.compaction._cache_economics import (
    _proactive_cache_economics, _proactive_payback_policy,
    main_route_pricing_verified as _main_route_pricing_verified,
)
from lib.tasks_pkg.compaction._receipt import (
    build_compaction_receipt,
    summary_usage_details,
)
from lib.tasks_pkg.compaction._layer2._anchor import (
    _extract_current_query,
    _extract_objective_anchor_text,
    _extract_recently_accessed_files,
    _plan_cross_turn_tail,
    _objective_anchor_index,
)
from lib.tasks_pkg.compaction._layer2._summary import (
    _generate_query_aware_summary, _summary_request_input_tokens,
)
from lib.tasks_pkg.compaction._layer2._projection import (
    _retained_projection as _retained_projection,
    _with_restoration_reserve as _with_restoration_reserve,
    converge_compaction_candidate,
)
from lib.tasks_pkg.compaction._layer2._summary_acceptance import accept_summary
from lib.tasks_pkg.compaction._layer2._candidate_policy import (
    EconomicPolicyPorts, admit_compaction_candidate, admit_compaction_sources,
    defers_for_unverified_pricing,
)
from lib.tasks_pkg.compaction._layer2._source_plan import (
    SourcePlanningPorts, plan_compaction_sources,
)
from lib.tasks_pkg.compaction._layer2._preparation import prepare_l2_projection
from lib.tasks_pkg.compaction._handoff import (
    HANDOFF_FIELD, HANDOFF_FORMAT, compaction_phase, has_open_tool_batch,
    insert_compaction_handoff, is_compaction_handoff, make_compaction_handoff,
)
from lib.tasks_pkg.compaction._coverage import summary_coverage
from lib.tasks_pkg.compaction._history import (
    history_entries, is_human_message,
    coalesce_history_carriers, make_history_carrier,
)
from lib.tasks_pkg.compaction._layer2._prompt import _extract_summary_objective

logger = get_logger(__name__)

_USER_VERBATIM_AUDIT_LATCH = {'done': False}
_DETERMINISTIC_RECOVERY_SNAPSHOT_CHARS = 12_000
_DETERMINISTIC_RECOVERY_EVIDENCE_CHARS = 8_000
_DETERMINISTIC_RECOVERY_MAX_CHARS = 22_000


def _task_owner(task):
    from lib.tasks_pkg.manager import task_user_id

    return task_user_id(task)


def _audit_user_verbatim_once() -> None:
    """One-time §10.1 audit entry for the user-verbatim hyperparameters."""
    if _USER_VERBATIM_AUDIT_LATCH['done']:
        return
    _USER_VERBATIM_AUDIT_LATCH['done'] = True
    try:
        from lib.log import audit_log
        from lib.tasks_pkg.compaction._constants import (
            _USER_VERBATIM_BUDGET_TOKENS,
            _USER_VERBATIM_MAX_MESSAGES,
        )
        audit_log('config_change', change='user_verbatim_retention',
                  budget_tokens=_USER_VERBATIM_BUDGET_TOKENS,
                  max_messages=_USER_VERBATIM_MAX_MESSAGES,
                  approved_by='user')
    except Exception as e:
        logger.debug('[Compact] user-verbatim config audit skipped: %s', e)


def _bounded_task_state_text(snapshot, *, max_chars: int) -> str:
    """Render a valid, globally bounded TaskStateSnapshotV1 JSON object."""
    limit = max(1_000, int(max_chars))
    raw = snapshot.to_dict()
    payload = {}
    sequences: dict[str, list] = {}
    for key, value in raw.items():
        if isinstance(value, (list, tuple)):
            payload[key] = []
            sequences[key] = list(value)
        elif isinstance(value, str):
            scalar_limit = 4_000 if key == 'goal' else 1_000
            payload[key] = value[:scalar_limit]
        else:
            payload[key] = value

    def _render() -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        )

    rendered = _render()
    while len(rendered) > limit:
        candidates = [
            key for key, value in payload.items()
            if isinstance(value, str) and value
        ]
        if not candidates:
            break
        key = max(candidates, key=lambda candidate: len(payload[candidate]))
        overflow = len(rendered) - limit
        payload[key] = payload[key][:-max(1, overflow)]
        rendered = _render()

    # Round-robin allocation prevents one large state category from starving
    # all later categories. Every accepted candidate keeps the JSON valid.
    longest = max((len(values) for values in sequences.values()), default=0)
    for index in range(longest):
        for key, values in sequences.items():
            if index >= len(values):
                continue
            payload[key].append(str(values[index])[:600])
            rendered = _render()
            if len(rendered) > limit:
                payload[key].pop()
                rendered = _render()
    return rendered


def _deterministic_recovery_summary(
    messages: list, task: dict | None,
) -> str:
    """Build a provider-independent, bounded emergency compaction receipt.

    This path is reserved for the final dispatch-admission guard. It makes no
    completion claims from assistant prose: state and evidence are projected
    from the current transcript/tool records, while user instructions and the
    recent hot tail stay verbatim elsewhere in the compacted request.
    """
    sections = [
        '## Deterministic Compaction Recovery\n'
        'A model-written summary was unavailable. This receipt contains only '
        'bounded projections from the current transcript and task records; it '
        'does not claim that mutable files or tests are still current. The '
        'opening request, a bounded set of earlier user messages, and recent '
        'complete rounds remain separately preserved in the compacted request.',
    ]

    try:
        from lib.tasks_pkg.context_composer.task_state import (
            derive_task_state_snapshot,
        )
        snapshot = derive_task_state_snapshot(messages, task)
        snapshot_text = _bounded_task_state_text(
            snapshot,
            max_chars=_DETERMINISTIC_RECOVERY_SNAPSHOT_CHARS,
        )
        sections.append('## TaskStateSnapshotV1\n' + snapshot_text)
    except Exception as exc:
        logger.warning(
            '[Compact] deterministic task-state projection failed (%s)',
            type(exc).__name__,
        )
        sections.append(
            '## TaskStateSnapshotV1\n'
            'Projection unavailable because local extraction failed; no '
            'model-generated replacement claims were inserted.')

    try:
        from lib.tasks_pkg.compaction._evidence import (
            bound_evidence_ledger,
            build_evidence_ledger,
            format_evidence_ledger,
        )
        ledger = ((task or {}).get('_contextEvidenceLedger')
                  if isinstance(task, dict) else None)
        if not isinstance(ledger, dict):
            ledger = build_evidence_ledger(messages, task)
        ledger = bound_evidence_ledger(
            ledger,
            max_chars=_DETERMINISTIC_RECOVERY_EVIDENCE_CHARS,
        )
        if isinstance(task, dict):
            task['_contextEvidenceLedger'] = ledger
        evidence_text = format_evidence_ledger(ledger).strip()
        if evidence_text:
            sections.append(evidence_text)
    except Exception as exc:
        logger.warning(
            '[Compact] deterministic evidence projection failed (%s)',
            type(exc).__name__,
        )

    rendered = '\n\n'.join(sections).strip()
    if len(rendered) <= _DETERMINISTIC_RECOVERY_MAX_CHARS:
        return rendered
    # The evidence section is advisory and independently reconstructible.
    # Drop it whole rather than cutting JSON or an evidence record mid-entry.
    rendered_without_evidence = '\n\n'.join(sections[:2]).strip()
    if len(rendered_without_evidence) <= _DETERMINISTIC_RECOVERY_MAX_CHARS:
        return rendered_without_evidence
    return sections[0]


def _summary_covers_state_value(summary: str, value: str) -> bool:
    """Conservatively detect whether a mutable state value survived prose."""
    candidate = ' '.join(str(value or '').lower().split())
    rendered = ' '.join(str(summary or '').lower().split())
    if not candidate or candidate[:120] in rendered:
        return True
    terms = list(dict.fromkeys(re.findall(
        r'[a-z0-9_./:-]{3,}|[\u3400-\u9fff]', candidate, re.I)))
    if not terms:
        return False
    required = 1 if len(terms) <= 3 else min(4, max(2, len(terms) // 4))
    return sum(term in rendered for term in terms) >= required



def _summary_usage_tokens(usage: dict | None) -> int:
    return int(summary_usage_details(usage).get('totalTokens') or 0)


def _projected_summary_usage_tokens(
    messages: list,
    current_query: str,
    task: dict | None,
    anchor_text: str = '',
    source_messages: list | None = None,
) -> int:
    """Pre-dispatch usage projection for proactive ROI gating.

    The old best-case check assumed a free summary, then sometimes paid for a
    real summary only to reject it as cache-negative. Price the request the
    summary dispatch will send, plus the advisory output projection, so
    economic declines happen before money and wall time are spent. Dispatch
    refuses any re-quote above this one, so this must be the dispatched
    composition (archived prior receipt and ledger policy included), not an
    approximation of its shape.
    """
    try:
        return max(1, _summary_request_input_tokens(
            messages, current_query, task=task,
            source_messages=source_messages, anchor_text=anchor_text,
        ) + _SUMMARY_MAX_TOKENS)
    except Exception as exc:
        logger.debug('[Compact] summary-cost projection failed: %s', exc)
        return max(1, _SUMMARY_MAX_TOKENS * 2)


def _proactive_retry_growth_tokens(
    tokens_before: int,
    *,
    reason: str,
    economics: dict | None,
) -> int:
    """Return the earliest prompt growth that could reverse a decline.

    The proof is deliberately optimistic: every future token is assumed to be
    droppable while rewrite/summary cost stays flat. If even that best case
    cannot meet the policy threshold, repeating the full candidate build is
    guaranteed waste. Cache-witness changes and the hard window gate bypass
    this floor in ``_should_force_compact``.
    """
    total = max(0, int(tokens_before))
    growth = max(8_192, int(total * 0.05))
    if not isinstance(economics, dict):
        return growth

    dropped = max(0, int(economics.get('dropped_tokens') or 0))
    proof_growth = 0
    if reason == 'low_yield':
        target = max(0.0, min(
            0.99, float(_AUTO_COMPACT_MIN_REDUCTION_RATIO)))
        deficit = target * total - dropped
        if deficit > 0 and target < 1.0:
            proof_growth = math.ceil(deficit / (1.0 - target))
    elif reason == 'cache_negative':
        marginal_savings = max(0.0, float(economics.get(
            'marginal_savings_per_dropped_token',
            economics.get('cache_read_mul') or 0.0)))
        try:
            target_rounds = float(economics.get(
                'payback_limit_rounds', _AUTO_COMPACT_MIN_PAYBACK_ROUNDS))
        except (TypeError, ValueError, OverflowError):
            target_rounds = float(_AUTO_COMPACT_MIN_PAYBACK_ROUNDS)
        if not math.isfinite(target_rounds):
            target_rounds = float(_AUTO_COMPACT_MIN_PAYBACK_ROUNDS)
        target_rounds = max(0.0, target_rounds)
        rewrite_cost = max(
            0.0, float(economics.get('rewrite_cost_tokens') or 0.0))
        summary_cost = max(
            0.0, float(economics.get('summary_cost_tokens') or 0.0))
        denominator = marginal_savings * target_rounds
        total_cost = rewrite_cost + summary_cost
        if denominator > 0 and math.isfinite(total_cost):
            required_dropped = math.ceil(total_cost / denominator)
            proof_growth = max(0, required_dropped - dropped)
    return max(growth, proof_growth)


def _defer_proactive_retry(
    task: dict | None,
    tokens_before: int,
    *,
    reason: str = '',
    economics: dict | None = None,
) -> int:
    """Record a bounded task-local retry floor after an economic decline."""
    if not isinstance(task, dict):
        return 0
    growth = _proactive_retry_growth_tokens(
        tokens_before, reason=reason, economics=economics)
    floor = max(0, int(tokens_before)) + growth
    previous_floor = int(task.get('_autoCompactRetryAfterTokens') or 0)
    task['_autoCompactRetryAfterTokens'] = max(floor, previous_floor)
    if floor >= previous_floor:
        warm_prefix = int((economics or {}).get('warm_prefix_tokens') or 0)
        if reason == 'cache_negative' and warm_prefix > 0:
            try:
                payback_limit = float((economics or {}).get(
                    'payback_limit_rounds',
                    _AUTO_COMPACT_MIN_PAYBACK_ROUNDS))
            except (TypeError, ValueError, OverflowError):
                payback_limit = float(_AUTO_COMPACT_MIN_PAYBACK_ROUNDS)
            task['_autoCompactRetryWitness'] = {
                'reason': reason,
                'warmPrefixTokens': warm_prefix,
                'paybackLimitRounds': payback_limit,
            }
        else:
            task.pop('_autoCompactRetryWitness', None)
    return int(task['_autoCompactRetryAfterTokens'])


# ═══════════════════════════════════════════════════════════════════════════════
#  Core: execute_compact_tool — pure LLM summary with selective turn compression
# ═══════════════════════════════════════════════════════════════════════════════

def _persist_compaction_candidate(messages, task, meta):
    """Commit the accepted bounded model view before adopting a durable Turn candidate."""
    archive_id = meta.get('archive_id')
    summary_text = meta.get('summary_text') or ''
    tokens_after = _estimate_total_tokens(messages)
    msgs_after = len(messages)
    from lib.tasks_pkg.compaction._continuity import read_continuity
    continuity = read_continuity(messages, task)
    retained = [entry['id'] for entry in (continuity or {}).get('observations', [])]
    lost = []
    budget_decision = (task or {}).get('_contextBudgetDecision') or {}
    prompt_evidence = (task or {}).get('_lastPromptAdmission') or {}
    context_budget = {**budget_decision, **prompt_evidence}
    context_budget['decisionKind'] = (
        'economic' if meta.get('economics') else
        'manual' if meta.get('trigger') == 'manual' else 'safety')

    receipt = build_compaction_receipt(
        trigger=meta.get('trigger') or 'force',
        status='completed',
        strategy='selective_summary',
        implementation=(
            'deterministic_recovery_receipt'
            if meta.get('summaryFallback')
            else 'deterministic_task_state_projection'
            if meta.get('summaryRejected')
            else 'model_summary'),
        mode=meta.get('mode') or 'turns',
        continuation_format=HANDOFF_FORMAT,
        continuation_phase=meta.get('handoff_phase') or '',
        continuity_digest=meta.get('continuityDigest') or '',
        summary_generated=not bool(meta.get('summaryFallback')),
        summary_text=summary_text,
        summary_usage=meta.get('summary_usage'),
        summary_duration_ms=meta.get('summary_duration_ms') or 0,
        projected_summary_usage_tokens=(
            meta.get('projected_summary_usage_tokens') or 0),
        summary_rejected=bool(meta.get('summaryRejected')),
        summary_rejection_reason=(
            meta.get('summaryRejectionReason') or ''),
        outcome_reason=(
            meta.get('summaryFallbackReason') or ''),
        summarized_messages=meta.get('summarized_messages') or 0,
        preserved_turns=meta.get('preserved_turns') or 0,
        folded_tool_rounds=meta.get('folded_tool_rounds') or 0,
        objective_anchored=bool(meta.get('objective_anchored')),
        durable_objective_applied=bool(
            meta.get('durable_objective_applied')),
        retained_user_messages=meta.get('retained_user_messages') or 0,
        recent_files=meta.get('recent_files') or (),
        turn_diff_included=bool(meta.get('turn_diff_included')),
        economics=meta.get('economics'),
        context_budget=context_budget,
        evidence_retained=retained,
        evidence_lost=lost,
    )
    if archive_id is not None:
        try:
            if receipt.get('summary', {}).get('accepted'):
                try:
                    from lib.tasks_pkg.compaction._resume_projection import compaction_resume_witness
                    witness = compaction_resume_witness(task)
                    if witness is not None:
                        receipt['resumeWitness'] = witness
                except Exception as exc:
                    logger.debug('[Compact] resume witness unavailable: %s', type(exc).__name__)
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().update_archive_summary(
                archive_id, summary_text, tokens_after, msgs_after,
                user_id=_task_owner(task), receipt=receipt,
                archive_scope=(task or {}).get('_archiveScope', ''),
                post_messages=deepcopy(messages))
        except Exception as exc:
            if task and task.get('aborted'):
                return receipt
            if task and task.get('_turnId'):
                from lib.tasks_pkg.compaction._archive import fail_compaction_archive
                fail_compaction_archive(task, archive_id, meta, exc)
            logger.warning('[Compact] archive row update failed: %s', type(exc).__name__)

    return receipt


def _settle_failed_compaction_attempt(task, result_meta, messages, *, conv_id: str,
                                      tokens_before: int, msgs_before: int,
                                      summary_usage: dict,
                                      summary_duration_ms: int,
                                      trigger: str, round_num: int,
                                      outcome_reason: str,
                                      implementation: str = 'summary_dispatch') -> None:
    """Give a non-adopting summary attempt an authentic terminal UI fact.

    ``force_compact_if_needed`` (working_set/window) emits a durable
    ``COMPACTION`` archive row before the summary dispatch, but when the
    summary then fails the attempt returned False without any settlement —
    the row stayed ``running`` forever (the muu1klptjtwd3t symptom). The
    admission (dispatch_guard) pass emits no archive at all, leaving the
    transient ``Phase.COMPACTING`` beats as the only durable fact.
    This helper:
      1. settles the same-attempt ``Phase.COMPACTING`` row via the
         ``reactive archive`` bridge the fold layer already watches
         (``task['_reactiveCompactions']`` — the done event re-uses its
         ``archiveId``), and
      2. emits a terminal ``COMPACTION_DONE`` whose receipt carries the real
         failure.  A pending archive row is updated + settled through it;
         an admission attempt with no archive emits the archive-less
         failed-DONE the fold layer already accepts (span ``failed:{seq}``).
    Best effort only: observability never aborts a compaction return path.
    """
    if task is None:
        return
    meta = result_meta if isinstance(result_meta, dict) else {}
    try:
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
        from lib.tasks_pkg.compaction._ownership import compaction_owner_key
        from lib.tasks_pkg.manager import append_event
        from lib.tasks_pkg.manager import task_user_id
        tokens_after = _estimate_total_tokens(messages) or int(tokens_before)
        receipt = build_compaction_receipt(
            trigger=trigger or 'force', status='failed',
            strategy='unchanged', implementation=implementation,
            continuation_format='none',
            summary_usage=summary_usage, summary_duration_ms=summary_duration_ms,
            outcome_reason=outcome_reason or 'summary_failed')
        archive_id = meta.get('archive_id')
        if archive_id is not None:
            try:
                from lib.agent_core.store import get_conversation_store
                get_conversation_store().update_archive_summary(
                    archive_id, '', int(tokens_after), int(msgs_before),
                    user_id=task_user_id(task),
                    archive_scope=task.get('_archiveScope', ''),
                    receipt=receipt, post_messages=None)
            except Exception as exc:
                logger.debug('[Compact] failed-attempt archive update failed: %s', exc)
        # Bridge the live phase row: the fold layer swaps a pending
        # reactive-archive placeholder into the Phase.COMPACTING row, and
        # this done event settles through that same archiveId — one authentic
        # failed compaction row instead of a stuck "compressing" ghost.
        bridge_id = f'summary-failed:{(task.get("id") or "")[:8]}:{int(round_num or 0)}'
        reactive = task.get('_reactiveCompactions')
        if not isinstance(reactive, dict):
            reactive = {}
            task['_reactiveCompactions'] = reactive
        reactive[bridge_id] = {
            'trigger': trigger or 'force', 'roundNum': int(round_num or 0),
            'tokensBefore': int(tokens_before), 'msgsBefore': int(msgs_before),
        }
        reduction = 1 - (tokens_after / max(1, tokens_before))
        append_event(task, build_event(
            EventType.COMPACTION_DONE, archiveId=bridge_id,
            convId=compaction_owner_key(task, conv_id), trigger=trigger or 'force',
            tokensBefore=int(tokens_before), tokensAfter=int(tokens_after),
            tokenCountKind='estimated', msgsBefore=int(msgs_before),
            msgsAfter=int(msgs_before), reductionPct=round(reduction * 100, 1),
            roundNum=int(round_num or 0), receipt=receipt,
            summaryDispatchCalls=summary_dispatch_calls(summary_usage),
            **(meta.get('activityBoundary') or {})))
    except Exception as exc:
        logger.debug('[Compact] failed-attempt settlement failed: %s', exc)

def _announce_summary_start(task: dict | None, pfx: str) -> None:
    """Publish accepted summary work without making telemetry a prerequisite."""
    if task is not None:
        try:
            from lib.agent_core.events import Phase, build_phase
            from lib.tasks_pkg.manager import append_event
            append_event(task, build_phase(
                Phase.COMPACTING,
                detail='Compressing earlier context to fit the window…',
                detailKey='stream.phase.compactingWindow'))
        except Exception as exc:
            logger.debug('%s [Compact] phase emit failed: %s', pfx, exc)


def _compaction_handoff(compact_result: str, phase, task, messages, summary_input, kwargs, digest) -> dict:
    """The handoff naming the Turns it folds and the owner-scheduled request it settles.

    An owner-scheduled request stays proven when a later summary folds it.
    """
    scheduled_request = kwargs.get('_scheduled_request_id') or next((
        message[HANDOFF_FIELD]['scheduledRequestId']
        for message in reversed(summary_input)
        if is_compaction_handoff(message)
        and isinstance(message[HANDOFF_FIELD].get('scheduledRequestId'), str)), '')
    return make_compaction_handoff(
        compact_result, phase=phase, coverage=summary_coverage(task, messages, summary_input),
        scheduled_request_id=scheduled_request, continuity_digest=digest)


def execute_compact_tool(messages: list, task: dict | None = None, **kwargs) -> str:
    """Build and adopt one local continuation projection at a closed boundary.

    Historical entry-point name only: not a model tool or durable Turn write.
    The caller owns admission/economics; this owner installs the full handoff
    transactionally after generation and cancellation checks.
    """
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    conv_id = compaction_owner_key(task)
    log_id = conv_id[:8] if conv_id else '?'
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    # Optional out-param: caller passes a mutable dict to learn whether
    # messages were actually mutated. Stays False on every early-return
    # failure path; flipped to True only after the message list is
    # replaced.  reactive_compact relies on this so its head-truncate
    # safety net engages when the LLM summary comes back empty.
    _result_meta = kwargs.get('_result_meta')
    if not isinstance(_result_meta, dict):
        _result_meta = {}
    if isinstance(_result_meta, dict):
        _result_meta['compacted'] = False

    def cancelled() -> bool:
        # Generation, deterministic recovery, file hints and archive I/O can
        # all yield to owner Stop. No candidate may commit after observing it.
        if not (task and task.get('aborted')):
            return False
        if isinstance(_result_meta, dict):
            _result_meta.update(compacted=False, reason='aborted',
                                summaryFailureReason='aborted')
        return True

    if cancelled():
        return 'Context compaction cancelled — messages preserved as-is.'

    _handoff_phase = compaction_phase(messages)
    if has_open_tool_batch(messages):
        if isinstance(_result_meta, dict):
            _result_meta['reason'] = 'open_tool_batch'
        return 'Context compaction deferred — an execution batch is still open.'

    _premeasured_tokens = kwargs.get('_message_tokens_before') if kwargs else None
    if (isinstance(_premeasured_tokens, int)
            and not isinstance(_premeasured_tokens, bool)
            and _premeasured_tokens >= 0):
        tokens_before = _premeasured_tokens
    else:
        tokens_before = _estimate_total_tokens(messages)
    msg_count_before = len(messages)
    if isinstance(_result_meta, dict):
        _result_meta.update({
            'tokens_before': int(tokens_before),
            'msgs_before': int(msg_count_before),
        })
    context_limit = _get_context_limit(task)
    usable = _usable_context(context_limit)

    budget_override = kwargs.get('preserve_budget_tokens') if kwargs else None
    if budget_override is not None:
        budget_tokens = max(1, int(budget_override))
    else:
        budget_tokens = max(1, int(usable * _PRESERVE_BUDGET_RATIO))

    _krp = kwargs.get('keep_recent_pairs') if kwargs else None
    max_turns = _MAX_PRESERVE_TURNS if _krp is None else max(1, int(_krp))

    logger.info('%s [Compact] Starting  conv=%s  tokens=%d  usable=%d  messages=%d  '
                'budget=%d  max_turns=%d',
                pfx, log_id, tokens_before, usable, msg_count_before,
                budget_tokens, max_turns)

    current_query = _extract_current_query(messages)
    # The earliest-request anchor is re-supplied to the summary model as
    # VERBATIM evidence (it is pulled out of ``summary_input`` below for live
    # re-insertion, so the model would not see it otherwise); the model
    # authors the receipt's Objective itself from that evidence.
    anchor_text = _extract_objective_anchor_text(messages)

    recent_files = _extract_recently_accessed_files(messages)
    _recent_files_block = ''
    if recent_files:
        file_list = '\n'.join(f'  - {f}' for f in recent_files)
        _recent_files_block = (
            f'\n\n### Recently Accessed Files\n'
            f'Use read_files to review current state if needed:\n'
            f'{file_list}'
        )

    # An optional attempt without verified main-route rates defers whatever it
    # plans. It builds no continuation, so it skips the journal-backed turn
    # diff and summary-route selection; one verdict serves both skips.
    _unverified_pricing_deferral = defers_for_unverified_pricing(
        task, kwargs, tokens_before=tokens_before, usable=usable,
        pricing_verified=_main_route_pricing_verified)

    # Codex-inspired (turn_diff_tracker.rs): the summary must preserve WHAT
    # changed this turn, not just WHICH files. Journal pre-images make this
    # free of extra writes; failures are advisory-only.
    _turn_diff_included = False
    _diff_block = ''
    if not _unverified_pricing_deferral:
        try:
            from lib.tasks_pkg.commit_round._turn_diff import build_turn_diff_block
            _cfg = (task or {}).get('config', {}) or {}
            _diff_block = build_turn_diff_block(
                task, _cfg.get('projectPath') or '',
                _cfg.get('projectPaths') or None)
            if _diff_block:
                _turn_diff_included = True
        except Exception as e:
            logger.debug('%s [Compact] turn-diff block failed: %s', pfx, e)


    planning_messages = (messages if _unverified_pricing_deferral
                         else prepare_l2_projection(messages, task))
    if cancelled():
        return 'Context compaction cancelled — messages preserved as-is.'
    sources = plan_compaction_sources(
        messages, task, budget_tokens=budget_tokens, max_turns=max_turns,
        handoff_phase=_handoff_phase, recent_files_block=_recent_files_block,
        turn_diff_block=_diff_block, options=kwargs, result_meta=_result_meta,
        tokens_before=tokens_before, log_prefix=pfx,
        ports=SourcePlanningPorts(
            plan_tail=_plan_cross_turn_tail, anchor_index=_objective_anchor_index,
            retained_projection=lambda *args: _retained_projection(*args, task=task),
            restoration_reserve=lambda *args: _with_restoration_reserve(*args, task=task),
        ),
        planning_messages=planning_messages,
    )
    if isinstance(sources, str):
        return sources

    economic_ports = EconomicPolicyPorts(
        estimate_tokens=_estimate_total_tokens,
        projected_summary_usage=_projected_summary_usage_tokens,
        payback_policy=_proactive_payback_policy, defer_retry=_defer_proactive_retry,
        cache_economics=_proactive_cache_economics,
        summary_usage_tokens=_summary_usage_tokens,
    )
    policy = admit_compaction_sources(
        messages, task, sources, options=kwargs, tokens_before=tokens_before,
        usable=usable, current_query=current_query, anchor_text=anchor_text,
        result_meta=_result_meta, log_prefix=pfx, ports=economic_ports,
        unverified_pricing_deferral=_unverified_pricing_deferral,
    )
    if isinstance(policy, str):
        return policy

    preserved_turns = sum(is_human_message(m) for m in sources.recent_messages)

    logger.info('%s [Compact] Summarizing %d messages (%d old + %d cold intra-turn '
                'rounds), preserving %d recent (%d turns), query=%.100s',
                pfx, len(sources.summary_input), len(sources.old_messages), len(sources.cold_round_msgs),
                len(sources.recent_messages), preserved_turns, current_query)

    _summary_usage: dict = {}
    _summary_started = time.monotonic()
    _summary_pipeline_failure_reason = ''
    # Economic/eligibility preflights above may decline without model I/O.
    # Once accepted, announce the blocking work BEFORE it starts.
    _announce_summary_start(task, pfx)
    try:
        summary_text = _generate_query_aware_summary(
            sources.summary_input, current_query, pfx, conv_id=conv_id, task=task,
            usage_out=_summary_usage,
            anchor_text=anchor_text,
            source_messages=messages,
            **({'output_budget_tokens': sources.summary_output_budget}
               if sources.summary_output_budget is not None else {}),
            **({'expected_route': policy.summary_route} if policy.active else {}),
        )
    except Exception as exc:
        if not kwargs.get('_allow_deterministic_summary_fallback'):
            raise
        _summary_pipeline_failure_reason = 'summary_pipeline_exception'
        summary_text = None
        logger.exception(
            '%s [Compact] Summary pipeline raised (%s) — attempting '
            'deterministic recovery', pfx, type(exc).__name__)
    _summary_duration_ms = round(
        (time.monotonic() - _summary_started) * 1000)

    if cancelled():
        return 'Context compaction cancelled — messages preserved as-is.'
    _summary_fallback = False
    _summary_fallback_reason = ''
    from lib.tasks_pkg.compaction._summary_budget import bounded_summary_task
    if (not summary_text
            and not bounded_summary_task(task)
            and kwargs.get('_allow_deterministic_summary_fallback')):
        try:
            summary_text = _deterministic_recovery_summary(messages, task)
        except Exception as exc:
            _summary_pipeline_failure_reason = 'deterministic_recovery_failed'
            summary_text = None
            logger.exception(
                '%s [Compact] Deterministic recovery pipeline raised (%s)',
                pfx, type(exc).__name__)
        _summary_fallback = bool(summary_text)
        if _summary_fallback:
            _summary_fallback_reason = (
                _summary_pipeline_failure_reason
                or 'model_summary_unavailable')
            logger.warning(
                '%s [Compact] Summary model unavailable — using bounded '
                'deterministic recovery receipt', pfx)
            try:
                from lib.log import audit_log
                audit_log(
                    'compaction_summary_fallback',
                    conv=str(conv_id or '')[:16],
                    task=str((task or {}).get('id') or '')[:16],
                    reason=_summary_fallback_reason,
                    implementation='deterministic_recovery_receipt',
                )
            except Exception as exc:
                logger.debug(
                    '%s [Compact] deterministic fallback audit failed: %s',
                    pfx, exc)

    if cancelled():
        return 'Context compaction cancelled — messages preserved as-is.'

    if not summary_text:
        logger.warning('%s [Compact] Summary generation failed — keeping messages intact', pfx)
        _failure_reason = (
            summary_failure_reason(task)
            or _summary_pipeline_failure_reason or 'summary_failed')
        if isinstance(_result_meta, dict):
            _result_meta.update({
                'compacted': False,
                'tokens_before': int(tokens_before),
                'msgs_before': int(msg_count_before),
                'trigger': kwargs.get('_compaction_trigger') or 'force',
                'reason': kwargs.get('_compaction_reason') or '',
                'summary_usage': dict(_summary_usage),
                'summary_duration_ms': int(_summary_duration_ms),
                'summaryFailureReason': _failure_reason,
            })
        return ('Context compaction attempted but summary generation failed. '
                'Messages preserved as-is.')

    accepted_summary = accept_summary(
        summary_text, sources.summary_input, task, fallback=_summary_fallback,
        recent_files_block=_recent_files_block, turn_diff_block=_diff_block,
        turn_diff_included=_turn_diff_included, result_meta=_result_meta,
        log_prefix=pfx,
        recovery_max_chars=_DETERMINISTIC_RECOVERY_MAX_CHARS,
    )
    if isinstance(accepted_summary, str):
        return accepted_summary
    summary_text = accepted_summary.text
    _turn_diff_included = accepted_summary.turn_diff_included

    retained_wrapper = make_history_carrier(sources.retained_entries)
    if retained_wrapper:
        _audit_user_verbatim_once()
    new_messages = _retained_projection(
        sources.old_messages, sources.recent_messages, sources.anchor_msg, sources.retained_entries,
        task=task)

    protected_history_count = sum(
        len(history_entries(message)) for message in new_messages
    ) - len(sources.retained_entries)
    new_messages = coalesce_history_carriers(new_messages)
    all_history_carriers = [message for message in new_messages
                            if history_entries(message)]
    retained_history_count = sum(len(history_entries(message))
                                 for message in all_history_carriers)

    compacted_message_count = sum(
        1 for message in sources.summary_input if message.get('role') not in ('system', 'developer'))

    # Counts/reduction belong to archive + event telemetry, not model prose.
    # The accepted projection includes its handoff: no later caller may append
    # an uncounted summary or publish a folded history without continuation.
    compact_result = '## Context Compacted — Selective Summary\n\n' + summary_text
    insert_compaction_handoff(new_messages, _compaction_handoff(
        compact_result, _handoff_phase, task, messages, sources.summary_input, kwargs,
        accepted_summary.continuity_digest))
    # Task progress is a maintained state projection, not disposable prose.
    # Carry it BEFORE admission/accounting; otherwise every compaction deletes
    # and re-injects the same state as a spurious new intervention next round.
    from lib.tasks_pkg.context_composer.checklist import carry_checklist_through_compaction
    carry_checklist_through_compaction(new_messages, task)
    _candidate_tokens = _estimate_total_tokens(new_messages)
    if (callable(sources.dispatch_measure) and isinstance(sources.dispatch_target, int)
            and sources.dispatch_measure(_with_restoration_reserve(new_messages, messages, task=task))
            > sources.dispatch_target):
        if isinstance(_result_meta, dict):
            _result_meta.update(compacted=False,
                summaryFailureReason='summary_projection_over_budget')
        return ('Complete summary projection exceeds the final request budget; '
                'original context preserved without adoption.')

    candidate_economics = admit_compaction_candidate(
        messages, new_messages, task, policy, candidate_tokens=_candidate_tokens,
        tokens_before=tokens_before, summary_usage=_summary_usage,
        result_meta=_result_meta, log_prefix=pfx, ports=economic_ports,
    )
    if isinstance(candidate_economics, str):
        return candidate_economics

    _round_num = kwargs.get('_compaction_round')
    if _round_num is None:
        _round_num = task.get('round_num') if task else 0
    _round_num = int(_round_num or 0)

    if cancelled():
        return 'Context compaction cancelled — messages preserved as-is.'

    _archive_id: str | None = None
    if not kwargs.get('_compaction_skip_archive'):
        _archive_id = _archive_transcript(
            conv_id, messages,
            user_id=_task_owner(task),
            trigger=kwargs.get('_compaction_trigger') or 'force',
            task=task,
            round_num=_round_num,
            tokens_before=int(tokens_before or 0),
            msgs_before=int(msg_count_before or 0),
            reason=kwargs.get('_compaction_reason') or '',
            emit_event=True,
        )
    else:
        # A skip-archive caller (reactive_compact) already wrote its own
        # PRE-compaction snapshot row — with tokens_after/msgs_after still
        # at their 0 placeholders.  Adopt that row so the caller-side
        # post-summary UPDATE (update_archive_summary + compaction_done,
        # keyed off _result_meta['archive_id']) back-fills IT instead of
        # leaving the row at 0 forever (the "→ 0" viewer artifact).
        _archive_id = kwargs.get('_compaction_archive_id')

    if cancelled():
        if not kwargs.get('_compaction_skip_archive'):
            # The caller owns cancellation closure for an adopted pre-archive.
            from lib.tasks_pkg.compaction._archive import cancel_compaction_archive
            cancel_compaction_archive(
                _archive_id, messages, task,
                trigger=kwargs.get('_compaction_trigger') or 'force',
                round_num=_round_num, tokens_before=tokens_before,
                msgs_before=msg_count_before, summary_usage=_summary_usage,
                summary_duration_ms=_summary_duration_ms)
        return 'Context compaction cancelled — messages preserved as-is.'
    original_messages = messages
    messages = new_messages

    if isinstance(_result_meta, dict):
        _result_meta.update({
            'compacted': True,
            'tokens_before': int(tokens_before),
            'tokens_after_estimated': int(_candidate_tokens),
            'token_count_kind': 'estimated',
            'msgs_before': int(msg_count_before),
            'archive_id': _archive_id,
            'summary_text': summary_text,
            'handoff_phase': _handoff_phase,
            'continuation_format': HANDOFF_FORMAT,
            'trigger': kwargs.get('_compaction_trigger') or 'force',
            'reason': kwargs.get('_compaction_reason') or '',
            'round_num': _round_num,
            'summary_usage_tokens': int(candidate_economics.summary_cost_tokens),
            'projected_summary_usage_tokens': int(policy.projected_summary_cost),
            'summary_usage': dict(_summary_usage),
            'summary_duration_ms': int(_summary_duration_ms),
            'summaryFallback': bool(_summary_fallback),
            'summaryFallbackReason': _summary_fallback_reason,
            'summarized_messages': int(compacted_message_count),
            'preserved_turns': int(preserved_turns),
            'folded_tool_rounds': sum(
                1 for message in sources.cold_round_msgs
                if isinstance(message, dict)
                and message.get('role') == 'assistant'
                and message.get('tool_calls')
            ),
            'objective_anchored': sources.anchor_msg is not None,
            'durable_objective_applied': bool(
                _extract_summary_objective(summary_text)),
            'retained_user_messages': retained_history_count,
            'selected_history_messages': len(sources.retained_entries),
            'protected_shell_history_messages': protected_history_count,
            'retained_history_estimated_tokens': _estimate_total_tokens(
                all_history_carriers),
            'recent_files': list(recent_files),
            'turn_diff_included': bool(_turn_diff_included),
            'mode': (
                'turns_and_intra_turn'
                if sources.cold_round_msgs and any(
                    message.get('role') != 'system'
                    for message in sources.old_messages)
                else 'intra_turn' if sources.cold_round_msgs else 'turns'
            ),
            'economics': candidate_economics.accounting,
            'adoptionEconomics': candidate_economics.adoption,
        })

    converge_compaction_candidate(
        messages, task, usable=usable, proactive=policy.active, options=kwargs,
        log_prefix=pfx, log_id=log_id, estimate_tokens=_estimate_total_tokens,
        trigger_threshold=_compaction_trigger_threshold, is_bounded=bounded_summary_task,
    )

    if cancelled():
        return 'Context compaction cancelled — messages preserved as-is.'
    if task and task.get('_turnId'):
        _result_meta['_persistedReceipt'] = _persist_compaction_candidate(
            messages, task, _result_meta)
        if cancelled():
            from lib.tasks_pkg.compaction._archive import cancel_compaction_archive
            cancel_compaction_archive(_archive_id, original_messages, task,
                trigger=_result_meta['trigger'], round_num=_round_num,
                tokens_before=tokens_before, msgs_before=msg_count_before,
                summary_usage=_summary_usage, summary_duration_ms=_summary_duration_ms)
            return 'Context compaction cancelled — messages preserved as-is.'
    original_messages[:] = messages
    if _result_meta.get('_persistedReceipt'):
        task['_acceptedCompactionReceipt'] = deepcopy(_result_meta['_persistedReceipt'])

    # No state may advance from a generated-but-rejected economic candidate.
    try:
        from lib.goal_runs.run_pins import update_objective_from_receipt as _update_objective_from_receipt
        from lib.tasks_pkg.invocation import is_child_invocation
        if not is_child_invocation(task or {}):
            _update_objective_from_receipt(
                conv_id, _extract_summary_objective(summary_text),
                user_id=_task_owner(task))
    except Exception as exc:
        logger.debug('%s [Compact] objective re-pin skipped: %s', pfx, exc)
    if task is not None:
        task.pop('_autoCompactRetryAfterTokens', None)
        task.pop('_autoCompactRetryWitness', None)
    with _cooldown_lock:
        _summary_cooldowns[conv_id] = time.time()

    return compact_result


# ═══════════════════════════════════════════════════════════════════════════════
#  Force compact: inject context_compact tool call when over threshold
# ═══════════════════════════════════════════════════════════════════════════════

def _settle_head_truncate_recovery(
        archive_id, messages, task, *, conv_id, trigger, round_num,
        tokens_before, msgs_before, tokens_after, receipt, summary_usage,
        adopted, pfx):
    """Publish an explicit utility recovery outcome after its mutation decision.

    Only an adopted projection supplies a detached post snapshot. Receipt and
    live-event failures remain independent best-effort observations; admission,
    archival, cancellation and mutation stay in the calling coordinator.
    """
    if archive_id is None:
        return
    failure_label = '' if adopted else 'failed '
    try:
        from lib.agent_core.store import get_conversation_store
        get_conversation_store().update_archive_summary(
            archive_id, '', int(tokens_after), len(messages),
            user_id=_task_owner(task),
            archive_scope=(task or {}).get('_archiveScope', ''),
            receipt=receipt,
            **({'post_messages': deepcopy(messages)} if adopted else {}))
    except Exception as exc:
        logger.debug('%s [ForceCompact] %sfallback receipt update failed: %s',
                     pfx, failure_label, exc)
    if task is not None:
        try:
            from lib.agent_core.events import EventType, build_event
            from lib.tasks_pkg.manager import append_event
            reduction_pct = (
                (1 - tokens_after / max(1, tokens_before)) * 100
                if adopted else 0.0)
            append_event(task, build_event(
                EventType.COMPACTION_DONE,
                archiveId=str(archive_id), convId=conv_id, trigger=trigger,
                tokensBefore=int(tokens_before), tokensAfter=int(tokens_after),
                tokenCountKind='estimated', msgsBefore=int(msgs_before),
                msgsAfter=len(messages), reductionPct=round(reduction_pct, 1),
                roundNum=int(round_num or 0), receipt=receipt,
                summaryDispatchCalls=summary_dispatch_calls(summary_usage)))
        except Exception as exc:
            logger.debug('%s [ForceCompact] %sfallback done event failed: %s',
                         pfx, failure_label, exc)


def force_compact_if_needed(messages: list, task: dict | None = None,
                            keep_recent_pairs: int | None = None,
                            preserve_budget_tokens: int | None = None,
                            *, force: bool = False,
                            **kwargs) -> bool:
    """Check token usage and record an accepted local continuation handoff.

    Args:
        keep_recent_pairs: Legacy knob mapped to ``max_turns`` (turn-count cap).
        preserve_budget_tokens: Token budget for verbatim preservation.
        force: Skip the ``_should_force_compact`` threshold gate.

    Returns True if compaction was performed, False otherwise.
    """
    _measurement_out = kwargs.get('_measurement_out')
    if not isinstance(_measurement_out, dict):
        _measurement_out = {}
    if not force and not _should_force_compact(
            messages, task, measurement_out=_measurement_out,
            current_round=kwargs.get('_compaction_round'),
            remaining_api_rounds=kwargs.get(
                '_compaction_remaining_api_rounds')):
        return False

    if not force:
        from lib.tasks_pkg.compaction._pressure import try_pressure_l1
        effective, safety, _ = _compaction_trigger_threshold(task)
        measured = int(_measurement_out.get('gate_tokens') or 0)
        if try_pressure_l1(
                messages, task, target_tokens=effective, total_tokens=measured,
                schema_tokens=max(0, measured - int(
                    _measurement_out.get('message_tokens') or measured)),
                mandatory=measured > safety,
                round_num=kwargs.get('_compaction_round') or 0,
                remaining_api_rounds=kwargs.get('_compaction_remaining_api_rounds')):
            # This was L1, not a summary: do not publish an L2 receipt/metric.
            _measurement_out.clear()
            return False
        if measured > safety:
            from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
            from lib.tasks_pkg.compaction._pressure import retain_oversized_tool_batch
            from lib.tasks_pkg.compaction._tokens import _count_tokens_authoritative
            _, admission_target = resolve_admission_budget(task)
            if retain_oversized_tool_batch(
                    messages, task, target_tokens=admission_target,
                    schema_tokens=max(0, measured - int(
                        _measurement_out.get('message_tokens') or measured)),
                    measure=lambda candidate: _count_tokens_authoritative(candidate, task)[0]):
                _measurement_out.clear()
                return False

    # The historical preservation budget scales from the model's usable
    # context (259K on a 1M model).  When the economic 128K trigger fires,
    # preserving 259K means there is literally nothing to summarize.  Scale
    # the verbatim hot region from the same effective ceiling that triggered
    # this automatic compaction.  Reactive/manual forced compactions keep
    # their explicit budgets and therefore remain byte-identical.
    if preserve_budget_tokens is None and not force:
        effective, window_threshold, _working_set = (
            _compaction_trigger_threshold(task))
        if effective < window_threshold:
            preserve_budget_tokens = max(
                8_000, int(effective * _PRESERVE_BUDGET_RATIO))

    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    conv_id = compaction_owner_key(task)
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    logger.info('%s [ForceCompact] Preparing continuation handoff for conv=%s '
                'preserve_budget=%s', pfx,
                conv_id[:8] if conv_id else '?', preserve_budget_tokens)

    _explicit_trigger = (kwargs.get('_compaction_trigger')
                         if isinstance(kwargs, dict) else None)
    _effective = _window_threshold = _working_set = 0
    if _explicit_trigger:
        _trigger = _explicit_trigger
    elif force:
        _trigger = 'force'
    else:
        _effective, _window_threshold, _working_set = (
            _compaction_trigger_threshold(task))
        _trigger = ('working_set'
                    if 0 < _working_set <= _window_threshold else 'window')
    _reason = (kwargs.get('_compaction_reason')
               if isinstance(kwargs, dict) else None) or ''
    if not _reason and not force:
        _reason = (
            f'estimated input crossed {_effective:,}-token {_trigger} '
            'threshold')
    _skip_archive = bool(kwargs.get('_compaction_skip_archive')
                         if isinstance(kwargs, dict) else False)
    _external_meta = kwargs.get('_result_meta')
    _meta: dict = _external_meta if isinstance(_external_meta, dict) else {}
    from lib.turn_activity_timeline import capture_compaction_boundary
    _meta['activityBoundary'] = capture_compaction_boundary({}, task or {})
    # The Turn row reports the complete provider request (messages, managed
    # context, tool schemas). The gate already counted it; a forced pass counts
    # it before the compactor replaces the list. Recomposition settles the row
    # on the rebuilt request (_pipeline._settle_rebuilt_request_size).
    _request_tokens_before = int(_measurement_out.get('gate_tokens') or 0)
    if task is not None:
        task.pop('_compactionRequestSize', None)
        if _request_tokens_before <= 0:
            from lib.tasks_pkg.compaction._tokens import _count_tokens_authoritative
            _request_tokens_before = _count_tokens_authoritative(messages, task)[0]
    execute_compact_tool(
        messages, task=task,
        keep_recent_pairs=keep_recent_pairs,
        preserve_budget_tokens=preserve_budget_tokens,
        _compaction_trigger=_trigger,
        _compaction_reason=_reason,
        _compaction_skip_archive=_skip_archive,
        _compaction_archive_id=kwargs.get('_compaction_archive_id'),
        _compaction_round=kwargs.get('_compaction_round'),
        _compaction_remaining_api_rounds=kwargs.get(
            '_compaction_remaining_api_rounds'),
        _scheduled_request_id=kwargs.get('_scheduled_request_id'),
        _dispatch_prompt_target=kwargs.get('_dispatch_prompt_target'),
        _dispatch_prompt_measure=kwargs.get('_dispatch_prompt_measure'),
        _proactive_economic=(not force and int(
            _measurement_out.get('gate_tokens') or 0)
            <= _compaction_trigger_threshold(task)[1]),
        _allow_deterministic_summary_fallback=bool(
            kwargs.get('_allow_deterministic_summary_fallback')),
        _message_tokens_before=(
            _measurement_out.get('message_tokens')
            if _measurement_out is not None else None),
        _result_meta=_meta,
    )
    if _measurement_out is not None:
        _measurement_out.setdefault(
            'message_tokens', int(_meta.get('tokens_before') or 0))
        _measurement_out.setdefault(
            'message_count', int(_meta.get('msgs_before') or len(messages)))

    # If the summary LLM returned empty / compaction refused, the message
    # list was NOT mutated. Injecting a synthetic context_compact
    # tool-pair here would only grow the context and — worse — make the
    # caller (reactive_compact) believe compaction succeeded, skipping its
    # head-truncate safety net and looping the same oversized prompt back
    # to the API. Report failure so the caller can fall through.
    if not _meta.get('compacted'):
        if task is not None:
            # The ledger is a per-attempt handoff to retention telemetry, not
            # durable task state. A declined/failed attempt has no summary to
            # audit, so release the bounded working set immediately.
            task.pop('_contextEvidenceLedger', None)
        if ((task and task.get('aborted'))
                or _meta.get('reason') in {'aborted', 'open_tool_batch'}):
            return False
        # Only explicit utility callers may opt into lossy recovery after
        # a failed summary, and only above the usable window. Live/transient
        # tasks preserve their source even when a legacy caller sets the flag.
        from lib.tasks_pkg.compaction._summary_budget import bounded_summary_task
        _allow_ht = bool(kwargs.get('_allow_head_truncate_fallback'))
        if bounded_summary_task(task):
            _allow_ht = False
        if _allow_ht:
            _est_tokens = int(
                (_measurement_out or {}).get('gate_tokens') or 0)
            _tok_method = str(
                (_measurement_out or {}).get('method') or '')
            if _est_tokens <= 0:
                try:
                    from lib.tasks_pkg.compaction._tokens import (
                        _count_tokens_authoritative)
                    _est_tokens, _tok_method = _count_tokens_authoritative(
                        messages, task)
                except Exception as _ce:
                    logger.debug('%s [ForceCompact] authoritative count failed, '
                                 'using heuristic: %s', pfx, _ce)
                    _est_tokens = _estimate_total_tokens(messages)
                    _tok_method = 'heuristic'
            _usable = _usable_context(_get_context_limit(task))
            if _est_tokens >= _usable:
                logger.warning(
                    '%s [ForceCompact] Compaction unavailable AND context critically '
                    'over budget (est=%d via %s >= usable=%d) — falling back '
                    'to deterministic head-truncate so the context is bounded '
                    'without depending on the summary LLM',
                    pfx, _est_tokens, _tok_method, _usable)
                from lib.tasks_pkg.compaction._reactive import _head_truncate
                _fallback_archive_id = None
                _fallback_tokens_before = int(_est_tokens)
                _fallback_msgs_before = len(messages)
                # Archive the exact list before the deterministic fallback
                # mutates it. Summary candidates are not archived earlier
                # because an economic decline must leave no user-visible row.
                try:
                    _fallback_archive_id = _archive_transcript(
                        conv_id, messages,
                        user_id=_task_owner(task),
                        trigger=_trigger,
                        task=task,
                        round_num=int(kwargs.get('_compaction_round') or 0),
                        tokens_before=_fallback_tokens_before,
                        msgs_before=_fallback_msgs_before,
                        reason=(_reason or 'summary failed over window'),
                        emit_event=True,
                    )
                except Exception as _ar_e:
                    logger.debug('%s [ForceCompact] fallback archive failed: %s',
                                 pfx, _ar_e)
                from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
                _, _target = resolve_admission_budget(
                    task, round_num=int(kwargs.get('_compaction_round') or 0))
                _schema_reserve = max(0, _est_tokens - int(
                    _measurement_out.get('message_tokens')
                    or _estimate_total_tokens(messages)))
                if task and task.get('aborted'):
                    _meta.update(compacted=False, reason='aborted',
                                 summaryFailureReason='aborted')
                    from lib.tasks_pkg.compaction._archive import cancel_compaction_archive
                    cancel_compaction_archive(
                        _fallback_archive_id, messages, task, trigger=_trigger,
                        round_num=int(kwargs.get('_compaction_round') or 0),
                        tokens_before=_fallback_tokens_before,
                        msgs_before=_fallback_msgs_before,
                        summary_usage=_meta.get('summary_usage'),
                        summary_duration_ms=_meta.get('summary_duration_ms') or 0)
                    return False
                _dropped = _head_truncate(
                    messages, task,
                    token_target=max(1, _target - _schema_reserve),
                    event_name='proactive_head_truncate')
                if _dropped:
                    _fallback_tokens_after = _estimate_total_tokens(messages)
                    _fallback_receipt = build_compaction_receipt(
                        trigger=_trigger,
                        status='completed',
                        strategy='deterministic_recovery',
                        implementation='pairing_safe_head_truncate',
                        mode='compaction_unavailable_over_window',
                        continuation_format='none',
                        summary_generated=False,
                        summary_usage=_meta.get('summary_usage'),
                        summary_duration_ms=(
                            _meta.get('summary_duration_ms') or 0),
                        dropped_messages=_dropped,
                        outcome_reason=(
                            'economic_declined_over_window'
                            if _meta.get('reason') in {'cache_negative', 'low_yield'}
                            else 'nothing_foldable_over_window'
                            if _meta.get('reason') == 'nothing_foldable'
                            else 'summary_failed_over_window'),
                    )
                    _settle_head_truncate_recovery(
                        _fallback_archive_id, messages, task,
                        conv_id=conv_id, trigger=_trigger,
                        round_num=kwargs.get('_compaction_round'),
                        tokens_before=_fallback_tokens_before,
                        msgs_before=_fallback_msgs_before,
                        tokens_after=_fallback_tokens_after,
                        receipt=_fallback_receipt,
                        summary_usage=_meta.get('summary_usage'),
                        adopted=True, pfx=pfx)
                    if task is not None and _fallback_archive_id is not None:
                        task['_compactionRequestSize'] = {
                            'archiveId': str(_fallback_archive_id),
                            'tokensBefore': _fallback_tokens_before}
                    # Context was bounded — surface as a real compaction so
                    # the pipeline notifies the cache tracker (prefix changed)
                    # and the round proceeds with a smaller prompt.
                    _record_compaction_cadence(
                        task, kwargs.get('_compaction_round'))
                    if task is not None:
                        task.pop('_compactionCacheObservation', None)
                    return True
                logger.warning(
                    '%s [ForceCompact] Head-truncate dropped 0 messages '
                    '(too few to shed) — reporting failure', pfx)
                if _fallback_archive_id is not None:
                    _failed_receipt = build_compaction_receipt(
                        trigger=_trigger,
                        status='failed',
                        strategy='deterministic_recovery',
                        implementation='pairing_safe_head_truncate',
                        mode='compaction_unavailable_over_window',
                        continuation_format='none',
                        summary_generated=False,
                        summary_usage=_meta.get('summary_usage'),
                        summary_duration_ms=(
                            _meta.get('summary_duration_ms') or 0),
                        outcome_reason='no_safe_messages_to_drop',
                    )
                    _settle_head_truncate_recovery(
                        _fallback_archive_id, messages, task,
                        conv_id=conv_id, trigger=_trigger,
                        round_num=kwargs.get('_compaction_round'),
                        tokens_before=_fallback_tokens_before,
                        msgs_before=_fallback_msgs_before,
                        tokens_after=_fallback_tokens_before,
                        receipt=_failed_receipt,
                        summary_usage=_meta.get('summary_usage'),
                        adopted=False, pfx=pfx)
        _decline_reason = str(_meta.get('reason') or '')
        if _decline_reason in {
                'cache_negative', 'low_yield', 'nothing_foldable'}:
            logger.debug(
                '%s [ForceCompact] Expected proactive decline (%s); '
                'messages unchanged', pfx, _decline_reason)
        else:
            logger.warning('%s [ForceCompact] Compaction did not mutate messages '
                           '(summary empty or refused) — reporting failure so the '
                           'caller can fall back', pfx)
            # Settle the attempt as a terminal, authentic UI fact — scoped to
            # the passes that leave a stuck ghost otherwise: the
            # working_set/window pass emits a durable COMPACTION archive row
            # before the summary dispatch (stays `running` forever without
            # settlement — the muu1klptjtwd3t ghost), and the dispatch_guard
            # admission pass emits no archive at all.  Reactive/backfill/heal
            # and deterministic-recovery paths keep their own terminal
            # accounting and are untouched.  `_meta` carries `archive_id`
            # only when this attempt archived; helper is best-effort.
            if _trigger in {'working_set', 'window', 'dispatch_guard'}:
                _failure_reason = str(
                    _meta.get('summaryFailureReason')
                    or _meta.get('summaryRejectionReason')
                    or _meta.get('reason')
                    or 'summary_failed')
                _settle_failed_compaction_attempt(
                    task, _meta, messages, conv_id=conv_id,
                    tokens_before=int(_meta.get('tokens_before') or 0),
                    msgs_before=int(_meta.get('msgs_before') or 0),
                    summary_usage=dict(_meta.get('summary_usage') or {}),
                    summary_duration_ms=int(
                        _meta.get('summary_duration_ms') or 0),
                    trigger=str(_trigger),
                    round_num=int(kwargs.get('_compaction_round') or 0),
                    outcome_reason=_failure_reason)
        return False

    tokens_before = int(_meta.get('tokens_before', 0))
    msgs_before = int(_meta.get('msgs_before', 0))
    tokens_after = _estimate_total_tokens(messages)
    msgs_after = len(messages)
    reduction_pct = (1 - tokens_after / max(1, tokens_before)) * 100

    logger.info('%s [Compact] Complete  conv=%s  '
                'tokens: %d → %d (%.0f%% reduction)  messages: %d → %d',
                pfx, conv_id[:8] if conv_id else '?',
                tokens_before, tokens_after, reduction_pct,
                msgs_before, msgs_after)

    if conv_id:
        try:
            from lib.tasks_pkg.cache_tracking._roi import record_l2_compaction
            record_l2_compaction(
                conv_id, tokens_before=tokens_before, tokens_after=tokens_after,
                msgs_before=msgs_before, msgs_after=msgs_after,
                user_id=_task_owner(task))
        except Exception as _roi_e:
            logger.debug('%s [Compact] record_l2_compaction failed: %s', pfx, _roi_e)

    archive_id = _meta.get('archive_id')
    receipt = _meta.get('_persistedReceipt')
    if receipt is None:
        receipt = _persist_compaction_candidate(messages, task, _meta)
    if archive_id is not None:
        if task is not None:
            try:
                from lib.agent_core.events import EventType, build_event
                from lib.tasks_pkg.manager import append_event
                append_event(task, build_event(
                    EventType.COMPACTION_DONE,
                    archiveId=str(archive_id),
                    convId=conv_id,
                    trigger=_meta.get('trigger') or 'force',
                    tokensBefore=tokens_before,
                    tokensAfter=tokens_after,
                    tokenCountKind='estimated',
                    msgsBefore=msgs_before,
                    msgsAfter=msgs_after,
                    reductionPct=round(reduction_pct, 1),
                    roundNum=int(_meta.get('round_num') or 0),
                    receipt=receipt,

                    summaryDispatchCalls=summary_dispatch_calls(_meta.get('summary_usage')),
                ))
            except Exception as _ev_e:
                logger.debug('[Compact] compaction_done emit failed: %s', _ev_e)
            if _request_tokens_before > 0:
                task['_compactionRequestSize'] = {
                    'archiveId': str(archive_id),
                    'tokensBefore': _request_tokens_before}

    if task is not None:
        try:
            from lib.context_telemetry import record_compaction_event
            from lib.tasks_pkg.compaction._continuity import read_continuity
            state = read_continuity(messages, task)
            retained = [entry['id'] for entry in (state or {}).get('observations', [])]
            record_compaction_event(
                task,
                trigger=_meta.get('trigger') or 'force',
                reason=_meta.get('reason') or '',
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                archive_id=archive_id,
                evidence_retained=retained,
                evidence_lost=[],
            )
        except Exception as _ct_e:
            logger.debug('%s [Compact] context telemetry failed: %s', pfx, _ct_e)
        finally:
            # Release the legacy utility-recovery bridge. Normal summary
            # preparation is pure; verified handoffs own continuity.
            task.pop('_contextEvidenceLedger', None)

    _record_compaction_cadence(task, _meta.get('round_num'))

    if task is not None:
        task.pop('_compactionCacheObservation', None)

    return True
