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

    INTERRUPTED = 'interrupted'
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
      timeout_seconds — wall-clock ceiling, default 1800s (0 = unlimited)
      tools_hint    — preferred tools (empty = all allowed for role)
      max_retries   — auto-retry on failure (0 = no retry)
      model_override— explicit model slug, '' = auto from role hint
      tools         — explicit per-spawn tool allowlist (empty = role scope)
    """
    role: str = 'general'                        # e.g. 'researcher', 'coder', 'analyst'
    objective: str = ''                          # What this agent should accomplish
    context: str = ''                            # Additional context from parent / deps
    depends_on: list = field(default_factory=list)  # IDs of specs this depends on
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    priority: int = 0                            # Higher = run sooner (within wave)
    tools_hint: list = field(default_factory=list)  # Preferred tools (empty = all allowed)
    timeout_seconds: int = 0                     # explicit ceiling (0 = liveness-governed)
    """OPTIONAL hard wall-clock ceiling. ``0`` (default) = governed by liveness.

    This was 1800, added after the 2026-07-27 runaway. It did not work as
    intended, for two measured reasons:

      * it is evaluated only in ``before_round``, so an agent blocked INSIDE a
        tool never reaches it — one sat in a ``pytest`` child for over an hour
        without tripping it. It could not catch the hang it was added for;
      * against agents that DO keep looping it fires on total runtime, so it
        reaps productive long jobs. Measured legitimate runs: 1809s, 1846s,
        1903s — all of which this value would have truncated.

    The runaway it was meant to stop is now covered precisely by two guards
    that key on BEHAVIOUR rather than duration: the chassis' no-progress
    breaker (N identical tool-call rounds) and the ProgressBeacon stall check
    (total silence). Both catch a wedged agent in bounded time while leaving a
    working one alone — which is the property a wall clock cannot have.

    A caller may still set a deliberate ceiling; it is simply no longer
    something you get by accident. Pinned by tests/swarm/test_swarm_runaway_guard.py
    and tests/swarm/test_swarm_liveness.py.
    """

    # ── Extended fields ──
    max_retries: int = 0                         # Auto-retry on failure (0 = no retry)
    model_override: str = ''                     # Explicit model, '' = auto from role hint
    model_tier: str = 'standard'                 # 'light', 'standard', or 'heavy'
    # Per-child slice of the spawning task's remaining execution budget
    # ({max_tokens, max_tool_calls, deadline}); None = unbounded parent.
    execution_limits: dict | None = None

    # ── Delegation field (context-hygiene contract) ──
    tools: list = field(default_factory=list)
    """Explicit per-spawn tool-name allowlist. Intersected with the role
    scope and the sub-agent denylist (narrow-only — it can never widen a
    role beyond its declared scope), so the orchestrator can hand a worker
    exactly the tools it needs — including MCP tools it has never used
    itself — and nothing else. Empty = role default."""

    # Set only by the server-side manage_agents dispatcher, never by the
    # model's spawn_agents payload. Persisted with the Swarm spec so restart
    # recovery can attach the same durable identity before worker entry.
    retained_agent_id: str = ''
    retained_scope: str = ''
    retained_run_id: str = ''
    required_for_completion: bool = True         # False = optional background exploration

    def to_dict(self) -> dict:
        """Serialize to a plain dict (all fields)."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> SubTaskSpec:
        """Construct from a dict; reject the removed round-limit contract."""
        if 'max_rounds' in d:
            raise ValueError(
                'max_rounds has been removed; sub-agents run until the model '
                'naturally completes')
        if type(d.get('required_for_completion', True)) is not bool:
            raise ValueError('required_for_completion must be boolean')
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

    # Recovery evidence belongs to the checkpoint, not to a second task store.
    recovery_reason: str = ''
    checkpoint_phase: str = 'ready'
    actuals_complete: bool = True

    def to_dict(self) -> dict:
        """Serialize to a plain dict."""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}
