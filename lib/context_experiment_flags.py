"""Validated, request-local switches for context-efficiency experiments.

These switches are intentionally separate from the conversation-sticky cost
experiment.  Each optimization can be enabled independently for a benchmark
arm, and an absent/invalid value always falls back to the shipped behavior.
No process-global state is mutated, so concurrent arms cannot contaminate one
another.
"""

from __future__ import annotations

from typing import Any


DEFAULT_CONTEXT_EXPERIMENT_FLAGS = {
    'cache': {'gpt56BreakpointMode': 'explicit'},
    'tools': {
        'nativeExposure': 'routed',
        'programmaticCalling': 'auto',
        # Public GPT-5.6 Responses requests automatically defer only the
        # non-pinned portion of a large tool catalog.  Frontend/caller-selected
        # tools are carried separately as an immutable direct-exposure set.
        'toolSearch': 'auto',
        # Schema exposure is not execution authority.  In the default mode,
        # every task-available tool remains searchable/callable by its exact
        # name even when a composer toggle keeps its schema off the wire.
        # ``selected_only`` preserves the former opt-in authority semantics.
        'executionScope': 'available',
    },
    'responses': {
        'transport': 'sse',
        'reasoningMode': 'standard',
        'verbosity': 'medium',
        'imageDetail': 'auto',
        'promptProfile': 'auto',
        'multiAgent': 'auto',
        'maxConcurrentSubagents': 3,
    },
    'compaction': {'evidenceLedger': False},
}

