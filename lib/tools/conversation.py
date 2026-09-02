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
            "DEFAULT OUTPUT IS RAW — omit `raw` and you get the COMPLETE, "
            "un-summarized DB record as structured JSON, the way you would read "
            "it straight out of the database: every row column (created_at, "
            "updated_at, msg_count, rev, settings) plus every field of every "
            "message preserved (finishReason, usage, model, timestamp, _msgId, "
            "modifiedFileList, the full toolRounds). This is what you want for "
            "debugging and for any question about what actually happened, "
            "because nothing is summarized away.\n"
            "Pass raw=false ONLY when you want a READABLE prose transcript "
            "instead — user prompts + assistant responses + a condensed view of "
            "tool calls. It reads more easily but SUMMARIZES tool rounds and "
            "drops per-message metadata, so a detail you need may simply not be "
            "there.\n\n"
            "Long conversations are WINDOWED (head + tail) rather than cut "
            "mid-token, so the JSON always parses. The header states DELIVERED "
            "N of M messages — check it, because on a long conversation one "
            "call cannot carry everything. Use `before` to page backwards "
            "through older messages and `limit` to widen the window.\n\n"
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
                    "description": "Whether to include full tool call arguments and results (default: true). Set to false for a shorter summary. Only applies to the raw=false prose transcript; the raw record always carries them."
                },
                "raw": {
                    "type": "boolean",
                    "description": "Output mode. DEFAULT true — the full raw DB record (all columns + settings + every message field preserved) as structured JSON, nothing summarized. Pass false for the readable prose transcript, which drops per-message metadata and condenses tool rounds."
                },
                "limit": {
                    "type": "integer",
                    "description": "How many recent messages to render. Omit for a window sized to fit the output budget with whole, unclamped messages. Raising it past what fits causes per-message fields to be clamped, which the header reports."
                },
                "before": {
                    "type": "integer",
                    "description": "Page backwards: render the window ending just BEFORE this message number (1-based, exclusive). Take it from the header's 're-read with before=N' hint to walk further back through a long history."
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
# PROPOSE amendments. It may NOT commit: a charter always requires human review
# (owner-directed 2026-07-30, reversing the 2026-07-12 self-commit de-gating).
# Committing, editing and removing decisions are human actions on the REST
# routes. Project-scoped, registered only in project mode
# (registry._build_conv_ref).

CHARTER_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "project_charter_read",
        "description": (
            "Read this project's CHARTER — the shared north-star document every "
            "conversation of the project reads: the project goal/direction plus the "
            "list of COMMITTED key decisions. Use it to align your work with the "
            "project's shared intent and to avoid contradicting an already-committed "
            "decision. Read-only. DEFAULT returns the headline list (the same "
            "shape the per-turn injection shows); pass `index` for ONE entry's "
            "full text — the evidence chain costs one entry, not the whole charter. "
            "To raise a NEW key decision for human review, use project_charter_propose."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "Optional. 0-based decision index (negative counts from the end). When given, returns ONLY that entry's full text + its summary."
                },
            },
            "required": [],
        },
    },
}

