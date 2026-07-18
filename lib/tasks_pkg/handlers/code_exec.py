# HOT_PATH
"""Code execution handler: run_command (shell commands in project sandbox)."""

from __future__ import annotations

import re
import threading
import time

from lib.log import get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


# ── Streaming output coalescing ─────────────────────────────
# The subprocess can emit chunks faster than the SSE channel can deliver them
# (especially over VSCode port-forward / nginx — see the SSE proxy buffering
# memory).  Coalesce chunks into ≤ COALESCE_BYTES OR flush every COALESCE_MS,
# whichever first.  Tunable knobs (per CLAUDE.md §10.1, hyperparameters need
# user approval — defaults agreed in chat).
_COALESCE_MS = 200            # max wall-clock between flushes
_COALESCE_BYTES = 4096        # flush as soon as buffered output exceeds this


def _make_run_command_progress_cb(task, rn, round_entry, command):
    """Build an ``on_chunk(stream, text)`` callback for tool_run_command.

    Each call appends the chunk to ``round_entry['_partialOutput']`` for
    state-snapshot recovery, and emits a coalesced ``tool_progress`` SSE
    event so the frontend can render output as it arrives.

    Coalescing: chunks are buffered for up to ``_COALESCE_MS`` or
    ``_COALESCE_BYTES`` (whichever comes first) before being flushed as a
    single SSE event.  This avoids flooding the event queue when a command
    produces tight-loop output (e.g. ``yes``, build logs).
    """
    state = {
        'buf': [],            # list[(stream, text)]
        'bytes': 0,
        'last_flush': time.monotonic(),
        'lock': threading.Lock(),
        'timer': None,
    }

    def _flush_locked():
        if not state['buf']:
            return
        # Merge consecutive same-stream chunks for compactness
        merged = []
        cur_stream = None
        cur_parts = []
        for s, t in state['buf']:
            if s == cur_stream:
                cur_parts.append(t)
            else:
                if cur_stream is not None:
                    merged.append((cur_stream, ''.join(cur_parts)))
                cur_stream = s
                cur_parts = [t]
        if cur_stream is not None:
            merged.append((cur_stream, ''.join(cur_parts)))

        state['buf'] = []
        state['bytes'] = 0
        state['last_flush'] = time.monotonic()
        if state['timer'] is not None:
            try:
                state['timer'].cancel()
            except Exception as e:
                logger.debug('[run_command progress] timer cancel failed: %s', e)
            state['timer'] = None

        # Mirror partial output onto the round_entry so a
        # state-snapshot reconnect can replay it (see manager.append_event).
        partial = round_entry.setdefault('_partialOutput', '')
        for s, t in merged:
            partial = partial + t
            append_event(task, {
                'type': 'tool_progress',
                'roundNum': rn,
                'toolCallId': round_entry.get('toolCallId', ''),
                'stream': s,
                'chunk': t,
                'toolName': round_entry.get('toolName') or 'run_command',
            })
        round_entry['_partialOutput'] = partial

    def _delayed_flush():
        with state['lock']:
            _flush_locked()

    def _on_chunk(stream, text):
        if not text:
            return
        with state['lock']:
            state['buf'].append((stream, text))
            state['bytes'] += len(text)
            now = time.monotonic()
            if (state['bytes'] >= _COALESCE_BYTES
                    or (now - state['last_flush']) * 1000 >= _COALESCE_MS):
                _flush_locked()
            elif state['timer'] is None:
                # Schedule a deferred flush so the last partial chunk
                # doesn't sit forever waiting for a follow-up.
                t = threading.Timer(_COALESCE_MS / 1000.0, _delayed_flush)
                t.daemon = True
                state['timer'] = t
                t.start()

    # Expose a final-flush hook so the handler can drain after the command
    # exits (in case the last chunk fell below the threshold).
    def _final_flush():
        with state['lock']:
            _flush_locked()
    _on_chunk.flush = _final_flush  # attribute on closure for the handler
    return _on_chunk


