"""Canonical metadata helpers for tool calls refused before execution.

Tool dispatchers, task finalization, replay projections, and browser rendering
all consume the same typed descriptor.  ``rejection`` is the public field;
``_rejected`` and result-level ``rejected`` remain read/write compatibility
aliases for conversations persisted before the public contract was added.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any


REJECTION_FIELD = 'rejection'
LEGACY_REJECTION_FIELD = '_rejected'
LEGACY_RESULT_REJECTION_FIELD = 'rejected'
UNAVAILABLE_TOOL_REJECTION_KIND = 'hallucinated'
_KIND_MAX_CHARS = 128
_TOOL_NAME_MAX_CHARS = 256
_REASON_MAX_CHARS = 4096
_SUGGESTION_MAX_ITEMS = 16


def tool_rejection_descriptor(source: Any) -> dict[str, Any] | None:
    """Return the first typed rejection descriptor carried by *source*.

    ``source`` may be a tool round, a terminal tool event, or a result-meta
    object.  The lookup order prefers the public field and then accepts the two
    legacy aliases.  Nested ``result`` / ``results`` shapes are inspected so a
    cold projection and a live SSE event classify identically.
    """
    if not isinstance(source, Mapping):
        return None
    for field in (
        REJECTION_FIELD,
        LEGACY_REJECTION_FIELD,
        LEGACY_RESULT_REJECTION_FIELD,
    ):
        descriptor = source.get(field)
        if isinstance(descriptor, Mapping) and descriptor.get('kind'):
            return dict(descriptor)
    result = source.get('result')
    descriptor = tool_rejection_descriptor(result)
    if descriptor is not None:
        return descriptor
    results = source.get('results')
    if isinstance(results, (list, tuple)):
        for result_meta in results:
            descriptor = tool_rejection_descriptor(result_meta)
            if descriptor is not None:
                return descriptor
    return None


def stamp_tool_rejection(
    target: MutableMapping[str, Any] | None,
    descriptor: Mapping[str, Any],
    *,
    tool_name: str = '',
    reason: str = '',
    retryable: bool | None = None,
    legacy_result_alias: bool = False,
) -> dict[str, Any]:
    """Normalize and stamp one rejection on a round, event, or result meta.

    The returned dict is safe to reuse for related wire objects.  Callers must
    provide a stable ``kind``; missing kinds fail loudly instead of silently
    recreating the ambiguity this boundary exists to remove.
    """
    normalized = dict(descriptor)
    kind = str(normalized.get('kind') or '').strip()
    if not kind:
        raise ValueError('tool rejection descriptor requires a non-empty kind')
    if len(kind) > _KIND_MAX_CHARS:
        raise ValueError('tool rejection kind exceeds the wire contract')
    normalized['kind'] = kind
    if tool_name and not (normalized.get('tool') or normalized.get('attempted')):
        normalized['tool'] = str(tool_name)
    if reason and not normalized.get('reason'):
        normalized['reason'] = str(reason)
    for name_field in ('tool', 'attempted'):
        if normalized.get(name_field) is not None:
            normalized[name_field] = str(
                normalized[name_field])[:_TOOL_NAME_MAX_CHARS]
    if normalized.get('reason') is not None:
        normalized['reason'] = str(normalized['reason'])[:_REASON_MAX_CHARS]
    if isinstance(normalized.get('suggestions'), (list, tuple)):
        normalized['suggestions'] = [
            str(suggestion)[:_TOOL_NAME_MAX_CHARS]
            for suggestion in normalized['suggestions'][:_SUGGESTION_MAX_ITEMS]
            if suggestion is not None
        ]
    if retryable is not None:
        normalized['retryable'] = bool(retryable)
    if target is not None:
        target[REJECTION_FIELD] = normalized
        target[LEGACY_REJECTION_FIELD] = normalized
        if legacy_result_alias:
            target[LEGACY_RESULT_REJECTION_FIELD] = normalized
    return normalized


def rejection_tool_name(round_entry: Mapping[str, Any],
                        descriptor: Mapping[str, Any]) -> str:
    """Resolve the attempted tool name across current and legacy shapes."""
    return str(
        descriptor.get('attempted')
        or descriptor.get('tool')
        or round_entry.get('toolName')
        or round_entry.get('tool')
        or ''
    )


def rejection_reason(round_entry: Mapping[str, Any],
                     descriptor: Mapping[str, Any]) -> str:
    """Resolve the model/user-visible refusal detail without inventing text."""
    candidates = (
        descriptor.get('reason'),
        round_entry.get('toolContent'),
        round_entry.get('content'),
        round_entry.get('detail'),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ''


def is_unavailable_tool_rejection(source: Any) -> bool:
    """Whether *source* specifically means the tool did not exist this turn."""
    descriptor = tool_rejection_descriptor(source)
    return bool(
        descriptor
        and descriptor.get('kind') == UNAVAILABLE_TOOL_REJECTION_KIND
    )


def terminal_tool_rejection(
    tool_rounds: Iterable[Any],
) -> tuple[Mapping[str, Any] | None, dict[str, Any] | None]:
    """Return a typed rejection only when it is the turn's last terminal act.

    Transient/incomplete rows are ignored.  Any later terminal success, error,
    abort, or untyped refusal stops the scan so an earlier rejection cannot be
    misreported as the turn outcome.
    """
    terminal_statuses = frozenset({
        'done', 'error', 'failed', 'rejected', 'aborted', 'interrupted',
        'skipped', 'unanswerable',
    })
    for candidate in reversed(list(tool_rounds or ())):
        if not isinstance(candidate, Mapping):
            continue
        status = str(candidate.get('status') or '').strip().lower()
        if status not in terminal_statuses:
            continue
        if status == 'rejected':
            descriptor = tool_rejection_descriptor(candidate)
            if descriptor is not None:
                return candidate, descriptor
        break
    return None, None
