"""lib/swarm/types.py — Core data types for the swarm system.

Extracted from protocol.py for modularity.

Contains:
  • SESSION_TTL_SECONDS       — default session TTL constant
  • MAX_COMPRESSED_RESULT_CHARS — default budget for compress_result()
  • SubAgentStatus            — lifecycle states enum
  • SubTaskSpec               — task specification dataclass
  • SubAgentResult            — agent result dataclass
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ═══════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════

SESSION_TTL_SECONDS: int = 1800
"""Default TTL for swarm sessions (30 minutes).  Stale sessions are
cleaned up after this duration to prevent memory leaks."""

MAX_COMPRESSED_RESULT_CHARS: int = 8000
"""Default character budget for compress_result().  Kept as a module-level
constant for backward compatibility with older test suites."""


# ═══════════════════════════════════════════════════════════
#  SubAgentStatus — lifecycle states
# ═══════════════════════════════════════════════════════════

class SubAgentStatus(Enum):
    """Lifecycle states for a sub-agent."""
    PENDING   = 'pending'
    RUNNING   = 'running'
    COMPLETED = 'completed'
    FAILED    = 'failed'
    CANCELLED = 'cancelled'
    RETRYING  = 'retrying'


# ═══════════════════════════════════════════════════════════
#  SubTaskSpec — what needs to be done
# ═══════════════════════════════════════════════════════════

@dataclass
class SubTaskSpec:
    """Specification for a sub-agent task.

    Created by the planning phase (or directly via the ``spawn_agents`` tool).

    Fields:
      role          — role key from registry (e.g. 'researcher', 'coder')
      objective     — the task description for this agent
      context       — optional extra context (injected into system prompt)
      depends_on    — IDs of specs that must complete first
      id            — unique identifier (auto-generated if not set)
      priority      — higher = run sooner within the same wave
      max_rounds    — max LLM rounds for this sub-agent
      tools_hint    — preferred tools (empty = all allowed for role)
      max_retries   — auto-retry on failure (0 = no retry)
      model_override— explicit model slug, '' = auto from role hint
    """
    role: str = 'general'                        # e.g. 'researcher', 'coder', 'analyst'
    objective: str = ''                          # What this agent should accomplish
    context: str = ''                            # Additional context from parent / deps
    depends_on: list = field(default_factory=list)  # IDs of specs this depends on
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    priority: int = 0                            # Higher = run sooner (within wave)
    max_rounds: int = 0                          # Max LLM rounds (0 = unlimited)
    tools_hint: list = field(default_factory=list)  # Preferred tools (empty = all allowed)
    timeout_seconds: int = 300                   # Max wall-clock time

    # ── Extended fields ──
    max_retries: int = 0                         # Auto-retry on failure (0 = no retry)
    model_override: str = ''                     # Explicit model, '' = auto from role hint
    model_tier: str = 'standard'                 # 'light', 'standard', or 'heavy'

    def to_dict(self) -> dict:
        """Serialize to a plain dict (all fields)."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> SubTaskSpec:
        """Construct from a dict, ignoring unknown keys."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ═══════════════════════════════════════════════════════════
#  SubAgentResult — what comes back
# ═══════════════════════════════════════════════════════════

@dataclass
class SubAgentResult:
    """Result from a sub-agent execution.

    Contains the final answer text, status, token usage, cost,
    and optional structured artifacts for downstream use.
    """
    status: str = SubAgentStatus.PENDING.value
    final_answer: str = ''
    answer: str = ''                             # Alias for backward compat (prefer final_answer)
    reasoning_trace: str = ''                    # Condensed reasoning (for debugging)
    error_message: str = ''
    tool_log: list = field(default_factory=list) # [{round, tool, args_brief}, ...]
    rounds_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    tool_calls_made: int = 0                     # Total tool invocations

    # ── Extended fields ──
    retry_count: int = 0                         # How many times this was retried
    artifacts: dict[str, Any] = field(default_factory=dict)     # Structured artifacts
    artifacts_written: list = field(default_factory=list)        # Keys written to ArtifactStore
    artifacts_read: list = field(default_factory=list)           # Keys read from ArtifactStore

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}
