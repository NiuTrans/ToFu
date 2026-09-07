"""
Desktop Agent — Local machine control bridge for Tofu.

Runs on the user's local machine (not the server), connects back to Tofu
and exposes system-level tools that Chrome Extension cannot provide:

  ✅ File system operations (read/write/move/copy local files)
  ✅ Run local applications (open files in default app, launch programs)
  ✅ Clipboard read/write (richer than browser clipboard)
  ✅ Screenshot entire desktop (not just browser tabs)
  ✅ System info (processes, disk usage, battery, network)
  ✅ GUI automation via pyautogui (click anywhere on screen, type anywhere)
  ✅ Manage local services (start/stop processes)

Architecture:
  Desktop Agent (your PC)  ←→  Tofu Server  ←→  LLM

  The agent polls /api/desktop/poll just like the browser extension polls
  /api/browser/poll. The server queues commands and returns results.

Usage:
  pip install pyautogui pillow psutil
  python -m lib.desktop_agent --server http://your-server:5000

Security:
  The agent only accepts commands from YOUR Tofu server.
  All dangerous operations require --allow-write / --allow-exec flags.

This package is a facade: it re-exports every public and private symbol so
that ``from lib.desktop_agent import X`` continues to work byte-identically
after the split into sub-modules.  A bare import never triggers the CLI —
that lives in ``lib.desktop_agent.__main__`` (``python -m lib.desktop_agent``).
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  File-system & application handlers
# ═══════════════════════════════════════════════════════════════════════════════

from lib.desktop_agent._files import (  # noqa: E402,F401
    _get_root_path,
    cmd_list_files,
    cmd_read_file,
    cmd_write_file,
    cmd_move_file,
    cmd_open_file,
    cmd_open_app,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Local command execution
# ═══════════════════════════════════════════════════════════════════════════════

from lib.desktop_agent._exec import (  # noqa: E402,F401
    _SHELL_META_RE,
    cmd_run_local,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  GUI / screenshot / clipboard / system-info handlers
# ═══════════════════════════════════════════════════════════════════════════════

from lib.desktop_agent._gui import (  # noqa: E402,F401
    cmd_screenshot_desktop,
    cmd_gui_action,
    cmd_clipboard,
    cmd_system_info,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Dispatch tables & dispatcher (built AFTER cmd_* handlers are imported)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.desktop_agent._dispatch import (  # noqa: E402,F401
    COMMANDS,
    WRITE_COMMANDS,
    EXEC_COMMANDS,
    GUI_COMMANDS,
    dispatch_command,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Polling loop & CLI
# ═══════════════════════════════════════════════════════════════════════════════

from lib.desktop_agent._run import (  # noqa: E402,F401
    run_agent,
    main,
)


__all__ = [
    '_get_root_path',
    'cmd_list_files',
    'cmd_read_file',
    'cmd_write_file',
    'cmd_move_file',
    'cmd_open_file',
    'cmd_open_app',
    '_SHELL_META_RE',
    'cmd_run_local',
    'cmd_screenshot_desktop',
    'cmd_gui_action',
    'cmd_clipboard',
    'cmd_system_info',
    'COMMANDS',
    'WRITE_COMMANDS',
    'EXEC_COMMANDS',
    'GUI_COMMANDS',
    'dispatch_command',
    'run_agent',
    'main',
]
