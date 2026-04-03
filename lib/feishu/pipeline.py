"""lib/feishu/pipeline.py — Unified LLM task pipeline for Feishu.

Uses the SAME task pipeline as the web UI so tool calls, usage/cost,
thinking blocks, and tool summaries appear identically on both channels.
"""

import logging
import time
import uuid

from lib.feishu.conversation import (
    append_message,
    append_web_message,
    clear_web_messages,
    get_conv_id,
    get_history,
    get_mode,
    get_model,
    get_project,
    new_conv_id,
    sync_to_db,
)

logger = logging.getLogger(__name__)

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
        return f'❌ Tool error: {e}'


def run_task_pipeline(user_id: str, text: str,
                      send_progress_fn=None) -> str:
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
        Called with intermediate text during long operations (e.g., tool use).
        Currently not wired up — reserved for streaming progress in Feishu.
    """
    if send_progress_fn:
        logger.debug('[Feishu] send_progress_fn provided but not yet wired up')

    # ── Build conversation history ──
    append_message(user_id, 'user', text)
    history = get_history(user_id)

    model = get_model(user_id)
    mode = get_mode(user_id)
    project_path = get_project(user_id)
    conv_id = get_conv_id(user_id)

    # ── Prepare web-format user message ──
    user_web_msg = {
        'id': str(uuid.uuid4()),
        'role': 'user',
        'content': text,
        'timestamp': int(time.time() * 1000),
    }
    append_web_message(user_id, user_web_msg)

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
    from lib.tasks_pkg.endpoint import run_task_sync
    result = run_task_sync(config)

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
    append_web_message(user_id, assistant_web_msg)

    # Sync to DB (fire-and-forget, errors are logged)
    sync_to_db(user_id)

    return response_text

