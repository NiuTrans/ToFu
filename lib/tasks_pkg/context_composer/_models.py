"""Typed context blocks shared by every conversational LLM role.

Logical authority is deliberately independent from physical message placement:
cache-sensitive blocks may ride a user-role carrier without becoming user text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Authority = Literal[
    "platform",
    "user",
    "project",
    "workflow",
    "preference",
    "ambient",
    "evidence",
]
Placement = Literal["system", "head", "tail", "tool_result"]
Stability = Literal["static", "conversation", "turn", "round"]
Lifecycle = Literal["conversation", "task", "round"]
ContextLayer = Literal[
    "objective_constraints",
    "task_state",
    "evidence",
    "hot_tail",
    "cold_history",
]


@dataclass(frozen=True)
class ContextPlanEntryV2:
    """Auditable selection decision for one content-addressed context block."""

    id: str
    layer: ContextLayer
    selected: bool
    truncated: bool
    reason: str
    tokens: int
    content_hash: str
    recovery_handle: str = ""
    observed_at_ms: int = 0
    world_version: str = ""


@dataclass(frozen=True)
class ContextPlanV2:
    """Deterministic global plan for context owned by the composer.

    Raw conversation history remains the transcript authority and is budgeted
    by compaction. ``base_tokens`` makes that already-committed cost visible so
    the composer cannot independently spend the whole request budget again.
    """

    contract_version: str
    budget_tokens: int
    base_tokens: int
    selected_tokens: int
    overflow_tokens: int
    entries: tuple[ContextPlanEntryV2, ...]
    segment_hashes: dict[str, str] = field(default_factory=dict)
    cache_epoch: int = 0


@dataclass(frozen=True)
class ContextBlock:
    """One independently budgeted and observable unit of model context."""

    id: str
    source: str
    content: str
    authority: Authority
    placement: Placement
    stability: Stability
    lifecycle: Lifecycle
    priority: int = 100
    max_tokens: int | None = None
    dedupe_key: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    suppressed_reason: str = ""
    layer: ContextLayer = "cold_history"
    required: bool = False
    required_permissions: frozenset[str] = frozenset()
    access_count: int = 0
    observed_at_ms: int = 0
    world_version: str = ""
    recovery_handle: str = ""


@dataclass(frozen=True)
class ComposeRequest:
    project_path: str = ""
    project_enabled: bool = False
    memory_enabled: bool = False
    search_enabled: bool = False
    has_real_tools: bool = False
    conv_id: str = ""
    user_id: int = 0
    model: str = ""
    system_prompt_mode: str = "append"
    tool_names: frozenset[str] = frozenset()
    disabled_blocks: frozenset[str] = frozenset()
    task: dict[str, Any] | None = None
    global_budget_tokens: int | None = None
    base_context_tokens: int = 0
    granted_permissions: frozenset[str] = frozenset()


@dataclass
class ComposeResult:
    messages: list[dict[str, Any]]
    manifest: list[dict[str, Any]]
    plan: ContextPlanV2 | None = None


__all__ = [
    "ComposeRequest",
    "ComposeResult",
    "ContextBlock",
    "ContextLayer",
    "ContextPlanEntryV2",
    "ContextPlanV2",
]
