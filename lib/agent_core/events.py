"""lib/agent_core/events.py — The streaming event contract, declared.

This module is the **single source of truth** for the event vocabulary that
flows from the agent runtime to any frontend — over the SSE chat stream
(``/api/chat/stream/<id>``, ``/api/v1/tasks/<id>/stream``) and over the unified
push WebSocket (``/api/push``).

Why this exists
---------------
Before this module, the ~40 event ``type`` strings were an *implicit* contract:
defined only by scattered ``append_event(task, {'type': ...})`` call sites in
the orchestrator and the ``ev.type === "..."`` ladders in ``static/js``.  A
third party building their own frontend had to reverse-engineer the stream by
reading our JS.  This registry makes the contract explicit, versioned, and
**machine-discoverable** via ``GET /api/v1/capabilities`` (``events`` block).

What it is / is NOT
-------------------
* It is a *descriptive* registry — a catalogue of every event the runtime can
  emit, each with its category, terminal-ness, a one-line purpose, and the key
  payload fields.
* It is also the *generative* chokepoint: the built-in orchestrator emits via
  :func:`build_event` / :func:`emit` (with :class:`EventType` constants) rather
  than bare-string dict literals, so there is ONE typed event model.
  ``build_event(EventType.PHASE, phase='x')`` is byte-for-byte identical to the
  old ``{'type': 'phase', 'phase': 'x'}`` literal (kwargs preserve order) —
  the conversion changed no wire output, only the construction site.
* It is NOT a validator that rejects unknown events at runtime — the wire stays
  permissive (forward-compatible).  Drift is caught at TEST time by
  ``tests/test_event_registry.py``, which asserts (a) every ``'type':`` string
  emitted in core is registered here, and (b) every type the frontend handles
  is registered.  That is the analog of ``test_core_tool_isolation.py``.

Versioning
----------
``EVENT_CONTRACT_VERSION`` is bumped on any *breaking* change to an existing
event's shape (field removed/renamed/retyped).  Additive changes (new event
type, new optional field) do NOT bump it.  Mirrors the ``/api/v1`` policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# Bump only on a breaking change to an EXISTING event's shape.
EVENT_CONTRACT_VERSION = 1


# ── Categories — group events by lifecycle role ──
class EventCategory:
    """Coarse grouping for docs / client routing."""

    LIFECYCLE = 'lifecycle'        # turn-level: phase, done, error, state
    CONTENT = 'content'            # streamed assistant output: delta
    TOOL = 'tool'                  # tool-call lifecycle
    CONTEXT = 'context'            # context-window mgmt: compaction, snapshots
    INTERACTION = 'interaction'    # require a client response (approval, stdin)
    ENDPOINT = 'endpoint'          # Planner→Worker→Critic loop
    SWARM = 'swarm'                # multi-agent orchestration
    AUTOPILOT = 'autopilot'        # autonomous-loop value-unit events
    ARTIFACT = 'artifact'          # artifact creation
    SCHEDULER = 'scheduler'        # timer / proactive
    PRESENCE = 'presence'          # cross-conversation live presence / coordination
    TRANSPORT = 'transport'        # stream-level signals (ping, timeout)


@dataclass(frozen=True)
class EventSpec:
    """Describes one event ``type``.

    Parameters
    ----------
    type:
        The wire ``type`` string (what appears as ``{"type": ...}``).
    category:
        One of :class:`EventCategory`.
    purpose:
        One-line human description.
    terminal:
        True if this event ends the task stream (``done`` / fatal ``error``).
    requires_response:
        True if the client MUST reply (via a documented endpoint) before the
        task can proceed — interaction events (approval, stdin, guidance).
    fields:
        Map of payload field name → short description.  ``type`` is implicit
        and omitted.  Documents the shape without enforcing it.
    since:
        Contract version in which the event was introduced (for changelogs).
    """

    type: str
    category: str
    purpose: str
    terminal: bool = False
    requires_response: bool = False
    fields: dict[str, str] = field(default_factory=dict)
    since: int = 1


class EventType:
    """Canonical event ``type`` string constants.

    Reference these instead of bare strings in emission code, via the typed
    constructor — never a raw ``{'type': ...}`` dict literal::

        append_event(task, build_event(EventType.PHASE, phase='working'))

    See ``docs/EVENTS.md`` for the full emit discipline.
    """

    # ── lifecycle ──
    STATE = 'state'
    PHASE = 'phase'
    DONE = 'done'
    ERROR = 'error'
    RETRY_RESET = 'retry_reset'
    # ── content ──
    DELTA = 'delta'
    DELTA_RESET = 'delta_reset'
    # ── tool ──
    TOOL_START = 'tool_start'
    TOOL_PROGRESS = 'tool_progress'
    TOOL_RESULT = 'tool_result'
    TOOL_COMPLETE = 'tool_complete'
    TOOL_COMPACTED = 'tool_compacted'
    # ── context ──
    ROUND_USAGE = 'round_usage'
    ROUND_COMMITTED = 'round_committed'
    MESSAGES_SNAPSHOT = 'messages_snapshot'
    COMPACTION = 'compaction'
    COMPACTION_DONE = 'compaction_done'
    MEMORY_PREFETCH = 'memory_prefetch'
    PREFERENCES_APPLIED = 'preferences_applied'
    PREFERENCE_LEARNED = 'preference_learned'
    RELATED_CONVERSATIONS = 'related_conversations'
    PROJECT_EXTERNAL_EDIT = 'project_external_edit'
    WORKSPACE_ROOT_ADDED = 'workspace_root_added'
    # ── interaction (require client response) ──
    HUMAN_GUIDANCE_REQUEST = 'human_guidance_request'
    WRITE_APPROVAL_REQUEST = 'write_approval_request'
    APPROVAL_REQUIRED = 'approval_required'
    STDIN_REQUEST = 'stdin_request'
    STDIN_RESOLVED = 'stdin_resolved'
    # ── endpoint (Planner→Worker→Critic) ──
    ENDPOINT_ITERATION = 'endpoint_iteration'
    ENDPOINT_PLANNER_DONE = 'endpoint_planner_done'
    ENDPOINT_CRITIC_MSG = 'endpoint_critic_msg'
    ENDPOINT_NEW_TURN = 'endpoint_new_turn'
    ENDPOINT_COMPLETE = 'endpoint_complete'
    # ── swarm ──
    SWARM_PHASE = 'swarm_phase'
    SWARM_INBOX_INJECT = 'swarm_inbox_inject'
    SWARM_AGENT_PHASE = 'swarm_agent_phase'
    SWARM_AGENT_PROGRESS = 'swarm_agent_progress'
    SWARM_AGENT_COMPLETE = 'swarm_agent_complete'
    SWARM_AGENT_ERROR = 'swarm_agent_error'
    SWARM_AGENT_TOOL_CALL = 'swarm_agent_tool_call'
    # ── autopilot ──
    AUTOPILOT_VU_START = 'autopilot_vu_start'
    AUTOPILOT_VU_EVENT = 'autopilot_vu_event'
    AUTOPILOT_VU_DONE = 'autopilot_vu_done'
    AUTOPILOT_VU_CANCEL = 'autopilot_vu_cancel'
    AUTOPILOT_RUN_CONCLUDED = 'autopilot_run_concluded'
    # ── presence (cross-conversation live coordination) ──
    PRESENCE = 'presence'
    PEER_INBOX_INJECT = 'peer_inbox_inject'
    # ── artifact / scheduler / transport ──
    ARTIFACT = 'artifact'
    TIMER_POLL_CHECK = 'timer_poll_check'
    SSE_TIMEOUT = 'sse_timeout'
    PING = 'ping'


# ── The registry: every event the runtime can emit ──
_C = EventCategory
_SPECS: tuple[EventSpec, ...] = (
    # ───────────────────────── lifecycle ─────────────────────────
    EventSpec(EventType.STATE, _C.LIFECYCLE,
              'Full task state snapshot — emitted first on (re)connect / cold '
              'replay so a client can rebuild the live assistant bubble without '
              'recomputing it: carries the authoritative content, thinking, '
              'tool rounds and terminal status.',
              fields={'content': 'assistant text so far',
                      'thinking': 'reasoning text so far',
                      'status': 'task status (running|done|error|aborted)',
                      'toolRounds': 'authoritative tool-round list (status per round)',
                      'error': '(optional) error envelope',
                      'finishReason': '(optional) terminal finish reason',
                      'usage': '(optional) token usage', 'model': '(optional) model id'}),
    EventSpec(EventType.PHASE, _C.LIFECYCLE,
              'Progress / status hint for the current turn.',
              fields={'phase': "phase key (llm_thinking|tool_exec|retrying|working|…)",
                      'detail': 'human-readable detail', 'round': 'round number'}),
    EventSpec(EventType.DONE, _C.LIFECYCLE,
              'Terminal event — the turn finished (success or, with `error`, failure).',
              terminal=True,
              fields={'error': 'error envelope if failed (else absent)',
                      'finishReason': 'stop|error|aborted|max_turns',
                      'committedMessage': '(optional) the EXACT settled assistant '
                                          'message dict just written to '
                                          'conversations.messages — the frontend '
                                          'projects the terminal bubble from THIS '
                                          'verbatim, no keep-longer/snapshot '
                                          'reconstruction. Absent on skip paths '
                                          '(freshness/inline/CAS-exhaustion), where '
                                          'the client keeps its transient buffer.'}),
    EventSpec(EventType.ERROR, _C.LIFECYCLE,
              'Inline error envelope (non-terminal diagnostics; fatal errors '
              'arrive as a `done` with `error`).',
              fields={'content': 'error text', 'detail': 'structured detail'}),
    EventSpec(EventType.RETRY_RESET, _C.LIFECYCLE,
              'A transient-error turn is being auto-re-run from scratch. The '
              'client MUST clear the live bubble\'s accumulated content / '
              'thinking (and tool rounds) so the about-to-be-re-streamed deltas '
              'do not stack on top of the failed attempt\'s partial output. '
              'Non-terminal: the task stays `running`; a `phase:retrying` '
              'frame carrying the attempt/backoff detail accompanies it.',
              fields={'attempt': 'whole-turn retry number (1-based)',
                      'max': 'retry budget',
                      'kind': 'error kind that triggered the re-run'}),
    # ───────────────────────── content ─────────────────────────
    EventSpec(EventType.DELTA, _C.CONTENT,
              'Incremental assistant output — append to the live bubble.',
              fields={'content': 'text delta (may be absent)',
                      'thinking': 'reasoning delta (may be absent)'}),
    EventSpec(EventType.DELTA_RESET, _C.CONTENT,
              'The just-ended LLM round issued TOOL CALLS, so any prose it '
              'streamed before those calls was inter-round narration (e.g. '
              '"Now let me check the utility functions."), NOT the final '
              'answer. The client MUST clear the live bubble\'s accumulated '
              'content / thinking so this narration does not get concatenated '
              'in front of the terminal round\'s real answer. Unlike '
              '`retry_reset`, it MUST NOT touch tool rounds — the tool calls '
              'from this turn are legitimate and keep rendering. Non-terminal.',
              fields={'round': 'the tool-call round number whose prose is dropped'}),
    # ───────────────────────── tool ─────────────────────────
    EventSpec(EventType.TOOL_START, _C.TOOL,
              'A tool call began executing.',
              fields={'roundNum': 'round index', 'toolName': 'tool name',
                      'toolCallId': 'tool-call id', 'query': 'display string',
                      'toolArgs': 'serialized args',
                      'status': "(optional) 'rejected' when the tool was a "
                                'hallucination and never ran',
                      '_rejected': '(optional) {attempted, suggestions} for a '
                                   'rejected hallucinated tool'}),
    EventSpec(EventType.TOOL_PROGRESS, _C.TOOL,
              'Streaming progress emitted by a long-running tool.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'detail': 'progress text'}),
    EventSpec(EventType.TOOL_RESULT, _C.TOOL,
              'A tool produced a (possibly partial) result payload.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'results': 'list of {toolName,title,snippet,source}',
                      'query': 'display string',
                      'status': "(optional) 'rejected' for a hallucinated tool "
                                'that was rejected without executing',
                      '_rejected': '(optional) {attempted, suggestions} for a '
                                   'rejected hallucinated tool'}),
    EventSpec(EventType.TOOL_COMPLETE, _C.TOOL,
              'A tool call finished; carries the final tool message.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'content': 'final tool result', 'isError': 'bool'}),
    EventSpec(EventType.TOOL_COMPACTED, _C.TOOL,
              'A prior tool result was compacted out of context to save tokens.',
              fields={'toolCallId': 'tool-call id', 'roundNum': 'round index'}),
    # ───────────────────────── context ─────────────────────────
    EventSpec(EventType.ROUND_USAGE, _C.CONTEXT,
              'Token-usage accounting for a completed round.',
              fields={'usage': 'usage dict', 'round': 'round number',
                      'model': 'model id'}),
    EventSpec(EventType.ROUND_COMMITTED, _C.CONTEXT,
              'A round was persisted server-side (durable checkpoint).',
              fields={'round': 'round number'}),
    EventSpec(EventType.MESSAGES_SNAPSHOT, _C.CONTEXT,
              'A point-in-time copy of the message list (fallback/branch sync).',
              fields={'messages': 'message list', 'round': 'round id/label',
                      'label': 'human label'}),
    EventSpec(EventType.COMPACTION, _C.CONTEXT,
              'Context-window compaction started.',
              fields={'detail': 'what is being compacted'}),
    EventSpec(EventType.COMPACTION_DONE, _C.CONTEXT,
              'Context-window compaction finished.',
              fields={'archived': 'count/size archived'}),
    EventSpec(EventType.MEMORY_PREFETCH, _C.CONTEXT,
              'Memory-prefetch pipeline stage update.',
              fields={'stage': 'pipeline stage', 'results': 'retrieved notes'}),
    EventSpec(EventType.PREFERENCES_APPLIED, _C.CONTEXT,
              'The bounded personal-preference profile was injected into this '
              'turn (always-on, cache-safe _isMeta tail). Drives the quiet '
              '"preferences applied" chip so the user can see the assistant '
              'is honouring their stored preferences.',
              fields={'chars': 'profile size in chars',
                      'items': 'flat list of injected bullets (core + relevant detail) for the chip',
                      'core': 'always-on core-tier bullets injected this turn',
                      'detail': 'relevance-selected detail-tier bullets (empty on an irrelevant turn)'}),
    EventSpec(EventType.PREFERENCE_LEARNED, _C.CONTEXT,
              'A preference was learned/reinforced by the post-turn '
              'consolidation pass. Surfaces a "Noted: you prefer X" moment; '
              'when pending=true it awaits user confirm (undo/edit affordance).',
              fields={'kind': 'reinforced|pending',
                      'summary': 'one-line description of what was learned',
                      'pending': 'true when awaiting user confirm (new pref)',
                      'id': 'pending proposal id (empty for auto-reinforced)'}),
    EventSpec(EventType.RELATED_CONVERSATIONS, _C.CONTEXT,
              'The bounded cross-conversation project digest (sibling '
              'conversations of the same project) was injected into this turn '
              'for ambient awareness. Drives a quiet "related conversations" '
              'provenance segment so the user can see — and audit — the same '
              'siblings the model was told about.',
              fields={'count': 'number of siblings surfaced',
                      'items': 'list of {id, title, summary}',
                      'toolsAvailable': 'whether get_conversation/'
                                        'list_conversations were registered this turn'}),
    EventSpec(EventType.PROJECT_EXTERNAL_EDIT, _C.CONTEXT,
              'A project file changed on disk outside the agent (drift notice).',
              fields={'path': 'file path', 'action': 'create|modify|delete'}),
    EventSpec(EventType.WORKSPACE_ROOT_ADDED, _C.CONTEXT,
              'An absolute-path write auto-registered a NEW extra workspace '
              'root (the silent workspace expansion that was previously '
              'invisible — no tool round, only an app.log line). Surfaces a '
              'brief "added workspace root X" notice so the user knows the '
              'agent widened the project scope.',
              fields={'roots': 'list of {rootName, path} auto-registered this tool call'}),
    # ─────────────────── interaction (need client reply) ───────────────────
    EventSpec(EventType.HUMAN_GUIDANCE_REQUEST, _C.INTERACTION,
              'Agent asked the human a question (ask_human tool); turn pauses.',
              requires_response=True,
              fields={'question': 'prompt text', 'requestId': 'reply correlation id'}),
    EventSpec(EventType.WRITE_APPROVAL_REQUEST, _C.INTERACTION,
              'A write/exec tool needs explicit approval before running.',
              requires_response=True,
              fields={'toolName': 'tool', 'toolCallId': 'id', 'preview': 'diff/preview'}),
    EventSpec(EventType.APPROVAL_REQUIRED, _C.INTERACTION,
              'Generic approval gate (mode-based backends).',
              requires_response=True,
              fields={'detail': 'what needs approval'}),
    EventSpec(EventType.STDIN_REQUEST, _C.INTERACTION,
              'A running command requested interactive stdin.',
              requires_response=True,
              fields={'prompt': 'stdin prompt', 'requestId': 'reply correlation id'}),
    EventSpec(EventType.STDIN_RESOLVED, _C.INTERACTION,
              'A pending stdin request was satisfied (clears the prompt UI).',
              fields={'requestId': 'correlation id'}),
    # ───────────────────────── endpoint ─────────────────────────
    EventSpec(EventType.ENDPOINT_ITERATION, _C.ENDPOINT,
              'Endpoint loop entered a new Planner/Worker/Critic iteration.',
              fields={'iteration': 'index', 'phase': 'planner|worker|critic'}),
    EventSpec(EventType.ENDPOINT_PLANNER_DONE, _C.ENDPOINT,
              'Planner produced a plan.', fields={'plan': 'plan content'}),
    EventSpec(EventType.ENDPOINT_CRITIC_MSG, _C.ENDPOINT,
              'Critic verdict + feedback.',
              fields={'next_phase': 'planner|worker|stop',
                      'should_stop': '(legacy) bool', 'feedback': 'critic text'}),
    EventSpec(EventType.ENDPOINT_NEW_TURN, _C.ENDPOINT,
              'A fresh Worker/Planner turn began (new assistant bubble).',
              fields={'phase': 'planner|worker'}),
    EventSpec(EventType.ENDPOINT_COMPLETE, _C.ENDPOINT,
              'Endpoint loop terminated (approved or replan-capped).',
              fields={'iterations': 'total count'}),
    # ───────────────────────── swarm ─────────────────────────
    EventSpec(EventType.SWARM_PHASE, _C.SWARM,
              'Top-level swarm orchestration phase.',
              fields={'phase': 'phase', 'detail': 'detail'}),
    EventSpec(EventType.SWARM_INBOX_INJECT, _C.SWARM,
              'A completed sub-agent result was injected into the main thread.',
              fields={'agentId': 'sub-agent id', 'summary': 'result preview'}),
    EventSpec(EventType.SWARM_AGENT_PHASE, _C.SWARM,
              'A sub-agent changed phase (e.g. running).',
              fields={'agentId': 'sub-agent id', 'phase': 'phase'}),
    EventSpec(EventType.SWARM_AGENT_PROGRESS, _C.SWARM,
              'Sub-agent progress update.',
              fields={'agentId': 'sub-agent id', 'detail': 'progress'}),
    EventSpec(EventType.SWARM_AGENT_COMPLETE, _C.SWARM,
              'Sub-agent finished (status may be error).',
              fields={'agentId': 'sub-agent id', 'status': 'ok|error',
                      'result': 'result/preview'}),
    EventSpec(EventType.SWARM_AGENT_ERROR, _C.SWARM,
              'Sub-agent errored.',
              fields={'agentId': 'sub-agent id', 'error': 'error text'}),
    EventSpec(EventType.SWARM_AGENT_TOOL_CALL, _C.SWARM,
              'A sub-agent invoked a tool (for live trace UI).',
              fields={'agentId': 'sub-agent id', 'toolName': 'tool'}),
    # ───────────────────────── autopilot ─────────────────────────
    EventSpec(EventType.AUTOPILOT_VU_START, _C.AUTOPILOT,
              'Autopilot kicked in — create the simulated-user bubble eagerly '
              '(in-memory only; not persisted until autopilot_vu_done).',
              fields={'vuMsgId': 'stable id for the VU message bubble'}),
    EventSpec(EventType.AUTOPILOT_VU_EVENT, _C.AUTOPILOT,
              'Autopilot value-unit progress event.',
              fields={'detail': 'vu detail'}),
    EventSpec(EventType.AUTOPILOT_VU_DONE, _C.AUTOPILOT,
              'Autopilot value-unit completed.', fields={}),
    EventSpec(EventType.AUTOPILOT_VU_CANCEL, _C.AUTOPILOT,
              'Autopilot value-unit cancelled.', fields={'reason': 'why'}),
    EventSpec(EventType.AUTOPILOT_RUN_CONCLUDED, _C.AUTOPILOT,
              'An autopilot run reached its terminal boundary — the single '
              'BACKEND-AUTHORITATIVE "this run is over" fact the frontend folds '
              'on. Emitted on BOTH close-out paths, symmetrically: a clean '
              '[VU: TASK_DONE] (reason=task_done, usually with a close-out '
              'report) AND a manual stop / toggle-off / new-message supersede '
              '(reason=stopped, no report). The frontend NEVER infers run-end '
              'from stream/task state anymore — it folds the run\'s VU<->agent '
              'transcript iff a concluded record exists, and shows the report '
              '(when present) as the fold\'s read-only PANEL. The record is '
              'human-only: it lives in the conversation SIDECAR '
              '(settings.autopilotSummaries[runId]), NEVER as a chat message, so '
              'it never enters the transcript nor the LLM context. Also durably '
              'persisted server-side, so a disarm with no live stream still '
              'folds on the next load.',
              fields={'runId': 'autopilot run id grouping the folded turns',
                      'record': 'the sidecar run record {runId, status:'
                                '"concluded", reason:"task_done"|"stopped", '
                                'content?, translatedContent?, ts, _summaryId} '
                                '— NOT a message (no role, no _msgId); content '
                                'is absent on a manual stop'}),
    # ───────────────────────── presence ─────────────────────────
    EventSpec(EventType.PRESENCE, _C.PRESENCE,
              'Cross-conversation live-presence delta — the "who is working in '
              'this project right now" feed (the shared-document cursor analog). '
              'Broadcast to ALL push clients (taskId="*"); the frontend filters '
              'by the root it is displaying. The backend is the single source of '
              'truth: every status word (active|idle) and conflict string is '
              'fully formed server-side — the frontend NEVER derives liveness '
              'from mere presence, only renders what this frame carries. Emitted '
              'on announce / heartbeat / file-set change / idle / depart and on '
              'a detected file-set overlap between two active peers (notify-only, '
              'no locking).',
              fields={'kind': 'update|depart|conflict|snapshot',
                      'root': 'project root path this peer/conflict belongs to',
                      'peer': '(update/depart) {convId, agentId, parentTitle, '
                              'taskId, runId, title, objective, status, '
                              'statusLabel, phase, currentFile, files, '
                              'lastBeatTs, startedTs}. agentId="" = a '
                              'conversation peer; agentId set = a SUB-AGENT '
                              'peer that the frontend nests under its parent '
                              'conversation (grouped by convId). parentTitle = '
                              'the parent conversation title for the nested-row '
                              'label.',
                      'peers': '(snapshot) full active-peer list for the root',
                      'conflict': '(conflict) fully-formed advisory '
                                  '{path, message, peers:[peerKey…]} where a '
                                  'peerKey is convId or convId#agentId — so a '
                                  'sub-agent-vs-sub-agent overlap within ONE '
                                  'conversation is flagged like a cross-'
                                  'conversation one'}),
    EventSpec(EventType.PEER_INBOX_INJECT, _C.PRESENCE,
              'A peer message from a sibling conversation was delivered at a '
              'round boundary of THIS live turn (the fast-path lane of Pillar '
              '#6). Injected as a user-role message right before the next LLM '
              'round — never mid-stream, never splitting a tool_call/tool_result '
              'pair. The durable message_queue row is deleted in the same step '
              '(de-dup by queueId), so the message is delivered exactly once. '
              'Drives an in-timeline chip mirroring swarm_inbox_inject; the '
              'idle-target queue-lane case renders the persisted .peer-msg-banner '
              'instead.',
              fields={'round': 'round number the peer message was injected before',
                      'count': 'number of peer messages injected this round',
                      'previews': 'list of {fromConv, text} — sender short-id + '
                                  'the original (unframed) message text'}),
    # ───────────────── artifact / scheduler / transport ─────────────────
    EventSpec(EventType.ARTIFACT, _C.ARTIFACT,
              'An artifact (document/canvas) was created or updated.',
              fields={'artifactId': 'id', 'title': 'title', 'kind': 'artifact kind'}),
    EventSpec(EventType.TIMER_POLL_CHECK, _C.SCHEDULER,
              'Inline timer/scheduler poll heartbeat — one per poll cycle.',
              fields={'roundNum': 'tool round index', 'toolCallId': 'tool-call id',
                      'timerId': 'timer id', 'pollNum': 'poll counter',
                      'pollId': 'stable per-poll id ({timerId}.p{N}) for log/DB/UI correlation',
                      'decision': 'started|wait|ready|skipped|error|parse_error',
                      'reason': 'LLM/decision rationale',
                      'rawContent': "the LLM's full raw output (sent only on parse_error/error)",
                      'tokensUsed': 'tokens spent on this poll',
                      'checkInstruction': '(started) what is being verified',
                      'checkCommand': '(started) shell command run before each poll',
                      'cmdOutput': 'truncated check_command output (the evidence)',
                      'parseError': 'true if the decision could not be parsed',
                      'model': 'concrete model the poll LLM resolved to',
                      'toolTrace': 'list of {name,argsBrief,elapsed,isError} the poll agent invoked',
                      'pollInterval': '(started) seconds between polls',
                      'maxPolls': '(started) poll ceiling',
                      'nextPollTs': 'epoch-ms of the next scheduled poll'}),
    EventSpec(EventType.SSE_TIMEOUT, _C.TRANSPORT,
              'Server signalled the stream idle-timed-out; client may reconnect.',
              fields={}),
    EventSpec(EventType.PING, _C.TRANSPORT,
              'Keepalive frame on the push WebSocket (ignore).', fields={}),
)

# Indexes
_BY_TYPE: dict[str, EventSpec] = {s.type: s for s in _SPECS}

# Types that are stream-internal / transport and are NOT expected to be
# handled by an application frontend's event switch (the drift test exempts
# these from the "frontend must handle every type" direction).
TRANSPORT_TYPES: frozenset[str] = frozenset({EventType.PING, EventType.SSE_TIMEOUT})


def build_event(type_: str, **fields: Any) -> dict[str, Any]:
    """Construct a wire event dict ``{'type': type_, **fields}``.

    The typed constructor for the streaming contract.  Equivalent — byte for
    byte — to writing the literal ``{'type': type_, 'k': v, ...}``: Python
    preserves keyword-argument insertion order, so
    ``build_event(EventType.PHASE, phase='x', detail='y')`` yields exactly
    ``{'type': 'phase', 'phase': 'x', 'detail': 'y'}``.

    Use this (with :class:`EventType` constants) instead of bare-string dict
    literals so every emission references the declared vocabulary.  For an
    event whose fields are built up conditionally, call ``build_event(TYPE)``
    and mutate the returned dict exactly as before.

    Unregistered types are allowed (the wire stays forward-compatible) but log
    a debug line — the drift test is what enforces registration at CI time.
    """
    if type_ not in _BY_TYPE:
        logger.debug('[events] build_event for unregistered type=%r '
                     '(add an EventSpec to lib/agent_core/events.py)', type_)
    return {'type': type_, **fields}


def emit(task: Any, type_: str, **fields: Any) -> Any:
    """Build a typed event and deliver it through the task event chokepoint.

    Thin convenience over ``build_event`` + ``append_event`` — the one place
    the built-in orchestrator routes emissions, so the event MODEL is unified
    even though delivery still flows through the existing
    ``lib.tasks_pkg.manager.append_event`` (phase tracking + persistence +
    push fan-out).  Returns whatever ``append_event`` returns (the seq, or
    ``None``).

    ``append_event`` is imported lazily: ``events.py`` is part of the agent
    core, and importing the manager at module load would invert the dependency
    direction (and is unnecessary — delivery is a runtime concern).
    """
    event = build_event(type_, **fields)
    from lib.tasks_pkg.manager import append_event
    return append_event(task, event)


def all_event_specs() -> tuple[EventSpec, ...]:
    """Return every registered :class:`EventSpec`."""
    return _SPECS


def event_types() -> frozenset[str]:
    """Return the set of all registered event ``type`` strings."""
    return frozenset(_BY_TYPE)


def get_event_spec(type_: str) -> EventSpec | None:
    """Return the :class:`EventSpec` for *type_*, or ``None`` if unregistered."""
    return _BY_TYPE.get(type_)


def is_registered(type_: str) -> bool:
    """True if *type_* is a known event type."""
    return type_ in _BY_TYPE


def terminal_types() -> frozenset[str]:
    """Event types that end the task stream."""
    return frozenset(s.type for s in _SPECS if s.terminal)


def interaction_types() -> frozenset[str]:
    """Event types that require a client response before the task proceeds."""
    return frozenset(s.type for s in _SPECS if s.requires_response)


def to_capabilities_dict() -> dict[str, Any]:
    """Serialize the contract for ``GET /api/v1/capabilities`` (``events`` block).

    A foreign frontend reads this to discover the full event vocabulary —
    categories, terminal-ness, interaction events, and per-event field hints —
    without reading our JS.
    """
    by_category: dict[str, list[dict]] = {}
    for s in _SPECS:
        by_category.setdefault(s.category, []).append({
            'type': s.type,
            'purpose': s.purpose,
            'terminal': s.terminal,
            'requires_response': s.requires_response,
            'fields': s.fields,
            'since': s.since,
        })
    return {
        'contract_version': EVENT_CONTRACT_VERSION,
        'transports': {
            'sse': ['/api/chat/stream/<task_id>', '/api/v1/tasks/<task_id>/stream'],
            'websocket': '/api/push',
            'cursor_replay': '/api/v1/tasks/<task_id>/events?cursor=N',
        },
        'terminal_types': sorted(terminal_types()),
        'interaction_types': sorted(interaction_types()),
        'categories': by_category,
    }


__all__ = [
    'EVENT_CONTRACT_VERSION',
    'EventCategory',
    'EventSpec',
    'EventType',
    'TRANSPORT_TYPES',
    'build_event',
    'emit',
    'all_event_specs',
    'event_types',
    'get_event_spec',
    'is_registered',
    'terminal_types',
    'interaction_types',
    'to_capabilities_dict',
]