CHARTER_PROPOSE_TOOL = {
    "type": "function",
    "function": {
        "name": "project_charter_propose",
        "description": (
            "PROPOSE an amendment to the project charter — the ONLY way an "
            "agent can put something into the charter, and it takes effect only "
            "after the HUMAN approves it. The charter is human-reviewed by "
            "design: it is the shared north star every sibling conversation "
            "reads, so nothing lands in it unilaterally.\n"
            "Use this for a binding rule / architecture invariant you have "
            "concluded and want other conversations to align to — state it as a "
            "decision, not a musing, and anchor it to concrete measured "
            "evidence. Your work does NOT wait on the review; continue, and "
            "record what you actually did in JOURNAL.md.\n"
            "For a methodology lesson (how to work, not what is true of this "
            "codebase), prefer create_memory — it surfaces by relevance when a "
            "conversation touches that topic, instead of growing the charter "
            "every conversation pays for on every turn."
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
            "COMMIT a new key DECISION — immediately, without a human gate. "
            "Every commit MUST declare its `kind`, and the kind decides WHERE "
            "the text lands:\n"
            "  invariant — a binding rule that constrains FUTURE code and "
            "decisions (an architecture invariant, a build-order rule, a "
            "resolved design question other conversations must align to). "
            "LANDS IN THE CHARTER; every sibling reads its one-line `summary` "
            "in the injected block; the full `decision` text (evidence, "
            "archaeology) is read back on demand via project_charter_read.\n"
            "  lesson — a methodology experience note (e.g. 'guards must "
            "assert results, not implementation'). ROUTED TO PROJECT MEMORY "
            "instead — it surfaces via relevance prefetch exactly when a "
            "conversation works on that topic, and same-topic lessons are "
            "dedup-ed into one living memory. Does NOT grow the charter.\n"
            "  report — a completion / 'we decided not to' record. REJECTED: "
            "it constrains no future decision. Append it to JOURNAL.md "
            "instead.\n"
            "The test for the kind: does this text CHANGE what someone "
            "decides next week? Binding rule → invariant. How-to-work "
            "knowledge → lesson. What-happened → report.\n"
            "SCOPE: this tool can ONLY append — it can NOT edit the project's "
            "north-star goal/direction text (that stays human-owned). A human "
            "retains the ability to edit or remove a committed invariant "
            "afterwards, so this is self-service progress, not an "
            "irreversible act. If this resolves a proposal raised earlier with "
            "project_charter_propose, pass its proposalId as "
            "`resolves_proposal`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["invariant", "lesson", "report"],
                    "description": "Where this text lands: invariant → charter (binding rule); lesson → project memory (methodology, dedup-ed); report → rejected, belongs in JOURNAL.md."
                },
                "decision": {
                    "type": "string",
                    "description": "The full text. For an invariant this is the complete record (evidence, reasoning, anchors) — the injection shows only `summary`. For a lesson, the experience note that lands in project memory."
                },
                "summary": {
                    "type": "string",
                    "description": "REQUIRED when kind=invariant: ONE line stating the binding rule itself (e.g. 'Credential redaction is a fail-closed whitelist; never revert to name-based exclusion'). The per-turn injection renders ONLY this line."
                },
                "into_memory": {
                    "type": "string",
                    "description": "Optional, kind=lesson only: id or exact name of the EXISTING project memory this lesson is a variant of — it is then folded into that memory instead of creating a new file. When you have read a same-topic memory (via prefetch or search_memories), pass it here; the auto-fold fallback only catches near-duplicates."
                },
                "resolves_proposal": {
                    "type": "string",
                    "description": "Optional. The proposalId of a pending project_charter_propose this decision resolves, so it no longer shows as awaiting review."
                },
                "expected_version": {
                    "type": "integer",
                    "description": "Optional and rarely useful. This tool only ever APPENDS a decision, and appends commute — so a charter that moved since you read it is NOT a conflict: the commit re-reads and lands yours alongside the other one instead of refusing. A stale value here does not block the commit."
                },
            },
            "required": ["kind", "decision"],
        },
    },
}

