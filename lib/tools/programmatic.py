"""Production contracts for Programmatic Tool Calling (PTC).

The ordinary tool runtime returns canonical text.  PTC JavaScript needs a
predictable JSON object, so every opted-in tool exposes the same lossless text
envelope.  Eligibility is separately and explicitly declared on ``ToolSpec``;
it must never be inferred from the broader retry/dedup partition.

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
earns a typed, retryable interpreter error, and the read-only latch plus the
hard call/byte ceilings bound any damage.  ``TOFU_PTC_TIER=batch`` remains
as an operator/benchmark override that strips the ``program`` parameter and
advertises only the parallel ``calls[]`` form.
"""

from __future__ import annotations

import json
import os
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

#: Shared tail of both local-backend guidance shapes.
_DIRECT_CALL_SUFFIX = (
    'Use direct calls for semantic judgment, writes, and approvals.')

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
    """Return byte-stable read-only routing text for the local backend.

    The text is spliced into the provider-bound ``execute_tools`` schema, which
    is part of the cached request prefix.  It therefore depends only on the
    stable tier. ``eligible`` is accepted for caller compatibility and for the
    explicit authority boundary, but names are deliberately not interpolated:
    both changing visibility and a growing serial-read chain are per-round
    state that would invalidate the provider's prompt-cache prefix.  Execution
    still checks the task-owned eligible set independently.
    """
    del eligible
    allowlist_note = (
        'PTC may call only task-approved read-only tools returned by '
        'search_tools.')
    if str(tier or '').strip().lower() == 'program':
        return (
            f'{allowlist_note} Use one program for dependent reads and compact JSON; '
            'batch independent reads with calls execution=parallel. '
            'Do not continue a serial chain of dependent direct reads. '
            'Writes, approvals, and judgment stay direct.')
    return (
        f'{allowlist_note} Batch independent reads into one calls array with '
        f'execution=parallel. {_DIRECT_CALL_SUFFIX}')


def eligible_programmatic_tool_names() -> set[str]:
    """Return explicitly reviewed built-in tool names.

    Third-party plugins remain direct-only until the plugin trust/approval
    boundary has an equally explicit PTC review mechanism.
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


__all__ = [
    'ACTIVE_PROGRAMMATIC_MODES',
    'PROGRAMMATIC_MAX_CALLS',
    'PROGRAMMATIC_MAX_CONCURRENT_CALLS',
    'PROGRAMMATIC_MAX_OUTPUT_BYTES',
    'PROGRAMMATIC_MAX_CONTINUATIONS',
    'eligible_programmatic_tool_names',
    'encode_programmatic_output',
    'local_ptc_guidance',
    'programmatic_output_schema',
    'programmatic_tier',
    'resolve_programmatic_backend',
    'truncate_utf8',
]
