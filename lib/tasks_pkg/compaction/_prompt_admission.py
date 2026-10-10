"""Final provider-prompt admission after every dynamic context injection.

The ordinary compaction pipeline runs before the round inbox is drained and
before the final tool surface is known.  This module is the last local safety
boundary before request-body construction: it measures messages plus tool
schemas, attempts one forced semantic summary, then refuses the dispatch when
required context still exceeds the ceiling. It never calls the main provider
with an oversized prompt or silently drops durable history.
"""

from __future__ import annotations

from typing import Any

from lib.context_telemetry import validated_tool_schema_token_count
from lib.llm_errors import ContextCompactionError, PromptTooLongError
from lib.log import audit_log, get_logger
from lib.tasks_pkg.compaction._layer2 import force_compact_if_needed
from lib.tasks_pkg.compaction._pipeline import recompose_context_after_compaction

from lib.tasks_pkg.context_composer.checklist import restore_checklist_context
from lib.tasks_pkg.compaction._tokens import (
    _count_tokens_authoritative,
)
from lib.token_counter.base import (
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
)

logger = get_logger(__name__)

_MIN_SUMMARY_PRESERVE_TOKENS = 8_000
_MAX_SUMMARY_PRESERVE_TOKENS = 32_000
_MEASUREMENT_VERSION = 'tofu.prompt-admission/v2'


