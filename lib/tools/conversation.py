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

BOARD_DEFER_TOOL = {
    "type": "function",
    "function": {
        "name": "project_board_defer",
        "description": (
            "PARK a board epic that cannot progress autonomously right now — set "
            "its status to DEFERRED. Use this when an epic is gated on a decision "
            "only a human can make (e.g. a design-first / infra-choice epic you "
            "can't complete on your own): parking it STOPS the autonomous "
            "heartbeat from repeatedly re-dispatching it, so it no longer "
            "oscillates open↔claimed and wastes turns. The epic stays VISIBLE on "
            "the board (distinct from done) and a human reopens it when the "
            "blocking decision lands. This differs from project_board_block, which "
            "only flags a signal in the feed WITHOUT changing dispatchability — use "
            "defer to actually stop the sweep, block to merely flag."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The board epic id (from project_board_read)."},
                "reason": {"type": "string", "description": "Why it's parked (recorded in the project activity feed) — e.g. which human decision it awaits."},
            },
            "required": ["task_id"],
        },
    },
}

# ── Resource/path LEASE tools (durational file-avoidance) ──
# A lease is a PROACTIVE, path-level reservation posted BEFORE editing that
# reaches EVERY sibling (incl. an idle one the heartbeat wakes later) via the
# ambient [PROJECT BOARD] block. It is the STATE-based answer to "hold off on
# these paths" — complementary to the reactive, active-peers-only file-overlap
# advisory (lib/presence/conflict.py). NOT a broadcast (no fan-out messaging).
PATH_CLAIM_TOOL = {
    "type": "function",
    "function": {
        "name": "project_claim_path",
        "description": (
            "Reserve a file/path/subsystem you are about to change for a while, "
            "so sibling conversations hold off editing it concurrently. Use this "
            "BEFORE a big or long edit (e.g. 'I'm rewriting styles.css'): it "
            "posts a durational 'Held — do NOT edit' notice onto the project "
            "board that EVERY sibling sees on its next turn — including a "
            "currently-idle conversation the autonomous heartbeat wakes later "
            "(a plain message would miss it). This is a soft, advisory, "
            "auto-expiring lease (it can never deadlock the project); re-call to "
            "refresh the hold on a long job, and release it with "
            "project_release_path when done. It is NOT a lock and NOT a "
            "broadcast — it reserves a resource, it does not message anyone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": "The path(s)/subsystem to hold, e.g. 'static/styles.css' or 'the CSS layer'. Free text; one reservation per string."
                },
                "ttl_ms": {
                    "type": "integer",
                    "description": "Optional hold duration in ms (default 30 min). Ask for longer up front on a known-long job; a live holder can also just re-call to refresh."
                },
            },
            "required": ["resource"],
        },
    },
}

PATH_RELEASE_TOOL = {
    "type": "function",
    "function": {
        "name": "project_release_path",
        "description": (
            "Release a file/path reservation you previously took with "
            "project_claim_path, once you're done editing — clears the 'Held' "
            "notice for siblings. Only the holder can release its own hold; an "
            "unreleased hold simply auto-expires."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": "The exact resource string you held with project_claim_path."
                },
            },
            "required": ["resource"],
        },
    },
}

BOARD_TOOLS = [BOARD_READ_TOOL, BOARD_POST_TOOL, BOARD_CLAIM_TOOL,
               BOARD_COMPLETE_TOOL, BOARD_BLOCK_TOOL, BOARD_DEFER_TOOL,
               PATH_CLAIM_TOOL, PATH_RELEASE_TOOL]
BOARD_TOOL_NAMES = {'project_board_read', 'project_board_post',
                    'project_board_claim', 'project_board_complete',
                    'project_board_block', 'project_board_defer',
                    'project_claim_path', 'project_release_path'}


# ── Project Peer tools (Pillar #6 — cross-conversation communication) ──
# These close the last gap: the board/charter/feed give shared PERCEPTION, but
# nothing let one conversation TALK TO or INTERVENE in another. All three are
# project-scoped and advisory-first (a peer note the target sees on its NEXT
# turn — never a mid-stream interrupt). A genuine hard abort is human-gated.

PEER_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "project_peer_status",
        "description": (
            "See what OTHER conversations of this project are doing RIGHT NOW — "
            "LIVE state, not history. Returns each active sibling conversation "
            "(and its sub-agents): current phase, file being edited, tool round, "
            "and which board epic it is advancing. Use it to decide whether your "
            "planned work overlaps a sibling's, before you duplicate it. This is "
            "the live complement to get_conversation (which reads past messages)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "conv_id": {
                    "type": "string",
                    "description": "Optional: narrow to one peer conversation id (prefix ok). Omit to list all active peers."
                },
            },
            "required": [],
        },
    },
}

