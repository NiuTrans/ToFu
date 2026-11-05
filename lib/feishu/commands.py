"""lib/feishu/commands.py — Slash command handlers and dispatch.

Registry pattern: COMMAND_DISPATCH maps command prefixes to handler functions.
Each handler takes (sender, text_stripped) and returns a response string.
Commands are the owner's alone: :func:`dispatch_command` refuses every other
sender (``lib.feishu.authority``), and ``/project`` selects only one of the
owner's registered projects.
"""

import os

from lib.feishu.authority import (
    authorized_project,
    matching_registered_projects,
    registered_projects,
)
from lib.feishu.conversation import (
    clear_history,
    clear_pending,
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

def _cmd_help(sender, text_stripped: str) -> str:
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


def _cmd_status(sender, text_stripped: str) -> str:
    model = get_model(sender.open_id)
    mode = get_mode(sender.open_id)
    project = get_project(sender.open_id)
    return (
        f'📊 **状态**\n\n'
        f'🤖 模型: `{model}`\n'
        f'🔧 模式: `{mode}`\n'
        f'📁 项目: `{os.path.basename(project)}`\n'
        f'   路径: `{project}`\n'
    )


def _cmd_history(sender, text_stripped: str) -> str:
    history = get_history(sender.open_id)
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


def _cmd_clear(sender, text_stripped: str) -> str:
    clear_history(sender.open_id)
    new_conv_id(sender.open_id)
    return '🗑️ 对话已清除，开始新会话'


def _cmd_model(sender, text_stripped: str) -> str:
    parts = text_stripped.split(None, 1)
    if len(parts) < 2:
        current = get_model(sender.open_id)
        return f'🤖 当前模型: `{current}`\n💡 用法: `/model gpt-4o`'
    new_model = parts[1].strip()
    set_model(sender.open_id, new_model)
    return f'✅ 模型已切换: `{new_model}`'


def _cmd_tool(sender, text_stripped: str) -> str:
    set_mode(sender.open_id, 'tool')
    if authorized_project(sender) is None:
        return '🔧 已切换到工具模式；尚未选择已登记的项目，用 `/project` 选择后才能使用项目工具'
    return '🔧 已切换到工具模式 (可执行代码、搜索等)'


def _cmd_chat(sender, text_stripped: str) -> str:
    set_mode(sender.open_id, 'chat')
    return '💬 已切换到聊天模式'


def _cmd_project(sender, text_stripped: str) -> str:
    parts = text_stripped.split(None, 1)
    if len(parts) < 2:
        return _project_picker(sender)
    return _project_switch(sender, parts[1].strip())


def _cmd_ls(sender, text_stripped: str) -> str:
    path = text_stripped[3:].strip() or '.'
    result = exec_project_tool(sender, 'list_dir', {'path': path})
    return f'📂 `{path}`\n```\n{result}\n```'


def _cmd_cat(sender, text_stripped: str) -> str:
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
    result = exec_project_tool(sender, 'read_files', {'reads': [fn_args]})
    return f'📄 `{file_path}`\n```\n{result}\n```'


def _cmd_grep(sender, text_stripped: str) -> str:
    parts = text_stripped[5:].strip().split(None, 1)
    if not parts:
        return '💡 用法: `/grep pattern [path]`'
    fn_args = {'pattern': parts[0]}
    if len(parts) > 1:
        fn_args['path'] = parts[1]
    result = exec_project_tool(sender, 'search_files', fn_args)
    return f'🔍 `{parts[0]}`\n```\n{result}\n```'


# ── Helpers ────────────────────────────────────────────────

def _project_picker(sender) -> str:
    """List the owner's registered projects for selection."""
    projects = registered_projects(sender.owner_user_id)
    if not projects:
        return '📁 还没有已登记的项目：先在 Tofu 中打开项目，再用 `/project <名称>` 选择'
    lines = [f'📁 **已登记的项目** (共{len(projects)}个)\n']
    for i, path in enumerate(projects, 1):
        lines.append(f'  {i}. `{os.path.basename(os.path.normpath(path))}` — `{path}`')
    lines.append('\n💡 回复 `/project <名称或路径>` 切换项目')
    return '\n'.join(lines)


def _project_switch(sender, requested: str) -> str:
    """Select one of the owner's registered projects by exact path or name."""
    matches = [path for path in matching_registered_projects(sender.owner_user_id, requested)
               if os.path.isdir(path)]
    if len(matches) > 1:
        return ('❌ 多个已登记的项目同名，请使用完整路径:\n'
                + '\n'.join(f'  `{path}`' for path in matches))
    if not matches:
        return f'❌ 不是已登记的项目: `{requested}`\n💡 用 `/project` 查看可选项目'
    set_project(sender.open_id, matches[0])
    clear_pending(sender.open_id)
    name = os.path.basename(os.path.normpath(matches[0]))
    return f'✅ 项目已切换: `{name}`\n📁 `{matches[0]}`'


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


def dispatch_command(sender, text: str) -> str | None:
    """Try to match text against a registered command.

    Returns the command's response string, or None if not a command. A sender
    that is not the owner gets a refusal, never the command.
    """
    text_stripped = text.strip()
    for prefix, handler in COMMAND_DISPATCH.items():
        if text_stripped == prefix or text_stripped.startswith(prefix + ' '):
            if not sender.is_owner:
                logger.warning('[FeishuBot] Command %s refused for non-owner %s',
                               prefix, sender.open_id[:12])
                return '⛔ 命令仅限已绑定的应用用户本人使用'
            try:
                return handler(sender, text_stripped)
            except Exception as e:
                logger.error(
                    '[FeishuBot] Command %s failed: %s',
                    prefix, e, exc_info=True,
                )
                return '❌ 命令执行失败，请稍后重试'
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
