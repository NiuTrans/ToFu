"""Model-agnostic, request-local tool-orchestration policy.

Responsibility
--------------
Classify the current task shape and independently select two composable lanes:

* a programmatic data plane for bounded, deterministic read reductions; and
* a multi-agent control plane for independent workstreams.

This module does not know which provider will execute either lane.  Provider
adapters resolve native acceleration versus the local ToolScript/Swarm
fallback at the final wire boundary.  Execution authority, approval, and
budgets remain owned by the ordinary tool pipeline.

Entry point: :func:`resolve_tool_orchestration`.
Dependencies: the declarative tool registry through
``lib.tools.programmatic``; no provider or transport modules.
"""

from __future__ import annotations

import re
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)

TOOL_ORCHESTRATION_POLICY_VERSION = 'tool-orchestration/v1'
TOOL_ORCHESTRATION_POLICY_V2 = 'tool-orchestration/v2'


_MANY = re.compile(
    r'\b(multiple|several|all|across|batch|each|compare|rank|filter|join|'
    r'deduplicat\w*|aggregate|validate|survey|research)\b|'
    r'(多个|若干|所有|全部|逐个|批量|分别|比较|对比|排名|筛选|汇总|聚合|去重|验证|调研)',
    re.I,
)
_PTC_SHAPE = re.compile(
    r'\b(filter|join|rank|deduplicat\w*|aggregate|validate|compare|cross[- ]?check|'
    r'survey|research|search|inspect|analy[sz]e)\b|'
    r'(筛选|合并|关联|排名|去重|聚合|汇总|验证|交叉核对|比较|对比|调研|搜索|检查|分析)',
    re.I,
)
_INDEPENDENT = re.compile(
    r'\b(parallel|independent|separate(?:ly)?|workstreams?|in parallel|'
    r'compare|across (?:the )?(?:modules|files|services|sources))\b|'
    r'(并行|独立|分别|同时|多个方向|多个模块|多份文件|多种方案|全面)',
    re.I,
)
_COMPLEX = re.compile(
    r'\b(complex|comprehensive|end[- ]to[- ]end|architecture|migration|audit|'
    r'review|investigate|research|compare|implement)\b|'
    r'(复杂|全面|端到端|架构|迁移|审计|评审|排查|调研|比较|实现)',
    re.I,
)
_VERIFIED_LOOP = re.compile(
    r'\b(implement|fix|change|edit|migrate|refactor)\b.*\b(test|verify|check)|'
    r'\b(test|verify|check)\b.*\b(implementation|fix|change|edit)\b|'
    r'(实现|修复|修改|迁移|重构).*(测试|验证|检查)|'
    r'(测试|验证|检查).*(实现|修复|修改)', re.I | re.S)


def multi_agent_task_shape(text: str) -> bool:
    """Whether a request explicitly contains independent complex workstreams."""
    value = str(text or "")
    return bool(_INDEPENDENT.search(value) and _COMPLEX.search(value))


def latest_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ''
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return ' '.join(
                str(block.get('text') or '')
                for block in content if isinstance(block, dict)).strip()
    return ''


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ''
    function = tool.get('function')
    if isinstance(function, dict):
        return str(function.get('name') or '')
    return str(tool.get('name') or '')


def _eligible_programmatic_names(tools: Any) -> set[str]:
    try:
        from lib.tools.programmatic import eligible_programmatic_tool_names
        eligible = eligible_programmatic_tool_names()
    except Exception as exc:
        logger.debug(
            '[ToolOrchestration] programmatic registry unavailable: %s', exc)
        return set()
    return {
        name for name in (_tool_name(tool) for tool in (tools or ()))
        if name and name in eligible
    }


