"""Production contracts for Programmatic Tool Calling (PTC).

The ordinary tool runtime returns canonical text. Hosted PTC JavaScript needs
a predictable JSON object, so every opted-in tool exposes the same lossless
text envelope. Hosted eligibility is separately and explicitly declared on
``ToolSpec``; local ToolScript instead uses the task executable catalog and the
ordinary ToolContract/approval pipeline.

PTC is ONE semantic capability with TWO execution backends (mirroring the
Tool Search dual-backend precedent):

* ``native_openai`` — the hosted V8 runtime on the public OpenAI Responses
  API (GPT-5.6 family only).  The model's program runs upstream and its
  child calls stream back with ``caller.type='program'``.
* ``local`` — every other tool-capable model.  The model drives the
  ``execute_tools`` ToolScript interpreter instead; child calls stay
  server-side and only the compact program result re-enters the context.

The local backend exposes the SAME full ToolScript surface to every model:
small models are not demoted to a code-less form — a malformed program just
earns a typed, retryable interpreter error, and the catalog/schema/approval
checks plus hard call/byte ceilings bound execution.
``TOFU_PTC_TIER=batch`` remains
as an operator/benchmark override that strips the ``program`` parameter and
advertises only the parallel ``calls[]`` form.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any


# Hard application-side ceilings.  The hosted V8 runtime owns its own runtime
# timeout; these limits bound the client-owned calls and bytes that Tofu feeds
# back into one program, plus protocol continuations after program_output.
PROGRAMMATIC_MAX_CALLS = 16
PROGRAMMATIC_MAX_CONCURRENT_CALLS = 8
PROGRAMMATIC_MAX_OUTPUT_BYTES = 1_048_576
PROGRAMMATIC_MAX_CONTINUATIONS = 4


def programmatic_output_schema() -> dict[str, Any]:
    """Return a fresh exact schema for Tofu's canonical text envelope."""
    return {
        'type': 'object',
        'properties': {
            'content': {'type': 'string'},
            'truncated': {'type': 'boolean'},
        },
        'required': ['content', 'truncated'],
        'additionalProperties': False,
    }


_PROGRAMMATIC_TIERS = frozenset({'program', 'batch'})

#: Requested modes that activate a programmatic backend.  Shared by the
#: intent resolver, the orchestrator latch, and both wire converters so a
#: future mode can never drift across the four independent checkpoints.
ACTIVE_PROGRAMMATIC_MODES = frozenset({'auto', 'on'})

_LOCAL_CHILD_AUTHORITY_NOTE = (
    'Each child must name a task-executable tool and keeps ordinary '
    'schema, authority, and approval checks.')

def resolve_programmatic_backend(
        requested: str, *, protocol: str = '', model: str = '',
        responses_profile: str = '', base_url: str = '', oauth: str = '',
        eligible_present: bool = True) -> str:
    """Resolve ``programmaticCalling=auto|on`` into a concrete backend.

    Fail-closed: anything other than an explicit ``auto``/``on`` with at
    least one reviewed read-only tool resolves to ``off``.  ``native_openai``
    requires the full public-OpenAI chain (Responses protocol + GPT-5.6
    family + ``openai`` feature profile); every other tool-capable wire
    falls back to the local ToolScript channel.
    """
    if str(requested or '').strip().lower() not in ACTIVE_PROGRAMMATIC_MODES:
        return 'off'
    if not eligible_present:
        return 'off'
    profile = str(responses_profile or '').strip().lower()
    if not profile:
        from lib.llm.responses_features import (
            normalize_responses_feature_profile)
        profile = normalize_responses_feature_profile(
            '', protocol=protocol, base_url=base_url, oauth=oauth)
    if (str(protocol or '').strip().lower() == 'responses'
            and profile == 'openai'):
        from lib.model_info._openai_gpt56 import is_official_gpt56_model
        if is_official_gpt56_model(model):
            return 'native_openai'
    return 'local'


def programmatic_tier(model: str, *, provider_id: str = '') -> str:
    """Return the local-backend surface tier — ``program`` for every model.

    There is no model-size split: any tool-capable model may author bounded
    ToolScript reductions; the interpreter is sandboxed and answers malformed
    programs with typed, retryable errors.  ``TOFU_PTC_TIER`` is an
    operator/benchmark override (``batch`` strips the ``program`` parameter,
    leaving only the parallel ``calls[]`` form).  The model/provider_id
    arguments are kept for caller compatibility and are no longer consulted.
    """
    override = str(os.environ.get('TOFU_PTC_TIER') or '').strip().lower()
    if override in _PROGRAMMATIC_TIERS:
        return override
    return 'program'


def local_ptc_guidance(tier: str, eligible: list[str] | tuple | set) -> str:
    """Return byte-stable routing text for the local backend.

    The text is spliced into the provider-bound ``execute_tools`` schema, which
    is part of the cached request prefix.  It therefore depends only on the
    stable tier. ``eligible`` is accepted for hosted-PTC activation diagnostics
    and caller compatibility, but names are deliberately not interpolated:
    both changing visibility and a growing serial-read chain are per-round
    state that would invalidate the provider's prompt-cache prefix. Local
    execution independently checks the task executable catalog.
    """
    del eligible
    if str(tier or '').strip().lower() == 'program':
        return (
            'ToolScript may call any task-executable tool. '
            f'{_LOCAL_CHILD_AUTHORITY_NOTE} Use one program for dependent calls '
            'and compact JSON; batch independent calls with calls '
            'execution=parallel. Do not continue a serial chain.')
    return (
        f'{_LOCAL_CHILD_AUTHORITY_NOTE} Batch independent calls into one calls '
        'array with execution=parallel.')


