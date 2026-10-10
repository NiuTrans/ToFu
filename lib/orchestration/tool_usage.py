"""Normalize agent-runner tool usage for orchestration execution.

Runner implementations historically returned either ``tool_names`` or a
SubAgent-style ``tool_log``.  This module is the single compatibility boundary
for those shapes.  Flow execution, progress accounting and trace projection
consume one immutable value instead of learning each runner's wire format.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrchestrationToolUsage:
    """Detached classification of one runner result's tool activity."""

    state_changing_tools: tuple[str, ...] = ()
    exploratory_tools: tuple[str, ...] = ()
    reported: bool = False

    @property
    def state_changing_count(self) -> int:
        return len(self.state_changing_tools)

    @property
    def exploratory_count(self) -> int:
        return len(self.exploratory_tools)

    def engine_tuple(self) -> tuple[int, int, list[str], bool]:
        """Return the legacy FlowExecutor tuple with a detached names list."""
        return (
            self.state_changing_count,
            self.exploratory_count,
            list(self.state_changing_tools),
            self.reported,
        )


def _entry_name(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, Mapping):
        for key in ('tool', 'toolName'):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ''


def _entries(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    return []


def _arguments(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    args = entry.get('toolArgs')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return {}
    return args if isinstance(args, Mapping) else {}


def _failed_receipt(meta: Mapping[str, Any]) -> bool:
    return (
        meta.get('type') == 'error'
        or meta.get('writeOk') is False
        or bool(meta.get('refusal') or meta.get('rejection') or meta.get('error'))
        or any(meta.get(key) for key in (
            'notRun', 'timedOut', 'interrupted', 'backgrounded'))
        or ('exitCode' in meta and str(meta['exitCode']) != '0')
    )


def _nontrivial_edit(edit: Mapping[str, Any], *, tool: str) -> bool:
    operation = edit.get('operation') if tool == 'edit_file' else (
        'replace' if tool in ('apply_diff', 'apply_diffs') else 'insert_after')
    anchor = edit.get('anchor') if tool == 'edit_file' else edit.get(
        'search' if operation == 'replace' else 'anchor')
    content = edit.get('content') if tool == 'edit_file' else edit.get(
        'replace' if operation == 'replace' else 'content')
    if not isinstance(anchor, str) or not anchor or not isinstance(content, str):
        return False
    if operation == 'replace':
        # Matching can normalize whitespace. Do not credit an identity edit
        # (including a normalization-only match) as verified changed bytes.
        return (
            '\\' not in anchor
            and ''.join(anchor.split()) != ''.join(content.split())
        )
    return operation in ('insert_before', 'insert_after') and bool(content.strip())


def _verified_state_change(entry: Any, name: str) -> bool:
    """Use executor receipts, never names, command text, or model prose.

    ``done`` alone does not prove success (shell failures can settle done),
    and success alone does not prove mutation. ``fileChanges`` on shell
    results is task-cumulative; toolResultEvidence.worldVersionAfter records
    observations. Neither proves this call changed state. Shell/code_exec,
    MCP, names-only and unproven overwrites remain conservatively unverified.
    """
    if not isinstance(entry, Mapping) or entry.get('status') != 'done':
        return False
    if _failed_receipt(entry):
        return False

    evidence = entry.get('toolResultEvidence')
    if isinstance(evidence, Mapping) and any(
        key in evidence and evidence[key] not in ('ok', 'success')
        for key in ('status', 'executionStatus')
    ):
        return False
    results = entry.get('results')
    if not isinstance(results, list) or not results or any(
        not isinstance(meta, Mapping) or _failed_receipt(meta) for meta in results
    ):
        return False
    # Native project writes produce exactly one metadata receipt. Refuse
    # aggregates rather than attributing a sibling's success to this call.
    if len(results) != 1 or results[0].get('writeOk') is not True:
        return False
    meta = results[0]
    args = _arguments(entry)
    if name == 'write_file':
        # The UI badge is substring-based (an updated path containing
        # "created" can receive it). The executor success prefix is exact.
        content = entry.get('toolContent')
        created = isinstance(content, str) and content.startswith('File created:')
        return created or (
            args.get('append') is True
            and isinstance(args.get('content'), str)
            and bool(args['content'])
        )
    if name not in (
        'edit_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents',
    ):
        return False
    edits = args.get('edits')
    if edits is None:
        return name in ('apply_diff', 'insert_content') and _nontrivial_edit(
            args, tool=name)
    # Multiple operations can undo each other, including distinct path
    # aliases to one file. Receipts describe operations, not final byte
    # deltas, so aggregate edits remain unverified without per-file deltas.
    if (
        not isinstance(edits, list) or len(edits) != 1
        or not isinstance(edits[0], Mapping)
    ):
        return False
    summaries = meta.get('editSummaries')
    if not isinstance(summaries, list) or len(summaries) != len(edits) or any(
        not isinstance(summary, Mapping) or summary.get('status') != 'ok'
        for summary in summaries
    ):
        return False
    return any(_nontrivial_edit(edit, tool=name) for edit in edits)


def classify_orchestration_tool_usage(
    result: Mapping[str, Any] | None,
) -> OrchestrationToolUsage:
    """Project supported runner result shapes into one safe usage contract.

    ``reported`` intentionally records key presence, not whether a recognized
    name was found.  An explicit empty list means "the runner reported zero
    calls" and may drive the zero-deliverable guard; omitting both fields means
    "this runner does not support tool telemetry" and must not trigger it.
    Structured ``tool_log`` takes precedence over lossy ``tool_names``.
    Names-only telemetry proves activity, not success or mutation. The legacy
    ``exploratory`` bucket includes unknown/unverified effects; zero verified
    changes is not a claim that no external side effect could have occurred.
    """
    if not isinstance(result, Mapping):
        return OrchestrationToolUsage()

    reported = 'tool_names' in result or 'tool_log' in result
    raw = result.get('tool_log')
    if raw is None:
        raw = result.get('tool_names')

    state_changing: list[str] = []
    exploratory: list[str] = []
    for entry in _entries(raw):
        name = _entry_name(entry)
        if not name:
            continue
        if _verified_state_change(entry, name):
            state_changing.append(name)
        else:
            exploratory.append(name)
    return OrchestrationToolUsage(
        state_changing_tools=tuple(state_changing),
        exploratory_tools=tuple(exploratory),
        reported=reported,
    )


__all__ = [
    'OrchestrationToolUsage',
    'classify_orchestration_tool_usage',
]
