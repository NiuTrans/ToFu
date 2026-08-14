"""Request-local GPT-5.6 optimization routing.

PTC and native multi-agent are useful for particular task shapes, not blanket
quality switches.  This module turns the user-facing ``auto`` modes into a
bounded per-round decision while leaving execution authority and approval gates
unchanged.  It is pure and deterministic so benchmark arms can reproduce it.
"""

from __future__ import annotations

import re
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


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
        logger.debug('[GPT56Optimization] programmatic registry unavailable: %s', exc)
        return set()
    return {
        name for name in (_tool_name(tool) for tool in (tools or ()))
        if name and name in eligible
    }


def _ptc_stage(text: str) -> str:
    if re.search(r'\b(repo|code|files?|tests?|modules?)\b|代码|仓库|文件|测试|模块', text, re.I):
        return 'read the relevant project artifacts, reduce repeated observations, and return evidence-backed findings'
    if re.search(r'\b(search|research|sources?|urls?|web)\b|搜索|调研|来源|网页', text, re.I):
        return 'collect, filter, deduplicate, and compare the requested sources while retaining source evidence'
    return 'process the bounded set of read-only results, reduce duplicates, and return the requested comparison or validation evidence'


def _multi_agent_stage(text: str) -> str:
    if re.search(r'\b(repo|code|files?|tests?|modules?)\b|代码|仓库|文件|测试|模块', text, re.I):
        return 'independently inspect separable project areas or verification questions, then synthesize without mutating state'
    return 'delegate only the independent research, comparison, or verification workstreams in this request'


def resolve_gpt56_optimizations(
        *, requested_programmatic: str, requested_multi_agent: str,
        messages: Any, tools: Any, round_num: int) -> dict[str, Any]:
    """Resolve auto modes into explicit, bounded wire decisions."""
    text = latest_user_text(messages)
    eligible = _eligible_programmatic_names(tools)

    programmatic = 'off'
    ptc_reason = 'disabled'
    if requested_programmatic == 'auto':
        if not eligible:
            ptc_reason = 'no_eligible_read_tools'
        elif not (_MANY.search(text) and _PTC_SHAPE.search(text)):
            ptc_reason = 'task_not_bounded_reduction_shape'
        else:
            programmatic = 'auto'
            ptc_reason = 'bounded_read_only_reduction'

    multi_agent = 'off'
    ma_reason = 'disabled'
    if requested_multi_agent == 'read_only':
        multi_agent = 'read_only'
        ma_reason = 'explicit_read_only'
    elif requested_multi_agent == 'auto':
        if round_num != 1:
            ma_reason = 'first_round_only'
        elif not (_INDEPENDENT.search(text) and _COMPLEX.search(text)):
            ma_reason = 'task_not_independently_decomposable'
        else:
            multi_agent = 'read_only'
            ma_reason = 'independent_complex_workstreams'

    # Keep experimental orchestration modes isolated. A bounded deterministic
    # reduction is cheaper and more auditable through PTC; an explicit native
    # Multi-agent request wins over automatic PTC. This avoids sending an
    # unbenchmarked PTC + beta Multi-agent combination upstream.
    if programmatic == 'auto' and multi_agent == 'read_only':
        if requested_multi_agent == 'read_only':
            programmatic = 'off'
            ptc_reason = 'explicit_multi_agent_selected'
        else:
            multi_agent = 'off'
            ma_reason = 'bounded_reduction_prefers_ptc'

    return {
        'programmaticCalling': programmatic,
        'programmaticReason': ptc_reason,
        'programmaticStage': _ptc_stage(text) if programmatic == 'auto' else '',
        'programmaticEligibleTools': sorted(eligible),
        'multiAgent': multi_agent,
        'multiAgentReason': ma_reason,
        'multiAgentStage': _multi_agent_stage(text) if multi_agent == 'read_only' else '',
        'round': int(round_num),
    }


__all__ = ['latest_user_text', 'resolve_gpt56_optimizations']
