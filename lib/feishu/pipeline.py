"""lib/feishu/pipeline.py — Unified LLM task pipeline for Feishu.

Uses the SAME task pipeline as the web UI so tool calls, usage/cost,
thinking blocks, and tool summaries appear identically on both channels.
"""

import time
import uuid

from lib.feishu.conversation import (
    append_message,
    get_conv_id,
    get_history,
    get_mode,
    get_model,
    get_project,
    persist_exchange,
    resolve_owner_user_id,
)
from lib.feishu.user_state import MAX_FEISHU_HISTORY_MESSAGE_CHARS

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['exec_project_tool', 'run_task_pipeline']


def exec_project_tool(user_id: str, fn_name: str, fn_args: dict) -> str:
    """Execute a project tool and return the result string."""
    from lib.project_mod.tools import execute_tool
    base_path = get_project(user_id)
    try:
        result = execute_tool(fn_name, fn_args, base_path)
        if isinstance(result, tuple):
            result = result[0] if result else ''
        return str(result) if result else '(empty result)'
    except Exception as e:
        logger.warning(
            '[FeishuBot] project tool %s execution failed: %s',
            fn_name, e, exc_info=True)
        return '❌ 工具执行失败，请稍后重试'


def run_task_pipeline(user_id: str, text: str,
                      send_progress_fn=None, *, source_message_id: str = '') -> str:
    """Run the full LLM task pipeline for a Feishu message.

    This mirrors the web UI's _stream_chat_once flow:
    1. Append user message to history
    2. Build config (model, mode, tools)
    3. Call the task pipeline
    4. Collect response, sync to DB
    5. Return formatted text

    Parameters
    ----------
    send_progress_fn : callable, optional
        Called with a one-line progress string each time a long-running tool
        starts during the task (e.g. ``"Running web_search: …"``), so a Feishu
        consumer can post intermediate progress while the task runs. Wired
        through to ``run_task_sync(progress_fn=...)``.
    """
    if not isinstance(text, str) or not text.strip():
        return '❌ 消息内容为空'
    if len(text) > MAX_FEISHU_HISTORY_MESSAGE_CHARS:
        return (
            '❌ 消息过长，请缩短到 '
            f'{MAX_FEISHU_HISTORY_MESSAGE_CHARS} 个字符以内。'
        )

    owner_user_id = resolve_owner_user_id(user_id)
    if owner_user_id is None:
        logger.error(
            '[FeishuBot] No application owner mapped for %s', user_id[:12]
        )
        return (
            '❌ 该飞书账号尚未绑定应用用户。请配置 '
            'FEISHU_USER_OWNER_MAP 或 FEISHU_DEFAULT_OWNER_USER_ID。'
        )

    # ── Build conversation history ──
    append_message(user_id, 'user', text)
    history = get_history(user_id)

    model = get_model(user_id)
    mode = get_mode(user_id)
    project_path = get_project(user_id)
    conv_id = get_conv_id(user_id)

    # ── Prepare web-format user message ──
    user_web_msg = {
        'id': source_message_id or str(uuid.uuid4()),
        'role': 'user',
        'content': text,
        'timestamp': int(time.time() * 1000),
    }
    # ── Build task config ──
    config = {
        'model': model,
        'conversationId': conv_id,
        'stream': False,
        'messages': [
            {'role': m['role'], 'content': m['content']}
            for m in history
        ],
    }

    # Enable project tools if in tool mode
    if mode == 'tool' and project_path:
        config['project_path'] = project_path
        config['enable_tools'] = True

    # ── Execute pipeline ──
    from lib.tasks_pkg.sync_run import run_task_sync
    result = run_task_sync(
        config,
        user_id=owner_user_id,
        progress_fn=send_progress_fn,
    )

    if not result:
        result = '(无回复)'

    # ── Process result ──
    response_text = result if isinstance(result, str) else str(result)

    # Append assistant response
    append_message(user_id, 'assistant', response_text)

    # Web-format assistant message
    assistant_web_msg = {
        'id': str(uuid.uuid4()),
        'role': 'assistant',
        'content': response_text,
        'model': model,
        'timestamp': int(time.time() * 1000),
    }
    # Persist the same owner identity used by the task itself.
    try:
        persist_exchange(
            user_id,
            user_web_msg,
            assistant_web_msg,
            owner_user_id=owner_user_id,
        )
    except Exception:
        logger.warning(
            '[FeishuBot] Canonical exchange persistence failed for %s',
            user_id[:12],
            exc_info=True,
        )

    return response_text
