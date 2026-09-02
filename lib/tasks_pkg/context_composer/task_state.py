"""Versioned task-state projection derived from transcript and tool events.

The transcript remains authoritative.  This module produces a bounded,
rebuildable projection for context planning and compaction validation; it does
not write durable state or infer successful completion from assistant prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable


_CONSTRAINT = re.compile(
    r"\b(must|must not|never|required|only|at most|no more than)\b|"
    r"(必须|不得|不能|只允许|不超过|严禁)", re.I)
_TEST_COMMAND = re.compile(
    r"(?:^|\s)(?:pytest|unittest|npm\s+(?:run\s+)?test|pnpm\s+test|"
    r"yarn\s+test|go\s+test|cargo\s+test|mvn\s+test|gradle\s+test)", re.I)
_PATH_KEY = re.compile(r"(?:^|_)(?:path|file|filename)$", re.I)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or block.get("content") or "").strip()
        for block in content if isinstance(block, dict)
    ).strip()


def _bounded_unique(values: Iterable[str], limit: int = 32,
                    chars: int = 600) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        value = " ".join(str(raw or "").split())[:chars]
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= limit:
            break
    return tuple(out)


def _call_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    return str((function or {}).get("name") or call.get("name") or "")


def _call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    value = (function or {}).get("arguments", call.get("arguments", {}))
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _paths(value: Any, *, key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _paths(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child, key=key)
    elif _PATH_KEY.search(key) and isinstance(value, str) and value.strip():
        yield value.strip()


@dataclass(frozen=True)
class TaskStateSnapshotV1:
    """Bounded state rebuilt from one task's current observable events."""

    contract_version: str = "tofu.task-state/v1"
    goal: str = ""
    hard_constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    completed_work: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    todos: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    observed_at_ms: int = 0
    world_version: str = ""
    source_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_context_text(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"))


def derive_task_state_snapshot(messages: list[dict[str, Any]],
                               task: dict[str, Any] | None = None
                               ) -> TaskStateSnapshotV1:
    """Derive state without treating unverified assistant text as completion."""
    task = task if isinstance(task, dict) else {}
    user_texts = [_text(message.get("content")) for message in messages
                  if isinstance(message, dict) and message.get("role") == "user"
                  and not message.get("_isMeta")]
    goal = next((value for value in user_texts if value), "")[:4000]
    constraints = [value for value in user_texts if _CONSTRAINT.search(value)]
    constraints.extend(task.get("_hardConstraints") or ())

    decisions: list[str] = list(task.get("_decisions") or ())
    completed: list[str] = []
    files: list[str] = []
    tests: list[str] = []
    errors: list[str] = []
    evidence: list[str] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            name = _call_name(call)
            arguments = _call_arguments(call)
            files.extend(_paths(arguments))
            command = str(arguments.get("command") or arguments.get("cmd") or "")
            if command and _TEST_COMMAND.search(command):
                tests.append(f"requested:{command}")
            if call.get("id"):
                evidence.append(f"tool-call:{call['id']}")
        if message.get("role") != "tool":
            continue
        content = _text(message.get("content"))
        name = str(message.get("name") or "tool")
        status = str(message.get("status") or "").lower()
        if status in {"error", "failed", "rejected", "cancelled"}:
            errors.append(f"{name}:{status}:{content[:300]}")
        elif status in {"ok", "success", "completed", "done"}:
            completed.append(f"{name}:{content[:300]}")
        for key in ("evidenceId", "artifactRef", "tool_call_id"):
            if message.get(key):
                evidence.append(f"{key}:{message[key]}")

    for row in task.get("toolRounds") or ():
        if not isinstance(row, dict):
            continue
        name = str(row.get("tool") or row.get("name") or "tool")
        status = str(row.get("status") or "").lower()
        summary = str(row.get("summary") or row.get("result") or "")[:300]
        if status in {"ok", "success", "completed", "done"}:
            completed.append(f"{name}:{summary}")
        elif status in {"error", "failed", "rejected", "cancelled"}:
            errors.append(f"{name}:{status}:{summary}")
        for key in ("evidenceId", "artifactRef"):
            if row.get(key):
                evidence.append(f"{key}:{row[key]}")

    source = json.dumps({"messages": messages, "toolRounds": task.get("toolRounds")},
                        ensure_ascii=False, sort_keys=True, default=str,
                        separators=(",", ":"))
    return TaskStateSnapshotV1(
        goal=goal,
        hard_constraints=_bounded_unique(constraints),
        decisions=_bounded_unique(decisions),
        completed_work=_bounded_unique(completed),
        files=_bounded_unique(files, limit=64, chars=1000),
        tests=_bounded_unique([*tests, *(task.get("_tests") or ())]),
        errors=_bounded_unique(errors),
        open_questions=_bounded_unique(task.get("_openQuestions") or ()),
        todos=_bounded_unique(task.get("_todos") or ()),
        evidence_refs=_bounded_unique(evidence, limit=64),
        next_steps=_bounded_unique(task.get("_nextSteps") or ()),
        observed_at_ms=max(0, int(task.get("_observedAtMs") or 0)),
        world_version=str(task.get("_worldVersion") or ""),
        source_digest=hashlib.sha256(source.encode("utf-8")).hexdigest()[:24],
    )


__all__ = ["TaskStateSnapshotV1", "derive_task_state_snapshot"]
