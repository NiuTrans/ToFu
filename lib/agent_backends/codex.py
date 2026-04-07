"""lib/agent_backends/codex.py — Codex CLI subprocess backend.

Spawns ``codex exec --json`` and normalizes its JSONL events into
``NormalizedEvent`` instances.

Authentication: Uses the user's own OpenAI credentials (OPENAI_API_KEY or
``~/.codex/``).  We do NOT inject API keys or model configuration.
This is a "pure frontend" backend.

Codex exec --json event format::

    Top-level types:
      - thread.started: thread created
      - turn.started: new turn begins
      - turn.completed: turn finished successfully
      - turn.failed: turn failed with error
      - error: fatal error

    Item lifecycle (within a turn):
      - item.started: new work item begins
      - item.updated: progress update on item
      - item.completed: item finished

    Item subtypes:
      - agent_message: text output from the agent
      - reasoning: thinking/reasoning blocks
      - command_execution: shell command execution
      - file_change: file modification
      - web_search: web search
      - mcp_tool_call: MCP tool invocation
      - todo_list: progress tracker
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from typing import Any, Iterator

from lib.agent_backends.detection import detect_codex
from lib.agent_backends.protocol import (
    AgentBackend,
    BackendCapabilities,
    NormalizedEvent,
    NormalizedEventKind,
)
from lib.agent_backends.session_store import get_session, save_session
from lib.log import get_logger

logger = get_logger(__name__)

K = NormalizedEventKind


class CodexBackend(AgentBackend):
    """Codex CLI subprocess backend.

    Spawns ``codex exec --json <message>`` as a child process and normalizes
    its JSONL stdout into NormalizedEvents.
    """

    def __init__(self):
        self._active_procs: dict[str, subprocess.Popen] = {}
        self._proc_lock = threading.Lock()
        self._detection_cache: dict | None = None

    # ── Properties ──

    @property
    def name(self) -> str:
        return 'codex'

    @property
    def display_name(self) -> str:
        return 'Codex'

    # ── Capabilities ──

    def get_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=True,
            multi_turn=True,
            abort=True,
            # Codex has its own tool implementations
            has_web_search=True,
            has_file_tools=True,
            has_code_exec=True,
            # Tofu-only features — NOT available
            has_image_gen=False,
            has_browser_ext=False,
            has_desktop_agent=False,
            has_error_tracker=False,
            has_swarm=False,
            has_scheduler=False,
            has_conv_ref=False,
            has_human_guidance=False,
            # UI controls — limited (Codex handles its own config)
            model_selector=False,
            thinking_depth=False,
            search_toggle=False,
            project_selector=True,
            preset_selector=False,
            temperature_control=False,
            endpoint_mode=False,
            approval_system='mode-based',
        )

    # ── Detection ──

    def _detect(self) -> dict:
        if self._detection_cache is None:
            self._detection_cache = detect_codex()
        return self._detection_cache

    def is_available(self) -> bool:
        return self._detect().get('available', False)

    def is_authenticated(self) -> bool:
        return self._detect().get('authenticated', False)

    def get_version(self) -> str | None:
        return self._detect().get('version')

    # ── Session ──

    def get_session_id(self, conv_id: str) -> str | None:
        return get_session(conv_id, self.name)

    # ── Core: start_turn ──

    def start_turn(
        self,
        task: dict[str, Any],
        user_message: str,
        *,
        images: list[dict] | None = None,
        project_path: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[NormalizedEvent]:
        """Spawn codex subprocess and yield NormalizedEvents."""
        detection = self._detect()
        codex_path = detection.get('path', 'codex')

        cmd = self._build_command(codex_path, user_message, project_path, session_id)
        cwd = project_path or os.getcwd()

        env = {**os.environ, 'NO_COLOR': '1'}

        logger.info('[Codex] Starting subprocess: %s (cwd=%s)',
                     ' '.join(cmd[:6]) + '...', cwd)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError:
            logger.error('[Codex] codex binary not found at %s', codex_path)
            yield NormalizedEvent(kind=K.ERROR, error_message='Codex CLI not found')
            return
        except Exception as e:
            logger.error('[Codex] Failed to spawn subprocess: %s', e)
            yield NormalizedEvent(kind=K.ERROR, error_message=f'Failed to start Codex: {e}')
            return

        task_id = task.get('id', '')
        with self._proc_lock:
            self._active_procs[task_id] = proc

        try:
            for line in proc.stdout:
                # Check abort
                if task.get('aborted'):
                    logger.info('[Codex] Task %s aborted — terminating subprocess', task_id[:8])
                    self._kill_proc(proc)
                    yield NormalizedEvent(kind=K.DONE, finish_reason='aborted')
                    return

                raw = line.decode('utf-8', errors='replace').strip()
                if not raw:
                    continue

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug('[Codex] Skipping non-JSON line: %.100s', raw)
                    continue

                yield from self._normalize(event, task)

        except Exception as e:
            logger.error('[Codex] Error reading subprocess output: %s', e, exc_info=True)
            yield NormalizedEvent(kind=K.ERROR, error_message=f'Codex stream error: {e}')
        finally:
            try:
                stderr_out = proc.stderr.read().decode('utf-8', errors='replace').strip()
                if stderr_out:
                    logger.debug('[Codex] stderr: %.500s', stderr_out)
            except Exception as e:
                logger.debug('[Codex] Failed to read stderr: %s', e)

            proc.wait()
            exit_code = proc.returncode
            with self._proc_lock:
                self._active_procs.pop(task_id, None)

            if exit_code and exit_code != 0:
                logger.warning('[Codex] Subprocess exited with code %d', exit_code)

    # ── Abort ──

    def abort(self, task_id: str) -> bool:
        with self._proc_lock:
            proc = self._active_procs.get(task_id)
        if proc is None:
            return False
        self._kill_proc(proc)
        return True

    # ── Private helpers ──

    def _build_command(
        self,
        codex_path: str,
        message: str,
        project_path: str | None,
        session_id: str | None,
    ) -> list[str]:
        """Build the codex CLI command."""
        cmd = [
            codex_path, 'exec',
            '--json',
            message,
        ]
        if session_id:
            cmd.extend(['--thread', session_id])
        return cmd

    def _kill_proc(self, proc: subprocess.Popen) -> None:
        """Send SIGTERM, wait, then SIGKILL if needed."""
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception as e:
            logger.debug('[Codex] Error killing subprocess: %s', e)

    def _normalize(
        self,
        event: dict,
        task: dict,
    ) -> Iterator[NormalizedEvent]:
        """Normalize a Codex exec --json event to NormalizedEvents."""
        etype = event.get('type', '')

        # ── Thread lifecycle ──
        if etype == 'thread.started':
            thread_id = event.get('thread_id', '')
            if thread_id and task.get('convId'):
                try:
                    save_session(task['convId'], self.name, thread_id)
                except Exception as e:
                    logger.debug('[Codex] Failed to save thread_id: %s', e)
            yield NormalizedEvent(kind=K.PHASE, text='Thread started')

        elif etype == 'turn.started':
            yield NormalizedEvent(kind=K.PHASE, text='Working...')

        elif etype == 'turn.completed':
            usage = event.get('usage', {})
            result_usage = {}
            if usage:
                result_usage = {
                    'prompt_tokens': usage.get('input_tokens', usage.get('prompt_tokens', 0)),
                    'completion_tokens': usage.get('output_tokens', usage.get('completion_tokens', 0)),
                    'total_tokens': (
                        usage.get('input_tokens', usage.get('prompt_tokens', 0))
                        + usage.get('output_tokens', usage.get('completion_tokens', 0))
                    ),
                }
            yield NormalizedEvent(
                kind=K.DONE,
                finish_reason='stop',
                usage=result_usage,
            )

        elif etype == 'turn.failed':
            error_msg = event.get('error', event.get('message', 'Turn failed'))
            yield NormalizedEvent(
                kind=K.ERROR,
                error_message=str(error_msg),
            )

        elif etype == 'error':
            error_msg = event.get('error', event.get('message', 'Codex error'))
            yield NormalizedEvent(
                kind=K.ERROR,
                error_message=str(error_msg),
            )

        # ── Item lifecycle ──
        elif etype == 'item.started':
            yield from self._normalize_item_started(event)

        elif etype == 'item.updated':
            yield from self._normalize_item_updated(event)

        elif etype == 'item.completed':
            yield from self._normalize_item_completed(event)

    def _normalize_item_started(self, event: dict) -> Iterator[NormalizedEvent]:
        """Normalize an item.started event."""
        item = event.get('item', {})
        item_type = item.get('type', '')

        if item_type == 'command_execution':
            cmd = item.get('command', item.get('args', {}).get('command', ''))
            yield NormalizedEvent(
                kind=K.TOOL_START,
                tool_name='Bash',
                tool_id=item.get('id', ''),
                tool_input={'command': cmd} if cmd else {},
            )
        elif item_type == 'file_change':
            path = item.get('path', item.get('args', {}).get('path', ''))
            action = item.get('action', 'modify')
            yield NormalizedEvent(
                kind=K.TOOL_START,
                tool_name='FileChange',
                tool_id=item.get('id', ''),
                tool_input={'path': path, 'action': action},
            )
        elif item_type == 'web_search':
            query = item.get('query', item.get('args', {}).get('query', ''))
            yield NormalizedEvent(
                kind=K.TOOL_START,
                tool_name='WebSearch',
                tool_id=item.get('id', ''),
                tool_input={'query': query} if query else {},
            )
        elif item_type == 'mcp_tool_call':
            name = item.get('name', item.get('tool_name', 'mcp_tool'))
            yield NormalizedEvent(
                kind=K.TOOL_START,
                tool_name=name,
                tool_id=item.get('id', ''),
                tool_input=item.get('args', {}),
            )
        elif item_type == 'reasoning':
            # Reasoning blocks start — content comes in updates
            pass
        elif item_type == 'agent_message':
            # Message blocks start — content comes in updates
            pass
        elif item_type == 'todo_list':
            items = item.get('items', [])
            detail = self._format_todo(items)
            yield NormalizedEvent(kind=K.PHASE, text=detail)

    def _normalize_item_updated(self, event: dict) -> Iterator[NormalizedEvent]:
        """Normalize an item.updated event (streaming progress)."""
        item = event.get('item', {})
        item_type = item.get('type', '')
        delta = event.get('delta', {})

        if item_type == 'agent_message':
            text = delta.get('content', delta.get('text', ''))
            if text:
                yield NormalizedEvent(kind=K.TEXT_DELTA, text=text)

        elif item_type == 'reasoning':
            text = delta.get('content', delta.get('text', ''))
            if text:
                yield NormalizedEvent(kind=K.THINKING_DELTA, text=text)

        elif item_type == 'command_execution':
            output = delta.get('output', delta.get('stdout', ''))
            if output:
                yield NormalizedEvent(
                    kind=K.TOOL_OUTPUT,
                    tool_id=item.get('id', ''),
                    tool_output=output,
                )

        elif item_type == 'todo_list':
            items = item.get('items', delta.get('items', []))
            detail = self._format_todo(items)
            if detail:
                yield NormalizedEvent(kind=K.PHASE, text=detail)

    def _normalize_item_completed(self, event: dict) -> Iterator[NormalizedEvent]:
        """Normalize an item.completed event."""
        item = event.get('item', {})
        item_type = item.get('type', '')

        if item_type == 'agent_message':
            # Full message text
            content = item.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(
                    b.get('text', '') for b in content
                    if isinstance(b, dict)
                )
            if content:
                yield NormalizedEvent(kind=K.TEXT_DELTA, text=content)

        elif item_type == 'reasoning':
            content = item.get('content', item.get('text', ''))
            if content:
                yield NormalizedEvent(kind=K.THINKING_DELTA, text=content)

        elif item_type == 'command_execution':
            output = item.get('output', item.get('result', ''))
            exit_code = item.get('exit_code', item.get('exitCode', 0))
            yield NormalizedEvent(
                kind=K.TOOL_COMPLETE,
                tool_name='Bash',
                tool_id=item.get('id', ''),
                tool_output=str(output),
                tool_is_error=(exit_code != 0),
            )

        elif item_type == 'file_change':
            path = item.get('path', '')
            action = item.get('action', 'modify')
            yield NormalizedEvent(
                kind=K.FILE_CHANGE,
                file_path=path,
                file_action=action,
            )
            yield NormalizedEvent(
                kind=K.TOOL_COMPLETE,
                tool_name='FileChange',
                tool_id=item.get('id', ''),
                tool_output=f'{action}: {path}',
            )

        elif item_type == 'web_search':
            results = item.get('results', item.get('output', ''))
            yield NormalizedEvent(
                kind=K.TOOL_COMPLETE,
                tool_name='WebSearch',
                tool_id=item.get('id', ''),
                tool_output=str(results)[:3000],
            )

        elif item_type == 'mcp_tool_call':
            result = item.get('result', item.get('output', ''))
            yield NormalizedEvent(
                kind=K.TOOL_COMPLETE,
                tool_name=item.get('name', 'mcp_tool'),
                tool_id=item.get('id', ''),
                tool_output=str(result),
                tool_is_error=item.get('is_error', False),
            )

    def _format_todo(self, items: list) -> str:
        """Format a todo list for display."""
        if not items:
            return ''
        parts = []
        for item in items:
            if isinstance(item, dict):
                text = item.get('text', item.get('title', ''))
                done = item.get('completed', item.get('done', False))
                mark = '✓' if done else '○'
                parts.append(f'{mark} {text}')
            elif isinstance(item, str):
                parts.append(f'○ {item}')
        return ' | '.join(parts) if parts else ''
