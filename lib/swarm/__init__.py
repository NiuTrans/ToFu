"""lib/swarm — Agent Swarm: orchestrator-worker multi-agent system.

Architecture (streaming reactive pattern):
  ┌─────────────────────────────────────────────────────────────┐
  │ Main Orchestrator (existing)                                │
  │   └─ spawn_agents tool call                                 │
  │       └─ MasterOrchestrator (reactive streaming loop)       │
  │           ├─ StreamingScheduler (DAG-based, no wave barrier)│
  │           │   ├─ Agent A completes → notifies master        │
  │           │   ├─ Agent B completes → notifies master        │
  │           │   └─ Agents start as deps resolve, not waves    │
  │           ├─ Master LLM reviews incrementally               │
  │           │   └─ spawn_more_agents if needed                │
  │           ├─ New agents injected into live scheduler         │
  │           ├─ ArtifactStore (shared data between agents)     │
  │           └─ Final synthesis                                │
  └─────────────────────────────────────────────────────────────┘

Key features:
  • Streaming DAG scheduling — agents start as soon as deps finish
  • Reactive master — reviews results as they arrive, spawns follow-up
  • Shared artifact store — agents share data via key-value store
  • Auto-retry — configurable retry for failed agents
  • Cycle detection — prevents deadlock from circular dependencies
  • Rate limiting — shared semaphore for API backpressure
  • Session TTL — auto-cleanup of stale swarm sessions
"""

# Core protocol types
# Execution engine
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
)
from lib.swarm.master import (
    MasterOrchestrator,
    resolve_execution_order,
    run_swarm_task,
    spawn_sub_agent,
)
from lib.swarm.protocol import (
    AgentMessage,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
    SwarmEvent,
    SwarmEventType,
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
    CHECK_AGENTS_TOOL,
    LIST_ARTIFACTS_TOOL,
    MASTER_TOOLS,
    REACTIVE_MASTER_TOOLS,
    READ_ARTIFACT_TOOL,
    SPAWN_AGENTS_TOOL,
    SPAWN_MORE_AGENTS_TOOL,
    STORE_ARTIFACT_TOOL,
    SWARM_DONE_TOOL,
    SWARM_TOOL_NAMES,
)

__all__ = [
    # Protocol
    'SubTaskSpec', 'SubAgentResult', 'SubAgentStatus',
    'ArtifactStore', 'ArtifactBackend', 'InMemoryBackend',
    'SwarmEvent', 'SwarmEventType', 'AgentMessage',
    'compress_result', 'format_sub_results_for_master',
    # Execution
    'SubAgent', 'MasterOrchestrator', 'StreamingScheduler',
    'AsyncStreamingScheduler', 'RateLimiter',
    'resolve_execution_order', 'run_swarm_task', 'spawn_sub_agent',
    # Integration
    'execute_swarm_tool', 'get_active_session',
    # Registry
    'AGENT_ROLES', 'MODEL_TIERS',
    'scope_tools_for_role', 'get_tools_for_role',
    'get_role_system_suffix', 'get_role_config',
    'resolve_model_for_tier', 'configure_model_tiers',
    # Tool defs
    'SPAWN_AGENTS_TOOL', 'CHECK_AGENTS_TOOL',
    'SPAWN_MORE_AGENTS_TOOL', 'SWARM_DONE_TOOL',
    'STORE_ARTIFACT_TOOL', 'READ_ARTIFACT_TOOL', 'LIST_ARTIFACTS_TOOL',
    'MASTER_TOOLS', 'REACTIVE_MASTER_TOOLS', 'ARTIFACT_TOOLS',
    'SWARM_TOOL_NAMES',
]
