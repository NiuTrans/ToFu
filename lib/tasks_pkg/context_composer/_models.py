"""Typed context blocks shared by every conversational LLM role.

Logical authority is deliberately independent from physical message placement:
cache-sensitive blocks may ride a user-role carrier without becoming user text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Authority = Literal[
    'platform', 'user', 'project', 'workflow', 'preference', 'ambient',
    'evidence',
]
Placement = Literal['system', 'head', 'tail', 'tool_result']
Stability = Literal['static', 'conversation', 'turn', 'round']
Lifecycle = Literal['conversation', 'task', 'round']


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
    dedupe_key: str = ''
    provenance: dict[str, Any] = field(default_factory=dict)
    suppressed_reason: str = ''


@dataclass(frozen=True)
class ComposeRequest:
    project_path: str = ''
    project_enabled: bool = False
    memory_enabled: bool = False
    search_enabled: bool = False
    swarm_enabled: bool = False
    has_real_tools: bool = False
    conv_id: str = ''
    model: str = ''
    system_prompt_mode: str = 'append'
    tool_names: frozenset[str] = frozenset()
    disabled_blocks: frozenset[str] = frozenset()
    task: dict[str, Any] | None = None


@dataclass
class ComposeResult:
    messages: list[dict[str, Any]]
    manifest: list[dict[str, Any]]


__all__ = ['ComposeRequest', 'ComposeResult', 'ContextBlock']
