"""lib/tools/code_exec.py — Standalone code execution tool definition."""

import logging

logger = logging.getLogger(__name__)

CODE_EXEC_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Execute a shell command and return its output (stdout + stderr). "
            "Use this for running scripts, checking environments, installing packages, "
            "data processing, calculations, or any command-line task.\n"
            "The command runs in a default working directory.\n"
            "Commands run without a timeout by default — long-running processes are OK. "
            "Avoid interactive commands that require stdin input."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to execute, e.g. 'python3 -c \"print(1+1)\"', "
                        "'pip list', 'cat /etc/os-release'"
                    )
                }
            },
            "required": ["command"]
        }
    }
}

CODE_EXEC_TOOL_NAMES = {'run_command'}

__all__ = ['CODE_EXEC_TOOL', 'CODE_EXEC_TOOL_NAMES']