def _tool_schema_tokens(tools: Any, *, model: str) -> int:
    if not tools:
        return 0
    try:
        from lib.context_telemetry import tool_schema_tokens

        return max(0, int(tool_schema_tokens(tools, model=model) or 0))
    except Exception as exc:
        logger.debug('[PromptAdmission] tool-schema counter unavailable: %s', exc)
        try:
            import json

            from lib.token_counter.heuristic import cheap_estimate_text

            return max(0, int(cheap_estimate_text(json.dumps(
                tools, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')))))
        except Exception as fallback_exc:
            logger.warning(
                '[PromptAdmission] tool-schema fallback failed: %s',
                fallback_exc,
            )
            return 0


def _resolved_ceiling(task: dict[str, Any], *, round_num: int) -> tuple[int, int]:
    """Return ``(hard_ceiling, proactive_target)`` for this dispatch.

    The economic working set is advisory here: the proactive pipeline owns
    its ROI decision. Every round measures the full rendered prompt against
    the same route-derived safety ceiling; neither first dispatch nor a
    pricing-tier boundary reduces physical model capacity.
    """
    from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
    return resolve_admission_budget(task, round_num=round_num)


def _measure(
    messages: list[dict[str, Any]],
    tools: Any,
    task: dict[str, Any],
    *,
    model: str,
    precomputed_tool_schema_tokens: Any = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    prompt_tokens, method = _count_tokens_authoritative(
        messages,
        task,
        measurement_out=detail,
        tool_schema=tools,
        collect_reusable_text_counts=True,
    )
    schema_tokens = validated_tool_schema_token_count(
        tools, precomputed_tool_schema_tokens)
    if schema_tokens is None:
        schema_tokens = _tool_schema_tokens(tools, model=model)
    # The canonical counter above counts the complete request, including the
    # selected tool surface.  Schema counting is only a decomposition for
    # diagnostics and the message compaction budget; adding it again would
    # trigger lossy compaction one whole tool catalog too early.
    prompt_tokens = max(0, int(prompt_tokens))
    message_tokens = max(0, prompt_tokens - schema_tokens)
    measurement = {
        'measurementVersion': _MEASUREMENT_VERSION,
        'messageTokens': message_tokens,
        'toolSchemaTokens': schema_tokens,
        'totalTokens': prompt_tokens,
        'method': str(method or detail.get('method') or 'unknown'),
        'messageCount': len(messages),
    }
    if isinstance(detail.get('raw_estimate_tokens'), int):
        measurement['_rawEstimateTokens'] = detail['raw_estimate_tokens']
    reusable_counts = detail.get(
        REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY)
    if isinstance(reusable_counts, dict):
        measurement[REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY] = (
            reusable_counts)
    return measurement


def _record(
    task: dict[str, Any],
    *,
    round_num: int,
    stage: str,
    hard_ceiling: int,
    target: int,
    measurement: dict[str, Any],
    action: str,
) -> None:
    # Object identities authorize reuse only during the immediately following
    # body build. They are never task/audit evidence, even though their counts
    # contain no prompt text.
    public_measurement = {
        key: value
        for key, value in measurement.items()
        if not str(key).startswith('_')
    }
    evidence = {
        **(task.get('_contextBudgetDecision') or {}),
        'round': int(round_num) + 1,
        'stage': stage,
        'action': action,
        'hardCeilingTokens': int(hard_ceiling),
        'targetTokens': int(target),
        **public_measurement,
    }
    history = task.setdefault('_promptAdmissionHistory', [])
    if isinstance(history, list):
        history.append(evidence)
        if len(history) > 8:
            del history[:-8]
    task['_lastPromptAdmission'] = evidence
    if action in {'admit', 'prune_then_admit', 'compact_then_admit',
                  'defer_compaction_physical_fit', 'retain_tools_then_admit'}:
        raw = measurement.get('_rawEstimateTokens')
        if isinstance(raw, int) and raw > 0:
            from lib.tasks_pkg.compaction._calibration import update_and_stash_sample
            update_and_stash_sample(
                task, model=str((task.get('config') or {}).get('model') or ''),
                raw_estimate=raw, method=str(measurement.get('method') or ''))
    try:
        audit_log(
            'provider_prompt_admission',
            conv=str(task.get('convId') or '')[:16],
            task=str(task.get('id') or '')[:16],
            **evidence,
        )
    except Exception as exc:
        logger.debug('[PromptAdmission] audit failed: %s', exc)


def enforce_dispatch_prompt_limit(
    messages: list[dict[str, Any]],
    tools: Any,
    task: dict[str, Any],
    *,
    round_num: int,
    model: str,
    precomputed_tool_schema_tokens: Any = None,
) -> dict[str, Any]:
    """Bound the final prompt before request-body construction.

    Returns the final content-free measurement.  A summary model call is an
    internal compaction call whose own input is independently capped; this
    function controls the subsequent main agent call.
    """
    # Required execution state must be present even at round zero/Continue.
    # Optional per-round attachments run earlier and can be skipped or folded.
    restore_checklist_context(messages, task=task, model=model)
    hard_ceiling, target = _resolved_ceiling(task, round_num=round_num)
    before = _measure(
        messages,
        tools,
        task,
        model=model,
        precomputed_tool_schema_tokens=precomputed_tool_schema_tokens,
    )
    if before['totalTokens'] <= target:
        _record(
            task,
            round_num=round_num,
            stage='initial',
            hard_ceiling=hard_ceiling,
            target=target,
            measurement=before,
            action='admit',
        )
        return before

    message_target = target - int(before['toolSchemaTokens'])
    if message_target <= 0:
        _record(
            task,
            round_num=round_num,
            stage='initial',
            hard_ceiling=hard_ceiling,
            target=target,
            measurement=before,
            action='refuse_tool_surface',
        )
        raise PromptTooLongError(
            'Provider prompt refused before dispatch: the selected tool schemas '
            f'need {before["toolSchemaTokens"]:,} tokens, exceeding the '
            f'{hard_ceiling:,}-token prompt ceiling.'
        )

    from lib.tasks_pkg.compaction._pressure import try_pressure_l1
    if try_pressure_l1(
            messages, task, target_tokens=target,
            total_tokens=before['totalTokens'],
            schema_tokens=before['toolSchemaTokens'], mandatory=True,
            round_num=round_num,
            measure=lambda candidate: _measure(
                candidate, tools, task, model=model,
                precomputed_tool_schema_tokens=before['toolSchemaTokens'])['totalTokens']):
        after_l1 = _measure(messages, tools, task, model=model,
                            precomputed_tool_schema_tokens=before['toolSchemaTokens'])
        if after_l1['totalTokens'] <= target:
            _record(task, round_num=round_num, stage='after_l1',
                    hard_ceiling=hard_ceiling, target=target,
                    measurement=after_l1, action='prune_then_admit')
            return after_l1

    from lib.tasks_pkg.compaction._pressure import retain_oversized_tool_batch
    if retain_oversized_tool_batch(
            messages, task, target_tokens=target,
            schema_tokens=before['toolSchemaTokens'],
            measure=lambda candidate: _measure(
                candidate, tools, task, model=model,
                precomputed_tool_schema_tokens=before['toolSchemaTokens'])['totalTokens']):
        after_retention = _measure(
            messages, tools, task, model=model,
            precomputed_tool_schema_tokens=before['toolSchemaTokens'])
        _record(task, round_num=round_num, stage='after_tool_retention',
                hard_ceiling=hard_ceiling, target=target,
                measurement=after_retention, action='retain_tools_then_admit')
        return after_retention

    _record(task, round_num=round_num, stage='before_summary',
            hard_ceiling=hard_ceiling, target=target, measurement=before,
            action='summary_required')
    preserve_budget = max(
        _MIN_SUMMARY_PRESERVE_TOKENS,
        min(_MAX_SUMMARY_PRESERVE_TOKENS, message_target // 4),
    )
    from lib.llm_dispatch.native_context_scope import native_compaction_route_ready
    if not native_compaction_route_ready(task):
        _record(task, round_num=round_num, stage='before_summary',
                hard_ceiling=hard_ceiling, target=target, measurement=before,
                action='refuse_summary_route')
        raise PromptTooLongError('Native prompt requires an existing owned summary route; dispatch refused.')
    compaction_meta: dict[str, Any] = {}
    compacted = force_compact_if_needed(
        messages,
        task=task,
        preserve_budget_tokens=preserve_budget,
        force=True,
        _compaction_trigger='dispatch_guard',
        _compaction_reason=(
            f'final rendered prompt {before["totalTokens"]:,} tokens exceeds '
            f'{target:,}-token admission target'),
        _compaction_round=round_num,
        _allow_deterministic_summary_fallback=True,
        _dispatch_prompt_target=target,
        _dispatch_prompt_measure=lambda candidate: _measure(
            candidate, tools, task, model=model,
            precomputed_tool_schema_tokens=before['toolSchemaTokens'])['totalTokens'],
        _result_meta=compaction_meta,
    )
    if compacted:
        recompose_context_after_compaction(messages, task=task)
        task['_lastCompactionRound'] = int(round_num)

    # Inspect the actual resulting projection, not the compactor's boolean:
    # partial/fallback transformations may remove a carrier even on False.
    restore_checklist_context(messages, task=task, model=model)
    after_summary = _measure(
        messages,
        tools,
        task,
        model=model,
        precomputed_tool_schema_tokens=before['toolSchemaTokens'],
    )
    if after_summary['totalTokens'] <= target:
        _record(
            task,
            round_num=round_num,
            stage='after_summary',
            hard_ceiling=hard_ceiling,
            target=target,
            measurement=after_summary,
            action='compact_then_admit',
        )
        return after_summary

    # A known local summarization/validation failure is not a provider context
    # overflow. Keep the typed distinction all the way to the error envelope so
    # operators investigate compaction code instead of asking users to switch
    # models or delete conversation history. Unknown/irreducible false returns
    # still use PromptTooLongError below.
    from lib.tasks_pkg.compaction._summary_budget import summary_failure_reason
    local_failure = str(
        compaction_meta.get('summaryFailureReason')
        or summary_failure_reason(task)
        or compaction_meta.get('summaryRejectionReason')
        or ''
    ).strip()
    if not compacted and local_failure:
        # A failed summary is not automatically fatal: when the uncompacted
        # prompt still PHYSICALLY fits the model window (prompt + this
        # round's max_tokens), proceed this round and let the per-round
        # pipeline retry compaction on the next one. Endpoint-load transport
        # errors on the summary call are transient — killing the task with
        # tens of thousands of tokens of window headroom left (TB2.1 v10: 6
        # trials died at 128K of a 200K window on a summary transport_error)
        # forfeits otherwise-healthy work for a retryable failure. The fatal
        # path below stays for prompts that genuinely cannot fit.
        from lib.tasks_pkg.compaction._tokens import _get_context_limit
        _context_limit = _get_context_limit(task)
        _round_max_tokens = int(
            (task.get('config') or {}).get('maxTokens') or 0)
        _fits = (
            _context_limit > 0
            and _round_max_tokens > 0
            and after_summary['totalTokens'] + _round_max_tokens
            < _context_limit
        )
        if _fits:
            logger.warning(
                '[Admission] summary failed (%s) but prompt (%d tokens) + '
                'max_tokens (%d) still fits the %d-token window — '
                'proceeding uncompacted; compaction retries next round',
                local_failure, after_summary['totalTokens'],
                _round_max_tokens, _context_limit)
            _record(
                task,
                round_num=round_num,
                stage='after_summary',
                hard_ceiling=hard_ceiling,
                target=target,
                measurement=after_summary,
                action='defer_compaction_physical_fit',
            )
            return after_summary
        _record(
            task,
            round_num=round_num,
            stage='after_summary',
            hard_ceiling=hard_ceiling,
            target=target,
            measurement=after_summary,
            action='refuse_compaction_error',
        )
        raise ContextCompactionError(
            'Automatic context compaction failed locally before provider '
            f'dispatch ({local_failure}); the prompt remains '
            f'{after_summary["totalTokens"]:,} tokens. No main-provider '
            'request was sent.'
        )

    # A generation-level economic guard must not silently destroy durable
    # history when the semantic summary failed.  Deterministic head truncation
    # remains reserved for the separate reactive correctness path after a real
    # provider/window rejection.
    _record(
        task,
        round_num=round_num,
        stage='after_summary',
        hard_ceiling=hard_ceiling,
        target=target,
        measurement=after_summary,
        action='refuse_summary_failed',
    )
    raise ContextCompactionError(
        'Local prompt admission refused before dispatch: bounded semantic compaction '
        'did not reduce required context below the local admission ceiling '
        f'({after_summary["totalTokens"]:,} > {hard_ceiling:,} tokens; '
        f'messages {after_summary["messageTokens"]:,}, tools '
        f'{after_summary["toolSchemaTokens"]:,}). No main-provider request was sent.'
    )


__all__ = ['enforce_dispatch_prompt_limit']
