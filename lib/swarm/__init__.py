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

from importlib import import_module

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


# Importing a child such as ``lib.swarm.registry`` must not initialize agent
# execution, scheduler, task-manager, project-tool, or integration state. Keep
# the historical package-level API through lazy attribute resolution instead.
_EXPORT_MODULES = {
    # Protocol and artifacts.
    'SubTaskSpec': 'lib.swarm.protocol',
    'SubAgentResult': 'lib.swarm.protocol',
    'SubAgentStatus': 'lib.swarm.protocol',
    'SwarmEvent': 'lib.swarm.protocol',
    'SwarmEventType': 'lib.swarm.protocol',
    'AgentMessage': 'lib.swarm.protocol',
    'resolve_execution_order': 'lib.swarm.protocol',
    'ArtifactStore': 'lib.swarm.artifact_store',
    'ArtifactBackend': 'lib.swarm.artifact_store',
    'InMemoryBackend': 'lib.swarm.artifact_store',
    'compress_result': 'lib.swarm.result_format',
    'format_sub_results_for_master': 'lib.swarm.result_format',
    # Execution and integration.
    'SubAgent': 'lib.swarm.agent',
    'MasterOrchestrator': 'lib.swarm.master',
    'StreamingScheduler': 'lib.swarm.scheduler',
    'AsyncStreamingScheduler': 'lib.swarm.scheduler',
    'RateLimiter': 'lib.swarm.rate_limiter',
    'execute_swarm_tool': 'lib.swarm.integration',
    'get_active_session': 'lib.swarm.integration',
    'rehydrate_swarms_on_startup': 'lib.swarm.integration',
    # Role registry.
    'AGENT_ROLES': 'lib.swarm.registry',
    'MODEL_TIERS': 'lib.swarm.registry',
    'scope_tools_for_role': 'lib.swarm.registry',
    'get_tools_for_role': 'lib.swarm.registry',
    'get_role_system_suffix': 'lib.swarm.registry',
    'get_role_config': 'lib.swarm.registry',
    'resolve_model_for_tier': 'lib.swarm.registry',
    'configure_model_tiers': 'lib.swarm.registry',
    # Tool schemas.
    'SPAWN_AGENTS_TOOL': 'lib.swarm.tools',
    'AWAIT_AGENTS_TOOL': 'lib.swarm.tools',
    'GET_AGENT_RESULT_TOOL': 'lib.swarm.tools',
    'STORE_ARTIFACT_TOOL': 'lib.swarm.tools',
    'READ_ARTIFACT_TOOL': 'lib.swarm.tools',
    'LIST_ARTIFACTS_TOOL': 'lib.swarm.tools',
    'MASTER_TOOLS': 'lib.swarm.tools',
    'SUB_AGENT_TOOLS': 'lib.swarm.tools',
    'ARTIFACT_TOOLS': 'lib.swarm.tools',
    'SWARM_TOOL_NAMES': 'lib.swarm.tools',
    'SWARM_CONTROL_TOOL_NAMES': 'lib.swarm.tools',
    'SUB_AGENT_DENYLIST': 'lib.swarm.tools',
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()).union(__all__))
