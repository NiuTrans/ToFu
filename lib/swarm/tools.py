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
        "Launch one or more sub-agents in parallel to work on independent parts "
        "of the task. The call returns IMMEDIATELY with a handle "
        "(`{status:'async_launched', swarm_id, agents:[...]}`); each sub-agent "
        "runs in the background with its own LLM session and tool access, and "
        "completions arrive automatically on subsequent turns as `<swarm-update>` "
        "user messages.\n\n"
        "Available roles, when to choose each, and the tools each one "
        "actually receives:\n"
        f"{format_role_catalogue()}\n\n"
        "Each role sees ONLY the tools listed for it, plus the shared "
        "artifact tools. If the task needs a tool not listed for any "
        "specialist role (for example reading this app's past conversations "
        "via get_conversation), use 'general'. Sub-agents never receive the "
        "denylisted tools and cannot ask the user anything, so objectives "
        "must be fully self-contained.\n\n"
        "## If a sub-agent reports a missing tool\n"
        "- You picked the wrong role. Re-spawn the same task with "
        "role='general' (or a role whose tool list covers the need) — do "
        "NOT tell the sub-agent to work around a missing tool, and do not "
        "abandon the task.\n\n"
        "## When you SHOULD spawn agents\n"
        "- The user asks a question whose answer needs **2+ independent pieces** "
        "of work (e.g. \"is this branch ready to ship?\" → git audit + test audit + "
        "flag audit, three independent investigations).\n"
        "- A research task spans **multiple sources** (web pages, repos, libraries) "
        "that don't depend on each other.\n"
        "- A code task touches **multiple unrelated files / subsystems** that can "
        "be investigated or changed in parallel.\n"
        "- You want a **second-opinion review** of code or a design — spawn a "
        "`reviewer` so it forms its own conclusion without seeing your analysis.\n"
        "- You'd otherwise dump a lot of low-value tool output (long greps, big "
        "file reads) into your own context — fork that work to a sub-agent so "
        "only the conclusion comes back.\n\n"
        "## When NOT to spawn\n"
        "- Trivial single-step questions (one tool call would do it).\n"
        "- Tasks that are inherently sequential — each step needs the previous "
        "answer.\n"
        "- Reading one specific file you already know the path of (just use "
        "`read_files`).\n"
        "- A grep you can do yourself in a single call.\n\n"
        "## Mechanics\n"
        "- This tool is **fire-and-forget**. After calling, your turn ends. "
        "Sub-agent results land on later turns as `<swarm-update>` blocks; you "
        "do NOT poll, sleep, or check on them.\n"
        "- **Never fabricate or predict** sub-agent results before their "
        "`<swarm-update>` arrives. If the user asks mid-wait, give status, not a "
        "guess.\n"
        "- **Never read the `output_file`** unless the user explicitly asks for "
        "a progress check — that just imports the sub-agent's tool noise into "
        "your own context.\n"
        "- If you have nothing useful to do while waiting, call "
        "`await_agents(mode='any')` to block on the next completion. If a "
        "preview is too short, call `get_agent_result(id)` for the full body.\n"
        "- To launch agents in parallel **send a SINGLE message with one "
        "`spawn_agents` call containing multiple `agents` entries** — do NOT "
        "issue several spawn_agents calls in sequence; that defeats the parallelism.\n"
        "- Use `depends_on: [\"<id-of-prereq>\"]` ONLY when one agent's output "
        "is genuinely a prerequisite for another. Prefer maximum parallelism.\n"
        "- Sub-agents cannot themselves spawn further agents and cannot ask the "
        "user. Don't write objectives that assume they can.\n\n"
        "## Writing a good objective\n"
        "- Treat the sub-agent like a smart colleague who just walked in. It "
        "has none of your conversation context.\n"
        "- Say what to accomplish AND why, plus any context (file paths, URLs, "
        "constraints) it needs to do its job.\n"
        "- Specify the output you expect (\"report a punch list, under 200 "
        "words\"). Vague prompts produce shallow results.\n"
        "- Don't write \"based on your findings, fix the bug\" — that pushes "
        "synthesis onto the sub-agent. Synthesise in your own turn after the "
        "`<swarm-update>` lands."
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
                        "List of sub-tasks to execute IN PARALLEL within this "
                        "single tool call. To run N agents in parallel, put N "
                        "items here — do NOT issue N separate spawn_agents "
                        "tool calls."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": (
                                    "Optional stable id for THIS agent (e.g. 'audit', 'a1'). "
                                    "Set it when another agent in the same call must reference "
                                    "this one via `depends_on` — the referenced id must match "
                                    "exactly. When omitted a random id is assigned, which "
                                    "cannot be targeted by `depends_on` within the same call."
                                ),
                            },
                            "objective": {
                                "type": "string",
                                "description": (
                                    "What this sub-agent should accomplish. Brief it like a "
                                    "smart colleague who just walked in — explain the goal, "
                                    "give it the context it needs (file paths, URLs, prior "
                                    "findings), and say what output you expect."
                                ),
                            },
                            "context": {
                                "type": "string",
                                "description": (
                                    "Optional extra context: file paths, data, "
                                    "constraints, links to relevant docs."
                                ),
                            },
                            "role": {
                                "type": "string",
                                "description": (
                                    "One of: researcher / coder / analyst / "
                                    "browser / reviewer / writer / general. "
                                    "See the tool description for what each "
                                    "role is for. Defaults to 'general' when "
                                    "omitted."
                                ),
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional: ids of sibling agents in this "
                                    "spawn that must complete first. Use "
                                    "sparingly — prefer maximum parallelism."
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
            "auto-injected on a later turn, batched together. Hard cap is "
            "120 seconds; if more agents are still running when the timeout "
            "elapses, the call returns what's done plus a list of stragglers."
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
                        "Max wait in seconds (default 60, hard cap 120). "
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

#: The three MASTER swarm-control tools, as a list — the follow-up surface a
#: turn needs to COLLECT results from an already-launched swarm
#: (await_agents / get_agent_result) plus the ability to launch more.
MASTER_CONTROL_TOOLS = [SPAWN_AGENTS_TOOL, AWAIT_AGENTS_TOOL, GET_AGENT_RESULT_TOOL]


def augment_with_swarm_tools(tool_list):
    """Append any MISSING master swarm-control tools to *tool_list*.

    Pure — no DB / session / inbox lookup. The caller decides WHEN to call
    this (see :func:`resolve_turn_swarm_tools`). Idempotent: a list that
    already carries all three names is returned unchanged (same object) with
    an empty ``added`` list, so a turn that legitimately has swarm enabled
    incurs no churn.

    Args:
        tool_list: The turn's assembled tool list (list of OpenAI-style tool
            dicts), or ``None``.

    Returns:
        ``(new_list, added_names)`` — ``new_list`` is the original object when
        nothing was added, else a NEW list with the missing tools appended;
        ``added_names`` is the list of tool names that were injected.
    """
    existing: set[str] = set()
    for t in (tool_list or []):
        if isinstance(t, dict):
            n = (t.get('function') or {}).get('name')
            if n:
                existing.add(n)
    to_add = [tool for tool in MASTER_CONTROL_TOOLS
              if tool['function']['name'] not in existing]
    if not to_add:
        return tool_list, []
    merged = list(tool_list or [])
    merged.extend(to_add)
    return merged, [tool['function']['name'] for tool in to_add]


def resolve_turn_swarm_tools(tool_list, *, swarm_enabled: bool,
                             has_pending_or_live: bool):
    """Decide a turn's tool list w.r.t. the swarm follow-up tools.

    Root fix for the "swarm-update told me to call get_agent_result but that
    tool isn't in my schema → rejected as hallucinated" desync (conv
    ``mr2ysg473scxv8``): the swarm inbox drain is UNGATED and will inject a
    ``<swarm-update>`` instructing the model to call
    ``await_agents`` / ``get_agent_result`` even on a turn whose
    ``swarmEnabled`` is false. If a swarm is live-or-pending for the
    conversation, those tools MUST be real for this turn.

    Pure and fully injectable — ``has_pending_or_live`` is the caller's
    resolved answer to "is there a live session OR a pending inbox for this
    conversation?" (see ``lib.swarm.integration.has_live_or_pending_swarm``).

    Returns ``(tool_list, forced_names)``:
      * ``swarm_enabled`` true (assembly already added them) → unchanged, ``[]``.
      * ``swarm_enabled`` false AND ``has_pending_or_live`` → force the three
        master tools in because pending work remains actionable even when the
        composer toggle is currently off.
      * otherwise → unchanged, ``[]``.
    """
    if swarm_enabled or not has_pending_or_live:
        return tool_list, []
    return augment_with_swarm_tools(tool_list or [])


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
