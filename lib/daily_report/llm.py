"""LLM analysis layer.

- ``_run_llm_analysis`` — call ``smart_chat`` with the daily-report system
  prompt and return parsed (streams, tomorrow, yesterday_done, error).
- ``_extract_json_result`` — robust JSON extraction (handles markdown fences,
  legacy list-only output, embedded objects).
- ``_pick_persona`` — fun emoji/title based on usage stats.
"""

import json

from lib.llm_json import extract_json
from lib.log import get_logger

from .prompts import _ANALYSIS_SYSTEM

logger = get_logger(__name__)


def _extract_json_result(text):
    """Robustly extract JSON from LLM output.

    Handles both new format ``{streams: [...], tomorrow: [...]}``
    and legacy format ``[...]``. Fence stripping, first-balanced-block
    extraction, and truncated-output repair are delegated to the shared
    ``lib.llm_json.extract_json`` helper; this function only unpacks the
    parsed shape into the tuple the caller expects.

    Returns (streams_list, tomorrow_list, yesterday_done_list) tuple.
    """
    if not text:
        return [], [], []

    result = extract_json(text, repair=True)

    if isinstance(result, dict):
        streams = result.get('streams', [])
        if isinstance(streams, list):
            tomorrow = result.get('tomorrow', [])
            yd = result.get('yesterday_done', [])
            return (streams,
                    tomorrow if isinstance(tomorrow, list) else [],
                    yd if isinstance(yd, list) else [])
    elif isinstance(result, list):
        return result, [], []

    return [], [], []


def _run_llm_analysis(user_prompt, conv_count):
    """Call the LLM via smart_chat.

    Returns ``(streams_list, tomorrow_list, yesterday_done_list, error_msg|None)``.
    """
    try:
        from lib.llm_dispatch import smart_chat

        messages = [
            {'role': 'system', 'content': _ANALYSIS_SYSTEM},
            {'role': 'user', 'content': user_prompt},
        ]

        content, usage = smart_chat(
            messages,
            max_tokens=128000,
            temperature=0.3,
            capability='text',
            log_prefix='[DailyReport]',
            max_retries=3,
            timeout=90,
        )

        dispatch_info = usage.get('_dispatch', {}) if isinstance(usage, dict) else {}
        logger.info('[DailyReport] LLM via %s:%s in %dms',
                    dispatch_info.get('key', '?'),
                    dispatch_info.get('model', '?'),
                    dispatch_info.get('latency_ms', 0))

        if not content:
            logger.warning('[DailyReport] LLM returned empty content')
            return [], [], [], 'LLM returned empty response'

        streams, tomorrow, yesterday_done = _extract_json_result(content)
        if not streams:
            logger.warning('[DailyReport] JSON extraction failed: %.500s', content)
            return [], [], [], 'Failed to parse LLM JSON output'

        logger.info('[DailyReport] Parsed %d streams, %d tomorrow items, '
                    '%d yesterday_done',
                    len(streams), len(tomorrow), len(yesterday_done))
        return streams, tomorrow, yesterday_done, None

    except json.JSONDecodeError as e:
        logger.warning('[DailyReport] JSON parse error: %s', e)
        return [], [], [], f'JSON parse error: {e}'

    except Exception as e:
        logger.error('[DailyReport] LLM analysis failed: %s', e, exc_info=True)
        return [], [], [], f'LLM call failed: {e}'


def _pick_persona(stats):
    """Pick a fun persona based on usage patterns."""
    tc = stats.get('totalConversations', 0)
    sc = stats.get('searchCount', 0)
    pc = stats.get('projectCount', 0)
    cr = stats.get('codeRelated', False)
    tt = stats.get('toolTypesUsed', [])
    tm = stats.get('totalMessages', 0)
    ah = stats.get('activeHours', 0)

    if tc == 0:
        return {'emoji': '😴', 'name': '休眠树懒', 'desc': '今天还没开始呢'}
    if cr and pc >= 3:
        return {'emoji': '🐙', 'name': '八爪鱼程序员', 'desc': '多线程编码，触手可及每一个 bug'}
    if sc >= 10:
        return {'emoji': '🦅', 'name': '信息猎鹰', 'desc': '锐利的双眼扫过互联网的每一个角落'}
    if len(tt) >= 5:
        return {'emoji': '🦊', 'name': '瑞士军刀狐', 'desc': '十八般武艺样样精通'}
    if ah >= 8:
        return {'emoji': '🐺', 'name': '耐力狼', 'desc': '从早到晚持续战斗'}
    if tm >= 50:
        return {'emoji': '🐬', 'name': '社交海豚', 'desc': '与 AI 交流如鱼得水'}
    if cr:
        return {'emoji': '🐍', 'name': '代码蟒蛇', 'desc': '优雅地缠绕每一个逻辑链条'}
    if tc >= 5:
        return {'emoji': '🦋', 'name': '话题蝴蝶', 'desc': '在不同领域之间优雅穿梭'}
    if tc <= 2 and tm <= 10:
        return {'emoji': '🐱', 'name': '慵懒猫咪', 'desc': '高效摸鱼，点到为止'}
    return {'emoji': '🦉', 'name': '智慧猫头鹰', 'desc': '深思熟虑，每一次提问都恰到好处'}
