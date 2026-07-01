"""lib/tools/conversation.py — Conversation reference tool definitions."""

CONV_REF_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "list_conversations",
        "description": (
            "Search and list other conversations available in the application. "
            "Returns conversation IDs, titles, message counts, and timestamps. "
            "Use this to discover relevant past conversations before fetching their full content with get_conversation. "
            "The keyword matches both the conversation title AND its message content, so you can find a conversation by what was discussed.\n\n"
            "By default, when the current task is working inside a project, results are scoped to OTHER conversations of the SAME project (the most relevant siblings). Pass scope='all' to search across every conversation regardless of project.\n\n"
            "IMPORTANT: Only use this tool when the user EXPLICITLY asks to reference, search, or look up a previous conversation. "
            "Do NOT proactively call this to 'gather context' or 'understand background' on your own initiative."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Optional keyword to filter conversations by title or message content (case-insensitive substring match). Omit to list recent conversations."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of conversations to return (default: 20, max: 50)"
                },
                "scope": {
                    "type": "string",
                    "enum": ["auto", "project", "all"],
                    "description": "Which conversations to search. 'auto' (default) scopes to the current project when one is active, else all. 'project' forces same-project only. 'all' searches every conversation."
                }
            },
            "required": []
        }
    }
}

CONV_REF_GET_TOOL = {
    "type": "function",
    "function": {
        "name": "get_conversation",
        "description": (
            "Retrieve the full content of another conversation by its ID. "
            "Returns all messages including user prompts, assistant responses, tool calls, and tool results. "
            "Use this when the user asks you to reference specific information, decisions, code changes, "
            "debugging context, or tool outputs from a previous conversation. "
            "First use list_conversations to find the right conversation ID.\n\n"
            "IMPORTANT: Only use this when the user EXPLICITLY requests information from a past conversation. "
            "Never call this proactively or speculatively."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "The ID of the conversation to retrieve (use list_conversations to find IDs)"
                },
                "include_tool_details": {
                    "type": "boolean",
                    "description": "Whether to include full tool call arguments and results (default: true). Set to false for a shorter summary."
                }
            },
            "required": ["conversation_id"]
        }
    }
}

CONV_REF_TOOLS = [CONV_REF_LIST_TOOL, CONV_REF_GET_TOOL]
CONV_REF_TOOL_NAMES = {'list_conversations', 'get_conversation'}


# ── Project Charter tools (Pillar #2 of the project brain) ──
# The Charter is the shared "north star" of a project — read by every
# conversation, so they coordinate around one intent. An agent may READ it and
# PROPOSE amendments; it can NEVER commit a charter change directly (commit is
# human-gated). Both tools are project-scoped and registered only in project
# mode (registry._build_conv_ref).

CHARTER_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "project_charter_read",
        "description": (
            "Read this project's CHARTER — the shared north-star document every "
            "conversation of the project reads: the project goal/direction plus the "
            "list of COMMITTED key decisions. Use it to align your work with the "
            "project's shared intent and to avoid contradicting an already-committed "
            "decision. Read-only; returns the current charter text + decisions + version."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CHARTER_PROPOSE_TOOL = {
    "type": "function",
    "function": {
        "name": "project_charter_propose",
        "description": (
            "PROPOSE an amendment to the project charter (a new goal direction or a "
            "key decision you believe should become project-wide shared intent). This "
            "does NOT change the charter — it records your proposal for a human to "
            "review and commit. Use it when you've reached a decision that other "
            "conversations of this project should know about and align to. Be specific "
            "and actionable; anchor the proposal to concrete evidence, not vague intent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "proposal": {
                    "type": "string",
                    "description": "The proposed charter amendment / decision text. Specific and actionable."
                },
                "title": {
                    "type": "string",
                    "description": "Optional short label for the proposal."
                },
            },
            "required": ["proposal"],
        },
    },
}

CHARTER_TOOLS = [CHARTER_READ_TOOL, CHARTER_PROPOSE_TOOL]
CHARTER_TOOL_NAMES = {'project_charter_read', 'project_charter_propose'}


# ── Project Board tools (Pillar #3 — the coordination board) ──
# The board is what makes conversations AUTO-COORDINATE instead of colliding:
# read it before working, claim an epic so siblings step aside, complete it
# when done. Soft TTL leases — advisory, never a hard lock. Project-scoped.

BOARD_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "project_board_read",
        "description": (
            "Read this project's coordination BOARD before starting work — the list "
            "of epics with their status: OPEN (unclaimed), CLAIMED (another "
            "conversation is actively advancing it — do NOT duplicate), and recently "
            "DONE. Use it to avoid redoing or colliding with work a sibling "
            "conversation already owns, and to find an open epic to pick up. Read-only."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

BOARD_POST_TOOL = {
    "type": "function",
    "function": {
        "name": "project_board_post",
        "description": (
            "Post a new OPEN epic to the project board so sibling conversations can "
            "see and coordinate around it. Use for COARSE, human-meaningful units of "
            "work (an epic / workstream), NOT fine sub-steps. Keep titles concise."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short epic title."},
                "depends_on": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional list of board task ids this epic depends on."
                },
            },
            "required": ["title"],
        },
    },
}

BOARD_CLAIM_TOOL = {
    "type": "function",
    "function": {
        "name": "project_board_claim",
        "description": (
            "Claim an OPEN epic before you start working it, so sibling conversations "
            "know you own it and step aside (a soft, time-limited lease — advisory, it "
            "auto-expires so an abandoned epic frees up). Fails advisorily if another "
            "conversation already holds an active claim — in that case pick a different "
            "epic, don't duplicate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The board epic id (from project_board_read)."},
            },
            "required": ["task_id"],
        },
    },
}

BOARD_COMPLETE_TOOL = {
    "type": "function",
    "function": {
        "name": "project_board_complete",
        "description": (
            "Mark a board epic DONE when you've finished it, so siblings see it's "
            "complete and the board stays an accurate coordination surface."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The board epic id."},
            },
            "required": ["task_id"],
        },
    },
}

BOARD_BLOCK_TOOL = {
    "type": "function",
    "function": {
        "name": "project_board_block",
        "description": (
            "Report that a board epic is BLOCKED (you can't proceed — a dependency, a "
            "missing decision, an external wait). Surfaces the block in the project "
            "activity feed so a human or sibling conversation can unblock it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The board epic id."},
                "reason": {"type": "string", "description": "Why it's blocked."},
            },
            "required": ["task_id"],
        },
    },
}

BOARD_TOOLS = [BOARD_READ_TOOL, BOARD_POST_TOOL, BOARD_CLAIM_TOOL,
               BOARD_COMPLETE_TOOL, BOARD_BLOCK_TOOL]
BOARD_TOOL_NAMES = {'project_board_read', 'project_board_post',
                    'project_board_claim', 'project_board_complete',
                    'project_board_block'}

__all__ = [
    'CONV_REF_LIST_TOOL', 'CONV_REF_GET_TOOL',
    'CONV_REF_TOOLS', 'CONV_REF_TOOL_NAMES',
    'CHARTER_READ_TOOL', 'CHARTER_PROPOSE_TOOL',
    'CHARTER_TOOLS', 'CHARTER_TOOL_NAMES',
    'BOARD_READ_TOOL', 'BOARD_POST_TOOL', 'BOARD_CLAIM_TOOL',
    'BOARD_COMPLETE_TOOL', 'BOARD_BLOCK_TOOL',
    'BOARD_TOOLS', 'BOARD_TOOL_NAMES',
]
