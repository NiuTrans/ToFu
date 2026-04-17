"""lib/agent_backends/claude_code.py — Claude Code CLI subprocess backend.

Spawns ``claude`` CLI with ``--output-format stream-json`` and normalizes
its JSONL events into ``NormalizedEvent`` instances.

Authentication: Uses the user's own Claude Code credentials (``~/.claude/``).
We do NOT inject system prompts, API keys, or model configuration.
This is a "pure frontend" backend.

Claude Code stream-json event format::

    Top-level types:
      - system: init info, API retry notices
      - stream_event: wraps Anthropic SSE events
          - message_start, content_block_start, content_block_delta,
            content_block_stop, message_delta, message_stop
      - assistant: full completed message (non-streaming fallback)
      - result: final result with session_id, usage, cost

    content_block_delta subtypes:
      - text_delta: assistant text
      - thinking_delta: extended thinking
      - input_json_delta: tool input accumulation
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from typing import Any, Iterator

from lib.agent_backends.detection import detect_claude_code
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


class ClaudeCodeBackend(AgentBackend):
    """Claude Code CLI subprocess backend.

    Spawns ``claude -p <message> --output-format stream-json`` as a
    child process and normalizes its JSONL stdout into NormalizedEvents.
    """

    def __init__(self):
        self._active_procs: dict[str, subprocess.Popen] = {}
        self._proc_lock = threading.Lock()
        self._detection_cache: dict | None = None

    # ── Properties ──

    @property
    def name(self) -> str:
        return 'claude-code'

    @property
    def display_name(self) -> str:
        return 'Claude Code'

    # ── Capabilities ──

    def get_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=True,
            multi_turn=True,
            abort=True,
            # Claude Code has its own versions of these tools
            has_web_search=True,
            has_file_tools=True,
            has_code_exec=True,
            # Tofu-only features — NOT available
            has_image_gen=False,
            has_browser_ext=False,
            has_desktop_agent=False,
            has_swarm=False,
            has_scheduler=False,
            has_conv_ref=False,
            has_human_guidance=False,
            # UI controls — limited (CC handles its own config)
            model_selector=False,
            thinking_depth=False,
            search_toggle=False,
            project_selector=True,   # We tell CC which directory
            preset_selector=False,
            temperature_control=False,
            endpoint_mode=False,
            approval_system='mode-based',
        )

    # ── Detection ──

    def _detect(self) -> dict:
        if self._detection_cache is None:
            self._detection_cache = detect_claude_code()
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
        """Spawn claude subprocess and yield NormalizedEvents."""
        detection = self._detect()
        claude_path = detection.get('path', 'claude')

        cmd = self._build_command(claude_path, user_message, project_path, session_id)
        cwd = project_path or os.getcwd()

        env = {
            **os.environ,
            'CLAUDE_CODE_SKIP_UPDATE_CHECK': '1',
            'NO_COLOR': '1',
        }

        logger.info('[ClaudeCode] Starting subprocess: %s (cwd=%s)',
                     ' '.join(cmd[:6]) + '...', cwd)

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError:
            logger.error('[ClaudeCode] claude binary not found at %s', claude_path)
            yield NormalizedEvent(kind=K.ERROR, error_message='Claude Code CLI not found')
            return
        except Exception as e:
            logger.error('[ClaudeCode] Failed to spawn subprocess: %s', e)
            yield NormalizedEvent(kind=K.ERROR, error_message=f'Failed to start Claude Code: {e}')
            return

        task_id = task.get('id', '')
        with self._proc_lock:
            self._active_procs[task_id] = proc

        # State for tool input accumulation
        tool_input_buffers: dict[int, str] = {}  # index → accumulated JSON string
        current_tools: dict[int, dict] = {}       # index → {id, name}
        tool_id_to_name: dict[str, str] = {}       # tool_id → tool_name (for matching results)

        try:
            for line in proc.stdout:
                # Check abort
                if task.get('aborted'):
                    logger.info('[ClaudeCode] Task %s aborted — terminating subprocess', task_id[:8])
                    self._kill_proc(proc)
                    yield NormalizedEvent(kind=K.DONE, finish_reason='aborted')
                    return

                raw = line.decode('utf-8', errors='replace').strip()
                if not raw:
                    continue

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug('[ClaudeCode] Skipping non-JSON line: %.100s', raw)
                    continue

                etype = event.get('type', '?')
                subtype = event.get('subtype', '')
                se_type = ''
                if etype == 'stream_event':
                    se_type = event.get('event', {}).get('type', '?')
                logger.debug('[ClaudeCode] Event: type=%s%s%s',
                             etype,
                             '/%s' % subtype if subtype else '',
                             '/%s' % se_type if se_type else '')

                yield from self._normalize(
                    event, tool_input_buffers, current_tools,
                    tool_id_to_name, task,
                )

        except Exception as e:
            logger.error('[ClaudeCode] Error reading subprocess output: %s', e, exc_info=True)
            yield NormalizedEvent(kind=K.ERROR, error_message=f'Claude Code stream error: {e}')
        finally:
            # Read stderr for diagnostics
            try:
                stderr_out = proc.stderr.read().decode('utf-8', errors='replace').strip()
                if stderr_out:
                    logger.debug('[ClaudeCode] stderr: %.500s', stderr_out)
            except Exception as e:
                logger.debug('[ClaudeCode] Failed to read stderr: %s', e)

            proc.wait()
            exit_code = proc.returncode
            with self._proc_lock:
                self._active_procs.pop(task_id, None)

            if exit_code and exit_code != 0:
                logger.warning('[ClaudeCode] Subprocess exited with code %d', exit_code)

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
        claude_path: str,
        message: str,
        project_path: str | None,
        session_id: str | None,
    ) -> list[str]:
        """Build the claude CLI command."""
        cmd = [
            claude_path,
            '-p', message,
            '--output-format', 'stream-json',
            '--verbose',
        ]
        if session_id:
            cmd.extend(['--resume', session_id])
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
            logger.debug('[ClaudeCode] Error killing subprocess: %s', e)

    def _normalize(
        self,
        event: dict,
        tool_input_buffers: dict[int, str],
        current_tools: dict[int, dict],
        tool_id_to_name: dict[str, str],
        task: dict,
    ) -> Iterator[NormalizedEvent]:
        """Normalize a Claude Code stream-json event to NormalizedEvents.

        Handles the full event taxonomy:
          - system: init, api_retry
          - stream_event: content_block_start/delta/stop, message_start/delta/stop
          - assistant: full message (non-streaming, tool_use blocks)
          - user: tool results from Claude Code's internal execution
          - result: session_id, usage, cost
        """
        etype = event.get('type', '')

        if etype == 'system':
            sub = event.get('subtype', '')
            if sub == 'init':
                model = event.get('model', 'unknown')
                version = event.get('claude_code_version', '?')
                logger.info('[ClaudeCode] Init — model=%s version=%s tools=%d',
                            model, version, len(event.get('tools', [])))
                yield NormalizedEvent(kind=K.PHASE, text='Initializing Claude Code...')
            elif sub == 'api_retry':
                attempt = event.get('attempt', '?')
                max_retries = event.get('max_retries', '?')
                error_status = event.get('error_status', '')
                error_type = event.get('error', 'unknown')
                delay_ms = event.get('retry_delay_ms', 0)
                delay_s = delay_ms / 1000 if isinstance(delay_ms, (int, float)) else 0
                detail = 'API %s (HTTP %s) — retry %s/%s in %.1fs' % (
                    error_type, error_status, attempt, max_retries, delay_s)
                logger.warning('[ClaudeCode] %s', detail)
                yield NormalizedEvent(kind=K.PHASE, phase_type='retrying', text=detail)

        elif etype == 'stream_event':
            yield from self._normalize_stream_event(
                event, tool_input_buffers, current_tools,
                tool_id_to_name,
            )

        elif etype == 'assistant':
            # Full completed assistant message — extract text and tool_use calls
            msg = event.get('message', {})
            content_blocks = msg.get('content', [])
            for block in content_blocks:
                btype = block.get('type', '')
                if btype == 'text':
                    text = block.get('text', '')
                    if text:
                        yield NormalizedEvent(kind=K.TEXT_DELTA, text=text)
                elif btype == 'tool_use':
                    # Non-streaming tool call — emit TOOL_START with full input
                    tool_name = block.get('name', '')
                    tool_id = block.get('id', '')
                    if tool_id and tool_name:
                        tool_id_to_name[tool_id] = tool_name
                    yield NormalizedEvent(
                        kind=K.TOOL_START,
                        tool_name=tool_name,
                        tool_id=tool_id,
                        tool_input=block.get('input', {}),
                    )

        elif etype == 'user':
            # Tool results from Claude Code's internal tool execution.
            # Claude Code sends user-role messages with tool_result blocks
            # after it executes tools internally.
            msg = event.get('message', {})
            content_blocks = msg.get('content', [])
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get('type', '')
                if btype == 'tool_result':
                    tool_use_id = block.get('tool_use_id', '')
                    is_error = block.get('is_error', False)
                    content = block.get('content', '')
                    if isinstance(content, list):
                        content = '\n'.join(
                            b.get('text', '') for b in content
                            if isinstance(b, dict) and b.get('type') == 'text'
                        )
                    resolved_name = tool_id_to_name.get(tool_use_id, '')
                    logger.debug('[ClaudeCode] Tool result: id=%s name=%s error=%s output_len=%d',
                                 tool_use_id[:12], resolved_name, is_error, len(str(content)))
                    yield NormalizedEvent(
                        kind=K.TOOL_COMPLETE,
                        tool_id=tool_use_id,
                        tool_name=resolved_name,
                        tool_output=str(content),
                        tool_is_error=is_error,
                    )

        elif etype == 'result':
            session_id = event.get('session_id', '')
            usage = event.get('usage', {})
            cost = event.get('cost_usd')

            # Store session for multi-turn
            if session_id and task.get('convId'):
                try:
                    save_session(task['convId'], self.name, session_id)
                except Exception as e:
                    logger.debug('[ClaudeCode] Failed to save session: %s', e)

            result_usage = {}
            if usage:
                result_usage = {
                    'prompt_tokens': usage.get('input_tokens', 0),
                    'completion_tokens': usage.get('output_tokens', 0),
                    'total_tokens': usage.get('input_tokens', 0) + usage.get('output_tokens', 0),
                }
            if cost is not None:
                result_usage['cost_usd'] = cost

            is_error = event.get('is_error', False)
            error_msg = ''
            if is_error:
                error_msg = event.get('error', event.get('message', 'Claude Code error'))

            yield NormalizedEvent(
                kind=K.DONE,
                session_id=session_id,
                usage=result_usage,
                finish_reason='error' if is_error else 'stop',
                error_message=error_msg,
            )

    def _normalize_stream_event(
        self,
        event: dict,
        tool_input_buffers: dict[int, str],
        current_tools: dict[int, dict],
        tool_id_to_name: dict[str, str],
    ) -> Iterator[NormalizedEvent]:
        """Normalize a stream_event (wrapping Anthropic SSE)."""
        se = event.get('event', {})
        se_type = se.get('type', '')

        if se_type == 'content_block_start':
            cb = se.get('content_block', {})
            index = se.get('index', 0)
            cb_type = cb.get('type', '')

            if cb_type == 'tool_use':
                tool_id = cb.get('id', '')
                tool_name = cb.get('name', '')
                current_tools[index] = {'id': tool_id, 'name': tool_name}
                tool_input_buffers[index] = ''
                # Record the mapping early so tool results can resolve names
                if tool_id and tool_name:
                    tool_id_to_name[tool_id] = tool_name
                # Do NOT yield TOOL_START here — we don't have the full input yet.
                # TOOL_START is yielded in content_block_stop after input is accumulated.
                # Yield a PHASE hint so the UI knows something is happening.
                yield NormalizedEvent(
                    kind=K.PHASE,
                    phase_type='tool_exec',
                    text='%s — preparing…' % (tool_name or 'Tool'),
                )
            elif cb_type == 'thinking':
                pass  # Thinking block start — content comes in deltas
            elif cb_type == 'text':
                pass  # Text block start — content comes in deltas

        elif se_type == 'content_block_delta':
            delta = se.get('delta', {})
            dt = delta.get('type', '')
            index = se.get('index', 0)

            if dt == 'text_delta':
                text = delta.get('text', '')
                if text:
                    yield NormalizedEvent(kind=K.TEXT_DELTA, text=text)

            elif dt == 'thinking_delta':
                thinking = delta.get('thinking', '')
                if thinking:
                    yield NormalizedEvent(kind=K.THINKING_DELTA, text=thinking)

            elif dt == 'input_json_delta':
                # Accumulate tool input JSON fragments
                partial = delta.get('partial_json', '')
                if index in tool_input_buffers:
                    tool_input_buffers[index] += partial

        elif se_type == 'content_block_stop':
            index = se.get('index', 0)

            if index in current_tools:
                tool_info = current_tools.pop(index)
                raw_input = tool_input_buffers.pop(index, '')
                tool_input = {}
                if raw_input:
                    try:
                        tool_input = json.loads(raw_input)
                    except json.JSONDecodeError:
                        logger.debug('[ClaudeCode] Failed to parse tool input JSON: %.200s', raw_input)
                        tool_input = {'_raw': raw_input}

                # Tool call JSON is fully accumulated — now Claude Code will
                # execute it internally. Yield TOOL_START (not TOOL_COMPLETE)
                # because the tool hasn't been executed yet. The actual result
                # comes later as a 'user' type event with tool_result blocks.
                logger.info('[ClaudeCode] Tool call ready: name=%s id=%s input_keys=%s',
                            tool_info['name'], tool_info['id'][:12],
                            list(tool_input.keys()) if isinstance(tool_input, dict) else '?')
                yield NormalizedEvent(
                    kind=K.TOOL_START,
                    tool_name=tool_info['name'],
                    tool_id=tool_info['id'],
                    tool_input=tool_input,
                )

        elif se_type == 'message_start':
            pass  # Message start — no normalized event needed

        elif se_type == 'message_delta':
            # May contain stop_reason
            delta = se.get('delta', {})
            stop_reason = delta.get('stop_reason', '')
            if stop_reason:
                logger.debug('[ClaudeCode] message_delta stop_reason: %s', stop_reason)

        elif se_type == 'message_stop':
            pass  # Message complete — result event follows
