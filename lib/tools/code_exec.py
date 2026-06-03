"""lib/tools/code_exec.py — Standalone code execution tool definition.

The standalone ``run_command`` tool (no project attached) shares its schema
with the project-mode ``run_command`` so the model gets the same usage
guidance (WHEN-TO-USE matrix, prefer-grep_search hint, pipeline warning)
regardless of mode. The only difference is the parameter description for
``working_dir``, which can't reference a "project root" when there isn't
one — we override that field below.
"""
import copy

from lib.log import get_logger
from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

logger = get_logger(__name__)

# Reuse the rich project-mode description, then override working_dir's
# parameter doc since there's no project root in standalone mode.
CODE_EXEC_TOOL = copy.deepcopy(PROJECT_TOOL_RUN_COMMAND)
CODE_EXEC_TOOL['function']['parameters']['properties']['working_dir']['description'] = (
    "Working directory for the command (optional). Default: the server's CWD. "
    "Pass an absolute path to run somewhere specific."
)

CODE_EXEC_TOOL_NAMES = {'run_command'}

__all__ = ['CODE_EXEC_TOOL', 'CODE_EXEC_TOOL_NAMES']
