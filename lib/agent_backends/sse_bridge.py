"""lib/agent_backends/sse_bridge.py — Translate NormalizedEvents to our frontend SSE protocol.

This is the critical bridge between backend-agnostic NormalizedEvents and
our frontend's SSE event format (delta, tool_start, tool_result, phase, done, etc.).

The frontend doesn't know about backends — it always receives the same SSE
events regardless of which backend (builtin, Claude Code, Codex) produced them.

Uses a stateful ``SSEBridgeState`` to track round numbers and tool-id-to-round
mappings so that ``tool_start`` → ``tool_result`` events are correctly paired
(the frontend matches them by ``roundNum``).

Inspired by Craft Agent's BaseEventAdapter pattern.
"""

from __future__ import annotations

import json
from typing import Any

from lib.agent_backends.protocol import NormalizedEvent, NormalizedEventKind
from lib.agent_core.events import EventType, build_event
from lib.log import get_logger

__all__ = ['SSEBridgeState', 'normalized_to_sse']

logger = get_logger(__name__)

K = NormalizedEventKind


# ═══════════════════════════════════════════════════════════
#  Tool display helpers for external backend tool names
# ═══════════════════════════════════════════════════════════

def _build_tool_query(tool_name: str, tool_input: dict) -> str:
    """Build a human-friendly display string for an external backend tool call.

    Maps Claude Code / Codex tool names + inputs into readable strings
    similar to the built-in Tofu backend's tool_display.py.

    Args:
        tool_name: Raw tool name from the backend (e.g. 'Read', 'Bash', 'Write').
        tool_input: Tool input arguments dict.

    Returns:
        Human-friendly display string for the tool panel.
    """
    name = tool_name or 'tool'
    inp = tool_input or {}

    # ── Claude Code tools ──
    if name in ('Read', 'read_file', 'ReadFile'):
        path = inp.get('file_path', inp.get('path', inp.get('filePath', '')))
        return '📄 Read: %s' % (path or '…')

    if name in ('Write', 'write_file', 'WriteFile', 'write_to_file'):
        path = inp.get('file_path', inp.get('path', inp.get('filePath', '')))
        return '✏️ Write: %s' % (path or '…')

    if name in ('Edit', 'EditFile', 'edit_file'):
        path = inp.get('file_path', inp.get('path', inp.get('filePath', '')))
        return '✏️ Edit: %s' % (path or '…')

    if name in ('MultiEdit', 'multi_edit'):
        path = inp.get('file_path', inp.get('path', inp.get('filePath', '')))
        return '✏️ MultiEdit: %s' % (path or '…')

    if name in ('Bash', 'bash', 'BashExec', 'command_execution'):
        cmd = inp.get('command', inp.get('cmd', ''))
        if cmd:
            short_cmd = cmd[:80] + ('…' if len(cmd) > 80 else '')
            return '$ %s' % short_cmd
        return '$ (command)'

    if name in ('Glob', 'glob', 'GlobTool', 'ListFiles'):
        pattern = inp.get('pattern', inp.get('glob', ''))
        return '📁 Glob: %s' % (pattern or '…')

    if name in ('Grep', 'grep', 'GrepTool', 'SearchFiles'):
        pattern = inp.get('pattern', inp.get('regex', inp.get('query', '')))
        return '🔎 Grep: %s' % (pattern or '…')

    if name in ('WebSearch', 'web_search', 'Search'):
        query = inp.get('query', inp.get('search_query', ''))
        return '🔍 %s' % (query or '…')

    if name in ('WebFetch', 'web_fetch', 'fetch_url', 'Fetch'):
        url = inp.get('url', inp.get('URL', ''))
        short = url[:80] + ('…' if len(url) > 80 else '') if url else '…'
        return '🌐 %s' % short

    if name in ('TodoRead', 'todo_read'):
        return '📋 Reading todo list'

    if name in ('TodoWrite', 'todo_write'):
        return '📋 Updating todo list'

    if name in ('Task', 'task', 'SubAgent'):
        desc = inp.get('description', inp.get('prompt', ''))[:60]
        return '🤖 Task: %s' % (desc or '…')

    if name in ('NotebookRead', 'notebook_read'):
        path = inp.get('notebook_path', inp.get('path', ''))
        return '📓 Read notebook: %s' % (path or '…')

    if name in ('NotebookEdit', 'notebook_edit'):
        path = inp.get('notebook_path', inp.get('path', ''))
        return '📓 Edit notebook: %s' % (path or '…')

    if name in ('LS', 'ls', 'ListDirectory'):
        path = inp.get('path', inp.get('dir', '.'))
        return '📂 ls: %s' % path

    if name == 'FileChange':
        path = inp.get('path', '')
        action = inp.get('action', 'modify')
        return '📄 %s: %s' % (action.capitalize(), path or '…')

    # ── Codex tools ──
    # (Codex uses item types like 'command_execution', 'file_change', etc.
    #  which are already mapped to tool names in codex.py)

    # ── Generic fallback ──
    # Try to find a useful arg to display
    for key in ('file_path', 'path', 'query', 'command', 'pattern', 'url'):
        val = inp.get(key, '')
        if val:
            short_val = str(val)[:60] + ('…' if len(str(val)) > 60 else '')
            return '🔧 %s: %s' % (name, short_val)

    return '🔧 %s' % name


