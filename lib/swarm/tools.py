"""lib/swarm/tools.py — Tool definitions for the async swarm protocol.

Two levels of swarm tools:

  1. **MASTER_TOOLS** — exposed to the main orchestrator LLM.

     • ``spawn_agents``      — fire-and-forget; returns a handle immediately
     • ``get_agent_result``  — fetch one agent's full result
     • ``store_artifact`` / ``read_artifact`` / ``list_artifacts``
                             — shared key-value store

     Sub-agent results arrive as ``<swarm-update>...</swarm-update>`` user
     messages auto-injected at round boundaries (see ``lib/agent_inbox.py``).
     The background family contributes ``poll`` / ``kill`` separately.
     Wait with returned ``id`` / ``ids`` and ``wait_s``; no-id ``poll`` is
     only a dashboard. Automatic delivery makes status polling unnecessary.

  2. **SUB_AGENT_TOOLS** — granted to each sub-agent ON TOP of its
     role-scoped tool list. These are strictly the artifact tools:
     sub-agents can store/read/list, but **cannot** spawn, await, or
     query siblings — they have no view of the swarm at all.

Removed in the async migration (no longer exist):
  • ``check_agents``       — async push removes the need to poll status
  • ``spawn_more_agents``  — main agent uses ``spawn_agents`` again instead
  • ``swarm_done``          — async swarm has no internal mini-master to "stop"
"""


from lib.swarm.message_contract import SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL


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
#: because sub-agents are not interactive. ``set_approval_delegate`` registers
#: an approval policy on the task — control-plane authority that must never
#: be exercisable by a worker (nested adjudication + policy escalation).
SUB_AGENT_DENYLIST = frozenset({

    'send_project_message', 'get_project_messages', 'consider_project_message',
    'spawn_agents',
    'manage_agents',
    'list_agents',
    'await_agents',
    'get_agent_result',
    'ask_human',

    'send_agent_message',
    'get_agent_messages',
    'set_approval_delegate',
    # kill is control-plane authority over background handles (including
    # sibling agents); a worker must never hold it.
    'kill',
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
    # Role scoping still denies replay-only control names. The public prompt
    # must not teach those retired surfaces as tools to discover or call.
    role_catalogue = format_role_catalogue().replace(
        '/'.join(sorted(SUB_AGENT_DENYLIST)),
        '/'.join(sorted(SUB_AGENT_DENYLIST - {'await_agents'})),
    )
    return (
        "Launch one-off independent subtasks concurrently. For a named worker "
        "you can assign again later, use manage_agents. Returns immediately with an "
        "async handle; each agent has its own session/tools, and results arrive "
        "later as `<swarm-update>` messages.\n\n"

        "Use send_agent_message with the returned agent_id and swarm_run_id "
        "to add bounded context to an existing pending/running child. It starts "
        "no new work and does not grant permissions. get_agent_messages reads "
        "actual input receipts on demand, not in a polling loop. manage_agents "
        "assign is a new run for an idle identity, not live steering.\n\n"
        "Roles and exact tool scopes:\n"
        f"{role_catalogue}\n\n"
        "Choose this for 2+ independent branches or context isolation; keep trivial or sequential "
        "work local. Delegate MCP tools via the `tools` allowlist instead of loading "
        "them yourself. If the task needs a tool not listed by any specialist, use 'general'. "
        "Sub-agents cannot spawn or ask the user; provide self-contained objectives. "
        "If a sub-agent reports a missing tool, Re-spawn the same task with role='general' or a covering role; "
        "preserve scope and finish the task.\n\n"
        "Put all parallel agents in one `agents[]` call. Use `depends_on` only "
        "for a true prerequisite; otherwise maximize parallelism. This is "
        "fire-and-forget: continue useful local work or end the turn for "
        "automatic delivery. Do not sleep, predict results, or read "
        "`output_file` for progress. If only waiting remains, use "
        "`poll(ids=[<returned agent ids>], wait_s=60, mode='any')`; no-id "
        "`poll` is a dashboard, not a wait. Each completion arrives with the agent's "
        "full result; `get_agent_result(id)` re-reads it later. A running "
        "main agent receives individual results at round "
        "boundaries; an idle conversation is auto-resumed only after the whole "
        "swarm settles, subject to Stop and continuation limits. Failures may "
        "trigger one bounded diagnosis/closure pass, not an unlimited retry loop. "
        "Do not promise a wakeup on each agent completion or treat failed work "
        "as side-effect-free.\n\n"
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
                            "required_for_completion": {
                                "type": "boolean", "default": True,
                                "description": "Required delivery dependency; false only for optional exploration. Unresolved required work remains waiting after this turn ends.",
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
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Tool-name allowlist (e.g. "
                                    "['mcp__github__get_issue']); narrow-only, "
                                    "intersected with role scope."
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
            "Block this turn until sub-agents spawned via spawn_agents complete "
            "(hard cap 60s per call; on timeout agents keep running — call "
            "again). Errors immediately when no swarm is running; it never "
            "waits for background commands. Use ONLY when you have no other "
            "work to do."
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
            "Fetch the FULL final answer of one or more completed sub-agents "
            "again. Completion updates already carry the full result — use "
            "this to re-read an earlier result after it aged out of your "
            "context.\n\n"
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
    GET_AGENT_RESULT_TOOL,
    STORE_ARTIFACT_TOOL,
    READ_ARTIFACT_TOOL,
    LIST_ARTIFACTS_TOOL,
]

# Append-only additions preserve every established wire tool position.
MASTER_TOOLS.extend([SEND_AGENT_MESSAGE_TOOL, GET_AGENT_MESSAGES_TOOL])

#: What sub-agents may use IN ADDITION to their role-scoped tools.
#: Strictly the artifact tools — sub-agents have no swarm-control surface.
SUB_AGENT_TOOLS = list(ARTIFACT_TOOLS)




# ═══════════════════════════════════════════════════════════
#  Names — for routing & scoping
# ═══════════════════════════════════════════════════════════

#: All swarm-control tool names (routed by the executor's swarm dispatch).
#: Excludes artifact tools because those are handled inside SubAgent.
SWARM_CONTROL_TOOL_NAMES = frozenset({

    'send_agent_message', 'get_agent_messages',
    'spawn_agents',
    'await_agents',
    'get_agent_result',
})

#: Every name routed through ``execute_swarm_tool`` (control + artifact).
SWARM_TOOL_NAMES = frozenset({

    'send_agent_message', 'get_agent_messages',
    'spawn_agents', 'await_agents', 'get_agent_result',
    'store_artifact', 'read_artifact', 'list_artifacts',
})