PEER_FEED_TOOL = {
    "type": "function",
    "function": {
        "name": "project_feed_read",
        "description": (
            "Read the recent cross-conversation ACTIVITY FEED of this project — "
            "a chronological pulse (newest first) of what sibling conversations "
            "have been DOING: task starts/completions, board claims, committed "
            "or proposed decisions, blocks, and peer notes. This is the "
            "narrative complement to project_peer_status (who is live NOW) and "
            "project_board_read (the epic lanes): use it to catch up on what "
            "already happened across the team before you start or hand off "
            "work. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max events to return, newest first (default 25, max 60)."
                },
            },
            "required": [],
        },
    },
}

PEER_MESSAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "project_message",
        "description": (
            "Coordinate DIRECTLY with a sibling conversation (another AGENT) of "
            "this project — find its id with project_peer_status. You are "
            "writing TO A PEER AGENT, not reporting to a human: use the "
            "imperative coordination register — CLAIM work ('I'm taking the "
            "parser refactor, stand down on lib/parser/'), CONFIRM a boundary "
            "('are you touching styles.css? I'm about to rewrite it'), HAND OFF "
            "context ('the schema bump you need is on branch X, done'), or WARN "
            "of an overlap ('your epic Y duplicates the one I already own'). Do "
            "NOT narrate your progress or write a status update as if to a "
            "human. The message lands in the peer's queue and is seen on its "
            "NEXT turn — it NEVER interrupts a live turn mid-stream. The peer "
            "acts autonomously on what you send. Rate-limited per target to "
            "prevent message storms — spend it on coordination that changes what "
            "the peer does, not FYI chatter."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to_conv_id": {
                    "type": "string",
                    "description": "The target sibling conversation id (from project_peer_status)."
                },
                "text": {
                    "type": "string",
                    "description": "The coordination message, addressed to the peer AGENT. A concrete coordination act — a claim, a boundary question, a hand-off, or an overlap warning — NOT a status report. State what you want the peer to do or confirm."
                },
            },
            "required": ["to_conv_id", "text"],
        },
    },
}

PEER_INTERVENE_TOOL = {
    "type": "function",
    "function": {
        "name": "project_intervene",
        "description": (
            "Tell a sibling conversation (another AGENT) to STOP or change "
            "course when it is going wrong — e.g. it is duplicating an epic you "
            "already own, or heading down a path a committed decision rules out. "
            "Write it as a direct coordination directive TO THE PEER AGENT "
            "('stop — I own the parser epic, drop it and re-check the board'), "
            "not as a status report to a human. By DEFAULT this is ADVISORY: a "
            "high-priority notice the peer sees on its next turn asking it to "
            "pause and re-check the board — it does NOT stop the peer, the peer "
            "decides how to respond. A genuine hard abort of the peer's running "
            "task requires explicit HUMAN approval and cannot be done "
            "unilaterally by an agent; if you set hard_abort without that "
            "approval it is refused with guidance.\n"
            "Note: an advisory intervention shares the SAME per-target rate-limit "
            "budget as project_message (a few per target per window), so it can be "
            "refused if you have recently messaged the same conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to_conv_id": {
                    "type": "string",
                    "description": "The target sibling conversation id."
                },
                "message": {
                    "type": "string",
                    "description": "The directive to the peer AGENT explaining WHAT to stop/change and WHY (e.g. which epic overlaps and that you own it). Optional; a sensible default is used if omitted."
                },
                "hard_abort": {
                    "type": "boolean",
                    "description": "Request a hard abort of the peer's running task. Requires human approval — refused without it. Default false (advisory)."
                },
            },
            "required": ["to_conv_id"],
        },
    },
}

PEER_TOOLS = [PEER_STATUS_TOOL, PEER_FEED_TOOL, PEER_MESSAGE_TOOL,
              PEER_INTERVENE_TOOL]
PEER_TOOL_NAMES = {'project_peer_status', 'project_feed_read',
                   'project_message', 'project_intervene'}

__all__ = [
    'CONV_REF_LIST_TOOL', 'CONV_REF_GET_TOOL',
    'CONV_REF_TOOLS', 'CONV_REF_TOOL_NAMES',
    'CHARTER_READ_TOOL', 'CHARTER_PROPOSE_TOOL',
    'CHARTER_TOOLS', 'CHARTER_TOOL_NAMES',
    'BOARD_READ_TOOL', 'BOARD_POST_TOOL', 'BOARD_CLAIM_TOOL',
    'BOARD_COMPLETE_TOOL', 'BOARD_BLOCK_TOOL', 'BOARD_DEFER_TOOL',
    'PATH_CLAIM_TOOL', 'PATH_RELEASE_TOOL',
    'BOARD_TOOLS', 'BOARD_TOOL_NAMES',
    'PEER_STATUS_TOOL', 'PEER_FEED_TOOL', 'PEER_MESSAGE_TOOL',
    'PEER_INTERVENE_TOOL', 'PEER_TOOLS', 'PEER_TOOL_NAMES',
]