def _build_tool_results_meta(
    tool_name: str,
    tool_output: str,
    tool_is_error: bool,
) -> list[dict[str, Any]]:
    """Build the ``results`` array for a tool_result SSE event.

    The frontend expects ``ev.results`` to be an array of meta dicts, each with
    at minimum ``{toolName, title, snippet}``.

    Args:
        tool_name: Tool name.
        tool_output: Raw tool output string.
        tool_is_error: Whether the tool execution failed.

    Returns:
        List with one result meta dict.
    """
    snippet = tool_output[:2000] if tool_output else ''
    title = '%s — %s' % (tool_name, 'Error' if tool_is_error else 'Done')

    return [{
        'toolName': tool_name or 'tool',
        'title': title,
        'snippet': snippet,
        'source': 'external-backend',
    }]


# ═══════════════════════════════════════════════════════════
#  Stateful SSE Bridge
# ═══════════════════════════════════════════════════════════

class SSEBridgeState:
    """Stateful bridge for translating NormalizedEvents to frontend SSE events.

    Tracks round numbers and tool-id-to-round/name mappings so that
    ``tool_start`` and ``tool_result`` events carry the correct ``roundNum``
    that the frontend uses to match them.

    One instance is created per task (per ``_run_external()`` call).
    """

    def __init__(self):
        self._round_counter: int = 0
        self._tool_id_to_round: dict[str, int] = {}
        self._tool_id_to_name: dict[str, str] = {}

    def translate(self, event: NormalizedEvent) -> dict[str, Any] | None:
        """Translate a NormalizedEvent to a frontend SSE event dict.

        Args:
            event: A NormalizedEvent from any backend.

        Returns:
            SSE event dict ready for ``append_event(task, event)``,
            or None if the event should be silently skipped.
        """
        kind = event.kind

        if kind == K.TEXT_DELTA:
            if not event.text:
                return None
            return build_event(EventType.DELTA, content=event.text)

        elif kind == K.THINKING_DELTA:
            if not event.text:
                return None
            return build_event(EventType.DELTA, thinking=event.text)

        elif kind == K.TOOL_START:
            return self._translate_tool_start(event)

        elif kind == K.TOOL_OUTPUT:
            rn = self._tool_id_to_round.get(event.tool_id, 0)
            evt: dict[str, Any] = build_event(
                EventType.TOOL_RESULT,
                roundNum=rn,
                streaming=True,
            )
            if event.tool_id:
                evt['toolCallId'] = event.tool_id
            if event.tool_output:
                evt['result'] = event.tool_output[:5000]
            return evt

        elif kind == K.TOOL_COMPLETE:
            return self._translate_tool_complete(event)

        elif kind == K.FILE_CHANGE:
            return build_event(
                EventType.TOOL_RESULT,
                tool='file_change',
                result=json.dumps({
                    'path': event.file_path,
                    'action': event.file_action,
                }, ensure_ascii=False),
            )

        elif kind == K.PHASE:
            return build_event(
                EventType.PHASE,
                phase=event.phase_type or 'working',
                detail=event.text,
            )

        elif kind == K.APPROVAL_REQUEST:
            rn = self._tool_id_to_round.get(event.tool_id, 0)
            return build_event(
                EventType.APPROVAL_REQUIRED,
                roundNum=rn,
                tool=event.tool_name,
                toolId=event.tool_id,
                args=event.tool_input,
                detail=event.text,
            )

        elif kind == K.DONE:
            evt = build_event(EventType.DONE)
            if event.finish_reason:
                evt['finishReason'] = event.finish_reason
            if event.usage:
                evt['usage'] = event.usage
            if event.error_message:
                evt['error'] = event.error_message
            if event.session_id:
                evt['sessionId'] = event.session_id
            return evt

        elif kind == K.ERROR:
            return build_event(
                EventType.DONE,
                error=event.error_message or 'Unknown error',
                finishReason='error',
            )

        # Unknown kind — skip
        return None

    def _translate_tool_start(self, event: NormalizedEvent) -> dict[str, Any]:
        """Translate a TOOL_START event with round tracking."""
        self._round_counter += 1
        rn = self._round_counter

        tool_id = event.tool_id or ('_tool_%d' % rn)
        self._tool_id_to_round[tool_id] = rn
        self._tool_id_to_name[tool_id] = event.tool_name or 'tool'

        query = _build_tool_query(event.tool_name, event.tool_input)

        evt: dict[str, Any] = build_event(
            EventType.TOOL_START,
            roundNum=rn,
            query=query,
            toolName=event.tool_name or 'tool',
            toolCallId=tool_id,
        )
        if event.tool_input:
            evt['toolArgs'] = json.dumps(event.tool_input, ensure_ascii=False)[:2000]
        return evt

    def _translate_tool_complete(self, event: NormalizedEvent) -> dict[str, Any]:
        """Translate a TOOL_COMPLETE event with round matching."""
        tool_id = event.tool_id or ''
        rn = self._tool_id_to_round.get(tool_id, 0)
        tool_name = event.tool_name or self._tool_id_to_name.get(tool_id, 'tool')

        # If we didn't see a TOOL_START for this tool_id (e.g. from assistant
        # type events), create a round retroactively
        if rn == 0 and tool_id:
            self._round_counter += 1
            rn = self._round_counter
            self._tool_id_to_round[tool_id] = rn
            self._tool_id_to_name[tool_id] = tool_name

        results = _build_tool_results_meta(
            tool_name, event.tool_output, event.tool_is_error,
        )

        return build_event(
            EventType.TOOL_RESULT,
            roundNum=rn,
            toolCallId=tool_id,
            toolName=tool_name,
            results=results,
            isError=event.tool_is_error,
        )

    def get_round_for_tool(self, tool_id: str) -> int:
        """Get the roundNum assigned to a tool_id, or 0 if unknown."""
        return self._tool_id_to_round.get(tool_id, 0)

    def get_name_for_tool(self, tool_id: str) -> str:
        """Get the tool name for a tool_id, or 'tool' if unknown."""
        return self._tool_id_to_name.get(tool_id, 'tool')


