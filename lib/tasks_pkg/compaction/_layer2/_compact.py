"""Layer 2 — public entrypoints for query-aware context compaction.

  * ``execute_compact_tool``    — generates the summary, mutates messages.
  * ``force_compact_if_needed`` — gates on threshold + injects synthetic pair.
"""

import hashlib
import json
import math
import re
import time

from lib.ids import short_id
from lib.log import get_logger
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _COMPACT_TOOL_NAME,
    _cooldown_lock,
    _MAX_PRESERVE_TURNS,
    _PRESERVE_BUDGET_RATIO,
    _SUMMARY_MAX_TOKENS,
    _SUMMARY_TRIGGER_RATIO,
    _AUTO_COMPACT_MIN_PAYBACK_ROUNDS,
    _AUTO_COMPACT_MIN_REDUCTION_RATIO,
    _summary_cooldowns,
)
from lib.tasks_pkg.compaction._tokens import (
    _compaction_trigger_threshold,
    _estimate_total_tokens,
    _fixed_compaction_cadence_payback_horizon,
    _get_context_limit,
    _record_compaction_cadence,
    _should_force_compact,
    _usable_context,
)
from lib.tasks_pkg.compaction._receipt import (
    build_compaction_receipt,
    summary_usage_details,
)
from lib.tasks_pkg.compaction._layer2._anchor import (
    _collect_user_verbatim,
    _extract_current_query,
    _extract_objective_anchor_text,
    _extract_recently_accessed_files,
    _find_turn_boundary,
    _fold_recent_intra_turn,
    _objective_anchor_index,
)
from lib.tasks_pkg.compaction._layer2._summary import _generate_query_aware_summary
from lib.tasks_pkg.compaction._layer2._prompt import (
    _SUMMARY_SYSTEM_PROMPT,
    _build_summary_user_content,
    _extract_summary_objective,
    _format_messages_for_summary,
    _summary_input_char_budget,
)

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


def _summary_missing_task_state_fields(summary: str, snapshot) -> list[str]:
    """Return named critical snapshot fields absent from a model summary."""
    missing = []
    if snapshot.goal and not _summary_covers_state_value(summary, snapshot.goal):
        missing.append('goal')
    for index, constraint in enumerate(snapshot.hard_constraints):
        if not _summary_covers_state_value(summary, constraint):
            missing.append(f'hard_constraints[{index}]')
    pending_groups = (
        ('todos', snapshot.todos),
        ('open_questions', snapshot.open_questions),
        ('next_steps', snapshot.next_steps),
    )
    for field, values in pending_groups:
        for index, value in enumerate(values):
            if not _summary_covers_state_value(summary, value):
                missing.append(f'{field}[{index}]')
    return missing


def _summary_usage_tokens(usage: dict | None) -> int:
    return int(summary_usage_details(usage).get('totalTokens') or 0)


