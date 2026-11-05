"""Replay one recorded LLM script through the three transport seams.

``ReplaySession`` substitutes ONLY ``lib.llm.stream_chat`` /
``lib.llm.astream.async_stream_chat`` / ``lib.llm.chat.chat``; the real agent
loop, dispatch layer, tool pipeline, and persistence all run.  Calls are
matched in strict recording order: each incoming request's normalized digest
must equal the recorded step's digest, otherwise the scenario has drifted
from the recording and the session raises :class:`ScenarioDriftError`.  With
a ``request_store`` the error explains the drift: the JSON path and the
recorded vs current values of the first difference (``diff.py``).

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

from lib.llm.llm_errors import LocalRequestPreparationError
from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

from tests.support.llm_replay.diff import (
    describe_difference,
    request_differences,
)
from tests.support.llm_replay.normalize import (
    chat_request_proxy,
    json_digest,
    normalized_projection,
    projection_field_digests,
)
from tests.support.llm_replay.request_store import (
    RequestStoreError,
    load_recorded_field,
)
from tests.support.llm_replay.script import (
    ChatStep,
    ErrorStep,
    Step,
    StreamStep,
    load_script,
)

__all__ = ['ReplaySession', 'ScenarioDriftError']

_FOLLOWING_PATHS_REPORTED = 5


class ScenarioDriftError(LocalRequestPreparationError, AssertionError):
    """Local replay validation failed before any provider I/O.

    Preserve assertion identity for test callers while using the dispatcher's
    existing neutral failure boundary: retrying a stale fixture cannot repair
    it and must not cool down credentials or consume fallback attempts.
    """

    def __init__(self, message: str):
        super().__init__(message, stage='replay_validation')


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
        request_store=None,
    ):
        self.script_path = script_path
        self.cwd = str(cwd)
        self.verify = bool(verify)
        self.after_event = after_event
        self.request_store = request_store
        self.steps: tuple[Step, ...] = load_script(script_path)
        self._cursor = 0
        self._lock = threading.Lock()

    # ── sequence matching ────────────────────────────────────────────

    def _drift_report(self, step: Step, current: dict) -> str:
        recorded_digests = step.field_digests
        if not recorded_digests:
            return 'the fixture carries no per-field digests'
        current_digests = projection_field_digests(current)
        fields = [field for field in current
                  if current_digests[field] != recorded_digests.get(field)]
        if not fields:
            return 'per-field digests agree but the wholesale digest differs'
        report = f'differing fields: {", ".join(fields)}'
        if self.request_store is None:
            return f'{report}; no request store holds the recorded values'
        try:
            recorded = {
                field: load_recorded_field(
                    self.request_store, field, recorded_digests.get(field))
                for field in fields}
        except RequestStoreError as error:
            return f'{report}; recorded values unavailable: {error}'
        differences = request_differences(
            recorded, {field: current[field] for field in fields})
        report += (f'; {len(differences)} difference(s); first: '
                   f'{describe_difference(differences[0])}')
        following = [difference.path for difference in
                     differences[1:1 + _FOLLOWING_PATHS_REPORTED]]
        if following:
            more = len(differences) - 1 - len(following)
            report += '; then: ' + ', '.join(following) + (
                f' and {more} more' if more else '')
        return report

    def _advance(self, body) -> tuple[Step, int]:
        with self._lock:
            index = self._cursor
            if index >= len(self.steps):
                raise ScenarioDriftError(
                    f'script exhausted at call {index + 1} — re-record the '
                    f'scenario')
            step = self.steps[index]
            if self.verify:
                current = normalized_projection(body, cwd=self.cwd)
                if json_digest(current) != step.request_digest:
                    raise ScenarioDriftError(
                        f'step {index}: request digest mismatch — the '
                        f'scenario drifted from the recording '
                        f'({self._drift_report(step, current)}). Re-record '
                        f'the scenario if the change is intentional '
                        f'(tests/AGENTS.md "LLM record/replay").')
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
        """Patch the three transport seams and the public nonstream export.

        The package explicitly re-exports the lightweight ``chat`` function.
        Patch both that binding and the child module's function so direct
        transport callers and dispatch callers share the same replay session.
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