def eligible_programmatic_tool_names() -> set[str]:
    """Return built-ins reviewed for hosted PTC and activation decisions.

    This set never narrows local ToolScript child calls. Third-party plugins
    remain hosted-PTC-direct-only until the plugin trust/approval boundary has
    an equally explicit review mechanism.
    """
    from lib.tools.registry import all_specs

    names: set[str] = set()
    for spec in all_specs():
        if spec.source == 'builtin':
            # Fail closed if a registry edit misspells or over-declares a name.
            names.update(spec.programmatic_tools.intersection(spec.provides))
    return names


def truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    """Clamp *value* without splitting a UTF-8 code point."""
    raw = value.encode('utf-8')
    if len(raw) <= max(0, max_bytes):
        return value, False
    if max_bytes <= 0:
        return '', True
    return raw[:max_bytes].decode('utf-8', errors='ignore'), True


def encode_programmatic_output(content: Any, *, max_bytes: int | None = None
                               ) -> tuple[str, int, bool]:
    """Encode one tool result as the exact PTC JSON envelope.

    Returns ``(json_text, consumed_utf8_bytes, truncated)`` so the caller can
    maintain a cumulative per-program byte budget without reparsing JSON.
    """
    text = (content if isinstance(content, str)
            else json.dumps(content if content is not None else '',
                            ensure_ascii=False))
    truncated = False
    if max_bytes is not None:
        text, truncated = truncate_utf8(text, max_bytes)
    consumed = len(text.encode('utf-8'))
    return (json.dumps({'content': text, 'truncated': truncated},
                       ensure_ascii=False), consumed, truncated)


class ProgrammaticResultBudget:
    """One-program, memory-bounded lane for local ToolScript child results.

    Model-visible and durable child receipts still pass through the ordinary
    tool-result budget.  This transient lane gives the local interpreter the
    post-hook result before L0 compaction so it can reduce data itself. Each
    synchronous batch reserves a deterministic share per call; parallel
    completion order therefore cannot decide which sibling consumes the
    program's 1 MiB allowance, and retained transient text never exceeds that
    allowance.
    """

    def __init__(self, max_bytes: int = PROGRAMMATIC_MAX_OUTPUT_BYTES):
        self._max_bytes = max(0, int(max_bytes))
        self._remaining = self._max_bytes
        self._raw_bytes = 0
        self._output_bytes = 0
        self._truncated_results = 0
        self._lock = threading.Lock()

    def begin_batch(self, call_ids: list[str]) -> "ProgrammaticResultBatch":
        ordered = list(dict.fromkeys(str(call_id or '') for call_id in call_ids))
        if not ordered:
            return ProgrammaticResultBatch(self, {})
        with self._lock:
            remaining = self._remaining
        share, extra = divmod(remaining, len(ordered))
        limits = {
            call_id: share + (1 if index < extra else 0)
            for index, call_id in enumerate(ordered)
        }
        return ProgrammaticResultBatch(self, limits)

    def _commit(self, *, raw_bytes: int, output_bytes: int,
                truncated_results: int) -> None:
        with self._lock:
            consumed = min(self._remaining, max(0, int(output_bytes)))
            self._remaining -= consumed
            self._raw_bytes += max(0, int(raw_bytes))
            self._output_bytes += consumed
            self._truncated_results += max(0, int(truncated_results))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                'maxBytes': self._max_bytes,
                'rawBytes': self._raw_bytes,
                'outputBytes': self._output_bytes,
                'remainingBytes': self._remaining,
                'truncatedResults': self._truncated_results,
            }


class ProgrammaticResultBatch:
    """Thread-safe transient sink for one ordered child-call batch."""

    def __init__(self, owner: ProgrammaticResultBudget,
                 limits: dict[str, int]):
        self._owner = owner
        self._limits = dict(limits)
        self._results: dict[str, dict[str, Any]] = {}
        self._finished = False
        self._lock = threading.Lock()

    def capture(self, call_id: str, content: Any) -> None:
        call_id = str(call_id or '')
        with self._lock:
            if self._finished or call_id in self._results \
                    or call_id not in self._limits:
                return
        text = (content if isinstance(content, str)
                else json.dumps(content if content is not None else '',
                                ensure_ascii=False, default=str))
        raw_bytes = len(text.encode('utf-8'))
        visible, truncated = truncate_utf8(text, self._limits[call_id])
        delivery = {
            'content': visible,
            'rawBytes': raw_bytes,
            'outputBytes': len(visible.encode('utf-8')),
            'truncated': bool(truncated),
        }
        with self._lock:
            if self._finished or call_id in self._results:
                return
            self._results[call_id] = delivery

    def result(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._results.get(str(call_id or ''))
            return dict(value) if isinstance(value, dict) else None

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            values = list(self._results.values())
        self._owner._commit(
            raw_bytes=sum(int(value.get('rawBytes') or 0) for value in values),
            output_bytes=sum(
                int(value.get('outputBytes') or 0) for value in values),
            truncated_results=sum(
                bool(value.get('truncated')) for value in values),
        )


__all__ = [
    'ACTIVE_PROGRAMMATIC_MODES',
    'PROGRAMMATIC_MAX_CALLS',
    'PROGRAMMATIC_MAX_CONCURRENT_CALLS',
    'PROGRAMMATIC_MAX_OUTPUT_BYTES',
    'PROGRAMMATIC_MAX_CONTINUATIONS',
    'ProgrammaticResultBatch',
    'ProgrammaticResultBudget',
    'eligible_programmatic_tool_names',
    'encode_programmatic_output',
    'local_ptc_guidance',
    'programmatic_output_schema',
    'programmatic_tier',
    'resolve_programmatic_backend',
    'truncate_utf8',
]
