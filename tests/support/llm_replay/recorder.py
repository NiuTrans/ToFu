"""Record real LLM transport traffic into a replay script.

``Recorder`` wraps the same three transport callables the replayer
substitutes, tees (normalized digest, callback events in order, final result
or raised exception) to JSONL, then delegates to the REAL function.  Live
record mode is enabled ONLY when ``TOFU_LLM_REPLAY=record`` AND
``TOFU_PARITY_LIVE=1``; it writes ``script.next.jsonl`` next to the scenario
fixture and NEVER overwrites a committed ``script.jsonl`` — a new generation
is reviewed and renamed by hand.  Everywhere else the mode is ``off`` and
nothing is recorded.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from lib.llm.stream_result import ensure_provider_stream_result

from tests.support.llm_replay.normalize import (
    chat_request_proxy,
    normalized_field_digests,
    normalized_request_digest,
)
from tests.support.llm_replay.script import _EXCEPTION_ATTR_ALLOWLIST

__all__ = ['Recorder', 'RECORD_OUTPUT_NAME']

#: Record output lives next to (never over) the committed fixture.
RECORD_OUTPUT_NAME = 'script.next.jsonl'


def _exception_attributes(error: BaseException) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for key in _EXCEPTION_ATTR_ALLOWLIST:
        value = getattr(error, key, None)
        if value is None or isinstance(value, (str, int, float, bool)):
            if value is not None:
                attributes[key] = value
    return attributes


def _serialize_result(result) -> dict[str, Any]:
    return {
        'message': result.message,
        'compatibility_finish_reason': result.compatibility_finish_reason,
        'usage': result.usage,
        'state': result.state.value,
        'provider_finish_reason': result.provider_finish_reason,
        'saw_done': result.saw_done,
        'saw_finish_reason': result.saw_finish_reason,
        'malformed_frame_count': result.malformed_frame_count,
    }


class Recorder:
    """Tee one transport triple into ``out_path`` (JSONL)."""

    def __init__(self, out_path, *, cwd: str):
        self.out_path = Path(out_path)
        self.cwd = str(cwd)
        self._lock = threading.Lock()

    def _append(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            with self.out_path.open('a', encoding='utf-8') as handle:
                handle.write(line + '\n')

    def _identity(self, body) -> dict[str, Any]:
        return {
            'request_digest': normalized_request_digest(body, cwd=self.cwd),
            'field_digests': normalized_field_digests(body, cwd=self.cwd),
        }

    # ── stream ───────────────────────────────────────────────────────

    def wrap_stream_chat(self, real_stream_chat):
        """Return a ``stream_chat``-compatible wrapper that tees traffic."""

        def stream_chat(body, *, on_thinking=None, on_content=None,
                        on_tool_call_ready=None, on_attempt_restart=None,
                        abort_check=None, **kwargs):
            events: list[dict[str, Any]] = []

            def _on_thinking(text):
                events.append({'t': 'thinking', 'text': text})
                if on_thinking is not None:
                    on_thinking(text)

            def _on_content(text):
                events.append({'t': 'content', 'text': text})
                if on_content is not None:
                    on_content(text)

            def _on_tool_call_ready(call):
                events.append({'t': 'tool_call', 'call': dict(call)})
                if on_tool_call_ready is not None:
                    on_tool_call_ready(call)

            def _on_attempt_restart(*args, **kw):
                reason = kw.get('reason')
                if reason is None and args:
                    reason = args[0]
                events.append({'t': 'attempt_restart',
                               'reason': str(reason or '')})
                if on_attempt_restart is not None:
                    on_attempt_restart(*args, **kw)

            identity = self._identity(body)
            try:
                result = ensure_provider_stream_result(real_stream_chat(
                    body,
                    on_thinking=_on_thinking,
                    on_content=_on_content,
                    on_tool_call_ready=_on_tool_call_ready,
                    on_attempt_restart=_on_attempt_restart,
                    abort_check=abort_check,
                    **kwargs))
            except Exception as error:
                self._append({
                    'kind': 'error',
                    **identity,
                    'exception': type(error).__name__,
                    'message': str(error),
                    'attributes': _exception_attributes(error),
                })
                raise
            self._append({
                'kind': 'stream',
                **identity,
                'events': events,
                'result': _serialize_result(result),
            })
            return result

        return stream_chat

    def wrap_async_stream_chat(self, real_async_stream_chat):
        """Async twin of :meth:`wrap_stream_chat`."""

        async def async_stream_chat(body, *, on_thinking=None,
                                    on_content=None, on_tool_call_ready=None,
                                    on_attempt_restart=None, abort_check=None,
                                    **kwargs):
            events: list[dict[str, Any]] = []

            def _on_thinking(text):
                events.append({'t': 'thinking', 'text': text})
                if on_thinking is not None:
                    on_thinking(text)

            def _on_content(text):
                events.append({'t': 'content', 'text': text})
                if on_content is not None:
                    on_content(text)

            def _on_tool_call_ready(call):
                events.append({'t': 'tool_call', 'call': dict(call)})
                if on_tool_call_ready is not None:
                    on_tool_call_ready(call)

            def _on_attempt_restart(*args, **kw):
                reason = kw.get('reason')
                if reason is None and args:
                    reason = args[0]
                events.append({'t': 'attempt_restart',
                               'reason': str(reason or '')})
                if on_attempt_restart is not None:
                    on_attempt_restart(*args, **kw)

            identity = self._identity(body)
            try:
                result = ensure_provider_stream_result(
                    await real_async_stream_chat(
                        body,
                        on_thinking=_on_thinking,
                        on_content=_on_content,
                        on_tool_call_ready=_on_tool_call_ready,
                        on_attempt_restart=_on_attempt_restart,
                        abort_check=abort_check,
                        **kwargs))
            except Exception as error:
                self._append({
                    'kind': 'error',
                    **identity,
                    'exception': type(error).__name__,
                    'message': str(error),
                    'attributes': _exception_attributes(error),
                })
                raise
            self._append({
                'kind': 'stream',
                **identity,
                'events': events,
                'result': _serialize_result(result),
            })
            return result

        return async_stream_chat

    # ── non-streaming chat ───────────────────────────────────────────

    def wrap_chat(self, real_chat):
        """Return a ``chat``-compatible wrapper that tees the exchange."""

        def chat(messages, model=None, **kwargs):
            proxy = chat_request_proxy(messages, model, kwargs.get('extra'))
            identity = self._identity(proxy)
            try:
                content, usage = real_chat(messages, model=model, **kwargs)
            except Exception as error:
                self._append({
                    'kind': 'error',
                    **identity,
                    'exception': type(error).__name__,
                    'message': str(error),
                    'attributes': _exception_attributes(error),
                })
                raise
            self._append({
                'kind': 'chat',
                **identity,
                'content': content,
                'usage': usage,
            })
            return content, usage

        return chat

    # ── installation ─────────────────────────────────────────────────

    def install(self, monkeypatch, *, stream_chat=None,
                async_stream_chat=None, chat=None) -> 'Recorder':
        """Patch the three transport seams with tee-ing wrappers.

        The wrapped functions default to the REAL transports (live record
        mode); tests may inject deterministic fakes for offline fixture
        authoring.  Patch order mirrors ``ReplaySession.install`` (submodule
        imports first, the ``lib.llm.chat`` package facade last).
        """
        import importlib
        astream_mod = importlib.import_module('lib.llm.astream')
        chat_mod = importlib.import_module('lib.llm.chat')
        import lib.llm as llm_pkg
        real_stream = stream_chat or llm_pkg.stream_chat
        real_async = async_stream_chat or astream_mod.async_stream_chat
        real_chat = chat or chat_mod.chat
        chat_wrapper = self.wrap_chat(real_chat)
        monkeypatch.setattr('lib.llm.stream_chat',
                            self.wrap_stream_chat(real_stream))
        monkeypatch.setattr(astream_mod, 'async_stream_chat',
                            self.wrap_async_stream_chat(real_async))
        monkeypatch.setattr(chat_mod, 'chat', chat_wrapper)
        monkeypatch.setattr('lib.llm.chat', chat_wrapper)
        return self
