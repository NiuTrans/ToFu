"""lib/feishu/commands.py — Slash command handlers and dispatch.

Registry pattern: COMMAND_DISPATCH maps command prefixes to handler functions.
Each handler takes (user_id, text_stripped) and returns a response string.
"""

import os

from lib.feishu._state import WORKSPACE_ROOT
from lib.feishu.conversation import (
    clear_history,
    clear_pending,
    clear_web_messages,
    get_history,
    get_mode,
    get_model,
    get_project,
    new_conv_id,
    set_mode,
    set_model,
    set_project,
)
from lib.feishu.pipeline import exec_project_tool

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['COMMAND_DISPATCH', 'dispatch_command', 'MENU_MAP']


# ── Individual command handlers ────────────────────────────

def _cmd_help(user_id: str, text_stripped: str) -> str:
    return (
        '📖 **命令列表**\n\n'
        '`/help`      — 显示此帮助\n'
        '`/status`    — 当前模式/模型\n'
        '`/history`   — 显示对话历史\n'
        '`/clear`     — 清除对话\n'
        '`/model <名>` — 切换模型\n'
        '`/tool`      — 切换到工具模式\n'
        '`/chat`      — 切换到聊天模式\n'
        '`/project`   — 选择/切换项目\n'
        '`/ls [path]` — 列出文件\n'
        '`/cat <file>` — 查看文件\n'
        '`/grep <pattern> [path]` — 搜索文件\n'
    )


def _cmd_status(user_id: str, text_stripped: str) -> str:
    model = get_model(user_id)
    mode = get_mode(user_id)
    project = get_project(user_id)
    return (
        f'📊 **状态**\n\n'
        f'🤖 模型: `{model}`\n'
        f'🔧 模式: `{mode}`\n'
        f'📁 项目: `{os.path.basename(project)}`\n'
        f'   路径: `{project}`\n'
    )


def _cmd_history(user_id: str, text_stripped: str) -> str:
    history = get_history(user_id)
    if not history:
        return '📭 对话历史为空'
    lines = []
    for i, m in enumerate(history[-10:], 1):  # show last 10
        role_icon = '👤' if m['role'] == 'user' else '🤖'
        content = m['content'][:100]
        if len(m['content']) > 100:
            content += '...'
        lines.append(f'{role_icon} {content}')
    return f'📜 **最近 {len(lines)} 条消息**\n\n' + '\n'.join(lines)


def _cmd_clear(user_id: str, text_stripped: str) -> str:
    clear_history(user_id)
    clear_web_messages(user_id)
    new_conv_id(user_id)
    return '🗑️ 对话已清除，开始新会话'


def _cmd_model(user_id: str, text_stripped: str) -> str:
    parts = text_stripped.split(None, 1)
    if len(parts) < 2:
        current = get_model(user_id)
        return f'🤖 当前模型: `{current}`\n💡 用法: `/model gpt-4o`'
    new_model = parts[1].strip()
    set_model(user_id, new_model)
    return f'✅ 模型已切换: `{new_model}`'


def _cmd_tool(user_id: str, text_stripped: str) -> str:
    set_mode(user_id, 'tool')
    return '🔧 已切换到工具模式 (可执行代码、搜索等)'


def _cmd_chat(user_id: str, text_stripped: str) -> str:
    set_mode(user_id, 'chat')
    return '💬 已切换到聊天模式'


def _cmd_project(user_id: str, text_stripped: str) -> str:
    parts = text_stripped.split(None, 1)
    if len(parts) < 2:
        return _project_picker(user_id)
    return _project_switch(user_id, parts[1].strip())


def _cmd_ls(user_id: str, text_stripped: str) -> str:
    path = text_stripped[3:].strip() or '.'
    result = exec_project_tool(user_id, 'list_dir', {'path': path})
    return f'📂 `{path}`\n```\n{result}\n```'


def _cmd_cat(user_id: str, text_stripped: str) -> str:
    parts = text_stripped[5:].strip().split()
    if not parts:
        return '💡 用法: `/cat file.py [start:end]`'
    file_path = parts[0]
    fn_args = {'path': file_path}
    # Parse optional line range:
    #   /cat file.py 10:20   (colon format)
    #   /cat file.py 10 20   (space format, easier on mobile)
    if len(parts) > 1:
        range_str = parts[1]
        if ':' in range_str:
            try:
                start, end = range_str.split(':')
                fn_args['start_line'] = int(start)
                fn_args['end_line'] = int(end)
            except ValueError:
                logger.debug('[FeishuCmd] Invalid line range %r — ignoring', range_str, exc_info=True)
        else:
            try:
                fn_args['start_line'] = int(parts[1])
                if len(parts) > 2:
                    fn_args['end_line'] = int(parts[2])
            except ValueError:
                logger.debug('[FeishuCmd] Non-numeric line spec in %r — ignoring', parts, exc_info=True)
    result = exec_project_tool(user_id, 'read_files', {'reads': [fn_args]})
    return f'📄 `{file_path}`\n```\n{result}\n```'


