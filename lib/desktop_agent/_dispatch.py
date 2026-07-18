"""
Desktop Agent — command dispatch tables & dispatcher.

Build order matters: the module-level dispatch tables (``COMMANDS``,
``WRITE_COMMANDS``, ``EXEC_COMMANDS``, ``GUI_COMMANDS``) reference the
``cmd_*`` handler functions, so those handlers are imported *first* from
the ``_files`` / ``_exec`` / ``_gui`` sub-modules before the tables below
are constructed.
"""

import traceback

from lib.desktop_agent._exec import cmd_run_local
from lib.desktop_agent._files import (
    cmd_list_files,
    cmd_move_file,
    cmd_open_app,
    cmd_open_file,
    cmd_read_file,
    cmd_write_file,
)
from lib.desktop_agent._gui import (
    cmd_clipboard,
    cmd_gui_action,
    cmd_screenshot_desktop,
    cmd_system_info,
)
from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Command Dispatcher
# ══════════════════════════════════════════════════════════

COMMANDS = {
    # File system
    'desktop_list_files':    cmd_list_files,
    'desktop_read_file':     cmd_read_file,
    'desktop_write_file':    cmd_write_file,
    'desktop_move_file':     cmd_move_file,

    # Applications
    'desktop_open_file':     cmd_open_file,
    'desktop_open_app':      cmd_open_app,
    'desktop_run_command':   cmd_run_local,

    # GUI automation
    'desktop_screenshot':    cmd_screenshot_desktop,
    'desktop_gui_action':    cmd_gui_action,
    'desktop_clipboard':     cmd_clipboard,

    # System
    'desktop_system_info':   cmd_system_info,
}

# Permission levels
WRITE_COMMANDS = {'desktop_write_file', 'desktop_move_file'}
EXEC_COMMANDS = {'desktop_run_command', 'desktop_open_file', 'desktop_open_app'}
GUI_COMMANDS = {'desktop_gui_action', 'desktop_screenshot'}


def dispatch_command(cmd_type, params, permissions):
    """Execute a command if permitted."""
    if cmd_type not in COMMANDS:
        return {'error': f'Unknown command: {cmd_type}'}

    # Permission checks
    if cmd_type in WRITE_COMMANDS and not permissions.get('allow_write'):
        return {'error': f'Command {cmd_type} requires --allow-write flag'}
    if cmd_type in EXEC_COMMANDS and not permissions.get('allow_exec'):
        return {'error': f'Command {cmd_type} requires --allow-exec flag'}
    if cmd_type in GUI_COMMANDS and not permissions.get('allow_gui'):
        return {'error': f'Command {cmd_type} requires --allow-gui flag'}

    try:
        return COMMANDS[cmd_type](params)
    except Exception as e:
        logger.error('dispatch_command %s failed', cmd_type, exc_info=True)
        return {'error': f'{type(e).__name__}: {e}', 'traceback': traceback.format_exc()[-500:]}
