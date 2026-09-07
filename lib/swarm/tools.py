"""lib/swarm/tools.py — Tool definitions for the async swarm protocol.

Two levels of swarm tools:

  1. **MASTER_TOOLS** — exposed to the main orchestrator LLM.

     • ``spawn_agents``      — fire-and-forget; returns a handle immediately
     • ``await_agents``      — block until ≥1 / all listed agents complete
     • ``get_agent_result``  — fetch one agent's full result
     • ``store_artifact`` / ``read_artifact`` / ``list_artifacts``
                             — shared key-value store

     Sub-agent results arrive as ``<swarm-update>...</swarm-update>`` user
     messages auto-injected at round boundaries (see ``lib/agent_inbox.py``).
     The model never has to poll.

  2. **SUB_AGENT_TOOLS** — granted to each sub-agent ON TOP of its
     role-scoped tool list. These are strictly the artifact tools:
     sub-agents can store/read/list, but **cannot** spawn, await, or
     query siblings — they have no view of the swarm at all.

Removed in the async migration (no longer exist):
  • ``check_agents``       — async push removes the need to poll status
  • ``spawn_more_agents``  — main agent uses ``spawn_agents`` again instead
  • ``swarm_done``          — async swarm has no internal mini-master to "stop"
"""

from lib.swarm.resource_policy import swarm_max_agents_per_wave


# ═══════════════════════════════════════════════════════════
#  Shared constants — MUST be defined before SPAWN_AGENTS_TOOL
# ═══════════════════════════════════════════════════════════
#
# ``SPAWN_AGENTS_TOOL``'s description is built at IMPORT TIME and
# ``format_role_catalogue()`` (lib/swarm/registry.py) reads
# ``ARTIFACT_TOOLS`` / ``SUB_AGENT_DENYLIST`` to render each role's tool
# list. Defining them below the master section makes the catalogue hit a
# partially-initialized module (ImportError). Keep them on top.

#: Names that MUST be stripped from sub-agents' tool lists. The master may
#: spawn / await / inspect; sub-agents may not. ``ask_human`` is also stripped
#: because sub-agents are not interactive.
SUB_AGENT_DENYLIST = frozenset({
    'spawn_agents',
    'await_agents',
    'get_agent_result',
    'ask_human',
})

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
            "Read data from the shared artifact store. Use to access "
            "intermediate results stored by other agents."
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

ARTIFACT_TOOLS = [STORE_ARTIFACT_TOOL, READ_ARTIFACT_TOOL, LIST_ARTIFACTS_TOOL]


# ═══════════════════════════════════════════════════════════
#  MASTER — spawn_agents (async)
# ═══════════════════════════════════════════════════════════

def _build_spawn_agents_description() -> str:
    """Build the spawn_agents tool description with the live role catalogue.

    Built lazily so that adding / removing roles in
    ``lib.swarm.registry.AGENT_ROLES`` automatically flows into the prompt
    the model sees, without a second source of truth.
    """
    from lib.swarm.registry import format_role_catalogue
    return (
        "Launch independent subtasks concurrently. Returns immediately with an "
        "async handle; each agent has its own session/tools, and results arrive "
        "later as `<swarm-update>` messages.\n\n"
        "Roles and exact tool scopes:\n"
        f"{format_role_catalogue()}\n\n"
        "Choose this only for 2+ independent branches (parallel sources or "
        "subsystems, independent review, or context isolation); keep trivial or "
        "sequential work local. A role must list every tool it needs. If the task "
        "needs a tool not listed by any specialist, use 'general'. Sub-agents "
        "cannot spawn or ask the user, so objectives must be self-contained. If a "
        "sub-agent reports a missing tool, Re-spawn the same task with "
        "role='general' or a covering role; never ask it to work around the gap or "
        "abandon the task.\n\n"
        "Put all parallel agents in one `agents[]` call. Use `depends_on` only "
        "for a true prerequisite; otherwise maximize parallelism. This is "
        "fire-and-forget and ends the turn: do not poll/sleep, predict results, or "
        "read `output_file` unless the user asks for progress. Continue useful "
        "local work on later turns; if none remains, use "
        "`await_agents(mode='any')`. Use `get_agent_result(id)` when a preview is "
        "insufficient.\n\n"
        "Each objective states the goal and why, needed context, paths, "
        "constraints, and expected bounded output. The root synthesizes results."
    )