def _projected_summary_usage_tokens(
    messages: list,
    current_query: str,
    task: dict | None,
    anchor_text: str = '',
) -> int:
    """Conservative pre-dispatch cost estimate for proactive ROI gating.

    The old best-case check assumed a free summary, then sometimes paid for a
    real summary only to reject it as cache-negative. Estimate the same bounded
    prompt shape before dispatch, including the evidence-ledger reserve and the
    configured maximum completion, so economic declines happen before money
    and wall time are spent.
    """
    try:
        char_budget = _summary_input_char_budget(task)
        evidence_chars = max(2_000, min(16_000, char_budget // 4))
        history_budget = max(
            4_000,
            char_budget - evidence_chars - len(current_query or '')
            - len(anchor_text or '') - 900,
        )
        formatted = _format_messages_for_summary(
            messages, char_budget=history_budget)
        user_content = _build_summary_user_content(
            anchor_text=anchor_text,
            latest_user_message=current_query,
            formatted_history=formatted,
        )
        base_prompt = [
            {'role': 'system', 'content': _SUMMARY_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ]
        base_tokens = _estimate_total_tokens(base_prompt)
        # Evidence is structured JSON and paths: ~3 chars/token is a more
        # realistic conservative reserve than repetitive filler through BPE.
        evidence_tokens = (evidence_chars + 2) // 3
        return max(1, base_tokens + evidence_tokens + _SUMMARY_MAX_TOKENS)
    except Exception as exc:
        logger.debug('[Compact] summary-cost projection failed: %s', exc)
        return max(1, _SUMMARY_MAX_TOKENS * 2)


def _proactive_cache_economics(task: dict | None, *, tokens_before: int,
                                candidate_tokens: int,
                                summary_usage_tokens: int = 0) -> dict:
    """Project cache rewrite break-even for one automatic L2 candidate.

    Rates are resolved independently for the current and candidate prompt, so
    crossing any provider-declared context-pricing tier is reflected without a
    model-name special case. Recurring savings compare only the observed warm
    cached prefix before/after the rewrite; uncached tail tokens are not
    optimistically treated as future cache reads.
    """
    conv_id = (task or {}).get('convId', '') or ''
    model = ((task or {}).get('config', {}) or {}).get('model', '') or ''
    provider_id = (task or {}).get('provider_id') or ''
    try:
        from lib.tasks_pkg.cache_tracking._state import get_warm_cache_read
        cache_read = int(get_warm_cache_read(
            conv_id, user_id=_task_owner(task)) or 0) if conv_id else 0
    except Exception as e:
        logger.debug('[Compact] warm-cache lookup failed: %s', e)
        cache_read = 0

    total_before = max(0, int(tokens_before))
    total_after = max(0, int(candidate_tokens))
    dropped = max(0, total_before - total_after)
    cache_replay_before = min(total_before, max(0, cache_read))
    cache_replay_after = min(total_after, max(0, cache_read))

    cache_write_mul = 1.0
    cache_read_mul = 1.0
    pricing_source = 'conservative_default'
    pricing_before = None
    pricing_after = None
    try:
        from lib.pricing import get_pricing_data, lookup_pricing
        pricing_before = lookup_pricing(
            model, provider_id or None, prompt_tokens=total_before)
        pricing_after = lookup_pricing(
            model, provider_id or None, prompt_tokens=total_after)
        if pricing_after:
            cache_write_mul = max(0.0, float(
                pricing_after.get('cacheWriteMul', 1.0)))
        if pricing_before:
            cache_read_mul = max(0.0, float(
                pricing_before.get('cacheReadMul', 1.0)))
            pricing_source = str(
                pricing_before.get('_pricingSource') or 'resolved_price')

        if pricing_before and pricing_after:
            exchange_rate = float(
                get_pricing_data().get('usdToCny') or 1.0)

            def input_rate_usd(pricing: dict) -> float:
                rate = max(0.0, float(pricing.get('input') or 0.0))
                currency = str(pricing.get('currency') or 'USD').upper()
                return (rate / exchange_rate
                        if currency == 'CNY' and exchange_rate > 0 else rate)

            before_input_usd = input_rate_usd(pricing_before)
            after_input_usd = input_rate_usd(pricing_after)
            before_read_mul = max(0.0, float(
                pricing_before.get('cacheReadMul', 1.0)))
            after_read_mul = max(0.0, float(
                pricing_after.get('cacheReadMul', 1.0)))
            after_write_mul = max(0.0, float(
                pricing_after.get('cacheWriteMul', 1.0)))

            replay_before_usd = (
                cache_replay_before * before_input_usd * before_read_mul
                / 1_000_000)
            replay_after_usd = (
                cache_replay_after * after_input_usd * after_read_mul
                / 1_000_000)
            savings_per_round_usd = max(
                0.0, replay_before_usd - replay_after_usd)
            cache_rewrite_tokens = cache_replay_after
            rewrite_cost_usd = (
                cache_rewrite_tokens * after_input_usd * after_write_mul
                / 1_000_000)
            summary_cost_usd = (
                max(0, int(summary_usage_tokens)) * after_input_usd
                / 1_000_000)
            reference_input_usd = after_input_usd or before_input_usd
            if reference_input_usd > 0:
                token_scale = 1_000_000 / reference_input_usd
                rewrite_cost = rewrite_cost_usd * token_scale
                summary_cost = summary_cost_usd * token_scale
                savings_per_round = savings_per_round_usd * token_scale
                total_cost = rewrite_cost_usd + summary_cost_usd
                payback_rounds = (
                    total_cost / savings_per_round_usd
                    if savings_per_round_usd > 0 else float('inf'))
                before_tier = pricing_before.get('selectedTier') or {}
                after_tier = pricing_after.get('selectedTier') or {}
                return {
                    'cache_read_tokens': cache_read,
                    'cache_replay_tokens_before': cache_replay_before,
                    'cache_replay_tokens_after': cache_replay_after,
                    'cache_rewrite_tokens': cache_rewrite_tokens,
                    'dropped_tokens': dropped,
                    'cache_write_mul': after_write_mul,
                    'cache_read_mul': before_read_mul,
                    'marginal_savings_per_dropped_token': (
                        before_input_usd * before_read_mul
                        / reference_input_usd),
                    'rewrite_cost_tokens': rewrite_cost,
                    'summary_cost_tokens': summary_cost,
                    'savings_per_round_tokens': savings_per_round,
                    'rewrite_cost_usd': rewrite_cost_usd,
                    'summary_cost_usd': summary_cost_usd,
                    'savings_per_round_usd': savings_per_round_usd,
                    'payback_rounds': payback_rounds,
                    'pricing_source': pricing_source,
                    'pricing_before': {
                        'tier_id': before_tier.get('id'),
                        'input_rate_usd': before_input_usd,
                        'cache_read_mul': before_read_mul,
                    },
                    'pricing_after': {
                        'tier_id': after_tier.get('id'),
                        'input_rate_usd': after_input_usd,
                        'cache_read_mul': after_read_mul,
                        'cache_write_mul': after_write_mul,
                    },
                    'crosses_pricing_tier': (
                        before_tier.get('id') != after_tier.get('id')),
                }
    except Exception as e:
        logger.debug('[Compact] cache pricing lookup failed: %s', e)

    cache_rewrite_tokens = min(
        total_after, max(0, cache_read))
    rewrite_cost = cache_rewrite_tokens * cache_write_mul
    savings_per_round = dropped * cache_read_mul
    summary_cost = max(0, int(summary_usage_tokens))
    total_cost = rewrite_cost + summary_cost
    payback_rounds = (total_cost / savings_per_round
                      if savings_per_round > 0 else float('inf'))
    return {
        'cache_read_tokens': cache_read,
        'cache_replay_tokens_before': cache_replay_before,
        'cache_replay_tokens_after': cache_replay_after,
        'cache_rewrite_tokens': cache_rewrite_tokens,
        'dropped_tokens': dropped,
        'cache_write_mul': cache_write_mul,
        'cache_read_mul': cache_read_mul,
        'marginal_savings_per_dropped_token': cache_read_mul,
        'rewrite_cost_tokens': rewrite_cost,
        'summary_cost_tokens': summary_cost,
        'savings_per_round_tokens': savings_per_round,
        'payback_rounds': payback_rounds,
        'pricing_source': pricing_source,
    }


def _proactive_payback_policy(
    task: dict | None,
    *,
    current_round: object = None,
    remaining_api_rounds: object = None,
) -> tuple[float, str]:
    """Return the exact-ROI horizon selected by the request strategy.

    ``adaptive`` already makes an observable expected-value decision before
    entering L2.  Reapplying the fixed one-round horizon inside L2 used to
    veto candidates that the adaptive decision had explicitly admitted.  Use
    that same bounded remaining-round horizon for both exact candidate checks.
    The fixed strategy starts with the shipped one-round rule, then earns a
    bounded wider horizon from observed prefix-rewrite cadence. The task's
    remaining hard API-round budget caps that horizon.
    """
    fixed = max(0.0, float(_AUTO_COMPACT_MIN_PAYBACK_ROUNDS))
    cfg = ((task or {}).get('config') or {}) if isinstance(task, dict) else {}
    comp_cfg = cfg.get('compaction') if isinstance(cfg, dict) else None
    if not (isinstance(comp_cfg, dict)
            and str(comp_cfg.get('strategy') or '').lower() == 'adaptive'):
        horizon = _fixed_compaction_cadence_payback_horizon(
            task,
            current_round,
            remaining_api_rounds=remaining_api_rounds,
        )
        if horizon > fixed:
            return horizon, 'fixed_compaction_cadence'
        return fixed, 'fixed_one_round'

    decision = (task or {}).get('_adaptiveCompactionDecision')
    if not (isinstance(decision, dict) and decision.get('shouldTrigger')):
        return fixed, 'fixed_one_round'
    try:
        horizon = float(decision.get('remainingRoundsMedian'))
    except (TypeError, ValueError, OverflowError):
        return fixed, 'fixed_one_round'
    if not math.isfinite(horizon) or horizon < fixed:
        return fixed, 'fixed_one_round'
    # Mirrors _adaptive_compaction_economics' established request bound.  The
    # private decision is still validated here rather than trusted blindly.
    return min(200.0, horizon), 'adaptive_expected_horizon'


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
        cache_read = int((economics or {}).get('cache_read_tokens') or 0)
        if reason == 'cache_negative' and cache_read > 0:
            try:
                payback_limit = float((economics or {}).get(
                    'payback_limit_rounds',
                    _AUTO_COMPACT_MIN_PAYBACK_ROUNDS))
            except (TypeError, ValueError, OverflowError):
                payback_limit = float(_AUTO_COMPACT_MIN_PAYBACK_ROUNDS)
            task['_autoCompactRetryWitness'] = {
                'reason': reason,
                'cacheReadTokens': cache_read,
                'paybackLimitRounds': payback_limit,
            }
        else:
            task.pop('_autoCompactRetryWitness', None)
    return int(task['_autoCompactRetryAfterTokens'])


# ═══════════════════════════════════════════════════════════════════════════════
#  Core: execute_compact_tool — pure LLM summary with selective turn compression
# ═══════════════════════════════════════════════════════════════════════════════

def execute_compact_tool(messages: list, task: dict | None = None, **kwargs) -> str:
    """Execute context compaction — force-injected by the orchestrator only.

    NOT in the model's tool list. The model never calls this voluntarily.
    Triggered when estimated tokens exceed 80% of usable context.

    Pure LLM summary approach with selective turn compression.
    """
    conv_id = task.get('convId', '') if task else ''
    log_id = conv_id[:8] if conv_id else '?'
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    # Optional out-param: caller passes a mutable dict to learn whether
    # messages were actually mutated. Stays False on every early-return
    # failure path; flipped to True only after the message list is
    # replaced.  reactive_compact relies on this so its head-truncate
    # safety net engages when the LLM summary comes back empty.
    _result_meta = kwargs.get('_result_meta') if kwargs else None
    if isinstance(_result_meta, dict):
        _result_meta['compacted'] = False

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

    boundary = _find_turn_boundary(
        messages, budget_tokens=budget_tokens, max_turns=max_turns,
    )

    if boundary >= len(messages):
        logger.error(
            '%s [Compact] REFUSING — no user message found to anchor preservation. '
            'msg_count=%d  tokens=%d  model=%s',
            pfx, msg_count_before, tokens_before,
            (task.get('config', {}) or {}).get('model', '?') if task else '?',
        )
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction skipped — no user message found to '
                'anchor preservation. Messages preserved as-is.')

    system_end = 0
    for i, m in enumerate(messages):
        if m.get('role') == 'system':
            system_end = i + 1
        else:
            break

    if boundary >= len(messages) - 0 and boundary <= system_end:
        logger.error(
            '%s [Compact] REFUSING — boundary=%d would preserve 0 live messages '
            '(system_end=%d, total=%d)',
            pfx, boundary, system_end, msg_count_before,
        )
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction skipped — boundary calculation would '
                'preserve no live messages. Bailing out to prevent data loss.')

    old_messages = messages[:boundary]
    recent_messages = messages[boundary:]

    # OBJECTIVE ANCHOR — the first real user message is the north-star
    #   objective.  If it falls in the to-be-summarized ``old_messages`` it
    #   would be lossily paraphrased (and re-paraphrased every subsequent
    #   compaction → unbounded drift), so we PULL IT OUT and re-insert it
    #   verbatim exactly once, right after the system messages.  If it is
    #   already in ``recent_messages`` (short conversation) there is nothing to
    #   do — it's preserved as-is.  Re-insertion uses a shallow message copy so
    #   the request projection can carry a private structural identity without
    #   mutating the authoritative message. The API fields remain verbatim. A
    #   subsequent compaction finds the SAME content at the front of
    #   ``recent_messages`` and never duplicates it — idempotent,
    #   byte-identical, cache-prefix-stable.
    anchor_idx = _objective_anchor_index(messages)
    anchor_msg = None
    if anchor_idx is not None and anchor_idx < boundary:
        anchor_msg = dict(messages[anchor_idx])
        anchor_msg['_isObjectiveAnchor'] = True
        # Summarize everything old EXCEPT the anchor.
        old_messages = [m for k, m in enumerate(messages[:boundary])
                        if k != anchor_idx]
        logger.info('%s [Compact] Preserving objective anchor verbatim '
                    '(msg idx=%d) across summary', pfx, anchor_idx)

    # ── INTRA-TURN FOLD (single-giant-turn overflow) ──
    #   ``_find_turn_boundary`` ALWAYS preserves the current turn whole, so a
    #   single agentic turn (one user request answered with dozens of tool
    #   rounds) that fills the window on its own left ``recent_messages`` huge
    #   and ``old_messages`` tiny — summarizing only the old region barely
    #   shrank anything, and the automatic path could not reduce it at all
    #   (the structural gap the manual /compact 档B fold already fixed). Fold
    #   the COLD tool-call rounds OUT of the preserved region here too: keep
    #   the most-recent hot-tail rounds verbatim, and feed the cold rounds to
    #   the summarizer alongside ``old_messages``. Whole-round removal (shared
    #   ``_split_cold_rounds`` policy) can never orphan a ``tool`` message. A
    #   no-op when the preserved region has <= hot-tail tool-call rounds, so a
    #   normal multi-turn chat near the window is byte-identical to before.
    folded_recent, cold_round_msgs = _fold_recent_intra_turn(
        recent_messages, hot_budget_tokens=budget_tokens)
    if cold_round_msgs:
        logger.info('%s [Compact] Intra-turn fold: %d cold round-message(s) '
                    'folded out of the preserved region (%d recent → %d kept)',
                    pfx, len(cold_round_msgs), len(recent_messages),
                    len(folded_recent))
    recent_messages = folded_recent
    summary_input = list(old_messages) + list(cold_round_msgs)

    _proactive = bool(kwargs.get('_proactive_economic')) if kwargs else False
    _payback_limit, _payback_policy = _proactive_payback_policy(
        task,
        current_round=kwargs.get('_compaction_round'),
        remaining_api_rounds=kwargs.get('_compaction_remaining_api_rounds'),
    )

    # Nothing to summarize: no old region with real content AND the preserved
    # turn had too few tool-call rounds to fold (or is one fat non-tool
    # message). A summary_input of only leading ``system`` rows carries no
    # foldable history — summarizing it would waste a cheap-model call and
    # inject a contentless summary, so decline gracefully — mirrors the manual
    # path's "decline rather than risk a cross-message break".
    # _result_meta.compacted stays False so the reactive head-truncate net
    # still engages.
    if not any(m.get('role') != 'system' for m in summary_input):
        logger.info('%s [Compact] Nothing foldable — no old region and preserved '
                    'turn has <= hot-tail tool rounds; skipping', pfx)
        if isinstance(_result_meta, dict):
            _result_meta.update({
                'compacted': False,
                'reason': 'nothing_foldable',
            })
        if _proactive:
            _defer_proactive_retry(
                task, tokens_before, reason='nothing_foldable')
        return ('Context compaction skipped — no foldable history '
                '(preserved turn within the hot-round tail). '
                'Messages preserved as-is.')

    # Proactive L2 must earn the cache-prefix rewrite. Project the foldable
    # region vanishing while charging a conservative summary-call estimate.
    # This keeps an uneconomic attempt from buying a summary and rejecting it
    # only afterwards. Forced manual/reactive paths bypass this because their
    # goal is user intent or window correctness.
    _foldable_tokens = _estimate_total_tokens(summary_input)
    _projected_summary_cost = _projected_summary_usage_tokens(
        summary_input, current_query, task,
        anchor_text=anchor_text) if _proactive else 0
    if _proactive:
        _best_candidate_tokens = max(0, tokens_before - _foldable_tokens)
        _best_econ = _proactive_cache_economics(
            task, tokens_before=tokens_before,
            candidate_tokens=_best_candidate_tokens,
            summary_usage_tokens=_projected_summary_cost)
        _best_econ.update({
            'payback_limit_rounds': _payback_limit,
            'payback_policy': _payback_policy,
        })
        _best_reduction = (_best_econ['dropped_tokens']
                           / max(1, tokens_before))
        if _best_reduction < _AUTO_COMPACT_MIN_REDUCTION_RATIO:
            logger.info(
                '%s [Compact] Proactive low-yield decline before summary: '
                'foldable=%d total=%d projected_reduction=%.1f%% < %.1f%%',
                pfx, _foldable_tokens, tokens_before,
                _best_reduction * 100,
                _AUTO_COMPACT_MIN_REDUCTION_RATIO * 100)
            if isinstance(_result_meta, dict):
                _result_meta.update({
                    'compacted': False,
                    'reason': 'low_yield',
                    'foldable_tokens': _foldable_tokens,
                    'projected_reduction_ratio': _best_reduction,
                })
            _defer_proactive_retry(
                task, tokens_before, reason='low_yield',
                economics=_best_econ)
            return ('Context compaction skipped — foldable history is below '
                    'the automatic minimum reduction. Messages preserved '
                    'as-is.')
        if (_best_econ['cache_read_tokens'] > 0
                and _best_econ['payback_rounds']
                > _payback_limit):
            logger.info(
                '%s [Compact] Proactive uneconomic decline before summary: '
                'foldable=%d total=%d cache_read=%d projected_rewrite=%d '
                'summary_estimate=%d payback=%.2f rounds > %.2f — preserving '
                'cache prefix',
                pfx, _foldable_tokens, tokens_before,
                _best_econ['cache_read_tokens'],
                _best_econ['cache_rewrite_tokens'],
                _projected_summary_cost,
                _best_econ['payback_rounds'],
                _payback_limit)
            if isinstance(_result_meta, dict):
                _result_meta.update({
                    'compacted': False,
                    'reason': 'cache_negative',
                    'foldable_tokens': _foldable_tokens,
                    'projected_summary_cost_tokens': _projected_summary_cost,
                    'economics': _best_econ,
                })
            _defer_proactive_retry(
                task, tokens_before, reason='cache_negative',
                economics=_best_econ)
            return ('Context compaction skipped — foldable history cannot '
                    'repay the warm-cache rewrite. Messages preserved as-is.')

    preserved_turns = sum(
        1 for m in recent_messages if m.get('role') == 'user'
    )

    logger.info('%s [Compact] Summarizing %d messages (%d old + %d cold intra-turn '
                'rounds), preserving %d recent (%d turns), query=%.100s',
                pfx, len(summary_input), len(old_messages), len(cold_round_msgs),
                len(recent_messages), preserved_turns, current_query)

    _summary_usage: dict = {}
    _summary_started = time.monotonic()
    _summary_pipeline_failure_reason = ''
    try:
        summary_text = _generate_query_aware_summary(
            summary_input, current_query, pfx, conv_id=conv_id, task=task,
            usage_out=_summary_usage,
            anchor_text=anchor_text,
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

    _summary_fallback = False
    _summary_fallback_reason = ''
    if (not summary_text
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

    if not summary_text:
        logger.warning('%s [Compact] Summary generation failed — keeping messages intact', pfx)
        if isinstance(_result_meta, dict):
            _result_meta.update({
                'compacted': False,
                'tokens_before': int(tokens_before),
                'msgs_before': int(msg_count_before),
                'trigger': kwargs.get('_compaction_trigger') or 'force',
                'reason': kwargs.get('_compaction_reason') or '',
                'summary_usage': dict(_summary_usage),
                'summary_duration_ms': int(_summary_duration_ms),
                'summaryFailureReason': (
                    _summary_pipeline_failure_reason or 'summary_failed'),
            })
        return ('Context compaction attempted but summary generation failed. '
                'Messages preserved as-is.')

    _recovery_base_summary = summary_text if _summary_fallback else ''

    # V2 validates critical state before accepting lossy model prose. If the
    # model omitted unfinished work or grounded mutation/test evidence, reject
    # that prose and continue with a deterministic transcript-derived state
    # projection. This evicts cold history without claiming the rejected
    # summary was faithful.
    _comp_cfg = ((task or {}).get('config', {}) or {}).get('compaction') or {}
    if (not _summary_fallback
            and str(_comp_cfg.get('strategy') or '').lower() == 'adaptive'):
        try:
            import json as _json
            from lib.tasks_pkg.context_composer.task_state import (
                derive_task_state_snapshot,
            )
            _ledger = (task or {}).get('_contextEvidenceLedger') or {}
            _critical_types = {
                'test_result', 'error', 'mutation_result', 'modified_file',
                'unfinished',
            }
            _critical = [entry for entry in (_ledger.get('entries') or [])
                         if entry.get('type') in _critical_types]
            _missing_ids = [str(entry.get('id') or '') for entry in _critical
                            if str(entry.get('id') or '') not in summary_text]
            _snapshot = derive_task_state_snapshot(messages, task)
            _missing_fields = _summary_missing_task_state_fields(
                summary_text, _snapshot)
            _state_block = (
                '## TaskStateSnapshotV1\n' + _snapshot.to_context_text())
            _evidence_block = (
                '## Critical Evidence\n' + '\n'.join(
                    f"[EVIDENCE {entry.get('id')}] "
                    + _json.dumps(entry, ensure_ascii=False,
                                  sort_keys=True, separators=(',', ':'))
                    for entry in _critical[:32]))
            if _missing_ids or _missing_fields:
                _rejected_digest = hashlib.sha256(
                    summary_text.encode('utf-8')).hexdigest()[:16]
                summary_text = _state_block + '\n\n' + _evidence_block
                logger.warning(
                    '%s [Compact] rejected lossy summary digest=%s '
                    'missing_critical=%d missing_fields=%s; using '
                    'deterministic state projection', pfx, _rejected_digest,
                    len(_missing_ids), ','.join(_missing_fields))
                if isinstance(_result_meta, dict):
                    _result_meta.update({
                        'summaryRejected': True,
                        'summaryRejectedDigest': _rejected_digest,
                        'summaryRejectionReason': 'critical_state_missing',
                        'missingEvidenceIds': _missing_ids,
                        'missingTaskStateFields': _missing_fields,
                    })
            else:
                # Adaptive L2 always carries the versioned state projection;
                # accepted model prose is only the short narrative layer.
                summary_text = (
                    _state_block + '\n\n## Narrative Summary\n'
                    + summary_text + '\n\n' + _evidence_block)
        except Exception as exc:
            logger.warning('%s [Compact] v2 summary validation failed closed: %s',
                           pfx, exc)
            if isinstance(_result_meta, dict):
                _result_meta.update({
                    'compacted': False,
                    'summaryRejected': True,
                    'summaryRejectionReason': 'validation_failed',
                })
            return ('Context compaction attempted but summary validation '
                    'failed. Messages preserved as-is.')

    # The receipt's model-authored Objective is the current effective goal —
    # re-pin the autopilot objective when it changed so the VU measures the
    # assistant against the LATEST binding human goal, not a stale opening
    # ask. No-ops without a pin, without an Objective section, or when the
    # adaptive fallback replaced the model prose (no Objective → no re-pin).
    # Fully fail-safe: run bookkeeping must never break compaction.
    try:
        from lib.tasks_pkg.autopilot_state import _update_objective_from_receipt
        _update_objective_from_receipt(
            conv_id, _extract_summary_objective(summary_text),
            user_id=_task_owner(task))
    except Exception as exc:
        logger.debug('%s [Compact] autopilot objective re-pin skipped: %s',
                     pfx, exc)

    recent_files = _extract_recently_accessed_files(messages)
    _recent_files_block = ''
    if recent_files:
        file_list = '\n'.join(f'  - {f}' for f in recent_files)
        _recent_files_block = (
            f'\n\n### Recently Accessed Files\n'
            f'Use read_files to review current state if needed:\n'
            f'{file_list}'
        )
        summary_text += _recent_files_block

    # Codex-inspired (turn_diff_tracker.rs): the summary must preserve WHAT
    # changed this turn, not just WHICH files. Journal pre-images make this
    # free of extra writes; failures are advisory-only.
    _turn_diff_included = False
    try:
        from lib.tasks_pkg.commit_round._turn_diff import build_turn_diff_block
        _cfg = (task or {}).get('config', {}) or {}
        _diff_block = build_turn_diff_block(
            task, _cfg.get('projectPath') or '',
            _cfg.get('projectPaths') or None)
        if _diff_block:
            summary_text += f'\n\n{_diff_block}'
            _turn_diff_included = True
    except Exception as e:
        logger.debug('%s [Compact] turn-diff block failed: %s', pfx, e)

    if (_summary_fallback
            and len(summary_text) > _DETERMINISTIC_RECOVERY_MAX_CHARS):
        # Optional freshness hints must not break the emergency receipt's one
        # global budget. Prefer the bounded recovery state, then add recent
        # file handles only if the complete block still fits; drop the turn
        # diff whole rather than cutting any record mid-entry.
        summary_text = _recovery_base_summary
        _turn_diff_included = False
        if (_recent_files_block
                and len(summary_text + _recent_files_block)
                <= _DETERMINISTIC_RECOVERY_MAX_CHARS):
            summary_text += _recent_files_block
        logger.warning(
            '%s [Compact] Recovery receipt extras exceeded %d chars; '
            'dropped optional blocks to preserve the global budget',
            pfx, _DETERMINISTIC_RECOVERY_MAX_CHARS)

    system_msgs = []
    for msg in old_messages:
        if msg.get('role') == 'system':
            system_msgs.append(msg)
        else:
            break

    # ── USER-VERBATIM RETENTION (Codex-inspired, codex-rs compact.rs) ──
    #   The summary is lossy; the user's literal instructions must not survive
    #   ONLY as paraphrase. Retain the old region's real user messages
    #   VERBATIM inside ONE ``_isMeta`` wrapper message: the wrapper is
    #   transparent to ``_find_turn_boundary`` (batch-1 meta-skip), excluded
    #   from future retention passes (no feedback duplication), and never
    #   touches the RAW transcript (the automatic path is ephemeral), so the
    #   raw→api merge cannot mangle it. Extraction happens AFTER the anchor
    #   was pulled out of ``old_messages``, so the anchor is never duplicated.
    retained_user_texts = _collect_user_verbatim(old_messages)
    retained_wrapper = None
    if retained_user_texts:
        _audit_user_verbatim_once()
        quoted = '\n\n'.join(f'[{k}] {t}'
                             for k, t in enumerate(retained_user_texts, 1))
        retained_wrapper = {
            'role': 'user',
            'content': (
                '<retained_user_messages>\n'
                "The user's earlier messages from the now-summarized history, "
                'preserved VERBATIM (oldest first). They remain binding '
                'unless superseded by later messages.\n\n'
                f'{quoted}\n'
                '</retained_user_messages>'),
            '_isMeta': True,
        }
        logger.info('%s [Compact] Retaining %d user message(s) verbatim '
                    '(%d chars) across the summary',
                    pfx, len(retained_user_texts), len(quoted))

    # Rebuild: system → [objective anchor, if it was in the summarized region]
    # → [retained user-verbatim wrapper] → recent.  The anchor is placed
    # immediately after the system block so the model always sees the original
    # goal at a stable position, and exactly once (it was removed from
    # ``old_messages`` above, so it isn't also inside the summary text's
    # source, and it is NOT in ``recent_messages`` because
    # anchor_idx < boundary).
    anchor_block = [anchor_msg] if anchor_msg is not None else []
    retained_block = [retained_wrapper] if retained_wrapper is not None else []
    new_messages = (list(system_msgs) + anchor_block + retained_block
                    + list(recent_messages))

    intermediate_tokens_after = _estimate_total_tokens(new_messages)
    compacted_message_count = sum(
        1 for message in summary_input if message.get('role') != 'system')

    def _render_compact_result(estimated_final_tokens: int) -> str:
        estimated_reduction_pct = (
            1 - estimated_final_tokens / max(1, tokens_before)) * 100
        return '\n'.join([
            '## Context Compacted — Selective Summary\n',
            f'Compressed {compacted_message_count} historical messages. '
            'Estimated compacted context at this stage (receipt included; '
            'later context providers may add content): '
            f'{tokens_before:,} → {estimated_final_tokens:,} tokens '
            f'({estimated_reduction_pct:.0f}% reduction).\n',
            summary_text,
        ])

    # The receipt contains its own projected total, so converge the tiny
    # self-reference until the rendered text and estimate agree. This replaces
    # the old misleading intermediate count that omitted the receipt itself.
    compact_result = _render_compact_result(intermediate_tokens_after)
    _candidate_pair = [
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': '_roi_candidate', 'type': 'function',
                         'function': {'name': _COMPACT_TOOL_NAME,
                                      'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': '_roi_candidate',
         'name': _COMPACT_TOOL_NAME, 'content': compact_result},
    ]
    for _projection_pass in range(3):
        _candidate_tokens = _estimate_total_tokens(
            new_messages + _candidate_pair)
        rendered = _render_compact_result(_candidate_tokens)
        if rendered == compact_result:
            break
        compact_result = rendered
        _candidate_pair[-1]['content'] = compact_result
    _candidate_tokens = _estimate_total_tokens(new_messages + _candidate_pair)

    _summary_cost_tokens = _summary_usage_tokens(_summary_usage)
    _accounting_econ = None
    _adoption_econ = None
    if _proactive:
        _accounting_econ = _proactive_cache_economics(
            task, tokens_before=tokens_before,
            candidate_tokens=_candidate_tokens,
            summary_usage_tokens=_summary_cost_tokens)
        # Once generated, summary tokens are sunk. Adoption must compare only
        # future cache rewrite cost with future savings; otherwise a paid,
        # faithful candidate can be discarded and regenerated on a later turn.
        _adoption_econ = _proactive_cache_economics(
            task, tokens_before=tokens_before,
            candidate_tokens=_candidate_tokens,
            summary_usage_tokens=0)
        for _economics in (_accounting_econ, _adoption_econ):
            _economics.update({
                'payback_limit_rounds': _payback_limit,
                'payback_policy': _payback_policy,
            })
        _realized_reduction = (_accounting_econ['dropped_tokens']
                               / max(1, tokens_before))
        if _realized_reduction < _AUTO_COMPACT_MIN_REDUCTION_RATIO:
            logger.info(
                '%s [Compact] Proactive candidate rejected: projected=%d '
                'before=%d realized_reduction=%.1f%% < %.1f%% — preserving '
                'messages and cache prefix',
                pfx, _candidate_tokens, tokens_before,
                _realized_reduction * 100,
                _AUTO_COMPACT_MIN_REDUCTION_RATIO * 100)
            if isinstance(_result_meta, dict):
                _result_meta.update({
                    'compacted': False,
                    'reason': 'low_yield',
                    'candidate_tokens': int(_candidate_tokens),
                    'realized_reduction_ratio': _realized_reduction,
                    'economics': _accounting_econ,
                    'adoptionEconomics': _adoption_econ,
                })
            _defer_proactive_retry(
                task, tokens_before, reason='low_yield',
                economics=_accounting_econ)
            return ('Context compaction skipped — generated summary is below '
                    'the automatic minimum reduction. Messages preserved '
                    'as-is.')
        if (_adoption_econ['cache_read_tokens'] > 0
                and _adoption_econ['payback_rounds']
                > _payback_limit):
            logger.info(
                '%s [Compact] Proactive candidate rejected: projected=%d '
                'before=%d dropped=%d cache_read=%d rewrite=%d '
                'summary_cost_sunk=%d future_payback=%.2f rounds > %.2f — '
                'preserving cache prefix',
                pfx, _candidate_tokens, tokens_before,
                _adoption_econ['dropped_tokens'],
                _adoption_econ['cache_read_tokens'],
                _adoption_econ['cache_rewrite_tokens'],
                _summary_cost_tokens, _adoption_econ['payback_rounds'],
                _payback_limit)
            if isinstance(_result_meta, dict):
                _result_meta.update({
                    'compacted': False,
                    'reason': 'cache_negative',
                    'candidate_tokens': int(_candidate_tokens),
                    'economics': _accounting_econ,
                    'adoptionEconomics': _adoption_econ,
                })
            _defer_proactive_retry(
                task, tokens_before, reason='cache_negative',
                economics=_accounting_econ)
            return ('Context compaction skipped — generated summary would not '
                    'repay the warm-cache rewrite. Messages preserved as-is.')

    _round_num = kwargs.get('_compaction_round')
    if _round_num is None:
        _round_num = task.get('round_num') if task else 0
    _round_num = int(_round_num or 0)

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

    messages.clear()
    messages.extend(new_messages)
    if task is not None:
        task.pop('_autoCompactRetryAfterTokens', None)
        task.pop('_autoCompactRetryWitness', None)
    with _cooldown_lock:
        _summary_cooldowns[conv_id] = time.time()

    if isinstance(_result_meta, dict):
        _result_meta.update({
            'compacted': True,
            'tokens_before': int(tokens_before),
            'tokens_after_estimated': int(_candidate_tokens),
            'token_count_kind': 'estimated',
            'msgs_before': int(msg_count_before),
            'archive_id': _archive_id,
            'summary_text': summary_text,
            'trigger': kwargs.get('_compaction_trigger') or 'force',
            'reason': kwargs.get('_compaction_reason') or '',
            'round_num': _round_num,
            'summary_usage_tokens': int(_summary_cost_tokens),
            'projected_summary_usage_tokens': int(_projected_summary_cost),
            'summary_usage': dict(_summary_usage),
            'summary_duration_ms': int(_summary_duration_ms),
            'summaryFallback': bool(_summary_fallback),
            'summaryFallbackReason': _summary_fallback_reason,
            'summarized_messages': int(compacted_message_count),
            'preserved_turns': int(preserved_turns),
            'folded_tool_rounds': sum(
                1 for message in cold_round_msgs
                if isinstance(message, dict)
                and message.get('role') == 'assistant'
                and message.get('tool_calls')
            ),
            'objective_anchored': anchor_msg is not None,
            'durable_objective_applied': bool(
                _extract_summary_objective(summary_text)),
            'retained_user_messages': len(retained_user_texts),
            'recent_files': list(recent_files),
            'turn_diff_included': bool(_turn_diff_included),
            'mode': (
                'turns_and_intra_turn'
                if cold_round_msgs and any(
                    message.get('role') != 'system'
                    for message in old_messages)
                else 'intra_turn' if cold_round_msgs else 'turns'
            ),
            'economics': _accounting_econ,
            'adoptionEconomics': _adoption_econ,
        })

    # ── SUCCESS-PATH CONVERGENCE CHECK (post-fold hot-tail overflow) ──
    #   The fold + summary succeeded, but ``_find_turn_boundary`` preserves the
    #   hot-tail rounds WHOLE. If those surviving rounds are themselves so large
    #   that the PROJECTED request — the current ``messages`` PLUS the summary
    #   tool-pair the caller (``force_compact_if_needed``) will append — STILL
    #   exceeds the same trigger ceiling, sending it would only be bounded next
    #   round or by the reactive 413 net. In an OOM-kill-prone deploy the "next
    #   round" may never arrive, so converge NOW with the pairing-safe
    #   head-truncate rather than emit an over-window request. Measured with the
    #   SAME yardstick as ``_should_force_compact`` (authoritative count vs
    #   ``usable × _SUMMARY_TRIGGER_RATIO``) so the check and the trigger agree.
    _summary_pair = [
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': '_conv_proj', 'type': 'function',
                         'function': {'name': _COMPACT_TOOL_NAME,
                                      'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': '_conv_proj',
         'name': _COMPACT_TOOL_NAME, 'content': compact_result},
    ]

    def _project_tokens() -> tuple[int, str]:
        try:
            # Local import (same pattern as the _allow_ht block below): the
            # bare name was never bound in this module, so EVERY projection
            # raised NameError into the except and silently ran 'heuristic'.
            from lib.tasks_pkg.compaction._tokens import (
                _count_tokens_authoritative)
            return _count_tokens_authoritative(list(messages) + _summary_pair, task)
        except Exception as _pe:
            logger.debug('%s [Compact] convergence projection count failed '
                         '(%s) — using heuristic', pfx, _pe)
            return _estimate_total_tokens(messages), 'heuristic'

    window_ceiling = int(usable * _SUMMARY_TRIGGER_RATIO)
    effective_trigger, _window_trigger, _working_set_trigger = (
        _compaction_trigger_threshold(task))
    ceiling = (min(window_ceiling, int(effective_trigger))
               if _proactive else window_ceiling)
    ceiling_kind = 'effective automatic target' if _proactive else 'window safety'
    proj_tokens, proj_method = _project_tokens()
    if proj_tokens > ceiling:
        logger.warning(
            '%s [Compact] Post-fold STILL over ceiling: projected=%d (via %s) '
            '> ceiling=%d (%s; usable=%d) — preserved hot-tail rounds are '
            'oversized; converging with pairing-safe head-truncate',
            pfx, proj_tokens, proj_method, ceiling, ceiling_kind, usable)
        from lib.tasks_pkg.compaction._reactive import _head_truncate
        _tok_pre = _estimate_total_tokens(messages)
        _dropped = _head_truncate(
            messages, task, reported_token_count=proj_tokens,
            event_name='post_compact_converge')
        _tok_post = _estimate_total_tokens(messages)
        logger.warning(
            '%s [Compact] Post-fold head-truncate dropped %d message(s): '
            'preserved-region tokens %d → %d (conv=%s round=%s)',
            pfx, _dropped, _tok_pre, _tok_post, log_id,
            (task.get('round_num') if task else '?'))
        # Re-measure. If STILL over (pathological: a single hot round bigger
        # than the window — head-truncate is floored at system_end+4 messages),
        # do NOT raise and abort the round. Log an ERROR so this boundary is
        # visible in error.log, and pass through: the reactive 413 net is the
        # last resort this round.
        proj_after, _ = _project_tokens()
        if proj_after > ceiling:
            logger.error(
                '%s [Compact] Post-fold convergence INCOMPLETE — projected still '
                '%d > ceiling=%d after head-truncate (dropped=%d; only the system '
                'prefix + an oversized hot round remain). Passing through; the '
                'reactive 413 net must bound this request. conv=%s round=%s',
                pfx, proj_after, ceiling, _dropped, log_id,
                (task.get('round_num') if task else '?'))

    return compact_result


# ═══════════════════════════════════════════════════════════════════════════════
#  Force compact: inject context_compact tool call when over threshold
# ═══════════════════════════════════════════════════════════════════════════════

def force_compact_if_needed(messages: list, task: dict | None = None,
                            keep_recent_pairs: int | None = None,
                            preserve_budget_tokens: int | None = None,
                            *, force: bool = False,
                            **kwargs) -> bool:
    """Check token usage and force-inject a context_compact tool round if needed.

    Args:
        keep_recent_pairs: Legacy knob mapped to ``max_turns`` (turn-count cap).
        preserve_budget_tokens: Token budget for verbatim preservation.
        force: Skip the ``_should_force_compact`` threshold gate.

    Returns True if compaction was performed, False otherwise.
    """
    _measurement_out = kwargs.get('_measurement_out')
    if not isinstance(_measurement_out, dict):
        _measurement_out = None
    if not force and not _should_force_compact(
            messages, task, measurement_out=_measurement_out,
            current_round=kwargs.get('_compaction_round'),
            remaining_api_rounds=kwargs.get(
                '_compaction_remaining_api_rounds')):
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

    conv_id = task.get('convId', '') if task else ''
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    logger.info('%s [ForceCompact] Injecting context_compact for conv=%s '
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
    compact_result = execute_compact_tool(
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
        _proactive_economic=not force,
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
        # Deterministic proactive safety net (fix for the OOM fatal loop).
        #   The summary LLM is the ONLY mechanism the proactive path had; on
        #   a vanilla/exported deploy the cheap-model dispatch can fail
        #   outright (no model tagged 'cheap', saturated single model,
        #   summary input itself too big). Historically force-compact then
        #   returned False and did nothing, so the context stayed pinned near
        #   the window every round — and the reactive head-truncate net never
        #   fired because the max_tokens clamp keeps the request just under
        #   the hard ceiling (no API rejection). Nothing bounded the context
        #   → unbounded re-send → OOM (SIGKILL).
        #
        #   So when the proactive pipeline opts in (_allow_head_truncate_fallback)
        #   AND we are genuinely over the usable window, fall through to the
        #   same last-resort _head_truncate the reactive path already trusts,
        #   right here. This is bounded, logged (audit_log
        #   'proactive_head_truncate') context loss — strictly better than a
        #   process death. The empty-summary→False contract is preserved for
        #   the NON-critical case (still headroom before the window): we only
        #   head-truncate when estimated input >= usable window.
        _allow_ht = bool(kwargs.get('_allow_head_truncate_fallback')
                         if isinstance(kwargs, dict) else False)
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
                    '%s [ForceCompact] Summary failed AND context critically '
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
                _dropped = _head_truncate(
                    messages, task,
                    reported_token_count=_est_tokens,
                    event_name='proactive_head_truncate')
                if _dropped:
                    _fallback_tokens_after = _estimate_total_tokens(messages)
                    _fallback_receipt = build_compaction_receipt(
                        trigger=_trigger,
                        status='completed',
                        strategy='deterministic_recovery',
                        implementation='pairing_safe_head_truncate',
                        mode='summary_failure_over_window',
                        continuation_format='none',
                        summary_generated=False,
                        summary_usage=_meta.get('summary_usage'),
                        summary_duration_ms=(
                            _meta.get('summary_duration_ms') or 0),
                        dropped_messages=_dropped,
                        outcome_reason='summary_failed_over_window',
                    )
                    if _fallback_archive_id is not None:
                        try:
                            from lib.agent_core.store import get_conversation_store
                            get_conversation_store().update_archive_summary(
                                _fallback_archive_id, '',
                                int(_fallback_tokens_after), len(messages),
                                user_id=_task_owner(task),
                                receipt=_fallback_receipt)
                        except Exception as _up_e:
                            logger.debug('%s [ForceCompact] fallback receipt '
                                         'update failed: %s', pfx, _up_e)
                        if task is not None:
                            try:
                                from lib.agent_core.events import EventType, build_event
                                from lib.tasks_pkg.manager import append_event
                                _fallback_reduction = (
                                    1 - _fallback_tokens_after
                                    / max(1, _fallback_tokens_before)) * 100
                                append_event(task, build_event(
                                    EventType.COMPACTION_DONE,
                                    archiveId=str(_fallback_archive_id),
                                    convId=conv_id,
                                    trigger=_trigger,
                                    tokensBefore=int(_fallback_tokens_before),
                                    tokensAfter=int(_fallback_tokens_after),
                                    tokenCountKind='estimated',
                                    msgsBefore=int(_fallback_msgs_before),
                                    msgsAfter=len(messages),
                                    reductionPct=round(_fallback_reduction, 1),
                                    roundNum=int(
                                        kwargs.get('_compaction_round') or 0),
                                    receipt=_fallback_receipt,
                                ))
                            except Exception as _ev_e:
                                logger.debug('%s [ForceCompact] fallback done '
                                             'event failed: %s', pfx, _ev_e)
                    # Context was bounded — surface as a real compaction so
                    # the pipeline notifies the cache tracker (prefix changed)
                    # and the round proceeds with a smaller prompt.
                    _record_compaction_cadence(
                        task, kwargs.get('_compaction_round'))
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
                        mode='summary_failure_over_window',
                        continuation_format='none',
                        summary_generated=False,
                        summary_usage=_meta.get('summary_usage'),
                        summary_duration_ms=(
                            _meta.get('summary_duration_ms') or 0),
                        outcome_reason='no_safe_messages_to_drop',
                    )
                    try:
                        from lib.agent_core.store import get_conversation_store
                        get_conversation_store().update_archive_summary(
                            _fallback_archive_id, '',
                            _fallback_tokens_before, len(messages),
                            user_id=_task_owner(task), receipt=_failed_receipt)
                    except Exception as _up_e:
                        logger.debug('%s [ForceCompact] failed fallback receipt '
                                     'update failed: %s', pfx, _up_e)
                    if task is not None:
                        try:
                            from lib.agent_core.events import EventType, build_event
                            from lib.tasks_pkg.manager import append_event
                            append_event(task, build_event(
                                EventType.COMPACTION_DONE,
                                archiveId=str(_fallback_archive_id),
                                convId=conv_id,
                                trigger=_trigger,
                                tokensBefore=int(_fallback_tokens_before),
                                tokensAfter=_fallback_tokens_before,
                                tokenCountKind='estimated',
                                msgsBefore=int(_fallback_msgs_before),
                                msgsAfter=len(messages),
                                reductionPct=0.0,
                                roundNum=int(
                                    kwargs.get('_compaction_round') or 0),
                                receipt=_failed_receipt,
                            ))
                        except Exception as _ev_e:
                            logger.debug('%s [ForceCompact] failed fallback done '
                                         'event failed: %s', pfx, _ev_e)
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
        return False

    # The economic preflight may decline without mutation.  Surface the phase
    # only after the candidate has been accepted so the UI never advertises a
    # compaction that was intentionally skipped.
    if task is not None:
        try:
            from lib.agent_core.events import Phase, build_phase
            from lib.tasks_pkg.manager import append_event
            append_event(task, build_phase(
                Phase.COMPACTING,
                detail='Compressing earlier context to fit the window…',
                detailKey='stream.phase.compactingWindow'))
        except Exception as _ph_e:
            logger.debug('%s [ForceCompact] phase emit failed: %s', pfx, _ph_e)

    compact_call_id = short_id('compact_', 12)

    messages.append({
        'role': 'assistant',
        'content': None,
        'tool_calls': [{
            'id': compact_call_id,
            'type': 'function',
            'function': {
                'name': _COMPACT_TOOL_NAME,
                'arguments': '{}',
            },
        }],
    })

    messages.append({
        'role': 'tool',
        'tool_call_id': compact_call_id,
        'name': _COMPACT_TOOL_NAME,
        'content': compact_result,
    })

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
    summary_text = _meta.get('summary_text') or ''
    retained: list[str] = []
    lost: list[str] = []
    if task is not None:
        try:
            ledger = task.get('_contextEvidenceLedger')
            if isinstance(ledger, dict):
                from lib.tasks_pkg.compaction._evidence import evidence_retention
                retained, lost = evidence_retention(summary_text, ledger)
        except Exception as _er_e:
            logger.debug('%s [Compact] evidence retention audit failed: %s',
                         pfx, _er_e)

    receipt = build_compaction_receipt(
        trigger=_meta.get('trigger') or 'force',
        status='completed',
        strategy='selective_summary',
        implementation=(
            'deterministic_recovery_receipt'
            if _meta.get('summaryFallback')
            else 'deterministic_task_state_projection'
            if _meta.get('summaryRejected')
            else 'model_summary'),
        mode=_meta.get('mode') or 'turns',
        continuation_format='context_compact_tool',
        summary_generated=not bool(_meta.get('summaryFallback')),
        summary_text=summary_text,
        summary_usage=_meta.get('summary_usage'),
        summary_duration_ms=_meta.get('summary_duration_ms') or 0,
        projected_summary_usage_tokens=(
            _meta.get('projected_summary_usage_tokens') or 0),
        summary_rejected=bool(_meta.get('summaryRejected')),
        summary_rejection_reason=(
            _meta.get('summaryRejectionReason') or ''),
        outcome_reason=(
            _meta.get('summaryFallbackReason') or ''),
        summarized_messages=_meta.get('summarized_messages') or 0,
        preserved_turns=_meta.get('preserved_turns') or 0,
        folded_tool_rounds=_meta.get('folded_tool_rounds') or 0,
        objective_anchored=bool(_meta.get('objective_anchored')),
        durable_objective_applied=bool(
            _meta.get('durable_objective_applied')),
        retained_user_messages=_meta.get('retained_user_messages') or 0,
        recent_files=_meta.get('recent_files') or (),
        turn_diff_included=bool(_meta.get('turn_diff_included')),
        economics=_meta.get('economics'),
        evidence_retained=retained,
        evidence_lost=lost,
    )
    if archive_id is not None:
        try:
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().update_archive_summary(
                archive_id, summary_text, tokens_after, msgs_after,
                user_id=_task_owner(task), receipt=receipt)
        except Exception as _upd_e:
            logger.debug('[Compact] archive row update failed: %s', _upd_e)

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
                ))
            except Exception as _ev_e:
                logger.debug('[Compact] compaction_done emit failed: %s', _ev_e)

    if task is not None:
        try:
            from lib.context_telemetry import record_compaction_event
            record_compaction_event(
                task,
                trigger=_meta.get('trigger') or 'force',
                reason=_meta.get('reason') or '',
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                archive_id=archive_id,
                evidence_retained=retained,
                evidence_lost=lost,
            )
        except Exception as _ct_e:
            logger.debug('%s [Compact] context telemetry failed: %s', pfx, _ct_e)
        finally:
            # `_contextEvidenceLedger` is an in-memory bridge between summary
            # generation and this retention measurement. Do not pin even its
            # bounded tool previews for the rest of a long-running task.
            task.pop('_contextEvidenceLedger', None)

    _record_compaction_cadence(task, _meta.get('round_num'))

    return True
