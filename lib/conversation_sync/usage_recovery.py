"""User-confirmed forward recovery, never reconciliation of missing usage.

Reads one owned settled Turn/Attempt, checks saved operation evidence and
proposes a finite new execution. The command service/turn.create_pair own
acceptance, source-revision/lane CAS and ordinary executor dispatch. No model
call, accounting mutation, journal takeover, timer or provider replay lives here.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import math

from .generated_contract import OPENAPI_SCHEMAS
from .validation import decode
from lib.llm.responses_state_origin import STATE_ORIGIN_TYPE
from lib.turn_lifecycle import get_attempt, get_turn, LifecycleConflict

# Only user-facing selection/preferences may cross into a fresh execution.
# In particular no resume/checkpoint, wallet, Goal, Flow or delegated authority.
_FORWARD_CONFIG_FIELDS = frozenset({
    'model', 'modelRef', 'routing', 'preset', 'maxTokens', 'temperature', 'topP',
    'thinkingEnabled', 'thinkingDepth', 'reasoningEffort', 'systemPrompt',
    'memoryEnabled', 'projectEnabled', 'projectPath', 'projectPaths',
    'searchMode', 'tools', 'toolMode', 'mcpServers', 'disabledTools',
    'preferredProviderId', 'systemPromptMode', 'systemPromptBlocks',
    'mcpToolExposure', 'fetchEnabled', 'codeExecEnabled', 'schedulerEnabled',
    'readOnlyPaths', 'autoApply', 'project_remote', 'browserEnabled',
    'desktopEnabled', 'imageGenEnabled', 'humanGuidanceEnabled', 'browserClientId',
    'chatMode', 'cache', 'responses', 'compaction', 'autoTranslate',
    'langCorrectionEnabled', 'uiLang',
    'language', 'outputLanguage', 'translationEnabled', 'stream',
})


@dataclass(frozen=True)
class UsageRecoveryPlan:
    source: dict
    preview: dict
    config: dict
    objective: str = ''


def fresh_recovery_model_messages(messages: list[dict]) -> list[dict]:
    """A new execution retains facts, not old opaque reasoning credentials.

    Only Responses reasoning/compaction state is retired from this request-local
    view. Tool calls/results, media, readable output and native execution items
    keep their ordinary owners and bytes; durable source records never change.
    """
    result = []
    for message in messages:
        items = message.get('_responses_items')
        if message.get('role') != 'assistant' or not isinstance(items, list):
            result.append(message)
            continue
        # The state-origin marker describes exactly the state retired here.
        retained = [item for item in items
                    if not isinstance(item, Mapping)
                    or item.get('type') not in {
                        'reasoning', 'compaction', STATE_ORIGIN_TYPE}]
        detached = dict(message)
        if retained:
            detached['_responses_items'] = retained
        else:
            detached.pop('_responses_items', None)
        result.append(detached)
    return result


def _continuation_prompt(objective: str = '') -> str:
    text = (
        '请从本会话已保存的上下文、结果和进度继续完成原来未完成的任务，只处理尚未完成的部分。'
        '这是我确认的新一轮有限预算；此前未确认的用量记录保持不变。'
        '先检查已有结果，不要重新发送中断的模型请求或重复已完成的操作。'
        '原任务和约束仍然适用，但不要重新启动旧的自动运行流程。')
    if objective:
        text += '\n\n原任务（已保存的用户要求，不恢复旧运行实例）：\n' + objective
    return text


def _saved_request_input_tokens(projection: Mapping) -> int:
    """Request-size evidence is a sizing hint, never reconstructed consumption."""
    from lib.cost import observed_usage_totals, split_input_tokens
    from lib.token_counter.evidence import validated_admitted_input_tokens

    largest = 0
    for row in projection.get('apiRounds') or ():
        if not isinstance(row, Mapping) or not isinstance(row.get('usage'), dict):
            continue
        usage = row['usage']
        identity = usage.get('_request_identity')
        if isinstance(identity, Mapping):
            admitted = validated_admitted_input_tokens(identity.get('admitted_input_tokens'))
            largest = max(largest, admitted or 0)
        _, complete = observed_usage_totals(usage)
        if complete:
            largest = max(largest, split_input_tokens(usage)[1])
    return largest


def _output_reserve(config: Mapping) -> int:
    value = config.get('maxTokens')
    return value if type(value) is int and value > 0 else 4096


def _budget(config: Mapping, attempt: Mapping, *, input_tokens: int = 0) -> dict:
    properties = OPENAPI_SCHEMAS['UsageRecoveryBudget']['properties']
    defaults = {key: schema['default'] for key, schema in properties.items()}
    # A finite renewal must fit real input plus output, not a flat ceiling
    # smaller than the very first request. Two request-sized windows retain a
    # bounded continuation opportunity; an explicit smaller source cap wins.
    defaults['maxTokens'] = min(properties['maxTokens']['maximum'],
        max(defaults['maxTokens'], 2 * (input_tokens + _output_reserve(config))))
    limits = config.get('executionLimits') or {}
    if not isinstance(limits, Mapping):
        return defaults
    for source, target in (('max_tokens', 'maxTokens'), ('max_tool_calls', 'maxToolCalls')):
        cap = limits.get(source)
        # Renew only the context-sized finite proposal, never an unlimited
        # source allowance, and never increase a smaller explicit source cap.
        if type(cap) is int and cap >= 0:
            defaults[target] = min(defaults[target], cap)
    deadline = limits.get('deadline')
    created = attempt.get('createdAt')
    if (type(deadline) in (int, float) and math.isfinite(deadline)
            and type(created) in (int, float) and math.isfinite(created)):
        duration = math.ceil(deadline - created / 1000)
        defaults['durationSeconds'] = max(1, min(defaults['durationSeconds'], duration))
    return defaults


def _operations_confirmed(projection: Mapping, conversation_id: str) -> bool:
    """Inspect saved receipts, not tool arguments or model-authored prose.

    Unknown writes are automatically checked only through their existing
    owner-scoped publication reader. Other unknown effects/background work
    remain fenced; returned is not evidence of background completion.
    """
    stack = [projection.get('toolRounds', []), projection.get('programRuns', []),
             projection.get('_programRuns', [])]
    effect_rows, pending_inspections = [], []
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, Mapping):
            continue
        effect = value.get('executionEffect')
        if 'executionEffect' in value:
            from .validation import ContractViolation
            try:
                decode('ToolExecutionEffect', effect)
            except ContractViolation:
                return False
        if isinstance(effect, Mapping):
            effect_rows.append(value)
            if effect.get('recovery') != 'read_only':
                uncertain = (effect.get('state') not in ('returned', 'not_started')
                             or (value.get('status') == 'error' and effect.get('inspection')))
                if uncertain:
                    pending_inspections.append(value)
        occurrence = (value.get('toolName') and any(key in value for key in
                      ('toolCallId', 'toolArgs', 'executionEffect'))
                      or value.get('name') and any(key in value for key in ('call_id', 'id')))
        if occurrence and not isinstance(effect, Mapping):
            # Result-display metadata is not an occurrence. Actual legacy or
            # program child calls still need an explicit captured verdict.
            if value.get('status') not in ('done', 'completed', 'rejected'):
                return False
        if (value.get('_programSynthetic') and value.get('programStatus') not in
                ('completed', 'error', 'failed', 'rejected')):
            return False
        if value.get('exitCode') == 'background':
            return False
        if value.get('toolName') in ('spawn_agents', 'manage_agents', 'task', 'agent'):
            # A launch ACK or a terminal panel snapshot is not a complete child
            # effect inventory. Do not grant forward execution from that alone.
            if not isinstance(effect, Mapping) or effect.get('state') != 'not_started':
                return False
        if value.get('_swarmSnapshot'):
            return False
        # Executed nested program/tool receipts stay in this same bounded
        # projection. Ignore result text and arbitrary JSON in toolContent.
        for key in ('toolRounds', 'tool_calls', 'calls', 'results', 'steps', 'children',
                    'programRuns', '_programRuns', 'toolResults', 'childCalls', 'childResults'):
            child = value.get(key)
            if isinstance(child, (list, Mapping)):
                stack.append(child)
    if pending_inspections:
        from .tool_effects import inspect_effect_rows
        for value in pending_inspections:
            # Reuse the domain reader's unique-occurrence fence across the
            # entire saved inventory, not a singleton that hides duplicates.
            outcome, _ = inspect_effect_rows(effect_rows, conversation_id=conversation_id,
                operation_id=value['executionEffect']['operationId'], expected_scope='attempt')
            if outcome not in ('file_published', 'file_not_published'):
                return False
    return True


def prepare_usage_recovery(conversation_id: str, turn_id: str, user_id: int) -> UsageRecoveryPlan:
    source = get_turn(conversation_id, turn_id, user_id=user_id)
    projection = source.get('projection') or {}
    attempt_id = source.get('currentAttemptId')
    attempt = (get_attempt(attempt_id, user_id=user_id, include_execution_config=True)
               if attempt_id else {})
    raw_config = attempt.get('config')
    config = raw_config if isinstance(raw_config, Mapping) else {}
    forward = {key: deepcopy(value) for key, value in config.items() if key in _FORWARD_CONFIG_FIELDS}
    forward.update(autopilot=False, activeFlow='', planMode=False)
    raw_objective = config.get('_goalObjective')
    objective = raw_objective[:32768] if isinstance(raw_objective, str) else ''
    budget = _budget(config, attempt)
    reason = 'ready'
    if (source.get('actor') != 'assistant' or source.get('status') not in ('failed', 'interrupted')
            or projection.get('usageComplete') is not False
            or attempt.get('status') not in ('completed', 'failed', 'interrupted', 'superseded')):
        reason = 'not_applicable'
    elif (not isinstance(config, Mapping) or not (config.get('model') or config.get('modelRef'))
          or not budget['maxTokens'] or attempt.get('turnId') != turn_id
          or attempt.get('conversationId') != conversation_id):
        reason = 'configuration_unavailable'
    elif not _operations_confirmed(projection, conversation_id):
        reason = 'operation_unconfirmed'
    else:
        from lib.turn_lifecycle import build_api_messages
        from lib.token_counter import count_tokens

        model_ref = forward.get('modelRef')
        model = forward.get('model') or (model_ref.get('model_id')
            if isinstance(model_ref, Mapping) else '') or ''
        # Explicitly local counting: a GET cannot contact a provider or consume
        # budget. Rebuild through the ordinary owner so original task/results
        # survive even when no provider receipt measured the pending request.
        messages = build_api_messages(conversation_id, turn_id,
            {**forward, 'excludeLast': False}, user_id=user_id, allow_model_replay=False)
        if not messages:
            reason = 'configuration_unavailable'
        else:
            counted = count_tokens(
                fresh_recovery_model_messages(messages)
                + [{'role': 'user', 'content': _continuation_prompt(objective)}],
                model=model, mode='tiktoken')['tokens']
            required_input = max(counted, _saved_request_input_tokens(projection))
            budget = _budget(config, attempt, input_tokens=required_input)
            if budget['maxTokens'] < required_input + _output_reserve(config):
                reason = 'budget_insufficient'
    preview = {'ok': True, 'conversationId': conversation_id, 'sourceTurnId': turn_id,
        'sourceProjectionRevision': source['projectionRevision'],
        'state': 'ready' if reason == 'ready' else 'blocked', 'reason': reason,
        # The response still needs a valid bounded proposal when blocked.
        'budget': {**budget, 'maxTokens': max(1, budget['maxTokens'])}}
    decode('ConversationUsageRecovery', preview)
    return UsageRecoveryPlan(source, preview, forward, objective)


def confirmed_recovery(plan: UsageRecoveryPlan, body: Mapping, *, now: float) -> tuple[dict, dict]:
    decode('RecoverUsageRequest', body)
    if body['expectedProjectionRevision'] != plan.source['projectionRevision']:
        raise LifecycleConflict('projection_conflict', 'Saved progress changed. Check recovery again.', plan.source)
    if plan.preview['state'] != 'ready':
        raise LifecycleConflict('usage_recovery_blocked', plan.preview['reason'], plan.source)
    if dict(body['budget']) != plan.preview['budget']:
        raise LifecycleConflict('usage_recovery_budget_changed', 'Check the new budget again.', plan.source)
    config = deepcopy(plan.config)
    budget = body['budget']
    config['executionLimits'] = {'max_tokens': budget['maxTokens'],
        'max_tool_calls': budget['maxToolCalls'], 'deadline': now + budget['durationSeconds']}
    projection = {'content': _continuation_prompt(plan.objective),
        'usageRecovery': {'sourceTurnId': plan.source['turnId'],
            'sourceProjectionRevision': plan.source['projectionRevision'], 'budget': dict(budget)}}
    return config, projection