SPAWN_AGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "spawn_agents",
        "description": _build_spawn_agents_description(),
        "parameters": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "maxItems": swarm_max_agents_per_wave(),
                    "description": (
                        "All parallel subtasks in one call; do not issue serial "
                        "spawn calls."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": (
                                    "Optional sibling-reference id. Set it when "
                                    "another item names it in `depends_on`; "
                                    "omission mints an id unavailable to same-call "
                                    "dependencies."
                                ),
                            },
                            "objective": {
                                "type": "string",
                                "description": (
                                    "Self-contained goal, rationale, necessary "
                                    "context, and expected output."
                                ),
                            },
                            "context": {
                                "type": "string",
                                "description": (
                                    "Optional paths, data, constraints, or links."
                                ),
                            },
                            "role": {
                                "type": "string",
                                "description": (
                                    "Specialist role; defaults to `general`. "
                                    "Exact scopes are listed above."
                                ),
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Sibling ids that must finish first. Use only "
                                    "for true prerequisites."
                                ),
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


# ═══════════════════════════════════════════════════════════
#  MASTER — await_agents (block until ≥1 / all complete)
# ═══════════════════════════════════════════════════════════

AWAIT_AGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "await_agents",
        "description": (
            "Block this turn until sub-agents complete. Use ONLY when you "
            "genuinely have no other work to do — otherwise let the swarm "
            "run in the background and continue with other tools.\n\n"
            "Returns the same `<swarm-update>` summaries that would have "
            "auto-injected on a later turn, batched together. Without ids, "
            "each completion is returned only once; use get_agent_result with "
            "explicit ids to reread a full result. Hard cap is "
            "60 seconds; if more agents are still running when the timeout "
            "elapses, the call returns what's done plus a list of stragglers. "
            "An identical retry with no new completion returns immediately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: specific agent ids to wait on. "
                        "If omitted, waits for ALL currently-running agents."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["any", "all"],
                    "description": (
                        "'any' returns when at least one matching agent "
                        "completes (default). 'all' waits for every match."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Max wait in seconds (default and hard cap 60). "
                        "On timeout, returns partial results + still-running list."
                    ),
                },
            },
        },
    },
}


# ═══════════════════════════════════════════════════════════
#  MASTER — get_agent_result
# ═══════════════════════════════════════════════════════════

GET_AGENT_RESULT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_agent_result",
        "description": (
            "Fetch the FULL final answer of one or more completed sub-agents. "
            "Use this when a `<swarm-update>` preview was insufficient and you "
            "need an agent's complete output (not just the truncated 200-char "
            "preview).\n\n"
            "For MULTIPLE agents in one call, provide an `agent_ids` array — "
            "all results come back together in a single `results` list. This "
            "is much better than issuing several separate get_agent_result "
            "calls: prefer ONE batched call when you want the full bodies of "
            "the agents a `spawn_agents` wave produced.\n\n"
            "For agents that are still running, the entry reports a "
            "running-status notice; for unknown ids it reports an error — "
            "neither aborts the rest of the batch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "A single agent id from a `<swarm-update>` payload or "
                        "the spawn_agents handle. Single-fetch mode; omit when "
                        "using `agent_ids`."
                    ),
                },
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Array of agent ids to fetch in ONE call (batch mode). "
                        "All results are returned together in the `results` "
                        "field. Use INSTEAD of `agent_id` when you need several "
                        "agents' full bodies — do NOT issue N separate "
                        "get_agent_result calls."
                    ),
                },
            },
        },
    },
}


# ═══════════════════════════════════════════════════════════
#  Bundles
# ═══════════════════════════════════════════════════════════

#: What the main orchestrator LLM sees.
MASTER_TOOLS = [
    SPAWN_AGENTS_TOOL,
    AWAIT_AGENTS_TOOL,
    GET_AGENT_RESULT_TOOL,
    STORE_ARTIFACT_TOOL,
    READ_ARTIFACT_TOOL,
    LIST_ARTIFACTS_TOOL,
]

#: What sub-agents may use IN ADDITION to their role-scoped tools.
#: Strictly the artifact tools — sub-agents have no swarm-control surface.
SUB_AGENT_TOOLS = list(ARTIFACT_TOOLS)




# ═══════════════════════════════════════════════════════════
#  Names — for routing & scoping
# ═══════════════════════════════════════════════════════════

#: All swarm-control tool names (routed by the executor's swarm dispatch).
#: Excludes artifact tools because those are handled inside SubAgent.
SWARM_CONTROL_TOOL_NAMES = frozenset({
    'spawn_agents',
    'await_agents',
    'get_agent_result',
})

#: Every name routed through ``execute_swarm_tool`` (control + artifact).
SWARM_TOOL_NAMES = frozenset({
    'spawn_agents', 'await_agents', 'get_agent_result',
    'store_artifact', 'read_artifact', 'list_artifacts',
})