# The MODEL-FACING charter toolset: read + propose ONLY (owner-directed
# 2026-07-30). project_charter_commit is NOT here — a charter always requires
# human review, so an agent may raise a binding rule but never make it binding.
#
# This is a REVERSAL of the 2026-07-12 de-gating. What made that de-gating
# survive unnoticed is instructive: registry/_build.py's comment claimed commit
# "is NEVER exposed as an agent tool" while CHARTER_TOOLS shipped all three
# names, so the code READ as safe. The comment is now true.
#
# CHARTER_COMMIT_TOOL itself is deliberately KEPT as a module symbol, and
# 'project_charter_commit' stays in CHARTER_TOOL_NAMES, for two reasons:
#   * execute_charter_tool must still RECOGNISE the name to refuse it with an
#     explanation — a model that learned the tool from an older transcript gets
#     told where the human gate is, instead of an opaque "unknown tool".
#     Dropping the name would turn a clear refusal into a phantom-tool error.
#   * historical tool rounds in existing conversations carry it and must still
#     render (static/js/ui/tool_rounds.js::_CONV_META_TOOLS).
CHARTER_TOOLS = [CHARTER_READ_TOOL, CHARTER_PROPOSE_TOOL]
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
            "conversation already owns, and to find an open epic to pick up. Read-only. "
            "To act on the board: project_board_claim an epic before working it, "
            "project_board_complete when done, project_board_post a new epic, "
            "project_board_block when waiting on a human."
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
                "isolated": {
                    "type": "boolean",
                    "description": (
                        "When true, this epic gets a DEDICATED git worktree (isolated writer "
                        "workspace): the assignee works only inside it and its result enters "
                        "the deterministic integration queue (human review → gated merge) "
                        "instead of landing in the shared checkout directly. Use for risky or "
                        "review-worthy changes; omit for ordinary shared-tree work."
                    )
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
            "Note that a board epic is WAITING on an external gate that YOU cannot "
            "clear yourself.\n\n"
            "BEFORE YOU CALL THIS — the autonomy rule. Blocking parks a workstream "
            "on a human who may not look for hours or days, so it is EXPENSIVE. "
            "Ask a human ONLY when the decision is one of these three:\n"
            "  (a) IRREVERSIBLE or expensive to undo (deleting data, force-pushing, "
            "spending money, anything visible to other people);\n"
            "  (b) a matter of TASTE, POLICY or PRODUCT INTENT with no "
            "technically-best answer (what the product should do, what wording is "
            "acceptable, which of two equally-valid designs the owner prefers);\n"
            "  (c) UNVERIFIABLE from inside the repo (needs a credential, a "
            "production system, or knowledge that exists only in someone's head).\n"
            "Everything else you are expected to DECIDE YOURSELF. If one option is "
            "more robust, more general, or better for the long term — take it, even "
            "when it costs more work — then raise the choice with "
            "project_charter_propose so a human can make it binding for siblings, "
            "and record what you did in JOURNAL.md. A reversible decision made now beats a perfect "
            "decision made after a two-day stall; the human can always overrule a "
            "committed decision, but they cannot recover the time an epic spent "
            "parked waiting to be asked. 'I wasn't sure' is NOT a reason to "
            "block — investigate, pick the most defensible option, and say in the "
            "charter decision what you chose and why.\n\n"
            "WHAT THIS DOES: puts the epic on a "
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
            "concrete blocker.\n\n"
            "HUMAN QUESTION: when you have applied the rule above and a HUMAN "
            "decision genuinely is required, also pass "
            "`question` (and `options` when the choice is enumerable). The epic "
            "then appears in the panel's 'Needs you' surface with one-click "
            "answer controls, and waits for the ANSWER instead of auto-retrying — "
            "the "
            "moment the human answers, the epic is re-dispatched with the answer "
            "in its kickoff. Always prefer this over a bare [human-gated] block "
            "(which keeps auto-retrying into the same unanswered gate). Make the "
            "question answerable in one word: state the concrete trade-off, give "
            "enumerated options with what each implies, and say which you would "
            "pick if forced — a question that requires an essay is another way of "
            "stalling.\n\n"
            "WHO READS WHAT: the `reason` renders VERBATIM as the card's "
            "'Why it stopped' background section, addressed to the OPERATOR — "
            "a busy human who was not in your conversation. Write it "
            "self-contained: what you were doing, what evidence forced the "
            "stop, what happens after each choice. Plain language, no "
            "file:line archaeology (that belongs in JOURNAL.md). Each "
            "option's `description` renders ON the card next to its label — "
            "spend it on the consequence of choosing that option, not on "
            "restating the label."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The board epic id."},
                "reason": {"type": "string", "description": "Why it's blocked. PREFIX with the block class: '[human-gated] …' or '[sibling] …'. For a sibling-commit blocker, name the files as '[sibling] path=a.py,b.py …' to auto-hold on them."},
                "question": {
                    "type": "string",
                    "description": "Optional. The concrete question you need the HUMAN to answer (for a [human-gated] block). Renders on the board with answer controls; the epic auto-re-dispatches the moment it is answered."
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "The choice label (concise)."},
                            "description": {"type": "string", "description": "Optional: what choosing this means."}
                        },
                        "required": ["label"]
                    },
                    "description": "Optional (max 6). Predefined choices for the human; omit for a free-text answer."
                },
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
            "the live complement to get_conversation (which reads past messages). "
            "To coordinate: project_message sends a note to a sibling, "
            "project_intervene asks it to stop/change course."
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
            "work. Read-only. To reply to a peer's activity, use project_message."
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
            "the peer does, not FYI chatter. When you RECEIVE a peer message, "
            "you may reply exactly once with project_message(to_conv_id=<the "
            "supplied reply id>) only to confirm a boundary, hand off context, "
            "or decline. Do not acknowledge just to be polite: this is "
            "coordination, not a chat channel, and the rate limit is the ceiling. "
            "By default the note WAKES an idle peer: a fresh turn is started so "
            "it answers promptly. Pass wake=false for a mailbox-only FYI — the "
            "note then waits for the peer's next natural turn and never starts "
            "one by itself (cheaper; use it for information the peer only needs "
            "when it next works, not for anything time-sensitive)."
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
                "wake": {
                    "type": "boolean",
                    "description": "trigger_turn semantics. true (default): an idle peer is woken with a fresh turn to answer this note. false: mailbox-only — never starts a turn on an idle peer; the note is delivered on the peer's next natural turn (or into its live turn at the next round boundary)."
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

# ── Integration writer tools (isolated-epic worktree lifecycle) ──
# An epic posted with isolated=true runs in a DEDICATED git worktree (created
# server-side before dispatch; the kickoff names it). The writer agent uses
# these three to snapshot progress, hand the work to the human review queue,
# and check where it stands. An ordinary shared-tree conversation NEVER needs
# them — calling them there returns a clear error, not damage.

INTEGRATION_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "integration_status",
        "description": (
            "Show this project's INTEGRATION pipeline: every isolated writer "
            "workspace with its queue state (running → checkpointed → ready "
            "(awaiting human review) → integrated | quarantined | discarded), "
            "its checkpoint commit, and which conversation produced it. "
            "Read-only. Use it to check whether YOUR isolated work has been "
            "reviewed/merged yet, or what a sibling's isolated epic is doing. "
            "An epic only appears here when it was posted with isolated=true."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

INTEGRATION_CHECKPOINT_TOOL = {
    "type": "function",
    "function": {
        "name": "integration_checkpoint",
        "description": (
            "Snapshot the CURRENT state of your isolated writer workspace as a "
            "checkpoint commit (kept under refs/tofu/checkpoints/, the shared "
            "checkout is untouched). Use at natural milestones — after a "
            "passing test run, before a risky refactor — so a crash never "
            "loses finished work and the human reviewer can see progress. "
            "This does NOT submit anything for review; keep working "
            "afterwards. ONLY valid when your kickoff said the epic is "
            "ISOLATED and named a workspace path — for ordinary shared-tree "
            "work just edit files, no checkpoint exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The board epic id your kickoff named (pt_…). Optional when this conversation owns exactly one active integration workspace — then it is resolved automatically."
                },
                "note": {
                    "type": "string",
                    "description": "Optional one-line milestone note for the reviewer (what works at this checkpoint)."
                },
            },
            "required": [],
        },
    },
}

