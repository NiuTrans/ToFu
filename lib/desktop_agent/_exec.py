"""
Desktop Agent — local command execution handler.

Contains ``_SHELL_META_RE`` and ``cmd_run_local`` (run a shell command on
the local machine, avoiding ``shell=True`` when possible).
"""

import os
import re
import shlex
import subprocess

from lib.log import get_logger

logger = get_logger(__name__)


# Shell metacharacters that require shell=True for correct behaviour
_SHELL_META_RE = re.compile(r'[|&;<>()$`\\"\'\ \n*?\[#~]')


def cmd_run_local(params):
    """Run a shell command on the local machine.

    Security: avoids shell=True when the command is a simple executable
    invocation (no pipes, redirects, globs, etc.).  When shell features
    *are* needed, the command is passed as a single argument to an
    explicit ``['/bin/sh', '-c', ...]`` invocation so that the
    argument vector is never ambiguously parsed.
    """
    command = params.get('command', '')
    if not isinstance(command, str) or not command.strip():
        return {'error': 'Empty or invalid command'}
    cwd = params.get('cwd')
    timeout = params.get('timeout', 30)

    resolved_cwd = os.path.expanduser(cwd) if cwd else None

    try:
        needs_shell = bool(_SHELL_META_RE.search(command))
        if needs_shell:
            # Use explicit shell invocation instead of shell=True so
            # that *command* is a single, unambiguous argument to sh.
            from lib.compat import get_shell_args
            args = get_shell_args(command)
        else:
            # Simple command — split into argv list, no shell involved.
            # On Windows, use posix=False so that backslash paths and
            # double-quote quoting are handled correctly.
            from lib.compat import IS_WINDOWS
            args = shlex.split(command, posix=not IS_WINDOWS)

        result = subprocess.run(
            args, shell=False,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=resolved_cwd,
        )
        return {
            'stdout': result.stdout[:100_000],
            'stderr': result.stderr[:20_000],
            'exit_code': result.returncode,
        }
    except subprocess.TimeoutExpired:
        logger.warning('cmd_run_local timed out: cmd=%s timeout=%ds', command[:120], timeout, exc_info=True)
        return {'error': f'Command timed out after {timeout}s'}
    except Exception as e:
        logger.warning('cmd_run_local failed for cmd=%s: %s', command[:120], e, exc_info=True)
        return {'error': str(e)}
