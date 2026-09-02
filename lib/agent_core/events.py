"""lib/agent_core/events.py — The streaming event contract, declared.

This module is the **single source of truth** for the event vocabulary that
flows from the agent runtime to any frontend — over the task replay stream
(``/api/v1/tasks/<id>/stream``) and the unified push WebSocket (``/api/push``).

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

The PHASE sub-vocabulary
------------------------
The ``phase`` event is the stream's *status text* channel ("Retrying…",
"Sent to kimi-k3…", "Compressing context…") — the highest-frequency
human-readable pushes a turn produces.  Its ``phase`` field has its own
registry here (:class:`Phase` constants + :class:`PhaseSpec` catalogue +
:func:`build_phase` / :func:`emit_phase` constructors), so the full set of
status pushes is perceivable in ONE place instead of being reverse-engineered
from scattered ``phase='...'`` call sites, and uniformly optimisable at the
single construction chokepoint.  ``tests/test_phase_registry.py`` drift-guards
it both directions (emitted values ⊆ registry; frontend-handled values ⊆
registry; zero raw ``{'type': 'phase'`` literals outside this module).

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
    FLOW = 'flow'                  # Flow-engine chat runs (goal-mode graph, Studio flows)
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


@dataclass(frozen=True)
class PhaseSpec:
    """Describes one ``phase`` value of the :data:`EventType.PHASE` event.

    The PHASE event is the stream's status-text channel; this is the
    catalogue of every status push the runtime can emit.  Mirrors
    :class:`EventSpec` one level down (type → phase).

    Parameters
    ----------
    phase:
        The wire ``phase`` string.
    domains:
        Which stream(s) emit it: ``'chat'`` for the main agent task stream
        (the shared frontend contract), or a production channel name
        (``'motion_video'`` / ``'podcast'`` / ``'research'`` / ``'longform'``)
        for the per-capability TaskRuntime streams.  Production phases ride
        the same ``phase`` event type and are catalogued here so the whole
        system's status pushes are perceivable in one place; their private
        event *types* (``phase_started`` / ``progress`` / …) stay
        unregistered per the docs/EVENTS.md §1 scope ruling.
    purpose:
        One-line human description.
    fields:
        Map of payload field name → short description (``type`` / ``phase``
        are implicit and omitted).
    since:
        Contract version in which the phase was introduced.
    """

    phase: str
    domains: tuple[str, ...]
    purpose: str
    fields: dict[str, str] = field(default_factory=dict)
    since: int = 1


class EventType:
    """Canonical event ``type`` string constants.

    Reference these instead of bare strings in emission code, via the typed
    constructor — never a raw ``{'type': ...}`` dict literal::

        emit_phase(task, Phase.WORKING, detail='…')

    (``Phase``/``build_phase``/``emit_phase`` below are the typed pair for the
    PHASE event's ``phase`` field — use them for status pushes.)

    See ``docs/EVENTS.md`` for the full emit discipline.
    """

    # ── lifecycle ──
    STATE = 'state'
    PHASE = 'phase'
    ROUND_START = 'round_start'
    ROUND_END = 'round_end'
    DONE = 'done'
    ERROR = 'error'
    RETRY_RESET = 'retry_reset'
    MODEL_REQUEST_START = 'model_request_start'
    MODEL_REQUEST_COMPLETE = 'model_request_complete'
    MODEL_FALLBACK = 'model_fallback'
    BUDGET_WARNING = 'budget_warning'
    # ── content ──
    DELTA = 'delta'
    DELTA_RESET = 'delta_reset'
    # ── tool ──
    TOOL_START = 'tool_start'
    TOOL_PROGRESS = 'tool_progress'
    TOOL_RESULT = 'tool_result'
    TOOL_COMPLETE = 'tool_complete'
    TOOL_SCHEMA_REJECTED = 'tool_schema_rejected'
    TOOL_WIRE_PROJECTION = 'tool_wire_projection'
    TOOL_COMPACTED = 'tool_compacted'
    TOOL_CALL_REPLAY = 'tool_call_replay'
    PROGRAM_START = 'program_start'
    PROGRAM_OUTPUT = 'program_output'
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
    # ── flow engine (Planner/Worker/Critic/VU graphs via FlowExecutor) ──
    # Renamed from endpoint_* when the endpoint chat mode was retired
    # (2026-08-27). No frontend reads the names; turns are the render
    # authority, so the rename is server-internal.
    FLOW_ITERATION = 'flow_iteration'
    FLOW_PLANNER_DONE = 'flow_planner_done'
    FLOW_CRITIC_MSG = 'flow_critic_msg'
    FLOW_NEW_TURN = 'flow_new_turn'
    FLOW_COMPLETE = 'flow_complete'
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
    # ── human steering (mid-turn human interjection) ──
    USER_STEER_INJECT = 'user_steer_inject'
    # ── artifact / scheduler / transport ──
    ARTIFACT = 'artifact'
    TIMER_POLL_CHECK = 'timer_poll_check'
    SSE_TIMEOUT = 'sse_timeout'
    PING = 'ping'


class Phase:
    """Canonical ``phase`` values for the :data:`EventType.PHASE` event.

    Reference these instead of bare strings in emission code, via the typed
    constructor — never a raw ``phase='...'`` literal::

        emit_phase(task, Phase.RETRYING, detail='…', attempt=1)

    Adding a NEW phase = add the constant here + a :class:`PhaseSpec` in
    ``_PHASE_SPECS`` below (the drift test enforces the pair).  Frontend-local
    phase states the client derives itself (e.g. ``thinking_active``) are NOT
    pushed and deliberately NOT registered here.
    """

    # ── chat turn (the streaming status row — shared frontend contract) ──
    LLM_THINKING = 'llm_thinking'
    TOOL_EXEC = 'tool_exec'
    RETRYING = 'retrying'
    WAITING_MODEL = 'waiting_model'
    STREAM_STALLED = 'stream_stalled'
    WORKING = 'working'
    COMPACTING = 'compacting'
    TODO_CONTINUATION = 'todo_continuation'
    INTENT_STALL_NUDGE = 'intent_stall_nudge'
    TOOL_HISTORY_RESTORED = 'tool_history_restored'
    TOOL_AUTHORITY = 'tool_authority'
    # ── motion_video channel ──
    RESEARCH = 'research'
    SCRIPT_DONE = 'script_done'
    PARSE = 'parse'
    STORYBOARD = 'storyboard'
    NARRATE = 'narrate'
    COMPOSE = 'compose'
    CONCAT = 'concat'
    BURN_IN = 'burn_in'
    MUX = 'mux'
    REGEN = 'regen'
    # ── podcast channel ──
    SCRIPT = 'script'
    AUDIO = 'audio'
    # ── research / longform channels (shared value) ──
    START = 'start'


# ── Tool-lifecycle timing contract ──────────────────────────────────
# Every tool event carries backend clocks so a slow turn is ATTRIBUTABLE. Three
# segments are derivable per tool row, and without them they are indivisible:
#
#   execution = tEnd - tStart            (upstream HTTP / MCP / subprocess)
#   transport = receivedAt - emittedAt   (queueing, SSE buffering, proxies)
#   render    = painted - receivedAt     (the client dropped or delayed a paint)
#
# ``receivedAt`` is stamped CLIENT-side at stream ingress; the two backend
# clocks and the emission clock are stamped here. All are epoch MILLISECONDS
# (the project has shipped a seconds/ms confusion before — see
# ``_isPlausibleEpochMs`` in the paper media tabs).
_TOOL_CLOCK_FIELDS: dict[str, str] = {
    'tStart': 'epoch ms when the tool actually began executing — present on '
              'EVERY tool frame so a still-running row can render a truthful '
              'elapsed time instead of a client stopwatch that re-mints on '
              'each paint',
    'emittedAt': 'epoch ms when the backend handed this frame to the event '
                 'chokepoint. Transport latency = receivedAt - emittedAt',
}
_TOOL_END_CLOCK_FIELD: dict[str, str] = {
    'tEnd': 'epoch ms when the tool returned (terminal frames only). '
            'Execution time = tEnd - tStart',
}


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
                      'usage': '(optional) token usage', 'model': '(optional) model id',
                      'phase': '(optional, running tasks only) authoritative '
                               'current phase snapshot — same shape as the poll '
                               'fallback task.phase, so reconnects resume the real '
                               'working label instead of a bare waiting placeholder'}),
    EventSpec(EventType.PHASE, _C.LIFECYCLE,
              'Progress / status hint for the current turn.',
              fields={'phase': 'phase key — the declared vocabulary lives in '
                                 'the Phase registry below (phase_values() / '
                                 'the capabilities `phases` block); the chat '
                                 'domain is the streaming status row, the '
                                 'rest are production-channel stage pushes',
                      'detail': 'human-readable detail (English fallback; '
                                'headless / non-i18n clients render this verbatim)',
                      'detailKey': '(optional) stable i18n key the client resolves '
                                   'through its translation table so the label reads '
                                   'in the UI language; falls back to `detail` when '
                                   'absent',
                      'detailArgs': '(optional) interpolation args for `detailKey` '
                                    '(e.g. {"round": 3, "model": "claude-4"})',
                      'roundNum': 'round number',
                      'tools': '(optional, tool_exec phase) raw tool-name list '
                               'of this dispatch — the i18n client composes '
                               'its localized label from these; `detail` is '
                               'the English fallback',
                      'toolContext': '(optional, llm_thinking round-open phase) '
                                     'pre-joined English label string of the '
                                     'PREVIOUS round\'s tools (headless fallback)',
                      'toolContextTools': '(optional) the structured raw tool '
                                          'names behind `toolContext` — compose '
                                          'the suffix in the UI language from '
                                          'THESE when present'}),
    EventSpec(EventType.ROUND_START, _C.LIFECYCLE,
              'Explicit start boundary of an LLM round (the orchestrator loop '
              'index). Emitted at the TOP of every round the model actually '
              'runs — INCLUDING a prose-only round (streams text, no tool calls) '
              'and BEFORE the phase hint — so the client keys round attribution '
              'off a real boundary instead of inferring it from the first '
              '`tool_start` (a round with no tools had NO signal before). '
              'Non-terminal.',
              fields={'roundNum': 'the round index this boundary opens'}),
    EventSpec(EventType.ROUND_END, _C.LIFECYCLE,
              'Explicit end boundary of an LLM round — the complement of '
              '`round_start`. Emitted when a round concludes on EVERY exit path: '
              'it issued tool calls (loop continues), it finished with prose and '
              'no tools (terminal), or it was aborted/budget-capped. `reason` '
              'distinguishes them so the client can close the round without '
              'inferring end-of-round from the next `round_start` or a `done`. '
              'Non-terminal (a `done` still follows on the terminal path).',
              fields={'roundNum': 'the round index this boundary closes',
                      'reason': 'tools|final|aborted|budget|error|tool_timeout|'
                                'tool_loop — why the round ended'}),
    EventSpec(EventType.DONE, _C.LIFECYCLE,
              'Terminal event — the turn finished (success or, with `error`, failure).',
              terminal=True,
              fields={'error': 'error envelope if failed (else absent)',
                      'finishReason': 'provider/normalized terminal reason',
                      'streamState': 'closed ProviderStreamState evidence; '
                                     'provider_finished is the only success state'}),
    EventSpec(EventType.ERROR, _C.LIFECYCLE,
              'Legacy terminal error envelope. New fatal paths normally emit '
              'a `done` with `error`, but Turn authority still settles this '
              'frame for compatibility.',
              terminal=True,
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
                      'kind': 'error kind that triggered the re-run',
                      'contentEpoch': 'monotonic text generation after this reset'}),
    EventSpec(EventType.MODEL_REQUEST_START, _C.LIFECYCLE,
              'A logical model dispatch started. This low-frequency boundary '
              'opens the durable activity span used to correlate subsequent '
              'wait/retry frames and the terminal request result.',
              fields={'spanId': 'task-local stable request span id',
                      'model': 'requested model id',
                      'providerId': '(optional) provider id when already known',
                      'roundNum': '(optional) model round number',
                      'requestTag': 'task-local request label (R1/FALLBACK/etc.)'}),
    EventSpec(EventType.MODEL_REQUEST_COMPLETE, _C.LIFECYCLE,
              'A logical model-dispatch span settled successfully or failed. '
              'This is diagnostic state only and does not itself settle the Turn.',
              fields={'spanId': 'matching model_request_start span id',
                      'model': 'model id',
                      'providerId': '(optional) provider that served the request',
                      'status': 'succeeded|failed|aborted',
                      'finishReason': '(optional) provider finish reason',
                      'streamState': '(optional) closed ProviderStreamState',
                      'durationMs': 'wall-clock request duration in milliseconds',
                      'errorKind': '(failure only) typed exception/error kind',
                      'errorDetail': '(failure only) bounded safe detail',
                      'statusCode': '(failure only) HTTP status when known',
                      'routeId': '(optional) credential-free concrete network route id',
                      'routeMode': '(optional) direct|proxy|env|desktop|unknown',
                      'routeDecision': '(optional) why this route was selected',
                      'failureStage': '(optional) connect/provider_response/midstream/progress stage',
                      'roundNum': '(optional) model round number',
                      'requestTag': 'task-local request label'}),
    EventSpec(EventType.MODEL_FALLBACK, _C.LIFECYCLE,
              'The primary model failed and the turn is being re-streamed on '
              'the configured fallback model. Emitted EARLY, at the decision '
              'moment — BEFORE the fallback stream starts — so the client can '
              'paint an in-bubble fallback banner for the whole (potentially '
              'minutes-long) fallback generation and a cold reload can '
              'repaint it from the task stamps. Non-terminal; the terminal '
              '`done` still follows.',
              fields={'fallbackModel': 'the model the turn fell back TO',
                      'fallbackFrom': 'the original model that failed',
                      'fallbackKind': 'error kind that triggered the fallback',
                      'fallbackReason': 'human-readable reason (kind: detail, '
                                        'capped at 300 chars)'}),
    EventSpec(EventType.BUDGET_WARNING, _C.LIFECYCLE,
              'A task crossed a configured soft threshold or entered its '
              'model-round finalization reserve. Work may continue until '
              'the corresponding hard limit is reached.',
              fields={'limit': 'promptTokens|apiRounds|toolOutputBytes|elapsedSeconds|estimatedCostUsd',
                      'used': 'current measured consumption',
                      'remaining': 'amount remaining before hard termination',
                      'hardLimit': 'configured hard ceiling',
                      'unit': 'tokens|rounds|bytes|seconds'}),
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
              'from this turn are legitimate and keep rendering. Non-terminal. '
              'With `discard: true` (the canned-greeting upstream-artifact '
              'retry — the ONLY retry bucket whose discarded round HAS '
              'content) the round issued NO tool calls, so there is no batch '
              'to stamp the prose onto: the client clears UNCONDITIONALLY '
              '(still keeping tool rounds).',
              fields={'roundNum': 'the tool-call round number whose prose is dropped',
                      'discard': 'optional; true = unconditional clear, no prose-capture',
                      'contentEpoch': 'monotonic text generation after this reset'}),
    # ───────────────────────── tool ─────────────────────────
    EventSpec(EventType.TOOL_START, _C.TOOL,
              'A tool call began executing.',
              fields={'roundNum': 'round index', 'toolName': 'tool name',
                      'toolCallId': 'tool-call id', 'query': 'display string',
                      'toolArgs': 'serialized args',
                      'caller': '(optional) Responses nested-call owner',
                      'programCallId': '(optional) parent program call id',
                      'status': "(optional) 'rejected' when the tool was a "
                                'hallucination and never ran',
                      '_rejected': '(optional) {attempted, suggestions} for a '
                                   'rejected hallucinated tool',
                      '_contractError': '(optional) stable ToolContractV2 '
                                        'error for a call rejected before '
                                        'execution',
                      **_TOOL_CLOCK_FIELDS}),
    EventSpec(EventType.TOOL_PROGRESS, _C.TOOL,
              'Streaming progress emitted by a long-running tool.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'detail': 'progress text',
                      'execStartTs': '(optional) epoch ms when the subprocess '
                                     'was actually SPAWNED. Distinct from '
                                     'tStart (round ANNOUNCE time): a write '
                                     'approval or serial-write wait sits '
                                     'between them, so an elapsed derived from '
                                     'tStart over-reports execution',
                      'deadlineTs': '(optional) epoch ms at which the backend '
                                    'will SIGKILL this command. Absolute, and '
                                    'authoritative: the client must NOT derive '
                                    'it from the requested timeout, because '
                                    'the effective budget is the requested one '
                                    'AFTER the cross-DC multiplier, the '
                                    'MAX_COMMAND_TIMEOUT clamp and the remote '
                                    'bridge formula. Absent = no deadline '
                                    '(the default: run_command has no ceiling)',
                      'batchItem': '(optional) the ONE item of a batch call '
                                   '(query string / URL) this frame reports',
                      'batchDone': '(optional) how many batch items have '
                                   'settled so far (1-based, monotonic)',
                      'batchTotal': '(optional) total items in the batch call',
                      'batchOk': '(optional) False when THIS item failed — a '
                                 'failed item must still advance the counter, '
                                 'else the row looks stuck on a dead query',
                      '_selfTick': '(optional) True when this frame is the '
                                   'tool-heartbeat pinging ITSELF (transport '
                                   'keepalive, NOT evidence the tool is alive '
                                   '— ). The reaper ignores it for '
                                   'liveness; the frontend stalled-card reads '
                                   'it to tell self-ticks from real output',
                      'query': '(optional) REPAIRED display string — present '
                               'only on the display-patch frame the dispatcher '
                               'sends when a late args repair rebuilt the '
                               'round display (the early announce was built '
                               'from truncated/unrepaired args). The client '
                               'patches the live row WITHOUT settling the '
                               'round (2026-08-06 mid-arguments cut rendered '
                               '"$ ?" for the whole command duration)',
                      '_repaired': '(optional) {label, detail, patterns} — '
                                   'rides with query on the display-patch '
                                   'frame so the row gains the auto-fixed '
                                   'badge together with the new text',
                      **_TOOL_CLOCK_FIELDS}),
    EventSpec(EventType.TOOL_RESULT, _C.TOOL,
              'A tool produced a (possibly partial) result payload.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'toolName': 'canonical tool name',
                      'results': 'list of {toolName,title,snippet,source}',
                      'query': 'display string',
                      'status': "terminal VERDICT, always present on frames "
                                "emitted via _finalize_tool_round: 'done' on "
                                "success, else the backend's failure verdict "
                                "('error' / 'rejected' / 'aborted' / "
                                "'unanswerable'). The client renders this "
                                "field as the ONLY truth — it must never "
                                "infer success from the frame's arrival "
                                "(older backends omitted it except "
                                "'rejected'; a missing status on an in-flight "
                                "round is the only case the client may "
                                "settle as 'done')",
                      '_rejected': '(optional) {attempted, suggestions} for a '
                                   'rejected hallucinated tool',
                      '_contractError': '(optional) stable ToolContractV2 '
                                        'error preserved when validation '
                                        'rejected the call',
                      **_TOOL_CLOCK_FIELDS, **_TOOL_END_CLOCK_FIELD}),
    EventSpec(EventType.TOOL_COMPLETE, _C.TOOL,
              'A tool call finished; carries the final tool message.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'toolName': 'canonical tool name',
                      'toolContent': 'final model-visible tool result',
                      'isError': 'bool',
                      'status': "(optional) terminal NON-SUCCESS verdict — "
                                "'rejected' / 'aborted' / 'error' (tool raised "
                                "or pool-timeout-cancelled, 2026-08-06 silent-"
                                "timeout incident). ABSENT on success; the "
                                "client must never promote a verdict-bearing "
                                "round to 'done'",
                      **_TOOL_CLOCK_FIELDS, **_TOOL_END_CLOCK_FIELD}),
    EventSpec(EventType.TOOL_SCHEMA_REJECTED, _C.TOOL,
              'A malformed provider-visible tool schema was isolated before '
              'the request left the process. The tool is omitted for this '
              'request; execution authority and the LLM transcript are unchanged.',
              fields={'toolName': 'isolated tool name (or unknown tool)',
                      'stage': 'provider-boundary validation stage',
                      'reasonCode': 'stable rejection class',
                      'detail': 'bounded structural validation detail',
                      'action': 'omitted',
                      'model': 'request model id',
                      'parentSpanId': '(optional) owning model request span',
                      'roundNum': '(optional) model round number'}),
    EventSpec(EventType.TOOL_WIRE_PROJECTION, _C.TOOL,
              'Bounded Request Inspector evidence for the final provider tool '
              'surface. This diagnostic grants no execution authority and '
              'never enters the LLM transcript.',
              fields={'roundNum': 'model round number',
                      'turn': '(optional) Flow node phase',
                      'model': 'wire model id',
                      'backend': 'resolved tool discovery backend',
                      'toolNames': 'ordered bounded final provider tool names',
                      'toolCount': 'exact final provider tool count',
                      'schemaTokens': 'model-tokenizer schema estimate',
                      'schemaFingerprint': 'opaque exact provider-schema digest',
                      'schemaBudgetTokens': 'explicit cost target, zero=uncapped',
                      'budgetDroppedNames': 'names omitted by explicit budget',
                      'compactedNames': 'names annotation-compacted by budget',
                      'executableToolCount': 'server-authorized catalog size',
                      'parentSpanId': 'owning model request span'}),
    EventSpec(EventType.TOOL_COMPACTED, _C.TOOL,
              'A prior tool result was compacted out of context to save tokens.',
              fields={'toolCallId': 'tool-call id', 'roundNum': 'round index'}),
    EventSpec(EventType.TOOL_CALL_REPLAY, _C.TOOL,
              'A previously settled idempotent tool call was reused without '
              'executing the tool again.',
              fields={'toolName': 'canonical tool name',
                      'content': 'replayed settled result',
                      'badge': 'replayed'}),
    EventSpec(EventType.PROGRAM_START, _C.TOOL,
              'A native or local orchestration program started. Its nested '
              'function calls remain ordinary tool lifecycle events.',
              fields={'roundNum': 'display-only parent round index',
                      'llmRound': 'model round that authored the program',
                      'programCallId': 'program call id used by child callers',
                      'code': 'JavaScript authored by the model',
                      'childCallIds': 'nested function-call ids',
                      'childToolNames': 'nested function names',
                      'limits': 'enforced calls/output/continuation ceilings',
                      'source': 'openai_ptc or execute_program',
                      'backend': 'native_openai or local_toolscript',
                      'status': 'running', 'tStart': 'program start epoch ms'}),
    EventSpec(EventType.PROGRAM_OUTPUT, _C.TOOL,
              'A native or local program produced its aggregate output.',
              fields={'roundNum': 'display-only parent round index',
                      'llmRound': 'originating model round',
                      'programCallId': 'program call id',
                      'result': 'program aggregate result',
                      'status': 'completed, incomplete, or error',
                      'childCallIds': 'final nested function-call ids',
                      'childToolNames': 'final nested function names',
                      'source': 'openai_ptc or execute_program',
                      'backend': 'native_openai or local_toolscript',
                      'tStart': 'program start epoch ms',
                      'tEnd': 'program completion epoch ms'}),
    # ───────────────────────── context ─────────────────────────
    EventSpec(EventType.ROUND_USAGE, _C.CONTEXT,
              'Token-usage accounting for a completed round.',
              fields={'usage': 'usage dict', 'roundNum': 'round number',
                      'model': 'model id'}),
    EventSpec(EventType.ROUND_COMMITTED, _C.CONTEXT,
              'A round was persisted server-side (durable checkpoint).',
              fields={
                  'roundNum': 'round number',
                  'taskId': 'owning task id',
                  'snapshotId': 'file-history snapshot id when available',
                  'gitSha': 'legacy alias for snapshotId',
                  'modifiedFileList': 'task-attributed changed paths',
                  'modifiedFiles': 'task-attributed changed-path count',
                  'linearGitCheckpoint': (
                      'single-checkout checkpoint/stable settlement receipt'),
              }),
    EventSpec(EventType.MESSAGES_SNAPSHOT, _C.CONTEXT,
              'A point-in-time copy of the message list (fallback/branch sync).',
              fields={'messages': 'message list', 'roundNum': 'round id/label (may be a string label like final/fallback)',
                      'label': 'human label'}),
    EventSpec(EventType.COMPACTION, _C.CONTEXT,
              'A pre-compaction transcript snapshot was archived.',
              fields={
                  'archiveId': 'owner-scoped archive id',
                  'convId': 'conversation id',
                  'trigger': 'working_set|window|force|reactive|manual',
                  'roundNum': 'task round number',
                  'tokensBefore': 'estimated pre-compaction input tokens',
                  'tokensAfter': 'estimated post tokens; zero until complete',
                  'tokenCountKind': 'estimated',
                  'msgsBefore': 'pre-compaction message count',
                  'msgsAfter': 'post-compaction count; zero until complete',
                  'model': 'task model id, not summary-model attribution',
                  'reason': 'bounded trigger explanation',
                  'snapshotKind': 'pre_compaction_transcript',
                  'ts': 'epoch seconds',
              }),
    EventSpec(EventType.COMPACTION_DONE, _C.CONTEXT,
              'A compaction archive received its final estimated counts.',
              fields={
                  'archiveId': 'owner-scoped archive id',
                  'convId': 'conversation id',
                  'trigger': 'working_set|window|force|reactive|manual',
                  'tokensBefore': 'estimated pre-compaction input tokens',
                  'tokensAfter': 'estimated complete post-compaction tokens',
                  'tokenCountKind': 'estimated',
                  'msgsBefore': 'pre-compaction message count',
                  'msgsAfter': 'complete post-compaction message count',
                  'reductionPct': 'estimated percentage token reduction',
                  'roundNum': 'task round number',
                  'receipt': 'bounded tofu.compaction-receipt/v1 result',
              }),
    EventSpec(EventType.MEMORY_PREFETCH, _C.CONTEXT,
              'Memory-prefetch pipeline stage update.',
              fields={'stage': 'pipeline stage', 'results': 'retrieved notes'}),
    EventSpec(EventType.PREFERENCES_APPLIED, _C.CONTEXT,
              'The bounded structured user context was injected into this '
              'turn (all categories always-on and cache-safe).',
              fields={'chars': 'profile size in chars',
                      'items': 'flat list of injected bullets (core + relevant detail) for the chip',
                      'core': 'always-on core-tier bullets injected this turn',
                      'detail': 'relevance-selected detail-tier bullets (empty on an irrelevant turn)'}),
    EventSpec(EventType.PREFERENCE_LEARNED, _C.CONTEXT,
              'A durable user-context item was learned or updated by the '
              'post-turn consolidation pass and can be undone.',
              fields={'kind': 'reinforced|pending',
                      'summary': 'one-line description of what was learned',
                      'pending': 'true when awaiting user confirm (new pref)',
                      'id': 'legacy alias for change_id',
                      'change_id': 'bounded undo-log change identifier',
                      'item_id': 'stable context item identifier',
                      'context_type': 'identity|work_rule|response_preference'}),
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
              'Off-agent (IDE) drift to a tracked file was snapshotted into '
              'file-history. Pure audit/provenance record — the frontend '
              'deliberately does NOT render it (the drift toast advertised an '
              "undo the UI had no path for; the snapshot's real consumer is "
              'the file-history timeline itself).',
              fields={'files': 'list of drifted project-relative paths',
                      'sha': 'file-history snapshot id'}),
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
    # ───────────────────────── flow ─────────────────────────
    EventSpec(EventType.FLOW_ITERATION, _C.FLOW,
              'Flow loop entered a new Planner/Worker/Critic/VU iteration.',
              fields={'iteration': 'index', 'phase': 'planner|worker|critic'}),
    EventSpec(EventType.FLOW_PLANNER_DONE, _C.FLOW,
              'Planner produced a plan.', fields={'plan': 'plan content'}),
    EventSpec(EventType.FLOW_CRITIC_MSG, _C.FLOW,
              'Critic verdict + feedback.',
              fields={'next_phase': 'planner|worker|stop',
                      'should_stop': '(legacy) bool', 'feedback': 'critic text'}),
    EventSpec(EventType.FLOW_NEW_TURN, _C.FLOW,
              'A fresh Worker/Planner turn began (new assistant bubble).',
              fields={'phase': 'planner|worker'}),
    EventSpec(EventType.FLOW_COMPLETE, _C.FLOW,
              'Flow loop terminated (approved or replan-capped).',
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
              fields={'roundNum': 'round number the peer message was injected before',
                      'count': 'number of peer messages injected this round',
                      'previews': 'list of {fromConv, text} — sender short-id + '
                                  'the original (unframed) message text'}),
    EventSpec(EventType.USER_STEER_INJECT, _C.LIFECYCLE,
              'A human "steer" message the user sent WHILE this turn was still '
              'generating (composer inject-mode = steer) was drained from the '
              'model-facing inbox and injected as a user-role message right '
              'before the next LLM round — never mid-stream, never splitting a '
              'tool_call/tool_result pair (postponed to the next CLEAN round '
              'boundary after any open tool_result closes). Distinct from a '
              'sibling peer message (peer_inbox_inject) and from a completed '
              'sub-agent result (swarm_inbox_inject): it is the OPERATOR '
              'talking to their own running turn. Delivered exactly once — the '
              'chip is emitted only AFTER the LLM call confirms consumption '
              '(deferred-confirm), and an abort before that re-routes the '
              'undelivered steer to the durable message_queue as a fresh next '
              'turn (never zero, never double). Drives an in-timeline chip '
              'mirroring peer_inbox_inject.',
              fields={'roundNum': 'round number the steer was injected before',
                      'count': 'number of steer messages injected this round',
                      'previews': 'list of {text} — the steer message text'}),
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
                      'conditionKind': 'current decision tier (llm|hybrid|code) — '
                                       'sent every poll so the UI reflects a mid-run '
                                       'hybrid→code auto-promotion, not just the '
                                       'creation-time kind',
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


# ── The phase registry: every status push the runtime can emit ──
# One level below the event registry: EventType.PHASE is the type, these are
# its declared `phase` values. Grouped by domain — 'chat' is the streaming
# status row (shared frontend contract); the production domains are the
# per-capability TaskRuntime channels (private producer↔consumer pairs whose
# phase events still ride the same wire type, catalogued here so the whole
# system's status pushes are perceivable in one place).
_CHAT = 'chat'
_PHASE_SPECS: tuple[PhaseSpec, ...] = (
    # ───────────────────────── chat turn ─────────────────────────
    PhaseSpec(Phase.LLM_THINKING, (_CHAT,),
              'An LLM round opened — the model is generating (the pre-token '
              'window and the inter-round "analyzing tool results" beat).',
              fields={'detail': 'English fallback label',
                      'detailKey': 'i18n key', 'detailArgs': 'interpolation args',
                      'roundNum': 'round index',
                      'toolContext': 'pre-joined English label of the previous '
                                     "round's tools (headless fallback)",
                      'toolContextTools': 'structured raw tool names behind '
                                          'toolContext (i18n clients compose '
                                          'from these)'}),
    PhaseSpec(Phase.TOOL_EXEC, (_CHAT,),
              'A tool batch is about to execute.',
              fields={'detail': 'English fallback label',
                      'tools': 'raw tool-name list of this dispatch (i18n '
                               'clients compose the localized label)'}),
    PhaseSpec(Phase.RETRYING, (_CHAT,),
              'A real retry after a failed provider attempt: dispatcher 429/'
              'transport failover, stream-anomaly retry, turn-level auto-retry, '
              'reactive compact, model fallback or pool rescue. Waiting '
              'heartbeats and self-tuning notices use other phases.',
              fields={'detail': 'human-readable fallback (may be zh)',
                      'detailKey': '(optional) i18n key',
                      'detailArgs': '(optional) interpolation args '
                                    '(model/attempt/max/elapsed/chars/reason/'
                                    'reasonKey)',
                      'attempt': '(optional) real retry counter',
                      'max': '(optional) retry budget',
                      'bucket': '(optional) retry signature bucket '
                                '(zero_byte|classic|partial_stream|empty_stop|'
                                'canned_greeting|turn)',
                      'backoff_s': '(optional) backoff before the retry',
                      'errorKind': '(optional) typed transient failure kind',
                      'continuationMode': '(optional) assistant_prefill or '
                                          'continuation_nudge',
                      'statusCode': '(optional) HTTP status that triggered the cycle',
                      'model': '(optional) raw model id'}),
    PhaseSpec(Phase.WAITING_MODEL, (_CHAT,),
              'Request dispatched and awaiting the first semantic provider '
              'progress; heartbeat frames keep this current.',
              fields={'detail': 'English fallback label',
                      'detailKey': 'i18n key', 'detailArgs': 'model label',
                      'model': 'raw model id'}),
    PhaseSpec(Phase.STREAM_STALLED, (_CHAT,),
              'A connected provider stream stopped making reasoning, text, '
              'or tool progress during the current attempt.',
              fields={'detail': 'English fallback label',
                      'detailKey': 'i18n key',
                      'detailArgs': 'model, total elapsed, semantic idle',
                      'model': 'raw model id'}),
    PhaseSpec(Phase.WORKING, (_CHAT,),
              'Generic working status: ordinary-turn and VU startup stages, '
              'Flow producer step_phase forwards, external CLI '
              'backends.',
              fields={'detail': 'English fallback status text',
                      'detailKey': '(optional) i18n key',
                      'detailArgs': '(optional) interpolation args',
                      'attempt': '(optional) retry counter (forwarded '
                                 'step_phase meta)',
                      'statusCode': '(optional) HTTP status (forwarded '
                                    'step_phase meta)'}),
    PhaseSpec(Phase.COMPACTING, (_CHAT,),
              'Proactive context-window compaction is running.',
              fields={'detail': 'English fallback label',
                      'detailKey': 'i18n key'}),
    PhaseSpec(Phase.TODO_CONTINUATION, (_CHAT,),
              'A checklist-incomplete stop was re-driven by the '
              'todo-continuation enforcer.',
              fields={'attempt': 'nudge counter', 'max': 'nudge budget',
                      'incomplete': 'incomplete item count',
                      'detail': 'fallback label (zh)'}),
    PhaseSpec(Phase.INTENT_STALL_NUDGE, (_CHAT,),
              'A prose-only stop right after a failed/rejected tool round was '
              'nudged once (the intent-stall guard).',
              fields={'attempt': 'nudge counter', 'max': 'nudge budget (1)',
                      'detail': 'English fallback label',
                      'detailKey': 'i18n key'}),
    PhaseSpec(Phase.TOOL_HISTORY_RESTORED, (_CHAT,),
              'Tool messages were rebuilt from the server-side store after a '
              'reload/compaction (diagnostic).',
              fields={'detail': 'summary text', 'stats': 'rebuild stats',
                      'overhead': 'estimated token overhead of the rebuild'}),
    PhaseSpec(Phase.TOOL_AUTHORITY, (_CHAT,),
              'A sub-agent repeatedly requested unavailable tools and is '
              'being stopped with an explicit capability-limit result.',
              fields={'detail': 'human-readable stop reason',
                      'roundNum': 'round at which the breaker opened'}),
    # ───────────────────── motion_video channel ─────────────────────
    PhaseSpec(Phase.RESEARCH, ('motion_video',),
              'Topic → recipe research is running.',
              fields={'topic': 'the job topic'}),
    PhaseSpec(Phase.SCRIPT_DONE, ('motion_video',),
              'The recipe produced scenes.json + the SRT timeline.',
              fields={'scenes': 'scene list', 'timed_from_audio': 'bool'}),
    PhaseSpec(Phase.PARSE, ('motion_video',),
              'The SRT was parsed into cues.',
              fields={'cues': 'cue count', 'span_s': '[start, end] seconds'}),
    PhaseSpec(Phase.STORYBOARD, ('motion_video',),
              'The storyboard was validated/built.',
              fields={'scenes': 'scene count'}),
    PhaseSpec(Phase.NARRATE, ('motion_video',),
              'Per-scene TTS narration finished (or degraded to silent).',
              fields={'degraded': 'bool', 'reused': '(optional) manifest reuse',
                      'scenes': '(optional) per-scene audio/target/overflow',
                      'detail': '(optional) degrade reason'}),
    PhaseSpec(Phase.COMPOSE, ('motion_video',),
              'Scene compositions authored (scene-author or template floor).',
              fields={'scenes': 'total scenes', 'authored': 'agent-authored count',
                      'templated': 'template-fallback count',
                      'gate_failed_scenes': 'scene ids with advisory gate findings'}),
    PhaseSpec(Phase.CONCAT, ('motion_video',),
              'Scene clips concatenated into the silent final.',
              fields={'duration_s': 'seconds', 'mode': 'concat mode'}),
    PhaseSpec(Phase.BURN_IN, ('motion_video',),
              'Sidecar subtitles burned in.',
              fields={'duration_s': 'seconds',
                      'auto': 'true when auto-triggered by degraded narration'}),
    PhaseSpec(Phase.MUX, ('motion_video',),
              'Narration muxed into the final video.',
              fields={'duration_s': 'seconds'}),
    PhaseSpec(Phase.REGEN, ('motion_video',),
              'A single-scene regeneration job started.',
              fields={'scene_id': 'scene being regenerated',
                      'regen_of': 'source job id'}),
    # ─────────────────────── podcast channel ───────────────────────
    PhaseSpec(Phase.SCRIPT, ('podcast',),
              'Spoken-script generation is running.', fields={}),
    PhaseSpec(Phase.AUDIO, ('podcast',),
              'TTS audio synthesis is running.',
              fields={'total': 'segment count'}),
    # ───────────────── research / longform channels ─────────────────
    PhaseSpec(Phase.START, ('research', 'longform'),
              'The job started (research: idea mining; longform: report).',
              fields={'direction': '(research) the mined direction',
                      'topic': '(longform) the report topic'}),
)

_PHASE_BY_VALUE: dict[str, PhaseSpec] = {s.phase: s for s in _PHASE_SPECS}

# Types that are stream-internal / transport and are NOT expected to be
# handled by an application frontend's event switch (the drift test exempts
# these from the "frontend must handle every type" direction).
TRANSPORT_TYPES: frozenset[str] = frozenset({EventType.PING, EventType.SSE_TIMEOUT})

#: Event types that get an ``emittedAt`` stamp at construction time.
#: Deliberately only low-frequency activity boundaries: tool execution, model
#: request spans/switches, and preflight schema isolation. ``delta`` is excluded on
#: purpose — stamping every token frame would add bytes to the hottest path in
#: the product for no diagnostic value.
_CLOCK_STAMPED_TYPES: frozenset[str] = frozenset({
    EventType.TOOL_START, EventType.TOOL_PROGRESS,
    EventType.TOOL_RESULT, EventType.TOOL_COMPLETE,
    EventType.TOOL_SCHEMA_REJECTED,
    EventType.MODEL_REQUEST_START, EventType.MODEL_REQUEST_COMPLETE,
    EventType.MODEL_FALLBACK,
})


def now_ms() -> float:
    """Wall-clock epoch MILLISECONDS — the wire unit for every event clock.

    Milliseconds, not seconds: the frontend compares these against
    ``Date.now()``, and a seconds/ms mixup renders as a 1970 timestamp that
    silently poisons every derived duration (the reason the paper media tabs
    carry a defensive ``_isPlausibleEpochMs``). One helper so the unit is
    decided in exactly one place.
    """
    import time as _time
    return _time.time() * 1000.0


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

    Low-frequency activity boundaries get an ``emittedAt`` stamp here — at the
    ONE typed construction chokepoint rather than at each call site, so the
    value always means the same instant ("the backend handed this frame to the
    stream") and cannot drift between emitters. That is what makes the
    transport segment (``receivedAt - emittedAt``) comparable across tools and
    gives model switches an exact durable decision time. An explicit
    ``emittedAt=`` kwarg wins, so replay can preserve the original. PHASE events
    intentionally remain unstamped: ``task['phase']`` is an immediate current-
    state snapshot, not a transport-duration sample, and keeps the byte shape
    older clients expect.

    Unregistered types are allowed (the wire stays forward-compatible) but log
    a debug line — the drift test is what enforces registration at CI time.
    """
    if type_ not in _BY_TYPE:
        logger.debug('[events] build_event for unregistered type=%r '
                     '(add an EventSpec to lib/agent_core/events.py)', type_)
    if type_ in _CLOCK_STAMPED_TYPES and 'emittedAt' not in fields:
        fields['emittedAt'] = now_ms()
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


def build_phase(phase: str, /, **fields: Any) -> dict[str, Any]:
    """The ONE construction site for :data:`EventType.PHASE` events.

    ``build_phase(Phase.RETRYING, detail='…', attempt=1)`` is byte-for-byte
    identical to the old ``{'type': 'phase', 'phase': 'retrying', ...}``
    literal (the ``phase`` field always lands second, right after ``type``).

    The unified interface for the stream's status-text pushes: every emitter
    — chat orchestrator, dispatcher retry hooks, compaction, endpoint/swarm
    adapters, production engines — constructs through here (delivering via
    their own append seam when it isn't ``manager.append_event``), so a
    cross-cutting change (a new envelope field, a dedup rule, an i18n
    convention) lands at ONE chokepoint instead of ~30 call sites.

    Unregistered phases are allowed (forward-compatible) but log a debug
    line — ``tests/test_phase_registry.py`` enforces registration at CI time.
    """
    if phase not in _PHASE_BY_VALUE:
        logger.debug('[events] build_phase for unregistered phase=%r '
                     '(add a Phase constant + PhaseSpec to '
                     'lib/agent_core/events.py)', phase)
    return build_event(EventType.PHASE, phase=phase, **fields)


def emit_phase(task: Any, phase: str, /, **fields: Any) -> Any:
    """Build a phase event and deliver it through the task event chokepoint.

    The one-liner form of ``append_event(task, build_phase(...))`` — the same
    lazy-import delivery contract as :func:`emit`.
    """
    event = build_phase(phase, **fields)
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


def all_phase_specs() -> tuple[PhaseSpec, ...]:
    """Return every registered :class:`PhaseSpec`."""
    return _PHASE_SPECS


def phase_values() -> frozenset[str]:
    """Return the set of all registered ``phase`` value strings."""
    return frozenset(_PHASE_BY_VALUE)


def get_phase_spec(phase: str) -> PhaseSpec | None:
    """Return the :class:`PhaseSpec` for *phase*, or ``None`` if unregistered."""
    return _PHASE_BY_VALUE.get(phase)


def is_registered_phase(phase: str) -> bool:
    """True if *phase* is a known ``phase`` value."""
    return phase in _PHASE_BY_VALUE


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
    by_domain: dict[str, list[dict]] = {}
    for p in _PHASE_SPECS:
        for d in p.domains:
            by_domain.setdefault(d, []).append({
                'phase': p.phase,
                'purpose': p.purpose,
                'fields': p.fields,
                'since': p.since,
            })
    return {
        'contract_version': EVENT_CONTRACT_VERSION,
        'transports': {
            'sse': ['/api/v1/tasks/<task_id>/stream'],
            'websocket': '/api/push',
            'cursor_replay': '/api/v1/tasks/<task_id>/events?cursor=N',
        },
        'terminal_types': sorted(terminal_types()),
        'interaction_types': sorted(interaction_types()),
        'categories': by_category,
        # The PHASE event's declared `phase` vocabulary, grouped by emitting
        # stream ('chat' = the shared status row; the rest are production
        # channels). A foreign frontend learns every status push it can see
        # without reading our source.
        'phases': by_domain,
    }


__all__ = [
    'EVENT_CONTRACT_VERSION',
    'EventCategory',
    'EventSpec',
    'EventType',
    'Phase',
    'PhaseSpec',
    'TRANSPORT_TYPES',
    'build_event',
    'build_phase',
    'emit',
    'emit_phase',
    'all_event_specs',
    'all_phase_specs',
    'event_types',
    'phase_values',
    'get_event_spec',
    'get_phase_spec',
    'is_registered',
    'is_registered_phase',
    'terminal_types',
    'interaction_types',
    'to_capabilities_dict',
]