# ═══════════════════════════════════════════════════════════
#  Backward-compat stateless function (used by builtin backend)
# ═══════════════════════════════════════════════════════════

def normalized_to_sse(event: NormalizedEvent) -> dict[str, Any] | None:
    """Stateless translation — for the builtin backend which manages its own rounds.

    The builtin backend already produces fully-formed SSE events with roundNum,
    query, etc. via tool_display.py. This function is only used as a fallback
    or for simple event types (delta, phase, done).

    For external backends, use ``SSEBridgeState.translate()`` instead.
    """
    kind = event.kind

    if kind == K.TEXT_DELTA:
        if not event.text:
            return None
        return build_event(EventType.DELTA, content=event.text)

    elif kind == K.THINKING_DELTA:
        if not event.text:
            return None
        return build_event(EventType.DELTA, thinking=event.text)

    elif kind == K.TOOL_START:
        evt: dict[str, Any] = build_event(
            EventType.TOOL_START,
            tool=event.tool_name,
        )
        if event.tool_id:
            evt['toolId'] = event.tool_id
        if event.tool_input:
            evt['args'] = event.tool_input
        return evt

    elif kind == K.TOOL_OUTPUT:
        evt = build_event(
            EventType.TOOL_RESULT,
            streaming=True,
        )
        if event.tool_id:
            evt['toolId'] = event.tool_id
        if event.tool_output:
            evt['result'] = event.tool_output[:5000]
        return evt

    elif kind == K.TOOL_COMPLETE:
        evt = build_event(EventType.TOOL_RESULT)
        if event.tool_id:
            evt['toolId'] = event.tool_id
        if event.tool_name:
            evt['tool'] = event.tool_name
        if event.tool_output:
            evt['result'] = event.tool_output[:5000]
        evt['isError'] = event.tool_is_error
        return evt

    elif kind == K.FILE_CHANGE:
        return build_event(
            EventType.TOOL_RESULT,
            tool='file_change',
            result=json.dumps({
                'path': event.file_path,
                'action': event.file_action,
            }, ensure_ascii=False),
        )

    elif kind == K.PHASE:
        return build_event(
            EventType.PHASE,
            phase=event.phase_type or 'working',
            detail=event.text,
        )

    elif kind == K.APPROVAL_REQUEST:
        return build_event(
            EventType.APPROVAL_REQUIRED,
            tool=event.tool_name,
            toolId=event.tool_id,
            args=event.tool_input,
            detail=event.text,
        )

    elif kind == K.DONE:
        evt = build_event(EventType.DONE)
        if event.finish_reason:
            evt['finishReason'] = event.finish_reason
        if event.usage:
            evt['usage'] = event.usage
        if event.error_message:
            evt['error'] = event.error_message
        if event.session_id:
            evt['sessionId'] = event.session_id
        return evt

    elif kind == K.ERROR:
        return build_event(
            EventType.DONE,
            error=event.error_message or 'Unknown error',
            finishReason='error',
        )

    return None
