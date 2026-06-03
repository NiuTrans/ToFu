"""LLM analysis layer.

- ``_run_llm_analysis`` — call ``smart_chat`` with the daily-report system
  prompt and return parsed (streams, tomorrow, yesterday_done, error).
- ``_extract_json_result`` — robust JSON extraction (handles markdown fences,
  legacy list-only output, embedded objects).
- ``_pick_persona`` — fun emoji/title based on usage stats.
"""

import json
import re

from lib.log import get_logger

from .prompts import _ANALYSIS_SYSTEM

logger = get_logger(__name__)


def _extract_json_result(text):
    """Robustly extract JSON from LLM output.

    Handles both new format ``{streams: [...], tomorrow: [...]}``
    and legacy format ``[...]``.

    Returns (streams_list, tomorrow_list, yesterday_done_list) tuple.
    """
    if not text:
        return [], [], []

    s = text.strip()

    # Strip markdown fences
    if s.startswith('```'):
        s = re.sub(r'^```\w*\n?', '', s)
        s = re.sub(r'\n?```\s*$', '', s)
        s = s.strip()

    def _unpack(result):
        """Unpack parsed JSON into (streams, tomorrow, yesterday_done)."""
        if isinstance(result, dict):
            streams = result.get('streams', [])
            tomorrow = result.get('tomorrow', [])
            yd = result.get('yesterday_done', [])
            if isinstance(streams, list):
                return (streams,
                        tomorrow if isinstance(tomorrow, list) else [],
                        yd if isinstance(yd, list) else [])
        if isinstance(result, list):
            return result, [], []
        return None

    # Direct parse
    try:
        result = json.loads(s)
        unpacked = _unpack(result)
        if unpacked:
            return unpacked
    except json.JSONDecodeError as e:
        logger.debug('Direct JSON parse failed, trying extraction: %s', e)

    # Find outermost { or [
    for opener, closer in [('{', '}'), ('[', ']')]:
        start = s.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(s)):
            ch = s[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        result = json.loads(s[start:i + 1])
                        unpacked = _unpack(result)
                        if unpacked:
                            return unpacked
                    except json.JSONDecodeError as e:
                        logger.debug('Extracted JSON parse failed: %s', e)
                    break

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
            max_tokens=min(4096, 400 * conv_count),
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