def _make_stdin_callback(task, rn, round_entry, command):
    """Create a callback that pauses execution and asks the user for stdin input.

    When the subprocess appears to be waiting for stdin (no output for N seconds),
    this callback:
    1. Emits a ``stdin_request`` SSE event with the prompt context
    2. Blocks until the user submits input via ``/api/chat/stdin_response``
    3. Returns the user's input string (or None if aborted)
    """
    from lib.ids import short_id
    from lib.tasks_pkg.stdin_handler import request_stdin

    def _stdin_cb(prompt_hint):
        stdin_id = short_id('stdin_', 12)
        logger.info('[Executor] stdin wait detected for command=%s, '
                    'stdin_id=%s, prompt_hint=%.200s',
                    command[:80], stdin_id, prompt_hint)

        round_entry['status'] = 'awaiting_stdin'
        round_entry['stdinId'] = stdin_id
        round_entry['stdinPrompt'] = prompt_hint
        append_event(task, {
            'type': 'stdin_request',
            'roundNum': rn,
            'toolCallId': round_entry.get('toolCallId', ''),
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
                'toolCallId': round_entry.get('toolCallId', ''),
                'stdinId': stdin_id,
            })

        return user_input

    return _stdin_cb


# code_exec is registered as a special handler (matched via round_entry, not fn_name)
@tool_registry.special('__code_exec__', category='code',
                       description='Execute a shell command in the project sandbox')
def _handle_code_exec(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.project_mod import execute_standalone_command
    cmd = fn_args.get('command', '')
    cb = _make_stdin_callback(task, rn, round_entry, cmd)
    progress_cb = _make_run_command_progress_cb(task, rn, round_entry, cmd)
    try:
        tool_content = execute_standalone_command(fn_name, fn_args,
                                                  stdin_callback=cb,
                                                  on_chunk=progress_cb)
    finally:
        # Flush any buffered tail that didn't reach the coalescing threshold.
        try:
            progress_cb.flush()
        except Exception as e:
            logger.debug('[code_exec] progress flush failed: %s', e)
    # Must anchor to END — command output may itself contain [exit code: N]
    m_exit = re.search(r'\[exit code: (-?\d+)\]\s*$', tool_content)
    exit_code = m_exit.group(1) if m_exit else '?'
    timed_out = '[Command timed out]' in tool_content
    # No marker + not a timeout = the command was REFUSED/BLOCKED before it
    # ran (read-only root, dangerous pattern, no project path, pre-hook block,
    # abort, or a start error). Classify it as not-run with the full message
    # as the reason so the UI shows a clear cause instead of "exit ?".
    not_run = (m_exit is None) and (not timed_out)
    prefix = f'$ {cmd}\n'
    if tool_content.startswith(prefix):
        output_text = tool_content[len(prefix):]
    else:
        output_lines = tool_content.split('\n', 1)
        output_text = output_lines[1] if len(output_lines) > 1 else ''
    output_text = re.sub(r'\n?\[exit code: -?\d+\]\s*$', '', output_text).strip()
    output_text = re.sub(r'\n?\[Command timed out\].*$', '', output_text).strip()
    if not_run:
        from lib.tools.meta import _classify_not_run_badge
        reason = (tool_content or '').strip()
        logger.warning('[code_exec] command not run (refused/blocked/error) '
                       'cmd=%.80s reason=%.160s', cmd, reason)
        meta = {
            'toolName': 'code_exec', 'command': cmd,
            'output': reason, 'reason': reason,
            'exitCode': 'not-run', 'notRun': True, 'timedOut': False,
            'badge': _classify_not_run_badge(reason),
        }
    else:
        meta = {
            'toolName': 'code_exec', 'command': cmd, 'output': output_text,
            'exitCode': 'timeout' if timed_out else exit_code, 'timedOut': timed_out,
        }
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
