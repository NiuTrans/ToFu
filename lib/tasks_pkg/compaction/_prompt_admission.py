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
from lib.tasks_pkg.compaction._tokens import (
    _compaction_trigger_threshold,
    _count_tokens_authoritative,
)
from lib.token_counter.base import (
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
)

logger = get_logger(__name__)

_FIRST_DISPATCH_MAX_CEILING_TOKENS = 256_000
_FIRST_DISPATCH_TARGET_RATIO = 120_000 / 128_000
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

    The configured working set remains the normal repeated-round authority.
    Round zero additionally caps any explicit/derived working set at 256K
    because it rebuilds durable history and has no generation-local provider
    measurement yet. Its 93.75% target preserves the former 120K target for a
    128K working set while allowing a provider-declared cheaper tier to select
    a larger, still-bounded first window.
    """
    effective, window_threshold, working_set = _compaction_trigger_threshold(task)
    configured = effective if working_set > 0 else window_threshold
    hard_ceiling = max(1, min(window_threshold, configured))
    proactive_target = hard_ceiling
    if round_num == 0:
        hard_ceiling = min(
            hard_ceiling, _FIRST_DISPATCH_MAX_CEILING_TOKENS)
        proactive_target = max(
            1, int(hard_ceiling * _FIRST_DISPATCH_TARGET_RATIO))
    return hard_ceiling, proactive_target


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

    preserve_budget = max(
        _MIN_SUMMARY_PRESERVE_TOKENS,
        min(_MAX_SUMMARY_PRESERVE_TOKENS, message_target // 4),
    )
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
        _result_meta=compaction_meta,
    )
    if compacted:
        recompose_context_after_compaction(messages, task=task)
        task['_lastCompactionRound'] = int(round_num)

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
    local_failure = str(
        compaction_meta.get('summaryFailureReason')
        or compaction_meta.get('summaryRejectionReason')
        or ''
    ).strip()
    if not compacted and local_failure:
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
    raise PromptTooLongError(
        'Provider prompt refused before dispatch: bounded semantic compaction '
        'did not reduce required context below the local admission ceiling '
        f'({after_summary["totalTokens"]:,} > {hard_ceiling:,} tokens; '
        f'messages {after_summary["messageTokens"]:,}, tools '
        f'{after_summary["toolSchemaTokens"]:,}).'
    )


__all__ = ['enforce_dispatch_prompt_limit']