def _programmatic_stage(text: str) -> str:
    if re.search(
            r'\b(repo|code|files?|tests?|modules?)\b|代码|仓库|文件|测试|模块',
            text, re.I):
        return (
            'within each active workstream, read the relevant project '
            'artifacts, reduce repeated observations, and return '
            'evidence-backed findings')
    if re.search(
            r'\b(search|research|sources?|urls?|web)\b|搜索|调研|来源|网页',
            text, re.I):
        return (
            'within each active workstream, collect, filter, deduplicate, '
            'and compare the requested sources while retaining source evidence')
    return (
        'within one workstream, process the bounded set of read-only results, '
        'reduce duplicates, and return the requested comparison or validation '
        'evidence')


def _observed_read_fanout(messages: Any, eligible: set[str]) -> bool:
    """Return whether recent rounds already show eligible read-only fan-out."""
    if not eligible:
        return False
    single = 0
    total = 0
    messages_with_calls = 0
    for message in list(messages or ())[-12:]:
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        count = sum(1 for tc in (message.get('tool_calls') or ())
                    if _tool_name(tc) in eligible)
        if not count:
            continue
        messages_with_calls += 1
        total += count
        single = max(single, count)
    return single >= 2 or (messages_with_calls >= 2 and total >= 3)


def _observed_serial_chain(messages: Any, eligible: set[str]) -> list[str]:
    """Return a trailing run of one eligible read call per model round."""
    if not eligible:
        return []
    chain: list[str] = []
    for message in reversed(list(messages or ())[-24:]):
        if not isinstance(message, dict):
            continue
        role = message.get('role')
        if role == 'tool':
            continue
        if role != 'assistant':
            break
        calls = [tc for tc in (message.get('tool_calls') or ())
                 if isinstance(tc, dict)]
        if len(calls) != 1:
            break
        name = _tool_name(calls[0])
        if name not in eligible:
            break
        chain.append(name)
    chain.reverse()
    return chain[-6:] if len(chain) >= 3 else []


def _multi_agent_stage(text: str) -> str:
    if re.search(
            r'\b(repo|code|files?|tests?|modules?)\b|代码|仓库|文件|测试|模块',
            text, re.I):
        return (
            'independently inspect separable project areas or verification '
            'questions, then synthesize without mutating shared state')
    return (
        'delegate only the independent research, comparison, or verification '
        'workstreams in this request')


def _composition_mode(programmatic: str, multi_agent: str) -> str:
    from lib.tools.programmatic import ACTIVE_PROGRAMMATIC_MODES
    has_programmatic = programmatic in ACTIVE_PROGRAMMATIC_MODES
    has_multi_agent = multi_agent == 'read_only'
    if has_programmatic and has_multi_agent:
        return 'multi_agent_with_programmatic_workers'
    if has_multi_agent:
        return 'multi_agent'
    if has_programmatic:
        return 'programmatic'
    return 'direct'


