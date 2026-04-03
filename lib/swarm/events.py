"""lib/swarm/events.py — Structured event types for the swarm system.

Extracted from protocol.py for modularity.

Contains:
  • SwarmEventType  — canonical event types enum
  • SwarmEvent      — structured event dataclass for UI / logging
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ═══════════════════════════════════════════════════════════
#  SwarmEventType — canonical event types
# ═══════════════════════════════════════════════════════════

class SwarmEventType(Enum):
    """Canonical event types emitted by the swarm system.

    Used for structured event routing — UI dashboards, logging, metrics.
    """
    # ── Lifecycle ──
    SPAWNING        = 'spawning'
    AGENT_START     = 'agent_start'
    AGENT_COMPLETE  = 'agent_complete'
    AGENT_FAILED    = 'agent_failed'
    AGENT_RETRY     = 'agent_retry'

    # ── Scheduling ──
    WAVE_DONE       = 'wave_done'

    # ── Review ──
    REVIEW          = 'review'
    REVIEW_START    = 'review_start'
    REVIEW_DECISION = 'review_decision'
    FAST_PATH       = 'fast_path'       # all agents OK — skipped master review
    FAST_PATH_SKIP  = 'fast_path_skip'  # explicit skip event for UI

    # ── Spawning ──
    SPAWN_MORE      = 'spawn_more'

    # ── Synthesis ──
    SYNTHESIS       = 'synthesis'
    SYNTHESIS_START = 'synthesis_start'
    SYNTHESIS_CHUNK = 'synthesis_chunk'

    # ── Terminal ──
    SWARM_COMPLETE  = 'swarm_complete'
    SWARM_ERROR     = 'swarm_error'
    ERROR           = 'error'
    DONE            = 'done'

    # ── UI ──
    DASHBOARD       = 'dashboard'


# ═══════════════════════════════════════════════════════════
#  SwarmEvent — structured event replaces ad-hoc event dicts
# ═══════════════════════════════════════════════════════════

@dataclass
class SwarmEvent:
    """Structured event emitted by the swarm system.

    Dual-purpose:
      1. UI rendering — structured fields for dashboards, progress bars
      2. Backward compat — to_legacy() produces the old dict format

    Usage::

        evt = SwarmEvent(type='agent_complete', text='Agent coder-a1 done',
                         agent_id='a1', role='coder', duration_s=4.2)
        on_event(evt.to_legacy())  # old UI still works
        ws.send(evt.to_dict())     # new UI gets structured data
    """
    type: str                      # SwarmEventType value string
    text: str = ''                 # Human-readable description
    agent_id: str = ''
    role: str = ''
    phase: str = ''                # 'executing', 'reviewing', 'synthesising'
    status: str = ''               # 'running', 'completed', 'failed'
    duration_s: float = 0.0
    tokens: int = 0
    round_num: int = 0
    total_agents: int = 0
    completed_agents: int = 0
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    # ── Convenience constructors ──

    @classmethod
    def from_type(cls, event_type: SwarmEventType, **kwargs) -> SwarmEvent:
        """Create a SwarmEvent from a SwarmEventType enum member."""
        return cls(type=event_type.value, **kwargs)

    # ── Serialisation ──

    def to_dict(self) -> dict:
        """Serialize to dict, omitting empty/zero fields (except timestamp)."""
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if k == 'timestamp':
                d[k] = v
            elif k == 'metadata' and v:
                d[k] = v
            elif isinstance(v, str) and v:
                d[k] = v
            elif isinstance(v, (int, float)) and v:
                d[k] = v
        return d

    def to_legacy(self) -> dict:
        """Produce backward-compatible event dict.

        Maps internal event types to what the frontend SSE handler expects:
          - agent_start  → type:'swarm_agent_phase', phase:'running'
          - agent_complete → type:'swarm_agent_complete'
          - agent_failed → type:'swarm_agent_complete', status:'error'
          - progress     → type:'swarm_agent_progress'
        Uses camelCase keys (agentId) for frontend compatibility.
        """
        # ── Map internal type → frontend-expected type ──
        _TYPE_MAP = {
            'agent_start':    'swarm_agent_phase',
            'agent_complete': 'swarm_agent_complete',
            'agent_failed':   'swarm_agent_complete',
            'agent_retry':    'swarm_agent_phase',
            'progress':       'swarm_agent_progress',
        }
        mapped_type = _TYPE_MAP.get(self.type, f'swarm_{self.type}')

        d: dict[str, Any] = {
            'type': mapped_type,
            'content': self.text,
        }
        # For agent_start, set phase to 'running'
        if self.type == 'agent_start':
            d['phase'] = 'running'
        elif self.type == 'agent_retry':
            d['phase'] = 'running'
            d['status'] = 'retrying'
        elif self.type == 'agent_failed':
            d['status'] = 'error'
        elif self.phase:
            d['phase'] = self.phase
        if self.agent_id:
            d['agentId'] = self.agent_id   # camelCase for frontend
        if self.role:
            d['role'] = self.role
        if self.total_agents:
            d['total_agents'] = self.total_agents
            d['completed_agents'] = self.completed_agents
        if self.duration_s:
            d['duration_s'] = round(self.duration_s, 2)
        if self.tokens:
            d['tokens'] = self.tokens
        if self.metadata:
            d.update(self.metadata)
        return d
