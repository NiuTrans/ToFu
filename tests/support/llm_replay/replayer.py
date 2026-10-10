"""Replay one recorded LLM script through the three transport seams.

``ReplaySession`` substitutes ONLY ``lib.llm.stream_chat`` /
``lib.llm.astream.async_stream_chat`` / ``lib.llm.chat.chat``; the real agent
loop, dispatch layer, tool pipeline, and persistence all run.  Calls are
matched in strict recording order: each incoming request's normalized digest
must equal the recorded step's digest, otherwise the scenario has drifted
from the recording and the session raises :class:`ScenarioDriftError`.

Abort semantics mirror ``lib/llm/stream.py``: the real stream polls
``abort_check`` as chunks arrive and, on a mid-stream owner cancel, stops
delivering deltas and settles the attempt as ``client_aborted`` (it does NOT
raise from the in-loop check).  The replayer checks ``abort_check`` before
delivering each recorded event and, once tripped, returns a partial
``client_aborted`` result built from the events delivered so far.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

from tests.support.llm_replay.normalize import (
    chat_request_proxy,
    normalized_field_digests,
    normalized_request_digest,
)
from tests.support.llm_replay.script import (
    ChatStep,
    ErrorStep,
    Step,
    StreamStep,
    load_script,
)

__all__ = ['ReplaySession', 'ScenarioDriftError']

class ScenarioDriftError(AssertionError):
    """The live request sequence diverged from the recorded script."""


class ReplaySession:
    """Strict-sequence replayer for one ``script.jsonl``.

    ``verify=False`` plays the script without digest checks; it exists ONLY
    for offline fixture authoring (the recorder tees the real digests), never
    for scenario assertions.
    """

    def __init__(
        self,
        script_path,
        cwd: str,
        *,
        verify: bool = True,
        after_event: Callable[[int, dict], None] | None = None,
    ):
        self.script_path = script_path
        self.cwd = str(cwd)
        self.verify = bool(verify)
        self.after_event = after_event
        self.steps: tuple[Step, ...] = load_script(script_path)
        self._cursor = 0
        self._lock = threading.Lock()

    # ── sequence matching ────────────────────────────────────────────

    def _drift_hint(self, step: Step, body) -> str:
        recorded = step.field_digests
        if not recorded:
            return ('first differing top-level field: unknown (the fixture '
                    'carries no per-field digests)')
        current = normalized_field_digests(body, cwd=self.cwd)
        for field in ('model', 'messages', 'system', 'tools'):
            if current.get(field) != recorded.get(field):
                return f'first differing top-level field: {field!r}'
        return ('first differing top-level field: unknown (per-field '
                'digests agree but the wholesale digest differs)')

    def _advance(self, body) -> tuple[Step, int]:
        with self._lock:
            index = self._cursor
            if index >= len(self.steps):
                raise ScenarioDriftError(
                    f'script exhausted at call {index + 1} — re-record the '
                    f'scenario')
            step = self.steps[index]
            if self.verify:
                digest = normalized_request_digest(body, cwd=self.cwd)
                if digest != step.request_digest:
                    raise ScenarioDriftError(
                        f'step {index}: request digest mismatch — the '
                        f'scenario drifted from the recording '
                        f'({self._drift_hint(step, body)}). Re-record the '
                        f'scenario if the change is intentional.')
            self._cursor = index + 1
        return step, index

    # ── stream replay ────────────────────────────────────────────────

    def _deliver_stream(
        self,
        step: StreamStep,
        index: int,
        *,
        on_thinking,
        on_content,
        on_tool_call_ready,
        on_attempt_restart,
        abort_check,
    ) -> ProviderStreamResult:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        for event in step.events:
            # Mirror lib/llm/stream.py's in-loop abort check: the owner
            # cancel seam settles the attempt as client_aborted instead of
            # delivering the rest of the recorded stream.
            if abort_check is not None and abort_check():
                message: dict[str, Any] = {
                    'role': 'assistant',
                    'content': ''.join(content_parts),
                }
                if thinking_parts:
                    message['reasoning_content'] = ''.join(thinking_parts)
                return ProviderStreamResult(
                    message=message,
                    compatibility_finish_reason='stop',
                    usage=None,
                    state=ProviderStreamState.CLIENT_ABORTED,
                )
            kind = event['t']
            if kind == 'thinking':
                thinking_parts.append(event['text'])
                if on_thinking is not None:
                    on_thinking(event['text'])
            elif kind == 'content':
                content_parts.append(event['text'])
                if on_content is not None:
                    on_content(event['text'])
            elif kind == 'tool_call':
                if on_tool_call_ready is not None:
                    on_tool_call_ready(dict(event['call']))
            elif kind == 'attempt_restart':
                if on_attempt_restart is not None:
                    on_attempt_restart(reason=event['reason'])
            if self.after_event is not None:
                self.after_event(index, event)
        return step.result

    # ── the three transport callables ────────────────────────────────

    def stream_chat(self, body, *, on_thinking=None, on_content=None,
                    on_tool_call_ready=None, abort_check=None, log_prefix='',
                    api_key=None, base_url=None, extra_headers=None,
                    api_protocol='openai', oauth='', adapter=None,
                    on_attempt_restart=None, on_first_byte_wait=None,
                    on_stream_wait=None,
                    owner_user_id=None) -> ProviderStreamResult:
        """Replay one streaming call; signature matches ``stream_chat``."""
        step, index = self._advance(body)
        if isinstance(step, ErrorStep):
            raise step.exception
        if not isinstance(step, StreamStep):
            raise ScenarioDriftError(
                f'step {index}: script holds a {type(step).__name__} but '
                f'the caller requested a stream')
        return self._deliver_stream(
            step, index,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            on_attempt_restart=on_attempt_restart, abort_check=abort_check)

    async def async_stream_chat(self, body, *, on_thinking=None,
                                on_content=None, on_tool_call_ready=None,
                                on_attempt_restart=None, abort_check=None,
                                log_prefix='', api_key=None, base_url=None,
                                extra_headers=None, api_protocol='openai',
                                oauth='', adapter=None,
                                on_first_byte_wait=None, on_stream_wait=None,
                                owner_user_id=None) -> ProviderStreamResult:
        """Async twin of :meth:`stream_chat` (callback-based, never blocks)."""
        step, index = self._advance(body)
        if isinstance(step, ErrorStep):
            raise step.exception
        if not isinstance(step, StreamStep):
            raise ScenarioDriftError(
                f'step {index}: script holds a {type(step).__name__} but '
                f'the caller requested a stream')
        return self._deliver_stream(
            step, index,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            on_attempt_restart=on_attempt_restart, abort_check=abort_check)

    def chat(self, messages, model=None, **kwargs):
        """Replay one non-streaming call; returns ``(content, usage)``."""
        proxy = chat_request_proxy(messages, model, kwargs.get('extra'))
        step, index = self._advance(proxy)
        if isinstance(step, ErrorStep):
            raise step.exception
        if not isinstance(step, ChatStep):
            raise ScenarioDriftError(
                f'step {index}: script holds a {type(step).__name__} but '
                f'the caller requested a non-streaming chat')
        return step.content, step.usage

    # ── installation ─────────────────────────────────────────────────

    def install(self, monkeypatch) -> 'ReplaySession':
        """Patch the three lazy-import transport seams.

        ``lib.llm.chat`` is BOTH a submodule and a lazy package-facade
        function export: importing the submodule rebinds the package
        attribute to the module object, so the submodules are imported first
        (object-form patches for their attributes) and the package facade
        string patch lands LAST — otherwise ``from lib.llm import chat``
        could still resolve to the unpatched module.
        """
        import importlib
        astream_mod = importlib.import_module('lib.llm.astream')
        chat_mod = importlib.import_module('lib.llm.chat')
        monkeypatch.setattr('lib.llm.stream_chat', self.stream_chat)
        monkeypatch.setattr(astream_mod, 'async_stream_chat',
                            self.async_stream_chat)
        monkeypatch.setattr(chat_mod, 'chat', self.chat)
        monkeypatch.setattr('lib.llm.chat', self.chat)
        return self
