"""lib/swarm/protocol.py — Facade re-exporting all swarm protocol types.

This module was the original monolith (887 lines).  It has been
decomposed into focused submodules:

  • ``lib.swarm.types``          — SubAgentStatus, SubTaskSpec, SubAgentResult,
                                   SESSION_TTL_SECONDS, MAX_COMPRESSED_RESULT_CHARS
  • ``lib.swarm.events``         — SwarmEventType, SwarmEvent
  • ``lib.swarm.messages``       — AgentMessage
  • ``lib.swarm.artifact_store`` — ArtifactBackend, InMemoryBackend, ArtifactStore
  • ``lib.swarm.result_format``  — _CODE_BLOCK_RE, compress_result,
                                   format_sub_results_for_master
  • ``lib.swarm.planner``        — resolve_execution_order

All symbols are re-exported here so existing imports like
``from lib.swarm.protocol import ArtifactStore`` continue to work
without any changes.
"""

from __future__ import annotations

# ── Artifact storage (lib.swarm.artifact_store) ──
from lib.swarm.artifact_store import (  # noqa: F401
    ArtifactBackend,
    ArtifactStore,
    InMemoryBackend,
)

# ── Events (lib.swarm.events) ──
from lib.swarm.events import (  # noqa: F401
    SwarmEvent,
    SwarmEventType,
)

# ── Messages (lib.swarm.messages) ──
from lib.swarm.messages import AgentMessage  # noqa: F401

# ── Execution ordering (lib.swarm.planner) ──
from lib.swarm.planner import (  # noqa: F401
    resolve_execution_order,
)

# ── Result compression/formatting (lib.swarm.result_format) ──
from lib.swarm.result_format import (  # noqa: F401
    _CODE_BLOCK_RE,
    compress_result,
    format_sub_results_for_master,
)

# ── Core types & constants (lib.swarm.types) ──
from lib.swarm.types import (  # noqa: F401
    MAX_COMPRESSED_RESULT_CHARS,
    SESSION_TTL_SECONDS,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
)

__all__ = [
    # Constants
    'SESSION_TTL_SECONDS',
    'MAX_COMPRESSED_RESULT_CHARS',
    # Enums
    'SubAgentStatus',
    'SwarmEventType',
    # Dataclasses
    'SubTaskSpec',
    'SubAgentResult',
    'SwarmEvent',
    'AgentMessage',
    # Artifact storage
    'ArtifactBackend',
    'InMemoryBackend',
    'ArtifactStore',
    # Result formatting
    '_CODE_BLOCK_RE',
    'compress_result',
    'format_sub_results_for_master',
    # Execution ordering
    'resolve_execution_order',
]