INTEGRATION_SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "integration_submit",
        "description": (
            "SUBMIT your isolated writer workspace to the human review queue: "
            "takes a final checkpoint and marks the work READY. A human then "
            "reviews the diff in the Project panel → Integration tab and "
            "promotes or discards it — the merge into the shared checkout is "
            "NEVER done by you. After submitting, the workspace is IMMUTABLE: "
            "stop editing it, mark the board epic complete, and let the human "
            "review. If more work is needed after review feedback, it comes "
            "back as a new kickoff. ONLY valid when your kickoff said the epic "
            "is ISOLATED; for ordinary shared-tree work there is nothing to "
            "submit — your edits are already live."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The board epic id your kickoff named (pt_…). Optional when this conversation owns exactly one active integration workspace — then it is resolved automatically."
                },
                "summary": {
                    "type": "string",
                    "description": "REQUIRED. What the reviewer is looking at: what changed, why, and how you verified it (tests run). Written for a HUMAN reviewer, self-contained."
                },
            },
            "required": ["summary"],
        },
    },
}

INTEGRATION_TOOLS = [INTEGRATION_STATUS_TOOL, INTEGRATION_CHECKPOINT_TOOL,
                     INTEGRATION_SUBMIT_TOOL]
INTEGRATION_TOOL_NAMES = {'integration_status', 'integration_checkpoint',
                          'integration_submit'}

# ── Project Brain surface split (read vs advisory-write) ──
# The registry contributes the brain in two ToolSpecs with different
# discovery policies (lib/tools/registry/_build.py):
#   * BRAIN_READ_TOOLS — eager, resident on the project-mode wire. These four
#     are what the per-turn prompt injections already reference by name
#     (render_charter_injection_block / render_board_injection_block), so a
#     deferred schema meant the prompt pointed at tools the model could not
#     see. ~2.7k chars total — cheap ambient awareness of the whole
#     cross-conversation mechanism.
#   * BRAIN_WRITE_TOOLS — searchable (deferred to Tool Search). They are the
#     larger half (~11k chars, project_board_block alone is 4.6k) and remain
#     exactly callable by name through the task-level executable catalog; the
#     read-side descriptions point at them.
BRAIN_READ_TOOLS = [CHARTER_READ_TOOL, BOARD_READ_TOOL,
                    PEER_STATUS_TOOL, PEER_FEED_TOOL,
                    INTEGRATION_STATUS_TOOL]
BRAIN_WRITE_TOOLS = [CHARTER_PROPOSE_TOOL, BOARD_POST_TOOL, BOARD_CLAIM_TOOL,
                     BOARD_COMPLETE_TOOL, BOARD_BLOCK_TOOL,
                     PEER_MESSAGE_TOOL, PEER_INTERVENE_TOOL,
                     INTEGRATION_CHECKPOINT_TOOL, INTEGRATION_SUBMIT_TOOL]
BRAIN_READ_TOOL_NAMES = {
    (t.get('function') or {}).get('name') for t in BRAIN_READ_TOOLS}
BRAIN_WRITE_TOOL_NAMES = {
    (t.get('function') or {}).get('name') for t in BRAIN_WRITE_TOOLS}

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
    'INTEGRATION_STATUS_TOOL', 'INTEGRATION_CHECKPOINT_TOOL',
    'INTEGRATION_SUBMIT_TOOL', 'INTEGRATION_TOOLS', 'INTEGRATION_TOOL_NAMES',
    'BRAIN_READ_TOOLS', 'BRAIN_WRITE_TOOLS',
    'BRAIN_READ_TOOL_NAMES', 'BRAIN_WRITE_TOOL_NAMES',
]
