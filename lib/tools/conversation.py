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
            "Retrieve the content of another conversation by its ID. "
            "Use this when the user asks you to reference specific information, decisions, code changes, "
            "debugging context, or tool outputs from a previous conversation. "
            "First use list_conversations to find the right conversation ID.\n\n"
            "TWO output modes:\n"
            "• Default (raw=false) — a READABLE prose transcript of user prompts + assistant "
            "responses + a condensed view of tool calls/results. Best for understanding what was "
            "discussed, but it SUMMARIZES tool rounds and drops per-message metadata.\n"
            "• raw=true — for DEBUGGING / inspecting exact state. Returns the COMPLETE, un-summarized "
            "DB record as structured JSON: every row column (created_at, updated_at, msg_count, rev, "
            "settings) plus every field of every message preserved (finishReason, usage, model, "
            "timestamp, _msgId, modifiedFileList, the full toolRounds), nothing summarized or "
            "truncated away. Use this when you need the original metadata, not just the readable gist.\n\n"
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
                    "description": "Whether to include full tool call arguments and results (default: true). Set to false for a shorter summary. Ignored when raw=true."
                },
                "raw": {
                    "type": "boolean",
                    "description": "When true, return the full raw DB record (all columns + settings + every message field preserved) as structured JSON for debugging, instead of the readable prose transcript. Default: false."
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
# conversation, so they coordinate around one intent. An agent may READ it,
# PROPOSE amendments, and (2026-07-12, owner-directed) self-COMMIT a DECISION
# (append implementation-level consensus) so shared intent advances without a
# human in the loop. The agent path can ONLY append a decision — it can never
# edit the north-star `content` (goal/direction stays human-owned), and a human
# retains the corrective levers (edit/remove a decision, delete the charter).
# Project-scoped, registered only in project mode (registry._build_conv_ref).

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

CHARTER_COMMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "project_charter_commit",
        "description": (
            "COMMIT a new key DECISION to the project charter — this makes it "
            "project-wide shared intent that every sibling conversation reads. "
            "Unlike project_charter_propose (which only records a suggestion for "
            "later), this WRITES the decision immediately and bumps the charter "
            "version. Use it when you and/or siblings have reached an "
            "implementation-level consensus other conversations must align to "
            "(an architecture invariant, a build-order rule, a resolved design "
            "question). Be specific and actionable; anchor to concrete evidence.\n"
            "SCOPE: this tool can ONLY append a decision — it can NOT edit the "
            "project's north-star goal/direction text (that stays human-owned). "
            "A human retains the ability to edit or remove a committed decision "
            "afterwards, so this is self-service progress, not an irreversible "
            "act. If this decision resolves a proposal you (or a sibling) raised "
            "earlier with project_charter_propose, pass its proposalId as "
            "`resolves_proposal` so it drops out of the pending-review list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "description": "The decision text to commit. Specific and actionable — it becomes injected shared intent for all conversations."
                },
                "resolves_proposal": {
                    "type": "string",
                    "description": "Optional. The proposalId of a pending project_charter_propose this decision resolves, so it no longer shows as awaiting review."
                },
                "expected_version": {
                    "type": "integer",
                    "description": "Optional concurrency guard: the charter version you last read. If the charter has since changed, the commit is rejected and you should re-read and retry."
                },
            },
            "required": ["decision"],
        },
    },
}

CHARTER_TOOLS = [CHARTER_READ_TOOL, CHARTER_PROPOSE_TOOL, CHARTER_COMMIT_TOOL]
CHARTER_TOOL_NAMES = {'project_charter_read', 'project_charter_propose',
                      'project_charter_commit'}


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
                "write_set": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional list of file paths / globs / subsystem tags this epic intends to WRITE. Declaring it lets the dispatcher avoid handing two conversations epics that will fight over the same files (it prefers epics whose write-set is disjoint from currently-claimed ones). Optional and advisory — an omitted write-set is treated as 'unknown footprint' and never blocks dispatch."
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
            "Note that a board epic is WAITING on an external gate (a dependency, a "
            "missing decision, an external wait). This puts the epic on a "
            "SELF-EXPIRING, escalating cooldown so the autonomous heartbeat stops "
            "re-dispatching it (and burning a billed turn) while its gate is unmet — "
            "the cooldown grows on repeated blocks and auto-clears with NO human "
            "action, and a human reopen resets it for an immediate retry. In the "
            "reason, PREFIX the block CLASS so it's visible on the board: "
            "'[human-gated] …' when only a human action can satisfy it (e.g. infra "
            "sign-off — this escalates to a long sleep fast), or '[sibling] …' when it "
            "will auto-resolve once another conversation commits (retry-after-cooldown "
            "is expected). When the sibling blocker is specific file(s) that must be "
            "committed first, name them in a structured token "
            "'[sibling] path=lib/x.py,static/js/y.js …' — the epic is then HELD "
            "precisely while a sibling holds a lease on those paths (auto-released when "
            "they finish), the precise complement to the cooldown. Then state the "
            "concrete blocker."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The board epic id."},
                "reason": {"type": "string", "description": "Why it's blocked. PREFIX with the block class: '[human-gated] …' or '[sibling] …'. For a sibling-commit blocker, name the files as '[sibling] path=a.py,b.py …' to auto-hold on them."},
            },
            "required": ["task_id"],
        },
    },
}

# ── (Removed 2026-07-13) The path-lease (project_claim_path/project_release_path)
# and project_commit agent tools were deleted along with the multi-worktree /
# auto-land machinery. The project now uses a single shared checkout: agent
# edits write straight to the served tree and take effect on the next restart —
# no lease, no commit, no land step. A minor same-file overlap between siblings
# is hand-fixable interference, never a block. The board/charter/feed/peer
# blackboard (advisory, never blocking) remains the coordination surface.

BOARD_TOOLS = [BOARD_READ_TOOL, BOARD_POST_TOOL, BOARD_CLAIM_TOOL,
               BOARD_COMPLETE_TOOL, BOARD_BLOCK_TOOL]
BOARD_TOOL_NAMES = {'project_board_read', 'project_board_post',
                    'project_board_claim', 'project_board_complete',
                    'project_board_block'}


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
            "Flag to a sibling conversation (another AGENT) that its work may "
            "overlap or conflict — e.g. it is duplicating an epic you "
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
    'CHARTER_READ_TOOL', 'CHARTER_PROPOSE_TOOL', 'CHARTER_COMMIT_TOOL',
    'CHARTER_TOOLS', 'CHARTER_TOOL_NAMES',
    'BOARD_READ_TOOL', 'BOARD_POST_TOOL', 'BOARD_CLAIM_TOOL',
    'BOARD_COMPLETE_TOOL', 'BOARD_BLOCK_TOOL',
    'BOARD_TOOLS', 'BOARD_TOOL_NAMES',
    'PEER_STATUS_TOOL', 'PEER_FEED_TOOL', 'PEER_MESSAGE_TOOL',
    'PEER_INTERVENE_TOOL', 'PEER_TOOLS', 'PEER_TOOL_NAMES',
]
