"""lib/swarm/tools.py — Tool definitions for the swarm system.

Three levels of tool integration:
  1. The ``spawn_agents`` tool — exposed to the main orchestrator LLM.
     When called, the executor delegates to integration.py which runs
     the full swarm lifecycle (spawn → execute → reactive review → synthesise).

  2. The ``REACTIVE_MASTER_TOOLS`` — used by the master LLM during reactive
     review rounds to decide whether to spawn more agents, check status,
     or declare done.  Also includes artifact-read tools so the master can
     inspect agent outputs.

  3. The ``ARTIFACT_TOOLS`` — extra tools available to each sub-agent
     (artifact read/write for inter-agent data sharing).
"""


# ═══════════════════════════════════════════════════════════
#  Level 1 — Tools exposed to the MAIN orchestrator
# ═══════════════════════════════════════════════════════════

SPAWN_AGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "spawn_agents",
        "description": (
            "Split a task into multiple sub-tasks and execute them IN PARALLEL for speed. "
            "Each sub-task runs as an independent agent with its own LLM session and full tool access. "
            "All results are collected and returned together.\n\n"
            "ALWAYS prefer spawning agents over doing sequential work when a task can be decomposed. "
            "More agents = faster completion. Don't overthink roles — just describe what each agent should do."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "description": "List of sub-tasks to execute in parallel",
                    "items": {
                        "type": "object",
                        "properties": {
                            "objective": {
                                "type": "string",
                                "description": "Clear, specific task description — what this agent should accomplish",
                            },
                            "context": {
                                "type": "string",
                                "description": "Any relevant context: file paths, data, constraints, etc.",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "Indices (0-based) of agents that must complete first (use sparingly — parallelism is the goal)",
                            },
                        },
                        "required": ["objective"],
                    },
                },
            },
            "required": ["agents"],
        },
    },
}

CHECK_AGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "check_agents",
        "description": (
            "Check the status of spawned sub-agents. "
            "Returns current status, progress, and any completed results."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

# Tools the main orchestrator LLM sees
MASTER_TOOLS = [SPAWN_AGENTS_TOOL, CHECK_AGENTS_TOOL]


# ═══════════════════════════════════════════════════════════
#  Level 3 — Tools for SUB-AGENTS (artifact sharing)
#  (Defined before Level 2 so REACTIVE_MASTER_TOOLS can
#   reference artifact tools.)
# ═══════════════════════════════════════════════════════════

STORE_ARTIFACT_TOOL = {
    "type": "function",
    "function": {
        "name": "store_artifact",
        "description": (
            "Store data in the shared artifact store for other agents to read. "
            "Use for intermediate results, extracted data, or analysis that "
            "downstream agents will need."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Unique key for the artifact (e.g. 'file_analysis_results')",
                },
                "content": {
                    "type": "string",
                    "description": "The artifact content to store",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization",
                },
            },
            "required": ["key", "content"],
        },
    },
}

READ_ARTIFACT_TOOL = {
    "type": "function",
    "function": {
        "name": "read_artifact",
        "description": (
            "Read data from the shared artifact store. "
            "Use to access results from agents that ran before you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key of the artifact to read",
                },
            },
            "required": ["key"],
        },
    },
}

LIST_ARTIFACTS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_artifacts",
        "description": "List all available artifacts in the shared store.",
        "parameters": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Optional tag to filter artifacts",
                },
            },
        },
    },
}

# Combined list for convenience
ARTIFACT_TOOLS = [STORE_ARTIFACT_TOOL, READ_ARTIFACT_TOOL, LIST_ARTIFACTS_TOOL]
SUB_AGENT_TOOLS = ARTIFACT_TOOLS  # Alias for backward compat


# ═══════════════════════════════════════════════════════════
#  Level 2 — Tools for the REACTIVE MASTER LLM
# ═══════════════════════════════════════════════════════════

SPAWN_MORE_AGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "spawn_more_agents",
        "description": (
            "Spawn additional sub-agents based on insights from completed agents. "
            "Use when results reveal new subtasks, gaps to fill, or follow-up work needed. "
            "New agents can reference completed agents' results via context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "description": "New sub-agent specifications",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "enum": ["researcher", "coder", "analyst",
                                         "browser", "reviewer", "writer", "general"],
                            },
                            "objective": {"type": "string"},
                            "context": {"type": "string"},
                            "id": {"type": "string"},
                        },
                        "required": ["role", "objective"],
                    },
                },
                "reason": {
                    "type": "string",
                    "description": "Why these additional agents are needed",
                },
            },
            "required": ["agents"],
        },
    },
}

SWARM_DONE_TOOL = {
    "type": "function",
    "function": {
        "name": "swarm_done",
        "description": (
            "Signal that all necessary work is complete and no more agents are needed. "
            "Call this when: (1) all subtasks are adequately covered, "
            "(2) results are sufficient to answer the original query, "
            "(3) further agents would not add value."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was accomplished across all agents",
                },
            },
            "required": ["summary"],
        },
    },
}

# Reactive master gets spawn/done controls PLUS artifact-read tools
# so it can inspect agent outputs when deciding whether more work is needed.
REACTIVE_MASTER_TOOLS = [
    SPAWN_MORE_AGENTS_TOOL,
    SWARM_DONE_TOOL,
    READ_ARTIFACT_TOOL,
    LIST_ARTIFACTS_TOOL,
]


# ═══════════════════════════════════════════════════════════
#  All swarm tool names (for routing in executor)
# ═══════════════════════════════════════════════════════════

SWARM_TOOL_NAMES = frozenset([
    'spawn_agents', 'check_agents', 'spawn_more_agents',
    'swarm_done', 'store_artifact', 'read_artifact', 'list_artifacts',
])
