# HOT_PATH
"""Fresh-read unchanged-result projection for bounded model context.

This module does not skip tool execution.  It records only bounded digests in
an independent per-task map, then replaces a byte-identical fresh read with a
short receipt *only while* the prior model-visible result is still present
unchanged in the active message list.  The next read returns the full result
automatically after compaction, truncation, or context replacement.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.tool_dispatch._flags import (
    _ensure_tool_result_cache,
    _make_cache_key,
)

logger = get_logger(__name__)

_STATE_KIND = 'fresh-unchanged-result/v1'
_STATE_TASK_FIELD = '_unchanged_tool_result_receipts'


@dataclass(frozen=True)
class UnchangedProjection:
    """One settlement's full-result identity and optional compact projection."""

    raw_digest: str
    model_content: str
    compacted: bool = False
    previous_tool_call_id: str = ''
    previous_model_tokens: int = 0


def compact_unchanged_tool_names() -> frozenset[str]:
    """Resolve the ToolSpec-owned fresh-read projection policy live."""
    names: set[str] = set()
    try:
        from lib.tools.registry import all_specs

        for spec in all_specs():
            names.update(spec.unchanged_receipt_tools)
    except Exception as exc:
        logger.debug(
            '[UnchangedResult] registry policy resolution failed: %s', exc)
    return frozenset(names)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]


def _state_key(tool_name: str, arguments: dict[str, Any]) -> str:
    identity = _make_cache_key(tool_name, arguments)
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _bounded_state(task: dict[str, Any], *, create: bool) -> dict[str, Any]:
    """Return the content-free state map under the shared receipt budget."""
    state = task.get(_STATE_TASK_FIELD)
    if not isinstance(state, dict):
        if not create:
            return {}
        state = {}
        task[_STATE_TASK_FIELD] = state

    # Reuse the launch-probed receipt capacity, but not its FIFO slots: cheap
    # control-plane digests must never evict an expensive web/fetch body.
    _ensure_tool_result_cache(task)
    capacity = int(task['_tool_result_cache_capacity'])
    evicted = 0
    while len(state) > capacity:
        state.pop(next(iter(state)), None)
        evicted += 1
    if evicted:
        try:
            previous = max(
                0, int(task.get(
                    '_unchanged_tool_result_receipt_evictions') or 0))
        except (TypeError, ValueError, OverflowError):
            previous = 0
        task['_unchanged_tool_result_receipt_evictions'] = min(
            1_000_000_000, previous + evicted)
    return state


def _visible_content(
    messages: list[dict[str, Any]] | None,
    tool_call_id: str,
) -> str | None:
    """Return an exact prior tool message, or None once it left context."""
    if not tool_call_id or not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if (not isinstance(message, dict)
                or message.get('role') != 'tool'
                or str(message.get('tool_call_id') or '') != tool_call_id):
            continue
        content = message.get('content')
        return content if isinstance(content, str) else None
    return None


def maybe_project_unchanged_result(
    task: dict[str, Any],
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_content: str,
    messages: list[dict[str, Any]] | None,
    enabled: bool,
) -> UnchangedProjection:
    """Return a compact receipt when a fresh result is safely redundant.

    Safety is conjunctive: the tool explicitly opted in, the new raw result is
    byte-identical, and the exact prior model projection is still in context.
    A miss returns the original content and is remembered only after normal
    budgeting/evidence settlement succeeds.
    """
    if not enabled:
        return UnchangedProjection('', tool_content)
    raw_digest = _digest(tool_content)
    state = _bounded_state(task, create=False)
    prior = state.get(_state_key(tool_name, arguments))
    if not isinstance(prior, dict) or prior.get('kind') != _STATE_KIND:
        return UnchangedProjection(raw_digest, tool_content)
    if prior.get('rawDigest') != raw_digest:
        return UnchangedProjection(raw_digest, tool_content)

    previous_call_id = str(prior.get('toolCallId') or '')
    visible = _visible_content(messages, previous_call_id)
    if (visible is None
            or _digest(visible) != str(prior.get('modelDigest') or '')):
        return UnchangedProjection(raw_digest, tool_content)

    receipt = (
        f'[Unchanged: fresh read matches {previous_call_id} in context. '
        'Reuse prior result.]'
    )
    # Keep the no-tokenizer helper itself conservatively monotonic. The caller
    # performs an exact model-token comparison as a second gate.
    try:
        previous_model_chars = max(0, int(prior.get('modelChars') or 0))
    except (TypeError, ValueError, OverflowError):
        previous_model_chars = 0
    if not previous_model_chars or len(receipt) * 2 > previous_model_chars:
        return UnchangedProjection(raw_digest, tool_content)
    try:
        previous_model_tokens = max(0, int(prior.get('modelTokens') or 0))
    except (TypeError, ValueError, OverflowError):
        previous_model_tokens = 0
    return UnchangedProjection(
        raw_digest, receipt, compacted=True,
        previous_tool_call_id=previous_call_id,
        previous_model_tokens=previous_model_tokens)


def remember_full_result(
    task: dict[str, Any],
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_call_id: str,
    projection: UnchangedProjection,
    final_model_content: str,
    result_evidence: dict[str, Any] | None,
    enabled: bool,
    final_model_tokens: int = 0,
) -> None:
    """Remember one full projection without retaining another result body."""
    if not enabled or projection.compacted:
        return
    evidence = result_evidence if isinstance(result_evidence, dict) else {}
    entry = {
        'kind': _STATE_KIND,
        'rawDigest': projection.raw_digest,
        'modelDigest': _digest(final_model_content),
        'modelChars': len(final_model_content),
        'modelTokens': max(0, int(final_model_tokens or 0)),
        'toolCallId': str(tool_call_id or ''),
        'evidenceId': str(evidence.get('evidenceId') or ''),
    }
    state = _bounded_state(task, create=True)
    key = _state_key(tool_name, arguments)
    if key in state:
        state.pop(key, None)
    state[key] = entry
    _bounded_state(task, create=True)