def _cmd_grep(user_id: str, text_stripped: str) -> str:
    parts = text_stripped[5:].strip().split(None, 1)
    if not parts:
        return '💡 用法: `/grep pattern [path]`'
    fn_args = {'pattern': parts[0]}
    if len(parts) > 1:
        fn_args['path'] = parts[1]
    result = exec_project_tool(user_id, 'grep_search', fn_args)
    return f'🔍 `{parts[0]}`\n```\n{result}\n```'


# ── Helpers ────────────────────────────────────────────────

def _project_picker(user_id: str, page: int = 0, page_size: int = 20) -> str:
    """List projects under WORKSPACE_ROOT for interactive selection."""
    try:
        all_dirs = sorted([
            d for d in os.listdir(WORKSPACE_ROOT)
            if os.path.isdir(os.path.join(WORKSPACE_ROOT, d))
            and not d.startswith('.')
        ])
    except OSError as e:
        logger.warning(
            'Failed to list workspace directory: %s — %s',
            WORKSPACE_ROOT, e, exc_info=True)
        return f'❌ 无法读取目录: `{WORKSPACE_ROOT}`'

    if not all_dirs:
        return f'📁 `{WORKSPACE_ROOT}` 下没有子目录'

    total = len(all_dirs)
    total_pages = (total + page_size - 1) // page_size
    page = min(page, total_pages - 1)
    start = page * page_size
    page_dirs = all_dirs[start:start + page_size]

    lines = [f'📁 **项目列表** (第{page + 1}/{total_pages}页, 共{total}个)\n']
    for i, d in enumerate(page_dirs, start + 1):
        lines.append(f'  {i}. `{d}`')

    lines.append('\n💡 回复 `/project <名称>` 切换项目')
    if total_pages > 1:
        lines.append('💡 回复数字选择，或 `/project` 下一页')

    return '\n'.join(lines)


def _project_switch(user_id: str, new_path: str) -> str:
    """Switch the user's active project."""
    # Try as-is first, then as subdirectory of WORKSPACE_ROOT
    if not os.path.isdir(new_path):
        candidate = os.path.join(WORKSPACE_ROOT, new_path)
        if os.path.isdir(candidate):
            new_path = candidate
    if os.path.isdir(new_path):
        set_project(user_id, new_path)
        clear_pending(user_id)
        name = os.path.basename(new_path)
        return f'✅ 项目已切换: `{name}`\n📁 `{new_path}`'
    else:
        return f'❌ 目录不存在: `{new_path}`\n💡 试试短名: `/project vllm`'


# ══════════════════════════════════════════════════════════
#  Command Dispatch Table (Registry Pattern)
# ══════════════════════════════════════════════════════════

COMMAND_DISPATCH = {
    '/help':    _cmd_help,
    '/status':  _cmd_status,
    '/history': _cmd_history,
    '/clear':   _cmd_clear,
    '/model':   _cmd_model,
    '/tool':    _cmd_tool,
    '/chat':    _cmd_chat,
    '/project': _cmd_project,
    '/ls':      _cmd_ls,
    '/cat':     _cmd_cat,
    '/grep':    _cmd_grep,
}


def dispatch_command(user_id: str, text: str) -> str | None:
    """Try to match text against a registered command.

    Returns the command's response string, or None if not a command.
    """
    text_stripped = text.strip()
    for prefix, handler in COMMAND_DISPATCH.items():
        if text_stripped == prefix or text_stripped.startswith(prefix + ' '):
            try:
                return handler(user_id, text_stripped)
            except Exception as e:
                logger.error(
                    '[FeishuBot] Command %s failed: %s',
                    prefix, e, exc_info=True,
                )
                return f'❌ 命令执行失败: {e}'
    return None


# ── Menu-to-command mapping (for Feishu bot menu clicks) ──

MENU_MAP = {
    'help':    '/help',
    'status':  '/status',
    'model':   '/model',
    'tool':    '/tool',
    'chat':    '/chat',
    'clear':   '/clear',
    'ls':      '/ls .',
    'history': '/history',
    'project': '/project',
}
