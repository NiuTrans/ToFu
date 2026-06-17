"""lib/swarm — Agent Swarm: async multi-agent system.

Architecture (async fire-and-forget pattern):

  ┌────────────────────────────────────────────────────────────────────┐
  │ Main agent loop (lib/tasks_pkg/orchestrator.py)                    │
  │                                                                    │
  │  round N:                                                          │
  │   tool_call: spawn_agents(...)  ─────────┐                         │
  │   ◄─ returns handle (immediately)        │                         │
  │   ... continues other tools ...          │                         │
  │                                          │                         │
  │  round N+1 (between-round hook):         │                         │
  │   inbox drained → <swarm-update> user msg│                         │
  └─────────────────┬────────────────────────┘                         │
                    │                                                  │
                    ▼                                                  │
  ┌────────────────────────────────────────────────────────────────────┐
  │ MasterOrchestrator.run_in_background() (daemon thread)             │
  │   └─ StreamingScheduler                                            │
  │      ├─ SubAgent (per spec) → on_complete → enqueue swarm-update   │
  │      └─ ArtifactStore (shared key-value store between agents)      │
  │                                                                    │
  │ When all done: clear session, drop handle.                         │
  └────────────────────────────────────────────────────────────────────┘

Sub-agent results NEVER come back as a synchronous tool result.  The main
agent sees them as ``<swarm-update>`` user messages on subsequent turns,
or by calling ``await_agents`` / ``get_agent_result`` explicitly.

What this package does NOT do anymore (removed in async migration):
  • There is no internal "mini master" reviewing results.
  • There is no synthesis step — the main agent IS the synthesizer.
  • Sub-agents cannot call ``spawn_agents`` / ``await_agents`` /
    ``get_agent_result`` / ``ask_human``  — see ``SUB_AGENT_DENYLIST``.
"""

# Core protocol types
from lib.swarm.agent import SubAgent

# Artifact storage (canonical location: artifact_store.py)
from lib.swarm.artifact_store import (
    ArtifactBackend,
    ArtifactStore,
    InMemoryBackend,
)

# Integration with existing system
from lib.swarm.integration import (
    execute_swarm_tool,
    get_active_session,
    rehydrate_swarms_on_startup,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import (
    AgentMessage,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
    SwarmEvent,
    SwarmEventType,
    resolve_execution_order,
)
from lib.swarm.rate_limiter import RateLimiter

# Role definitions & model tiers
from lib.swarm.registry import (
    AGENT_ROLES,
    MODEL_TIERS,
    configure_model_tiers,
    get_role_config,
    get_role_system_suffix,
    get_tools_for_role,
    resolve_model_for_tier,
    scope_tools_for_role,
)

# Result formatting (canonical location: result_format.py)
from lib.swarm.result_format import compress_result, format_sub_results_for_master
from lib.swarm.scheduler import AsyncStreamingScheduler, StreamingScheduler

# Tool definitions
from lib.swarm.tools import (
    ARTIFACT_TOOLS,
    AWAIT_AGENTS_TOOL,
    GET_AGENT_RESULT_TOOL,
    LIST_ARTIFACTS_TOOL,
    MASTER_TOOLS,
    READ_ARTIFACT_TOOL,
    SPAWN_AGENTS_TOOL,
    STORE_ARTIFACT_TOOL,
    SUB_AGENT_DENYLIST,
    SUB_AGENT_TOOLS,
    SWARM_CONTROL_TOOL_NAMES,
    SWARM_TOOL_NAMES,
)

__all__ = [
    # Protocol
    'SubTaskSpec', 'SubAgentResult', 'SubAgentStatus',
    'ArtifactStore', 'ArtifactBackend', 'InMemoryBackend',
    'SwarmEvent', 'SwarmEventType', 'AgentMessage',
    'compress_result', 'format_sub_results_for_master',
    'resolve_execution_order',
    # Execution
    'SubAgent', 'MasterOrchestrator', 'StreamingScheduler',
    'AsyncStreamingScheduler', 'RateLimiter',
    # Integration
    'execute_swarm_tool', 'get_active_session', 'rehydrate_swarms_on_startup',
    # Registry
    'AGENT_ROLES', 'MODEL_TIERS',
    'scope_tools_for_role', 'get_tools_for_role',
    'get_role_system_suffix', 'get_role_config',
    'resolve_model_for_tier', 'configure_model_tiers',
    # Tool defs
    'SPAWN_AGENTS_TOOL', 'AWAIT_AGENTS_TOOL', 'GET_AGENT_RESULT_TOOL',
    'STORE_ARTIFACT_TOOL', 'READ_ARTIFACT_TOOL', 'LIST_ARTIFACTS_TOOL',
    'MASTER_TOOLS', 'SUB_AGENT_TOOLS', 'ARTIFACT_TOOLS',
    'SWARM_TOOL_NAMES', 'SWARM_CONTROL_TOOL_NAMES', 'SUB_AGENT_DENYLIST',
]