_VALID_BREAKPOINT_MODES = frozenset({'implicit', 'explicit'})
_VALID_NATIVE_EXPOSURE = frozenset({'full', 'routed'})
_VALID_PROGRAMMATIC_CALLING = frozenset({'off', 'auto'})
_VALID_TOOL_SEARCH = frozenset({'off', 'auto', 'native', 'local'})
_VALID_TOOL_EXECUTION_SCOPE = frozenset({'available', 'selected_only'})
_VALID_RESPONSES_TRANSPORT = frozenset({'sse', 'websocket'})
_VALID_REASONING_MODE = frozenset({'standard', 'pro'})
_VALID_VERBOSITY = frozenset({'low', 'medium', 'high'})
_VALID_IMAGE_DETAIL = frozenset({'auto', 'original'})
_VALID_PROMPT_PROFILE = frozenset({'auto', 'full', 'lean'})
_VALID_MULTI_AGENT = frozenset({'off', 'auto', 'read_only'})


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_context_experiment_flags(
        request_config: Any, *, strict: bool = False) -> dict:
    """Return the complete experiment switch set for one request.

    ``request_config`` is the normal task config.  The function reads only the
    four documented nested blocks and ignores unrelated keys.  With
    ``strict=True`` invalid values raise ``ValueError``; the hot request path
    uses the fail-safe defaults instead.
    """
    cfg = _mapping(request_config)
    cache = _mapping(cfg.get('cache'))
    tools = _mapping(cfg.get('tools'))
    responses = _mapping(cfg.get('responses'))
    compaction = _mapping(cfg.get('compaction'))

    breakpoint_mode = cache.get(
        'gpt56BreakpointMode',
        cfg.get('cache.gpt56BreakpointMode', 'explicit'))
    native_exposure = tools.get(
        'nativeExposure', cfg.get('tools.nativeExposure', 'routed'))
    programmatic = tools.get(
        'programmaticCalling', cfg.get('tools.programmaticCalling', 'auto'))
    tool_search = tools.get(
        'toolSearch', cfg.get('tools.toolSearch', 'auto'))
    execution_scope = tools.get(
        'executionScope', cfg.get('tools.executionScope', 'available'))
    transport = responses.get(
        'transport', cfg.get('responses.transport', 'sse'))
    reasoning_mode = responses.get(
        'reasoningMode', cfg.get('responses.reasoningMode', 'standard'))
    verbosity = responses.get(
        'verbosity', cfg.get('responses.verbosity', 'medium'))
    image_detail = responses.get(
        'imageDetail', cfg.get('responses.imageDetail', 'auto'))
    prompt_profile = responses.get(
        'promptProfile', cfg.get('responses.promptProfile', 'auto'))
    multi_agent = responses.get(
        'multiAgent', cfg.get('responses.multiAgent', 'auto'))
    max_subagents = responses.get(
        'maxConcurrentSubagents',
        cfg.get('responses.maxConcurrentSubagents', 3))
    evidence = compaction.get(
        'evidenceLedger', cfg.get('compaction.evidenceLedger', False))

    checks = (
        ('cache.gpt56BreakpointMode', breakpoint_mode,
         _VALID_BREAKPOINT_MODES, 'explicit'),
        ('tools.nativeExposure', native_exposure,
         _VALID_NATIVE_EXPOSURE, 'routed'),
        ('tools.programmaticCalling', programmatic,
         _VALID_PROGRAMMATIC_CALLING, 'auto'),
        ('tools.toolSearch', tool_search, _VALID_TOOL_SEARCH, 'auto'),
        ('tools.executionScope', execution_scope,
         _VALID_TOOL_EXECUTION_SCOPE, 'available'),
        ('responses.transport', transport,
         _VALID_RESPONSES_TRANSPORT, 'sse'),
        ('responses.reasoningMode', reasoning_mode,
         _VALID_REASONING_MODE, 'standard'),
        ('responses.verbosity', verbosity, _VALID_VERBOSITY, 'medium'),
        ('responses.imageDetail', image_detail,
         _VALID_IMAGE_DETAIL, 'auto'),
        ('responses.promptProfile', prompt_profile,
         _VALID_PROMPT_PROFILE, 'auto'),
        ('responses.multiAgent', multi_agent, _VALID_MULTI_AGENT, 'auto'),
    )
    normalized: dict[str, str] = {}
    for field, raw, allowed, default in checks:
        value = str(raw or '').strip().lower()
        if value not in allowed:
            if strict:
                raise ValueError(
                    f'{field} must be one of: {", ".join(sorted(allowed))}')
            value = default
        normalized[field] = value

    if not isinstance(evidence, bool):
        if strict:
            raise ValueError('compaction.evidenceLedger must be a boolean')
        evidence = False

    try:
        max_subagents = int(max_subagents)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(
                'responses.maxConcurrentSubagents must be an integer')
        max_subagents = 3
    if not 1 <= max_subagents <= 8:
        if strict:
            raise ValueError(
                'responses.maxConcurrentSubagents must be between 1 and 8')
        max_subagents = 3

    return {
        'cache': {
            'gpt56BreakpointMode': normalized[
                'cache.gpt56BreakpointMode'],
        },
        'tools': {
            'nativeExposure': normalized['tools.nativeExposure'],
            'programmaticCalling': normalized[
                'tools.programmaticCalling'],
            'toolSearch': normalized['tools.toolSearch'],
            'executionScope': normalized['tools.executionScope'],
        },
        'responses': {
            'transport': normalized['responses.transport'],
            'reasoningMode': normalized['responses.reasoningMode'],
            'verbosity': normalized['responses.verbosity'],
            'imageDetail': normalized['responses.imageDetail'],
            'promptProfile': normalized['responses.promptProfile'],
            'multiAgent': normalized['responses.multiAgent'],
            'maxConcurrentSubagents': max_subagents,
        },
        'compaction': {'evidenceLedger': evidence},
    }


def context_experiment_arm(request_config: Any) -> dict:
    """Flatten the normalized switches for compact telemetry/JSONL records."""
    flags = normalize_context_experiment_flags(request_config)
    return {
        'gpt56BreakpointMode': flags['cache']['gpt56BreakpointMode'],
        'nativeExposure': flags['tools']['nativeExposure'],
        'evidenceLedger': flags['compaction']['evidenceLedger'],
        'programmaticCalling': flags['tools']['programmaticCalling'],
        'toolSearch': flags['tools']['toolSearch'],
        'executionScope': flags['tools']['executionScope'],
        'responsesTransport': flags['responses']['transport'],
        'reasoningMode': flags['responses']['reasoningMode'],
        'verbosity': flags['responses']['verbosity'],
        'imageDetail': flags['responses']['imageDetail'],
        'promptProfile': flags['responses']['promptProfile'],
        'multiAgent': flags['responses']['multiAgent'],
        'maxConcurrentSubagents': flags['responses'][
            'maxConcurrentSubagents'],
    }


__all__ = [
    'DEFAULT_CONTEXT_EXPERIMENT_FLAGS',
    'context_experiment_arm',
    'normalize_context_experiment_flags',
]
