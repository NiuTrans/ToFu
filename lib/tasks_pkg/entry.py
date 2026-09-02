# HOT_PATH
"""lib.tasks_pkg.entry — the shared, transport-agnostic chat kernel.

Both the HTTP route (``routes/api_v1/chat.py``) and the in-process façade
(``tofu.chat`` / ``tofu.stream``) converge here, so the "build a config →
create a task → run it → collect the result" choreography lives in exactly
one place.

What this module deliberately does NOT do:

  * **No billing.** Pre-flight reserve / post-flight settle is a multi-user
    HTTP concern; trusted in-process embedders don't have a wallet.
  * **No BYO ephemeral-slot resolution.** The ``model@prov_xxx`` suffix and
    its disposal thread are an HTTP-key-scoped feature.
  * **No SSE framing.** ``run_chat_stream`` yields the *native* Tofu event
    dicts (the declared event contract); the HTTP route is responsible for
    re-shaping those into OpenAI ``chat.completion.chunk`` frames.

The route keeps those concerns by calling :func:`build_chat_config` /
:func:`create_task` / :func:`spawn_task` itself and wrapping them — it does
not have to route through here. This kernel is the minimal common core, not
a mandatory funnel.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from lib.log import get_logger
from lib.turn_verdict import (
    TurnStatus,
    derive_task_verdict,
    terminal_finish_reason,
)

logger = get_logger(__name__)

_TERMINAL_STATES = ('done', 'error', 'aborted')


def build_chat_config(
    model: str = '',
    config: dict | None = None,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    tools: list | None = None,
    response_format: Any = None,
    thinking_depth: str | None = None,
    user: str = '',
) -> dict:
    """Assemble a task ``cfg`` from chat-completion-style knobs.

    Mirrors the field-mapping block in ``routes/api_v1/chat.py`` (minus the
    billing/BYO bits). Top-level knobs only fill a key when ``config`` did
    not already set it, so an explicit ``config`` value always wins — same
    precedence the route documents.
    """
    cfg = dict(config or {})
    if model:
        cfg['model'] = model
    cfg.setdefault('thinkingDepth', thinking_depth or '')
    if max_tokens is not None and 'maxTokens' not in cfg:
        cfg['maxTokens'] = max_tokens
    if temperature is not None and 'temperature' not in cfg:
        cfg['temperature'] = temperature
    if tools is not None and 'tools' not in cfg:
        cfg['tools'] = tools
    if response_format is not None and 'responseFormat' not in cfg:
        cfg['responseFormat'] = response_format
    if user and 'user' not in cfg:
        cfg['user'] = user
    # ── App-personal capabilities fail closed on the headless surface ──
    # build_chat_config backs every headless entry point (the HTTP
    # /chat/completions + /chat/stream-direct routes and the in-process
    # tofu.chat / tofu.stream facade), NONE of which is the interactive UI
    # (that goes through lib.conv_config.resolve_conv_config). So the
    # operator's personal memory store + preference profile must be strict
    # opt-in here, never inherited from the UI's default-on. setdefault =
    # an explicit caller value still wins. See lib/agent_core/personal_scope.
    from lib.agent_core.personal_scope import apply_headless_personal_defaults
    apply_headless_personal_defaults(cfg)
    return cfg


@dataclass
class ChatResult:
    """The terminal outcome of an in-process chat turn.

    ``error`` carries the typed error envelope (``lib.error_envelope``) when
    the task failed; ``ok`` is the convenience boolean. ``raw_task`` is the
    live task dict for callers that need fields not surfaced here (it is the
    same object the orchestrator mutated, not a copy).
    """

    content: str = ''
    thinking: str = ''
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str = 'error'
    error: dict | None = None
    task_id: str = ''
    status: str = ''
    raw_task: dict | None = None
    stream_state: str | None = None

    @property
    def ok(self) -> bool:
        verdict = derive_task_verdict({
            'status': self.status,
            'finishReason': self.finish_reason,
            'streamState': self.stream_state,
            'error': self.error,
            'aborted': self.status == 'aborted',
        })
        return self.status == 'done' and verdict.status is TurnStatus.COMPLETED


def _start_task(messages: list, cfg: dict, *, user_id: int) -> dict:
    """Create + spawn a chat task from ready messages + cfg. Returns the task."""
    from lib.agent_core.run_contract import apply_storage_free_runtime_policy
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.spawn import spawn_task

    if not messages:
        raise ValueError('run_chat requires a non-empty messages list')

    apply_storage_free_runtime_policy(cfg)
    conv_id = cfg.pop('_conversation_id', '') or ''
    task = create_task(
        conv_id,
        messages,
        cfg,
        user_id=int(user_id),
        supersede=False,
        transient=True,
    )
    spawn_task(task)
    return task


def _assemble_result(task: dict) -> ChatResult:
    """Project a terminal task dict into a :class:`ChatResult`."""
    usage = task.get('usage') or {}
    tool_calls: list = []
    rounds = task.get('toolRounds') or []
    if rounds:
        last = rounds[-1]
        if isinstance(last, dict) and last.get('tool_calls'):
            tool_calls = last['tool_calls']
    return ChatResult(
        content=task.get('content') or '',
        thinking=task.get('thinking') or '',
        tool_calls=tool_calls,
        usage=dict(usage) if isinstance(usage, dict) else {},
        finish_reason=terminal_finish_reason(task),
        stream_state=(
            str(task['streamState']) if task.get('streamState') else None),
        error=task.get('error'),
        task_id=task.get('id') or '',
        status=task.get('status') or '',
        raw_task=task,
    )


def run_chat_sync(
    messages: list,
    *,
    user_id: int,
    model: str = '',
    config: dict | None = None,
    timeout_s: float = 600.0,
    **knobs,
) -> ChatResult:
    """Run one chat turn in-process and block until it is terminal.

    ``knobs`` are forwarded to :func:`build_chat_config`
    (``max_tokens`` / ``temperature`` / ``tools`` / ``response_format`` /
    ``thinking_depth`` / ``user``).

    Raises ``TimeoutError`` if the task does not reach a terminal state
    within ``timeout_s``.
    """
    cfg = build_chat_config(model, config, **knobs)
    task = _start_task(messages, cfg, user_id=user_id)

    deadline = time.time() + timeout_s
    poll = 0.05
    while task.get('status') not in _TERMINAL_STATES:
        if time.time() >= deadline:
            raise TimeoutError(
                f'chat turn {task.get("id", "?")[:8]} did not finish '
                f'within {timeout_s}s')
        time.sleep(poll)
        poll = min(poll * 1.2, 1.0)

    return _assemble_result(task)


def run_chat_stream(
    messages: list,
    *,
    user_id: int,
    model: str = '',
    config: dict | None = None,
    timeout_s: float = 600.0,
    **knobs,
) -> Iterator[dict]:
    """Run one chat turn in-process, yielding the native Tofu event dicts.

    Each yielded item is an event from ``task['events']`` (``delta`` /
    ``phase`` / ``tool_start`` / … / terminal ``done``) — the same
    vocabulary declared in ``GET /api/v1/capabilities`` ``events``. The
    generator returns after yielding the terminal ``done`` event (or when
    the task reaches a terminal state, whichever happens first).

    The caller owns transport re-shaping (e.g. the HTTP route turns these
    into OpenAI ``chat.completion.chunk`` frames). In-process consumers can
    switch on ``ev['type']`` directly.
    """
    cfg = build_chat_config(model, config, **knobs)
    task = _start_task(messages, cfg, user_id=user_id)

    deadline = time.time() + timeout_s
    cursor = 0
    from lib.task_replay import task_memory_replay_page
    while True:
        page = task_memory_replay_page(task, cursor)
        new_events = page.events
        cursor = page.next_cursor

        for ev in new_events:
            yield ev
            if ev.get('type') == 'done':
                return

        if task.get('status') in _TERMINAL_STATES and not new_events:
            # Orchestrator finished before/without us seeing a 'done' event
            # in the buffer — synthesise a terminal marker so consumers can
            # stop cleanly.
            yield {
                'type': 'done',
                'finishReason': terminal_finish_reason(task),
                'streamState': task.get('streamState'),
                'usage': task.get('usage') or {},
                'error': task.get('error'),
            }
            return

        if time.time() >= deadline:
            yield {
                'type': 'done',
                'finishReason': 'error',
                'error': {'kind': 'timeout',
                          'message': f'chat turn timed out after {timeout_s}s'},
            }
            return

        time.sleep(0.03)


__all__ = ['ChatResult', 'build_chat_config', 'run_chat_sync', 'run_chat_stream']
