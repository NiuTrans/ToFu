# HOT_PATH
"""Code execution handler: run_command (shell commands in project sandbox)."""

from __future__ import annotations

import re

from lib.log import get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def _make_stdin_callback(task, rn, round_entry, command):
    """Create a callback that pauses execution and asks the user for stdin input.

    When the subprocess appears to be waiting for stdin (no output for N seconds),
    this callback:
    1. Emits a ``stdin_request`` SSE event with the prompt context
    2. Blocks until the user submits input via ``/api/chat/stdin_response``
    3. Returns the user's input string (or None if aborted)
    """
    import uuid as _uuid

    from lib.tasks_pkg.stdin_handler import request_stdin

    def _stdin_cb(prompt_hint):
        stdin_id = f'stdin_{_uuid.uuid4().hex[:12]}'
        logger.info('[Executor] stdin wait detected for command=%s, '
                    'stdin_id=%s, prompt_hint=%.200s',
                    command[:80], stdin_id, prompt_hint)

        round_entry['status'] = 'awaiting_stdin'
        round_entry['stdinId'] = stdin_id
        round_entry['stdinPrompt'] = prompt_hint
        append_event(task, {
            'type': 'stdin_request',
            'roundNum': rn,
            'stdinId': stdin_id,
            'prompt': prompt_hint,
            'command': command[:200],
        })

        user_input = request_stdin(stdin_id, task=task)

        if user_input is not None:
            round_entry['status'] = 'searching'
            round_entry.pop('stdinId', None)
            round_entry.pop('stdinPrompt', None)
            append_event(task, {
                'type': 'stdin_resolved',
                'roundNum': rn,
                'stdinId': stdin_id,
            })

        return user_input

    return _stdin_cb


# code_exec is registered as a special handler (matched via round_entry, not fn_name)
@tool_registry.special('__code_exec__', category='code',
                       description='Execute a shell command in the project sandbox')
def _handle_code_exec(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.project_mod import execute_standalone_command
    cb = _make_stdin_callback(task, rn, round_entry, fn_args.get('command', ''))
    tool_content = execute_standalone_command(fn_name, fn_args, stdin_callback=cb)
    cmd = fn_args.get('command', '')
    # Must anchor to END — command output may itself contain [exit code: N]
    m_exit = re.search(r'\[exit code: (-?\d+)\]\s*$', tool_content)
    exit_code = m_exit.group(1) if m_exit else '?'
    timed_out = '⏰' in tool_content
    prefix = f'$ {cmd}\n'
    if tool_content.startswith(prefix):
        output_text = tool_content[len(prefix):]
    else:
        output_lines = tool_content.split('\n', 1)
        output_text = output_lines[1] if len(output_lines) > 1 else ''
    output_text = re.sub(r'\n?\[exit code: -?\d+\]\s*$', '', output_text).strip()
    output_text = re.sub(r'\n?⏰ Command timed out.*$', '', output_text).strip()
    meta = {
        'toolName': 'code_exec', 'command': cmd, 'output': output_text,
        'exitCode': 'timeout' if timed_out else exit_code, 'timedOut': timed_out,
    }
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
