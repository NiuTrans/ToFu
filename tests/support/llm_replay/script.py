"""Load and validate one LLM record/replay script (``script.jsonl``).

One JSON object per line.  Kinds:

* ``stream`` — one streaming provider call::

    {"kind": "stream",
     "request_digest": "<sha256>",
     "field_digests": {"model": "...", "messages": "...", "system": "...",
                       "tools": "..."},
     "events": [{"t": "thinking", "text": "..."}
                | {"t": "content", "text": "..."}
                | {"t": "tool_call", "call": {...}}
                | {"t": "attempt_restart", "reason": "..."}],
     "result": {"message": {...}, "compatibility_finish_reason": "stop",
                "usage": {...}, "state": "provider_finished",
                "provider_finish_reason": "stop", "saw_done": true,
                "saw_finish_reason": true, "malformed_frame_count": 0}}

* ``error`` — one provider call that raised before/within the transport.
  This is the documented way to inject pre-chunk failures (429, 5xx)::

    {"kind": "error", "request_digest": "...",
     "exception": "RateLimitError", "message": "slow down",
     "attributes": {"status_code": 429}}

* ``chat`` — one non-streaming completion::

    {"kind": "chat", "request_digest": "...", "content": "...",
     "usage": {...}}

Every line is validated at load: unknown kinds, unknown exception names,
malformed events/results, and ``ProviderStreamResult`` constructibility all
fail loud with the line number.  ``field_digests`` is optional diagnostic
metadata written by the recorder; it lets the replayer name the first
differing top-level field on drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

__all__ = [
    'ChatStep',
    'ErrorStep',
    'ScriptError',
    'StreamStep',
    'Step',
    'load_script',
]

_STEP_KINDS = ('stream', 'error', 'chat')
_EVENT_KINDS = ('thinking', 'content', 'tool_call', 'attempt_restart')


class ScriptError(ValueError):
    """One malformed script line; the message always names the line."""


@dataclass(frozen=True)
class StreamStep:
    request_digest: str
    events: tuple[dict[str, Any], ...]
    result: ProviderStreamResult
    field_digests: dict[str, str] | None = None


@dataclass(frozen=True)
class ErrorStep:
    request_digest: str
    exception: Exception
    field_digests: dict[str, str] | None = None


@dataclass(frozen=True)
class ChatStep:
    request_digest: str
    content: str
    usage: dict[str, Any] | None
    field_digests: dict[str, str] | None = None


Step = Union[StreamStep, ErrorStep, ChatStep]

# Optional per-exception attributes the recorder captures and the replayer
# passes back to the constructor when it accepts them.
_EXCEPTION_ATTR_ALLOWLIST = (
    'status_code',
    'is_quota',
    'is_gateway',
    'reason',
    'retry_after_s',
    'url',
)


def _resolve_exception_class(name: str):
    """Resolve one exception name against the transport error vocabulary."""
    import lib.llm as llm_pkg
    import lib.llm_errors as llm_errors
    for module in (llm_pkg, llm_errors):
        candidate = getattr(module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return candidate
    return None


def build_exception(name: str, message: str,
                    attributes: dict[str, Any] | None = None) -> Exception:
    """Reconstruct one recorded exception; unknown names fail loud."""
    cls = _resolve_exception_class(name)
    if cls is None:
        raise ScriptError(
            f'unknown exception {name!r} — not exported by lib.llm or '
            f'lib.llm_errors')
    import inspect
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        parameters = {}
    for key, value in (attributes or {}).items():
        if key in _EXCEPTION_ATTR_ALLOWLIST and key in parameters:
            kwargs[key] = value
    try:
        return cls(message, **kwargs)
    except TypeError:
        return cls(message)


def _build_stream_result(raw: Any, *, where: str) -> ProviderStreamResult:
    if not isinstance(raw, dict):
        raise ScriptError(f'{where}: "result" must be an object')
    missing = {'message', 'state'} - set(raw)
    if missing:
        raise ScriptError(
            f'{where}: "result" misses required keys {sorted(missing)}')
    try:
        state = ProviderStreamState(str(raw['state']))
    except ValueError as error:
        raise ScriptError(f'{where}: unknown stream state: {error}') from error
    try:
        # evidence=None: _legacy_evidence derives consistent evidence from
        # (state, usage, saw_done, saw_finish_reason, malformed_frame_count),
        # so the strict __post_init__ invariants are checked here, at load.
        return ProviderStreamResult(
            message=dict(raw['message']),
            compatibility_finish_reason=str(
                raw.get('compatibility_finish_reason') or 'stop'),
            usage=raw.get('usage'),
            state=state,
            provider_finish_reason=raw.get('provider_finish_reason'),
            saw_done=bool(raw.get('saw_done')),
            saw_finish_reason=bool(raw.get('saw_finish_reason')),
            malformed_frame_count=int(raw.get('malformed_frame_count') or 0),
            evidence=None,
        )
    except (TypeError, ValueError) as error:
        raise ScriptError(
            f'{where}: result is not a constructible ProviderStreamResult: '
            f'{error}') from error


def _validate_events(raw: Any, *, where: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list):
        raise ScriptError(f'{where}: "events" must be a list')
    events: list[dict[str, Any]] = []
    for position, event in enumerate(raw):
        event_where = f'{where} event[{position}]'
        if not isinstance(event, dict):
            raise ScriptError(f'{event_where}: must be an object')
        kind = event.get('t')
        if kind not in _EVENT_KINDS:
            raise ScriptError(
                f'{event_where}: unknown event kind {kind!r} — expected one '
                f'of {_EVENT_KINDS}')
        if kind in ('thinking', 'content') and not isinstance(
                event.get('text'), str):
            raise ScriptError(f'{event_where}: "text" must be a string')
        if kind == 'tool_call' and not isinstance(event.get('call'), dict):
            raise ScriptError(f'{event_where}: "call" must be an object')
        if kind == 'attempt_restart' and not isinstance(
                event.get('reason'), str):
            raise ScriptError(f'{event_where}: "reason" must be a string')
        events.append(dict(event))
    return tuple(events)


def parse_step(line: str, *, where: str) -> Step:
    """Parse and validate one script line."""
    try:
        row = json.loads(line)
    except json.JSONDecodeError as error:
        raise ScriptError(f'{where}: invalid JSON: {error}') from error
    if not isinstance(row, dict):
        raise ScriptError(f'{where}: each line must be a JSON object')
    kind = row.get('kind')
    if kind not in _STEP_KINDS:
        raise ScriptError(
            f'{where}: unknown kind {kind!r} — expected one of {_STEP_KINDS}')
    digest = row.get('request_digest')
    if not isinstance(digest, str) or not digest:
        raise ScriptError(f'{where}: "request_digest" must be a string')
    field_digests = row.get('field_digests')
    if field_digests is not None and not isinstance(field_digests, dict):
        raise ScriptError(f'{where}: "field_digests" must be an object')
    if kind == 'stream':
        return StreamStep(
            request_digest=digest,
            events=_validate_events(row.get('events'), where=where),
            result=_build_stream_result(row.get('result'), where=where),
            field_digests=field_digests,
        )
    if kind == 'error':
        name = row.get('exception')
        if not isinstance(name, str) or not name:
            raise ScriptError(f'{where}: "exception" must name a class')
        attributes = row.get('attributes')
        if attributes is not None and not isinstance(attributes, dict):
            raise ScriptError(f'{where}: "attributes" must be an object')
        try:
            exception = build_exception(
                name, str(row.get('message') or ''), attributes)
        except ScriptError as error:
            raise ScriptError(f'{where}: {error}') from error
        return ErrorStep(
            request_digest=digest,
            exception=exception,
            field_digests=field_digests,
        )
    content = row.get('content')
    if not isinstance(content, str):
        raise ScriptError(f'{where}: "content" must be a string')
    usage = row.get('usage')
    if usage is not None and not isinstance(usage, dict):
        raise ScriptError(f'{where}: "usage" must be an object')
    return ChatStep(
        request_digest=digest,
        content=content,
        usage=usage,
        field_digests=field_digests,
    )


def load_script(path) -> tuple[Step, ...]:
    """Load one ``script.jsonl``; every line is validated eagerly."""
    script_path = Path(path)
    where0 = f'{script_path}'
    if not script_path.is_file():
        raise ScriptError(f'{where0}: no such script file')
    steps: list[Step] = []
    with script_path.open('r', encoding='utf-8') as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            steps.append(parse_step(stripped, where=f'{where0}:{lineno}'))
    return tuple(steps)