def resolve_tool_orchestration(
        *, requested_programmatic: str, requested_multi_agent: str,
        messages: Any, tools: Any, round_num: int,
        model: str = '', policy_version: str = 'v1') -> dict[str, Any]:
    """Resolve independent, composable lanes for one model round.

    The result describes task intent, not provider capability.  In particular,
    selecting both lanes is valid: Multi-agent partitions independent work,
    while Programmatic Tool Calling reduces deterministic reads within a root
    or worker workstream.  The final wire boundary chooses native acceleration
    or a local fallback for each lane independently.
    """
    text = latest_user_text(messages)
    eligible = _eligible_programmatic_names(tools)
    use_v2 = str(policy_version or 'v1').lower() == 'v2'

    programmatic = 'off'
    programmatic_reason = 'disabled'
    programmatic_tier = ''
    if requested_programmatic == 'on':
        if eligible:
            programmatic_reason = 'resident_eligible_read_tools'
        else:
            programmatic_reason = 'no_eligible_read_tools'
    elif requested_programmatic == 'auto':
        if not eligible:
            programmatic_reason = 'no_eligible_read_tools'
        elif _MANY.search(text) and _PTC_SHAPE.search(text):
            programmatic_reason = 'bounded_read_only_reduction'
        elif _observed_read_fanout(messages, eligible):
            programmatic_reason = 'observed_read_fanout'
        else:
            programmatic_reason = 'task_not_bounded_reduction_shape'
    if programmatic_reason in (
            'resident_eligible_read_tools',
            'bounded_read_only_reduction',
            'observed_read_fanout'):
        programmatic = requested_programmatic
        from lib.tools.programmatic import programmatic_tier
        programmatic_tier = programmatic_tier(model)

    multi_agent = 'off'
    multi_agent_reason = 'disabled'
    if requested_multi_agent == 'read_only':
        if use_v2 and round_num != 1:
            multi_agent_reason = 'first_round_only'
        elif use_v2 and not multi_agent_task_shape(text):
            multi_agent_reason = 'task_not_independently_decomposable'
        else:
            multi_agent = 'read_only'
            multi_agent_reason = 'explicit_read_only'
    elif requested_multi_agent == 'auto':
        if round_num != 1:
            multi_agent_reason = 'first_round_only'
        elif not multi_agent_task_shape(text):
            multi_agent_reason = 'task_not_independently_decomposable'
        else:
            multi_agent = 'read_only'
            multi_agent_reason = 'independent_complex_workstreams'

    if use_v2 and multi_agent == 'read_only':
        # V2 exposes exactly one orchestration shape. Worker-local reductions
        # remain an implementation detail, not a second router decision.
        programmatic = 'off'
        programmatic_reason = 'exclusive_multi_agent_shape'
        programmatic_tier = ''

    from lib.tools.programmatic import ACTIVE_PROGRAMMATIC_MODES
    serial_chain = (
        _observed_serial_chain(messages, eligible)
        if programmatic in ACTIVE_PROGRAMMATIC_MODES else [])

    composition = _composition_mode(programmatic, multi_agent)
    shape = composition
    expected_savings: dict[str, Any] = {}
    if use_v2:
        if multi_agent == 'read_only':
            shape = 'independent_read_only_agents'
            expected_savings = {'criticalPathFraction': 0.6,
                                'basis': 'independent_workstreams'}
        elif programmatic in ACTIVE_PROGRAMMATIC_MODES:
            shape = 'ptc_bounded_reduction'
            expected_savings = {'modelRounds': max(1, len(eligible) - 1),
                                'basis': 'eligible_read_fanout'}
        elif _VERIFIED_LOOP.search(text):
            shape = 'verified_loop'
            expected_savings = {'qualityGuard': 'verification_required',
                                'basis': 'mutation_plus_verification'}
        else:
            shape = 'direct_execution'
            expected_savings = {'modelRounds': 0, 'basis': 'simple_task'}
        composition = shape

    decision = {
        'policyVersion': (TOOL_ORCHESTRATION_POLICY_V2 if use_v2
                          else TOOL_ORCHESTRATION_POLICY_VERSION),
        'compositionMode': composition,
        'programmaticCalling': programmatic,
        'programmaticReason': programmatic_reason,
        'programmaticTier': programmatic_tier,
        'programmaticSerialChain': serial_chain,
        'programmaticStage': (
            _programmatic_stage(text)
            if programmatic in ACTIVE_PROGRAMMATIC_MODES else ''),
        'programmaticEligibleTools': sorted(eligible),
        'multiAgent': multi_agent,
        'multiAgentReason': multi_agent_reason,
        'multiAgentStage': (
            _multi_agent_stage(text) if multi_agent == 'read_only' else ''),
        'round': int(round_num),
    }
    if use_v2:
        decision.update({
            'shape': shape,
            'expectedSavings': expected_savings,
            'projectionEvidence': [],
            'adoptionEvidence': [],
        })
    return decision


__all__ = [
    'TOOL_ORCHESTRATION_POLICY_VERSION',
    'TOOL_ORCHESTRATION_POLICY_V2',
    'latest_user_text',
    'multi_agent_task_shape',
    'resolve_tool_orchestration',
]
