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

import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

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
class FieldSpec:
    """Machine-readable contract for ONE payload field of a schema'd event.

    Parameters
    ----------
    name:
        The wire field name (``type`` is implicit and never listed).
    kind:
        A tiny type DSL: one or more ``|``-separated alternatives of
        ``str`` / ``bool`` / ``int`` / ``number`` (int or float, bool
        excluded) / ``dict`` / ``list`` / ``None``.  Example:
        ``'int | None'``.  The vocabulary is deliberately closed — the
        conformance test rejects unknown alternatives, so a typo'd kind can
        never silently pass validation.
    required:
        True if every conforming emission MUST carry the field.  Keep this
        set minimal: a field that any real emitter legitimately omits (e.g.
        the success path omits ``status``) is optional, or the strict gate
        would raise on conforming traffic.
    """

    name: str
    kind: str
    required: bool = False


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
    schema:
        Optional machine-readable field contract — a tuple of
        :class:`FieldSpec`.  When present, :func:`build_event` validates every
        construction against it (strict under pytest / ``TOFU_EVENT_SCHEMA=
        strict``, warn-and-log in production) and the conformance suite
        (:mod:`tests/test_event_schema.py`) keeps it in exact sync with the
        prose ``fields`` map.  ``None`` means the event has not been migrated
        to a field-level contract yet; the wire stays permissive for it.
    """

    type: str
    category: str
    purpose: str
    terminal: bool = False
    requires_response: bool = False
    fields: dict[str, str] = field(default_factory=dict)
    since: int = 1
    schema: tuple[FieldSpec, ...] | None = None


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
    # ── detached run_command completion (background command) ──
    BACKGROUND_COMMAND_INJECT = 'background_command_inject'
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
    EXECUTOR_QUEUED = 'executor_queued'
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
    # research action runtime: the tool-epoch agent loop is executing
    AGENT = 'agent'


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
                               'working label instead of a bare waiting placeholder'},
              # No in-process emitter: the frame is served by the replay/
              # reconnect path. All fields optional (forward-compatible).
              schema=(
                  FieldSpec('content', 'str'),
                  FieldSpec('thinking', 'str'),
                  FieldSpec('status', 'str'),
                  FieldSpec('toolRounds', 'list'),
                  FieldSpec('error', 'dict'),
                  FieldSpec('finishReason', 'str'),
                  FieldSpec('usage', 'dict'),
                  FieldSpec('model', 'str'),
                  FieldSpec('phase', 'str'),
              )),
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
                      'model': '(optional) resolved model id',
                      'providerId': '(optional, retrying) physical provider '
                                    'for the current attempt',
                      'dispatchMode': '(optional, retrying) strict_model or '
                                      'pool_rescue',
                      'modelRoute': '(optional) selected→resolved orchestration '
                                    'route (selectedModel/resolvedModel/role/'
                                    'tier/kind)',
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
                                          'THESE when present',
                      # Production-channel and retry/queue phase fields (the
                      # PhaseSpec registry below carries the per-phase prose;
                      # the schema is the UNION every phase value may ride).
                      'attempt': '(optional) retry/nudge counter',
                      'max': '(optional) retry/nudge budget',
                      'bucket': '(optional) retry signature bucket',
                      'backoff_s': '(optional) backoff seconds before retry',
                      'errorKind': '(optional) typed transient failure kind',
                      'continuationMode': '(optional) retry continuation mode',
                      'statusCode': '(optional) HTTP status that triggered the cycle',
                      'incomplete': '(optional) todo-continuation incomplete count',
                      'stats': '(optional) tool-history rebuild stats',
                      'overhead': '(optional) tool-history rebuild token overhead',
                      'queuePosition': '(optional) executor FIFO position',
                      'queued': '(optional) executor queued root-task count',
                      'active': '(optional) executor active worker count',
                      'capacity': '(optional) executor worker-slot capacity',
                      'waitSeconds': '(optional) executor FIFO residence seconds',
                      'topic': '(optional, motion_video) job topic',
                      'scenes': '(optional, motion_video) scene list or count',
                      'timed_from_audio': '(optional, motion_video) SRT timing source',
                      'cues': '(optional, motion_video) parsed cue count',
                      'span_s': '(optional, motion_video) [start, end] seconds',
                      'degraded': '(optional, motion_video) narration degraded flag',
                      'reused': '(optional, motion_video) manifest-reuse flag',
                      'authored': '(optional, motion_video) agent-authored scene count',
                      'templated': '(optional, motion_video) template-fallback count',
                      'gate_failed_scenes': '(optional, motion_video) advisory '
                                            'gate finding scene ids',
                      'duration_s': '(optional, motion_video) clip seconds',
                      'mode': '(optional, motion_video) concat mode',
                      'auto': '(optional, motion_video) auto burn-in flag',
                      'scene_id': '(optional, motion_video) regen target scene',
                      'regen_of': '(optional, motion_video) regen source job id',
                      'total': '(optional, podcast) audio segment count',
                      'direction': '(optional, research) mined direction',
                      'action': '(optional, research) workspace action',
                      'toolCount': '(optional, research) bound tool-epoch size',
                      'routeId': '(optional, retrying) credential-free concrete '
                                 'network route id',
                      'routeMode': '(optional, retrying) direct|proxy|env|'
                                   'desktop|unknown',
                      'failureStage': '(optional, retrying) connect/'
                                      'provider_response/midstream/progress stage',
                      'creative_mode': '(optional, motion_video) scene-author '
                                       'creative mode',
                      'director': '(optional, motion_video) director brief'},
              schema=(
                  FieldSpec('phase', 'str', required=True),
                  FieldSpec('detail', 'str'),
                  FieldSpec('detailKey', 'str'),
                  FieldSpec('detailArgs', 'dict'),
                  FieldSpec('model', 'str'),
                  FieldSpec('providerId', 'str'),
                  FieldSpec('dispatchMode', 'str'),
                  FieldSpec('modelRoute', 'dict'),
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('tools', 'list'),
                  FieldSpec('toolContext', 'str'),
                  FieldSpec('toolContextTools', 'list'),
                  FieldSpec('attempt', 'int'),
                  FieldSpec('max', 'int'),
                  FieldSpec('bucket', 'str'),
                  FieldSpec('backoff_s', 'number'),
                  FieldSpec('errorKind', 'str'),
                  FieldSpec('continuationMode', 'str'),
                  FieldSpec('statusCode', 'int'),
                  FieldSpec('incomplete', 'int'),
                  FieldSpec('stats', 'dict'),
                  FieldSpec('overhead', 'int'),
                  FieldSpec('queuePosition', 'int'),
                  FieldSpec('queued', 'int'),
                  FieldSpec('active', 'int'),
                  FieldSpec('capacity', 'int'),
                  FieldSpec('waitSeconds', 'number'),
                  FieldSpec('topic', 'str'),
                  FieldSpec('scenes', 'int | list'),
                  FieldSpec('timed_from_audio', 'bool'),
                  FieldSpec('cues', 'int'),
                  FieldSpec('span_s', 'list'),
                  FieldSpec('degraded', 'bool'),
                  FieldSpec('reused', 'bool'),
                  FieldSpec('authored', 'int'),
                  FieldSpec('templated', 'int'),
                  FieldSpec('gate_failed_scenes', 'list'),
                  FieldSpec('duration_s', 'number'),
                  FieldSpec('mode', 'str'),
                  FieldSpec('auto', 'bool'),
                  FieldSpec('scene_id', 'str'),
                  FieldSpec('regen_of', 'str'),
                  FieldSpec('total', 'int'),
                  FieldSpec('direction', 'str'),
                  FieldSpec('action', 'str'),
                  FieldSpec('toolCount', 'int'),
                  FieldSpec('routeId', 'str'),
                  FieldSpec('routeMode', 'str'),
                  FieldSpec('failureStage', 'str'),
                  FieldSpec('creative_mode', 'str'),
                  FieldSpec('director', 'dict'),
              )),
    EventSpec(EventType.ROUND_START, _C.LIFECYCLE,
              'Explicit start boundary of an LLM round (the orchestrator loop '
              'index). Emitted at the TOP of every round the model actually '
              'runs — INCLUDING a prose-only round (streams text, no tool calls) '
              'and BEFORE the phase hint — so the client keys round attribution '
              'off a real boundary instead of inferring it from the first '
              '`tool_start` (a round with no tools had NO signal before). '
              'Non-terminal.',
              fields={'roundNum': 'the round index this boundary opens'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
              )),
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
                                'tool_loop|success_poll — why the round ended'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('reason', 'str', required=True),
              )),
    EventSpec(EventType.DONE, _C.LIFECYCLE,
              'Terminal event — the turn finished (success or, with `error`, failure).',
              terminal=True,
              fields={'error': 'error envelope if failed (else absent)',
                      'finishReason': 'provider/normalized terminal reason',
                      'streamState': 'closed ProviderStreamState evidence; '
                                     'provider_finished is the only success state',
                      'taskId': '(optional) owning task id (orchestrator '
                                'post-loop settle path)',
                      'usage': '(optional) accumulated token usage',
                      'preset': '(optional) generation preset name',
                      'model': '(optional) model id that produced the answer',
                      'thinkingDepth': '(optional) resolved thinking depth',
                      'toolSummary': '(optional) tool-summary text carried on '
                                     'the terminal frame',
                      'todoState': '(optional) public checklist state when the '
                                   'turn drove a todo list',
                      'todoBlocked': '(optional) true when the checklist '
                                     'blocked completion',
                      'waitingOn': '(optional) structured wait descriptor '
                                   '(e.g. a pending sub-agent set)',
                      'incomplete': '(optional) true when the flow loop '
                                    'finished with finishReason=incomplete',
                      'flowReason': '(optional) flow loop stop reason '
                                    '(orchestration terminal path only)',
                      'flowMode': '(optional) true when stamped by the flow '
                                  'orchestration terminal path',
                      'flowProjection': '(optional) orchestration projection '
                                        'label (orchestration path only)',
                      'orchestrationOutcome': '(optional) TerminalOutcome '
                                              'dict (orchestration path only)',
                      'actualModel': '(optional) server-authoritative model '
                                     'after mid-turn fallback/preset switch',
                      'actualDepth': '(optional) server-authoritative '
                                     'thinking depth at done',
                      'actualModes': '(optional) active mode labels '
                                     '(flow/goal) as {label,tone} dicts',
                      'userMsgId': '(optional) stable _msgId of the user '
                                   'turn that triggered this task',
                      'apiRounds': '(optional) per-API-round evidence dicts '
                                   '(usage/model/provider/cost per round)',
                      'fallbackModel': '(optional) model fell back to',
                      'fallbackFrom': '(optional) model fell back from',
                      'fallbackReason': '(optional) why fallback happened',
                      'fallbackKind': '(optional) fallback classification',
                      'modifiedFiles': '(optional) checkpoint-merged '
                                       'modified path list',
                      'modifiedFileList': '(optional) checkpoint-merged '
                                          'rich modified-file dicts',
                      'cost': '(optional) priced cost snapshot for the turn',
                      'costExperiment': '(optional) cost-experiment outcome '
                                        'dict when an arm is active',
                      'latestLiveTaskId': '(optional) successor live task id '
                                          '(autopilot VU chain)',
                      'latestLiveTaskIsVu': '(optional) true when the '
                                            'successor is a VU sub-task',
                      '_diagnostics': '(optional) internal loop-exit '
                                      'diagnostics on suspicion'},
              schema=(
                  FieldSpec('error', 'dict'),
                  FieldSpec('finishReason', 'str'),
                  FieldSpec('streamState', 'str'),
                  FieldSpec('taskId', 'str'),
                  FieldSpec('usage', 'dict'),
                  FieldSpec('preset', 'str'),
                  FieldSpec('model', 'str'),
                  FieldSpec('thinkingDepth', 'str'),
                  FieldSpec('toolSummary', 'str'),
                  FieldSpec('todoState', 'dict'),
                  FieldSpec('todoBlocked', 'bool'),
                  FieldSpec('waitingOn', 'dict'),
                  FieldSpec('incomplete', 'bool'),
                  FieldSpec('flowReason', 'str'),
                  FieldSpec('flowMode', 'bool'),
                  FieldSpec('flowProjection', 'str'),
                  FieldSpec('orchestrationOutcome', 'dict'),
                  FieldSpec('actualModel', 'str'),
                  FieldSpec('actualDepth', 'str'),
                  FieldSpec('actualModes', 'list'),
                  FieldSpec('userMsgId', 'str'),
                  FieldSpec('apiRounds', 'list'),
                  FieldSpec('fallbackModel', 'str'),
                  FieldSpec('fallbackFrom', 'str'),
                  FieldSpec('fallbackReason', 'str'),
                  FieldSpec('fallbackKind', 'str'),
                  FieldSpec('modifiedFiles', 'list'),
                  FieldSpec('modifiedFileList', 'list'),
                  FieldSpec('cost', 'dict'),
                  FieldSpec('costExperiment', 'dict'),
                  FieldSpec('latestLiveTaskId', 'str'),
                  FieldSpec('latestLiveTaskIsVu', 'bool'),
                  FieldSpec('_diagnostics', 'dict'),
              )),
    EventSpec(EventType.ERROR, _C.LIFECYCLE,
              'Legacy terminal error envelope. New fatal paths normally emit '
              'a `done` with `error`, but Turn authority still settles this '
              'frame for compatibility.',
              terminal=True,
              fields={'content': 'error text', 'detail': 'structured detail'},
              # No in-process emitter (Turn authority compat frame) — optional.
              schema=(
                  FieldSpec('content', 'str'),
                  FieldSpec('detail', 'dict'),
              )),
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
                      'contentEpoch': 'monotonic text generation after this reset'},
              schema=(
                  FieldSpec('attempt', 'int', required=True),
                  FieldSpec('max', 'int', required=True),
                  FieldSpec('kind', 'str', required=True),
                  FieldSpec('contentEpoch', 'int', required=True),
              )),
    EventSpec(EventType.MODEL_REQUEST_START, _C.LIFECYCLE,
              'A logical model dispatch started. This low-frequency boundary '
              'opens the durable activity span used to correlate subsequent '
              'wait/retry frames and the terminal request result.',
              fields={'spanId': 'task-local stable request span id',
                      'model': 'requested model id',
                      'providerId': '(optional) provider id when already known',
                      'roundNum': '(optional) model round number',
                      'requestTag': 'task-local request label (R1/FALLBACK/etc.)',
                      'emittedAt': 'epoch ms when the backend handed this frame '
                                   'to the event chokepoint'},
              schema=(
                  FieldSpec('spanId', 'str', required=True),
                  FieldSpec('model', 'str', required=True),
                  FieldSpec('providerId', 'str'),
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('requestTag', 'str', required=True),
                  FieldSpec('emittedAt', 'number'),
              )),
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
                      'errorUrl': '(failure only) request URL the error is '
                                  'bound to (abort/endpoint diagnostics)',
                      'statusCode': '(failure only) HTTP status when known',
                      'routeId': '(optional) credential-free concrete network route id',
                      'routeMode': '(optional) direct|proxy|env|desktop|unknown',
                      'routeDecision': '(optional) why this route was selected',
                      'failureStage': '(optional) connect/provider_response/midstream/progress stage',
                      'roundNum': '(optional) model round number',
                      'requestTag': 'task-local request label',
                      'observerIsolation': '(optional) '
                                           'tofu.provider-ingress-isolation/v1 '
                                           'receipt when provider dispatches were '
                                           'guarded this span',
                      'emittedAt': 'epoch ms when the backend handed this frame '
                                   'to the event chokepoint'},
              schema=(
                  FieldSpec('spanId', 'str', required=True),
                  FieldSpec('model', 'str', required=True),
                  FieldSpec('providerId', 'str'),
                  FieldSpec('status', 'str', required=True),
                  FieldSpec('finishReason', 'str'),
                  FieldSpec('streamState', 'str'),
                  FieldSpec('durationMs', 'int', required=True),
                  FieldSpec('errorKind', 'str'),
                  FieldSpec('errorDetail', 'str'),
                  FieldSpec('errorUrl', 'str'),
                  FieldSpec('statusCode', 'int'),
                  FieldSpec('routeId', 'str'),
                  FieldSpec('routeMode', 'str'),
                  FieldSpec('routeDecision', 'str'),
                  FieldSpec('failureStage', 'str'),
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('requestTag', 'str', required=True),
                  FieldSpec('observerIsolation', 'dict'),
                  FieldSpec('emittedAt', 'number'),
              )),
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
                                        'capped at 300 chars)',
                      'emittedAt': 'epoch ms when the backend handed this frame '
                                   'to the event chokepoint'},
              schema=(
                  FieldSpec('fallbackModel', 'str', required=True),
                  FieldSpec('fallbackFrom', 'str', required=True),
                  FieldSpec('fallbackKind', 'str', required=True),
                  FieldSpec('fallbackReason', 'str', required=True),
                  FieldSpec('emittedAt', 'number'),
              )),
    # ───────────────────────── content ─────────────────────────
    EventSpec(EventType.DELTA, _C.CONTENT,
              'Incremental assistant output — append to the live bubble.',
              fields={'content': 'text delta (may be absent)',
                      'thinking': 'reasoning delta (may be absent)'},
              schema=(
                  FieldSpec('content', 'str'),
                  FieldSpec('thinking', 'str'),
              )),
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
                      'contentEpoch': 'monotonic text generation after this reset'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('discard', 'bool'),
                  FieldSpec('contentEpoch', 'int', required=True),
              )),
    # ───────────────────────── tool ─────────────────────────
    EventSpec(EventType.TOOL_START, _C.TOOL,
              'A tool call began executing.',
              fields={'roundNum': 'round index', 'toolName': 'tool name',
                      'agentId': '(optional, swarm emissions) owning sub-agent '
                                 'id; the Request Inspector stream for this '
                                 'call is `{parentTaskId}#agent:{agentId}`',
                      'toolCallId': 'tool-call id', 'query': 'display string',
                      'toolArgs': 'serialized args',
                      'caller': '(optional) Responses nested-call owner',
                      'programCallId': '(optional) parent program call id',
                      'parentToolCallId': '(optional) presentation-only parent '
                                          'for nested/recovery calls',
                      'attentionKind': '(optional) stable routine, important, '
                                       'or interactive semantic importance',
                      'status': "(optional) 'rejected' when the tool was a "
                                'hallucination and never ran',
                      'llmRound': '(optional, swarm emissions) provider LLM '
                                  'round index when it differs from the task '
                                  'roundNum',
                      '_rejected': '(optional) {attempted, suggestions} for a '
                                   'rejected hallucinated tool',
                      '_contractError': '(optional) stable ToolContractV2 '
                                        'error for a call rejected before '
                                        'execution',
                      '_toolRoot': '(optional) resolved workspace root for '
                                   'multi-root fs tools (path display)',
                      'source': '(optional) provider wire origin '
                                '(native_direct/program/…), stamped on every '
                                'parsed call',
                      'rejection': '(optional) typed rejection descriptor '
                                   'when status=rejected',
                      'assistantContent': '(optional, first call of the round) '
                                          'prose the model emitted alongside '
                                          'its tool calls',
                      'thinking': '(optional, first call of the round) '
                                  'reasoning text for Continue replay',
                      'thinkingSignature': '(optional, first call of the '
                                           'round) thinking-continuity '
                                           'signature',
                      '_batchQueries': '(optional) full query list of a '
                                       'batch web_search call for '
                                       'structured rendering',
                      '_batchUrls': '(optional) full URL list of a batch '
                                    'fetch call for structured rendering',
                      '_artifactOrigin': '(optional) structured provenance '
                                         'twin of the display label on a '
                                         'continuation round (origin chip)',
                      '_hiddenToolAdapter': '(optional) true when this row '
                                            'is the presentation adapter of '
                                            'a hidden gateway call '
                                            '(execute_tools)',
                      '_mcpLinks': '(optional) {label → href} clickable-link '
                                   'map re-keyed to fresh MCP labels',
                      '_serverDownload': '(optional) true when the file is '
                                         'staged server-side for download',
                      '_swarm': '(optional) true on the spawn_agents round '
                                '(swarm panel presentation)',
                      **_TOOL_CLOCK_FIELDS},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('toolName', 'str'),
                  FieldSpec('agentId', 'str'),
                  FieldSpec('toolCallId', 'str'),
                  FieldSpec('query', 'str', required=True),
                  FieldSpec('toolArgs', 'str | dict'),
                  FieldSpec('caller', 'dict'),
                  FieldSpec('programCallId', 'str'),
                  FieldSpec('parentToolCallId', 'str'),
                  FieldSpec('attentionKind', 'str'),
                  FieldSpec('status', 'str'),
                  FieldSpec('llmRound', 'int'),
                  FieldSpec('_rejected', 'dict'),
                  FieldSpec('_contractError', 'dict'),
                  FieldSpec('_toolRoot', 'str'),
                  FieldSpec('source', 'str'),
                  FieldSpec('rejection', 'dict'),
                  FieldSpec('assistantContent', 'str'),
                  FieldSpec('thinking', 'str'),
                  FieldSpec('thinkingSignature', 'str'),
                  FieldSpec('_batchQueries', 'list'),
                  FieldSpec('_batchUrls', 'list'),
                  FieldSpec('_artifactOrigin', 'dict'),
                  FieldSpec('_hiddenToolAdapter', 'bool'),
                  FieldSpec('_mcpLinks', 'dict'),
                  FieldSpec('_serverDownload', 'bool'),
                  FieldSpec('_swarm', 'bool'),
                  FieldSpec('tStart', 'number'),
                  FieldSpec('emittedAt', 'number'),
              )),
    EventSpec(EventType.TOOL_PROGRESS, _C.TOOL,
              'Streaming progress emitted by a long-running tool.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'detail': 'progress text',
                      'agentId': '(optional, swarm emissions) owning sub-agent '
                                 'id (see tool_start)',
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
                      'toolName': '(optional) tool name (stream/batch frames)',
                      'elapsed': '(optional) heartbeat-reported elapsed '
                                 'seconds',
                      'contractVersion': '(optional) stream-chunk payload '
                                         'contract tag '
                                         '(tool_runtime/progress, e.g. '
                                         "'tofu.tool-progress/v1')",
                      'version': '(optional) stream-chunk format version',
                      'taskId': '(optional) owning task id (stream-chunk '
                                'frames stamp their own before the transport '
                                'setdefault)',
                      'seq': '(optional) per-tool stream-chunk sequence '
                             '(stream-chunk frames stamp their own before '
                             'the transport re-assigns)',
                      'stream': '(optional) stdout|stderr for stream chunks',
                      'chunk': '(optional) stream chunk text',
                      'bytes': '(optional) chunk byte length',
                      'chars': '(optional) chunk char length',
                      'spooling': '(optional) true while output is being '
                                  'spooled',
                      'truncated': '(optional) true when the chunk tail was '
                                   'truncated',
                      'terminalReason': '(optional) why the stream group '
                                        'closed (final frame only)',
                      'grepSearchIntercepted': '(optional) true on the '
                                               'display-only frame published '
                                               'when run_command delegates a '
                                               'file grep to the runtime '
                                               'grep_search engine',
                      **_TOOL_CLOCK_FIELDS},
              # tool_runtime/progress builds the frame EMPTY and mutates it
              # afterwards, so NOTHING here may be required (construction-time
              # validation sees only the type + emittedAt).
              schema=(
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('toolCallId', 'str'),
                  FieldSpec('detail', 'str'),
                  FieldSpec('agentId', 'str'),
                  FieldSpec('execStartTs', 'number'),
                  FieldSpec('deadlineTs', 'number'),
                  FieldSpec('batchItem', 'str'),
                  FieldSpec('batchDone', 'int'),
                  FieldSpec('batchTotal', 'int'),
                  FieldSpec('batchOk', 'bool'),
                  FieldSpec('_selfTick', 'bool'),
                  FieldSpec('query', 'str'),
                  FieldSpec('_repaired', 'dict'),
                  FieldSpec('toolName', 'str'),
                  FieldSpec('elapsed', 'int'),
                  FieldSpec('contractVersion', 'str'),
                  FieldSpec('version', 'int'),
                  FieldSpec('taskId', 'str'),
                  FieldSpec('seq', 'int'),
                  FieldSpec('stream', 'str'),
                  FieldSpec('chunk', 'str'),
                  FieldSpec('bytes', 'int'),
                  FieldSpec('chars', 'int'),
                  FieldSpec('spooling', 'bool'),
                  FieldSpec('truncated', 'bool'),
                  FieldSpec('terminalReason', 'str'),
                  FieldSpec('grepSearchIntercepted', 'bool'),
                  FieldSpec('tStart', 'number'),
                  FieldSpec('emittedAt', 'number'),
              )),
    EventSpec(EventType.TOOL_RESULT, _C.TOOL,
              'A tool produced a (possibly partial) result payload.',
              fields={'roundNum': 'round index',
                      'agentId': '(optional, swarm emissions) owning sub-agent '
                                 'id (see tool_start)',
                      'toolCallId': 'tool-call id',
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
                      'rejection': '(optional) normalized tool-rejection '
                                   'descriptor (see tool_complete)',
                      '_repaired': '(optional) {label, detail, patterns} — '
                                   'args auto-repair badge metadata',
                      'llmRound': '(optional, swarm emissions) provider LLM '
                                  'round index when it differs from the task '
                                  'roundNum',
                      'cacheSource': '(optional) prefetch|cache — the result '
                                     'was served from a cached tool call',
                      'engineBreakdown': '(optional) per-engine result counts '
                                         'on a cache-hit search frame',
                      'verticals': '(optional) batch web_search vertical list',
                      'vertical': '(optional) structured vertical of a '
                                  'cache-hit search (academic/finance/…)',
                      'searchDiag': '(optional) search-diagnostics dict '
                                    'carried through a cache hit',
                      'toolSearchTotal': '(optional) search_tools total '
                                         'catalogue hits',
                      'toolSearchNextCursor': '(optional) search_tools '
                                              'pagination cursor (null = end)',
                      'toolSearchFailOpen': '(optional) true when '
                                            'search_tools degraded to '
                                            'fail-open listing',
                      '_providerAttemptDiscarded': '(optional) true on an '
                                                   'orphan early-announced '
                                                   'round superseded by a '
                                                   'provider retry',
                      '_batchQueries': '(optional) full query list of a '
                                       'batch web_search call for '
                                       'structured rendering',
                      **_TOOL_CLOCK_FIELDS, **_TOOL_END_CLOCK_FIELD},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('agentId', 'str'),
                  FieldSpec('toolCallId', 'str'),
                  FieldSpec('toolName', 'str'),
                  FieldSpec('results', 'list', required=True),
                  FieldSpec('query', 'str', required=True),
                  FieldSpec('status', 'str'),
                  FieldSpec('_rejected', 'dict'),
                  FieldSpec('_contractError', 'dict'),
                  FieldSpec('rejection', 'dict'),
                  FieldSpec('_repaired', 'dict'),
                  FieldSpec('llmRound', 'int'),
                  FieldSpec('cacheSource', 'str'),
                  FieldSpec('engineBreakdown', 'dict'),
                  FieldSpec('verticals', 'list'),
                  FieldSpec('vertical', 'str'),
                  FieldSpec('searchDiag', 'dict'),
                  FieldSpec('toolSearchTotal', 'int'),
                  FieldSpec('toolSearchNextCursor', 'str | None'),
                  FieldSpec('toolSearchFailOpen', 'bool'),
                  FieldSpec('_providerAttemptDiscarded', 'bool'),
                  FieldSpec('_batchQueries', 'list'),
                  FieldSpec('tStart', 'number'),
                  FieldSpec('tEnd', 'number'),
                  FieldSpec('emittedAt', 'number'),
              )),
    EventSpec(EventType.TOOL_COMPLETE, _C.TOOL,
              'A tool call finished; carries the final tool message.',
              fields={'roundNum': 'round index', 'toolCallId': 'tool-call id',
                      'toolName': 'canonical tool name',
                      'toolContent': 'final model-visible tool result',
                      'toolTokens': '(optional) real tokens of the final '
                                    'model-visible toolContent',
                      'compactionLayer': "(optional) 'L0'/'unchanged' when the "
                                         'result was budget-enveloped or '
                                         'receipt-replaced before first '
                                         'entering context',
                      'compactedFromChars': 'pre-compaction producer chars',
                      'compactedToChars': 'post-compaction model-visible '
                                          'chars',
                      'rawToolTokens': '(optional) real tokens of the '
                                       'pre-compaction producer result '
                                       '(never entered context)',
                      'toolResultEvidence': '(optional) bounded non-model '
                                            'tofu.tool-result-evidence/v1 '
                                            'sidecar for runtime/evaluation',
                      'llmRound': '(optional, swarm emissions) provider LLM '
                                  'round index when it differs from the task '
                                  'roundNum',
                      'agentId': '(optional, swarm emissions) owning sub-agent '
                                 'id (see tool_start)',
                      'rejection': '(optional) normalized tool-rejection '
                                   'descriptor (kind/tool/reason/suggestions); '
                                   'stamped together with the `_rejected` '
                                   'legacy alias by stamp_tool_rejection',
                      '_rejected': '(optional) legacy alias of `rejection` '
                                   '(same normalized descriptor object)',
                      'isError': 'bool',
                      'status': "(optional) terminal NON-SUCCESS verdict — "
                                "'rejected' / 'aborted' / 'error' (tool raised "
                                "or pool-timeout-cancelled, 2026-08-06 silent-"
                                "timeout incident). ABSENT on success; the "
                                "client must never promote a verdict-bearing "
                                "round to 'done'",
                      **_TOOL_CLOCK_FIELDS, **_TOOL_END_CLOCK_FIELD},
              # The FIRST field-level wire contract (the pilot). Covers every
              # field the two real emitters (chat pipeline settle + swarm
              # agent) can put on the frame, including the post-construction
              # stamps the pipeline adds by mutation (status / rejection /
              # compaction fields). tests/test_event_schema.py keeps this
              # tuple in exact sync with the prose map above and validates
              # real pipeline emissions against it.
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('toolCallId', 'str', required=True),
                  FieldSpec('toolName', 'str', required=True),
                  FieldSpec('toolContent', 'str', required=True),
                  FieldSpec('toolTokens', 'int'),
                  FieldSpec('compactionLayer', 'str'),
                  # The L0 lane assigns these via ``round_entry.get(...)`` —
                  # a missing source value lands on the wire as null.
                  FieldSpec('compactedFromChars', 'int | None'),
                  FieldSpec('compactedToChars', 'int | None'),
                  FieldSpec('rawToolTokens', 'int'),
                  FieldSpec('toolResultEvidence', 'dict'),
                  FieldSpec('isError', 'bool'),
                  FieldSpec('status', 'str'),
                  FieldSpec('rejection', 'dict'),
                  FieldSpec('_rejected', 'dict'),
                  FieldSpec('llmRound', 'int'),
                  FieldSpec('agentId', 'str'),
                  FieldSpec('tStart', 'number'),
                  FieldSpec('tEnd', 'number'),
                  FieldSpec('emittedAt', 'number'),
              )),
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
                      'parentSpanId': 'owning model request span',
                      'roundNum': '(optional) model round number',
                      'emittedAt': 'epoch ms when the backend handed this frame '
                                   'to the event chokepoint'},
              schema=(
                  FieldSpec('toolName', 'str', required=True),
                  FieldSpec('stage', 'str', required=True),
                  FieldSpec('reasonCode', 'str', required=True),
                  FieldSpec('detail', 'str', required=True),
                  FieldSpec('action', 'str', required=True),
                  FieldSpec('model', 'str', required=True),
                  FieldSpec('parentSpanId', 'str', required=True),
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('emittedAt', 'number'),
              )),
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
                      'parentSpanId': 'owning model request span'},
              schema=(
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('turn', 'str', required=True),
                  FieldSpec('model', 'str', required=True),
                  FieldSpec('backend', 'str', required=True),
                  FieldSpec('toolNames', 'list', required=True),
                  FieldSpec('toolCount', 'int', required=True),
                  FieldSpec('schemaTokens', 'int', required=True),
                  FieldSpec('schemaFingerprint', 'str', required=True),
                  FieldSpec('schemaBudgetTokens', 'int', required=True),
                  FieldSpec('budgetDroppedNames', 'list', required=True),
                  FieldSpec('compactedNames', 'list', required=True),
                  FieldSpec('executableToolCount', 'int', required=True),
                  FieldSpec('parentSpanId', 'str', required=True),
              )),
    EventSpec(EventType.TOOL_COMPACTED, _C.TOOL,
              'A prior tool result was compacted out of context to save tokens.',
              fields={'toolCallId': 'tool-call id', 'roundNum': 'round index',
                      'toolName': 'canonical tool name',
                      'compactionLayer': 'replacement compaction layer',
                      'compactedFromChars': 'prior model-visible chars',
                      'compactedToChars': 'replacement model-visible chars',
                      'toolTokens': 'replacement model-visible tokens',
                      'compactedContent': 'replacement model-visible result',
                      'toolResultEvidence': '(optional) replacement bounded '
                                            'non-model evidence sidecar'},
              schema=(
                  FieldSpec('toolCallId', 'str', required=True),
                  FieldSpec('roundNum', 'int | None'),
                  FieldSpec('toolName', 'str', required=True),
                  FieldSpec('compactionLayer', 'str', required=True),
                  FieldSpec('compactedFromChars', 'int', required=True),
                  FieldSpec('compactedToChars', 'int', required=True),
                  FieldSpec('toolTokens', 'int', required=True),
                  FieldSpec('compactedContent', 'str', required=True),
                  FieldSpec('toolResultEvidence', 'dict'),
              )),
    EventSpec(EventType.TOOL_CALL_REPLAY, _C.TOOL,
              'A previously settled idempotent tool call was reused without '
              'executing the tool again.',
              fields={'toolName': 'canonical tool name',
                      'content': 'replayed settled result',
                      'badge': 'replayed'},
              # Registry-only (no in-process emitter) — all optional.
              schema=(
                  FieldSpec('toolName', 'str'),
                  FieldSpec('content', 'str'),
                  FieldSpec('badge', 'str'),
              )),
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
                      'status': 'running', 'tStart': 'program start epoch ms'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('llmRound', 'int | None'),
                  FieldSpec('programCallId', 'str', required=True),
                  FieldSpec('code', 'str', required=True),
                  FieldSpec('childCallIds', 'list', required=True),
                  FieldSpec('childToolNames', 'list', required=True),
                  FieldSpec('limits', 'dict', required=True),
                  FieldSpec('source', 'str', required=True),
                  FieldSpec('backend', 'str', required=True),
                  FieldSpec('status', 'str', required=True),
                  FieldSpec('tStart', 'number | None'),
              )),
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
                      'tEnd': 'program completion epoch ms'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('llmRound', 'int | None'),
                  FieldSpec('programCallId', 'str', required=True),
                  FieldSpec('result', 'str | dict | None', required=True),
                  FieldSpec('status', 'str', required=True),
                  FieldSpec('childCallIds', 'list', required=True),
                  FieldSpec('childToolNames', 'list', required=True),
                  FieldSpec('source', 'str', required=True),
                  FieldSpec('backend', 'str', required=True),
                  FieldSpec('tStart', 'number | None'),
                  FieldSpec('tEnd', 'number', required=True),
              )),
    # ───────────────────────── context ─────────────────────────
    EventSpec(EventType.ROUND_USAGE, _C.CONTEXT,
              'Token-usage accounting for a completed round.',
              fields={'usage': 'usage dict', 'roundNum': 'round number',
                      'model': 'model id',
                      'tag': 'request label (R1/FALLBACK/etc.)',
                      'turn': 'flow phase label (empty when no flow)',
                      'tokensIn': 'input tokens this round',
                      'tokensOut': 'output tokens this round'},
              schema=(
                  FieldSpec('usage', 'dict', required=True),
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('model', 'str', required=True),
                  FieldSpec('tag', 'str', required=True),
                  FieldSpec('turn', 'str', required=True),
                  FieldSpec('tokensIn', 'int', required=True),
                  FieldSpec('tokensOut', 'int', required=True),
              )),
    EventSpec(EventType.ROUND_COMMITTED, _C.CONTEXT,
              'A round was persisted server-side (durable checkpoint).',
              fields={
                  'taskId': 'owning task id',
                  'snapshotId': 'file-history snapshot id when available',
                  'gitSha': 'legacy alias for snapshotId',
                  'modifiedFileList': 'task-attributed changed paths',
                  'modifiedFiles': 'task-attributed changed-path count',
                  'linearGitCheckpoint': (
                      'single-checkout checkpoint/stable settlement receipt'),
                  'addedByGit': 'paths the file-history diff attributes to '
                                'this round beyond the task-attributed list',
              },
              schema=(
                  FieldSpec('taskId', 'str', required=True),
                  FieldSpec('snapshotId', 'str'),
                  FieldSpec('gitSha', 'str'),
                  FieldSpec('modifiedFileList', 'list | None'),
                  FieldSpec('modifiedFiles', 'int | None'),
                  FieldSpec('linearGitCheckpoint', 'dict'),
                  FieldSpec('addedByGit', 'list'),
              )),
    EventSpec(EventType.MESSAGES_SNAPSHOT, _C.CONTEXT,
              'A point-in-time copy of the message list (fallback/branch sync).',
              fields={'messages': 'message list',
                      'kind': 'request|state — pre-flight request vs post-hoc '
                              'state snapshot',
                      'model': 'model id',
                      'roundNum': 'round id/label (may be a string label like final/fallback)',
                      'label': 'human label',
                      'contextManifest': '(optional) bounded context-manifest '
                                         'entries',
                      'turn': '(optional) flow phase label',
                      'params': '(optional, request kind) sampling params '
                                '{maxTokens, temperature, thinkingEnabled, …}',
                      'tools': '(optional, request kind) provider-visible '
                               'tool schemas',
                      'agentId': '(optional, swarm) owning sub-agent id',
                      'agentRole': '(optional, swarm) sub-agent role'},
              schema=(
                  FieldSpec('messages', 'list', required=True),
                  FieldSpec('kind', 'str', required=True),
                  FieldSpec('model', 'str', required=True),
                  FieldSpec('roundNum', 'int | str', required=True),
                  FieldSpec('label', 'str', required=True),
                  FieldSpec('contextManifest', 'list'),
                  FieldSpec('turn', 'str'),
                  FieldSpec('params', 'dict'),
                  FieldSpec('tools', 'list'),
                  FieldSpec('agentId', 'str'),
                  FieldSpec('agentRole', 'str'),
              )),
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
              },
              schema=(
                  FieldSpec('archiveId', 'str | int', required=True),
                  FieldSpec('convId', 'str', required=True),
                  FieldSpec('trigger', 'str', required=True),
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('tokensBefore', 'int', required=True),
                  FieldSpec('tokensAfter', 'int', required=True),
                  FieldSpec('tokenCountKind', 'str', required=True),
                  FieldSpec('msgsBefore', 'int', required=True),
                  FieldSpec('msgsAfter', 'int', required=True),
                  FieldSpec('model', 'str', required=True),
                  FieldSpec('reason', 'str', required=True),
                  FieldSpec('snapshotKind', 'str', required=True),
                  FieldSpec('ts', 'int', required=True),
              )),
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
              },
              schema=(
                  FieldSpec('archiveId', 'str', required=True),
                  FieldSpec('convId', 'str', required=True),
                  FieldSpec('trigger', 'str', required=True),
                  FieldSpec('tokensBefore', 'int', required=True),
                  FieldSpec('tokensAfter', 'int', required=True),
                  FieldSpec('tokenCountKind', 'str', required=True),
                  FieldSpec('msgsBefore', 'int', required=True),
                  FieldSpec('msgsAfter', 'int', required=True),
                  FieldSpec('reductionPct', 'number', required=True),
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('receipt', 'dict', required=True),
              )),
    EventSpec(EventType.MEMORY_PREFETCH, _C.CONTEXT,
              'Memory-prefetch pipeline stage update.',
              fields={'phase': 'started|done|skipped|failed',
                      'reason': '(skipped/failed) why the prefetch did not run',
                      'total_memories': '(started) memory catalogue size',
                      'candidate_target': '(started) selection budget',
                      'strategy': '(started/done) selection strategy label',
                      'selected': '(done) memories injected',
                      'candidates': '(done) candidate count considered',
                      'rejectedLowConfidence': '(done) candidates dropped by '
                                               'the confidence floor',
                      'total_ms': '(done) pipeline wall time',
                      'auxiliaryLlmCalls': '(done) extra LLM calls spent',
                      'memories': '(done) selected {name, description, reason}'},
              schema=(
                  FieldSpec('phase', 'str', required=True),
                  FieldSpec('reason', 'str'),
                  FieldSpec('total_memories', 'int'),
                  FieldSpec('candidate_target', 'int'),
                  FieldSpec('strategy', 'str'),
                  FieldSpec('selected', 'int'),
                  FieldSpec('candidates', 'int'),
                  FieldSpec('rejectedLowConfidence', 'int'),
                  FieldSpec('total_ms', 'int'),
                  FieldSpec('auxiliaryLlmCalls', 'int'),
                  FieldSpec('memories', 'list'),
              )),
    EventSpec(EventType.PREFERENCES_APPLIED, _C.CONTEXT,
              'The bounded structured user context was injected into this '
              'turn (all categories always-on and cache-safe).',
              fields={'chars': 'profile size in chars',
                      'items': 'flat list of injected bullets (core + relevant detail) for the chip'},
              schema=(
                  FieldSpec('chars', 'int', required=True),
                  FieldSpec('items', 'list', required=True),
              )),
    EventSpec(EventType.PREFERENCE_LEARNED, _C.CONTEXT,
              'A durable user-context item was learned or updated by the '
              'post-turn consolidation pass and can be undone.',
              fields={'kind': 'reinforced|pending',
                      'summary': 'one-line description of what was learned',
                      'pending': 'true when awaiting user confirm (new pref)',
                      'id': 'legacy alias for change_id',
                      'change_id': 'bounded undo-log change identifier',
                      'item_id': 'stable context item identifier',
                      'context_type': 'identity|work_rule|response_preference'},
              schema=(
                  FieldSpec('kind', 'str', required=True),
                  FieldSpec('summary', 'str', required=True),
                  FieldSpec('pending', 'bool', required=True),
                  FieldSpec('id', 'str', required=True),
                  FieldSpec('change_id', 'str', required=True),
                  FieldSpec('item_id', 'str', required=True),
                  FieldSpec('context_type', 'str', required=True),
              )),
    EventSpec(EventType.RELATED_CONVERSATIONS, _C.CONTEXT,
              'The bounded cross-conversation project digest (sibling '
              'conversations of the same project) was injected into this turn '
              'for ambient awareness. Drives a quiet "related conversations" '
              'provenance segment so the user can see — and audit — the same '
              'siblings the model was told about.',
              fields={'count': 'number of siblings surfaced',
                      'items': 'list of {id, title, summary}',
                      'toolsAvailable': 'whether get_conversation/'
                                        'list_conversations were registered this turn'},
              # Registry-only (no in-process emitter) — all optional.
              schema=(
                  FieldSpec('count', 'int'),
                  FieldSpec('items', 'list'),
                  FieldSpec('toolsAvailable', 'bool'),
              )),
    EventSpec(EventType.PROJECT_EXTERNAL_EDIT, _C.CONTEXT,
              'Off-agent (IDE) drift to a tracked file was snapshotted into '
              'file-history. Pure audit/provenance record — the frontend '
              'deliberately does NOT render it (the drift toast advertised an '
              "undo the UI had no path for; the snapshot's real consumer is "
              'the file-history timeline itself).',
              fields={'files': 'list of drifted project-relative paths',
                      'sha': 'file-history snapshot id (null when the drift '
                             'snapshot could not be written)'},
              schema=(
                  FieldSpec('files', 'list', required=True),
                  FieldSpec('sha', 'str | None', required=True),
              )),
    EventSpec(EventType.WORKSPACE_ROOT_ADDED, _C.CONTEXT,
              'An absolute-path write auto-registered a NEW extra workspace '
              'root (the silent workspace expansion that was previously '
              'invisible — no tool round, only an app.log line). Surfaces a '
              'brief "added workspace root X" notice so the user knows the '
              'agent widened the project scope.',
              fields={'roots': 'list of {rootName, path} auto-registered this tool call'},
              schema=(
                  FieldSpec('roots', 'list', required=True),
              )),
    # ─────────────────── interaction (need client reply) ───────────────────
    EventSpec(EventType.HUMAN_GUIDANCE_REQUEST, _C.INTERACTION,
              'Agent asked the human a question (ask_human tool); turn pauses.',
              requires_response=True,
              fields={'roundNum': 'round index',
                      'toolCallId': 'ask_human tool-call id',
                      'guidanceId': 'reply correlation id',
                      'question': 'prompt text',
                      'responseType': 'free_text|choice',
                      'options': 'choice options (normalized dicts)'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('toolCallId', 'str', required=True),
                  FieldSpec('guidanceId', 'str', required=True),
                  FieldSpec('question', 'str', required=True),
                  FieldSpec('responseType', 'str', required=True),
                  FieldSpec('options', 'list', required=True),
              )),
    EventSpec(EventType.WRITE_APPROVAL_REQUEST, _C.INTERACTION,
              'A write/exec tool needs explicit approval before running.',
              requires_response=True,
              fields={'roundNum': 'round index',
                      'toolCallId': 'id of the tool call awaiting approval',
                      'approvalId': 'reply correlation id',
                      'meta': '{approvalId, toolName, path, description, + '
                              'tool-specific enricher fields} preview payload'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('toolCallId', 'str', required=True),
                  FieldSpec('approvalId', 'str', required=True),
                  FieldSpec('meta', 'dict', required=True),
              )),
    EventSpec(EventType.APPROVAL_REQUIRED, _C.INTERACTION,
              'Generic approval gate (mode-based backends).',
              requires_response=True,
              fields={'detail': 'what needs approval'},
              # Registry-only (no in-process emitter) — all optional.
              schema=(
                  FieldSpec('detail', 'str'),
              )),
    EventSpec(EventType.STDIN_REQUEST, _C.INTERACTION,
              'A running command requested interactive stdin.',
              requires_response=True,
              fields={'roundNum': 'round index',
                      'toolCallId': 'run_command tool-call id',
                      'stdinId': 'reply correlation id',
                      'prompt': 'stdin prompt',
                      'command': 'bounded command line awaiting input'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('toolCallId', 'str', required=True),
                  FieldSpec('stdinId', 'str', required=True),
                  FieldSpec('prompt', 'str', required=True),
                  FieldSpec('command', 'str', required=True),
              )),
    EventSpec(EventType.STDIN_RESOLVED, _C.INTERACTION,
              'A pending stdin request was satisfied (clears the prompt UI).',
              fields={'roundNum': 'round index',
                      'toolCallId': 'run_command tool-call id',
                      'stdinId': 'correlation id'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('toolCallId', 'str', required=True),
                  FieldSpec('stdinId', 'str', required=True),
              )),
    # ───────────────────────── flow ─────────────────────────
    EventSpec(EventType.FLOW_ITERATION, _C.FLOW,
              'Flow loop entered a new Planner/Worker/Critic/VU iteration.',
              fields={'iteration': 'index', 'phase': 'planner|worker|critic',
                      'flowProjection': '(optional) orchestration projection '
                                        'label (absent on the synthetic guard '
                                        'frame)',
                      'turnRole': '(optional) planner|worker|virtual_user '
                                  'turn role',
                      'emits': '(optional) declared emit surface of the role',
                      'vuMsgId': '(optional, virtual_user role) VU bubble id',
                      'autopilotRunId': '(optional) owning autopilot run id'},
              schema=(
                  FieldSpec('iteration', 'int', required=True),
                  FieldSpec('phase', 'str', required=True),
                  FieldSpec('flowProjection', 'str'),
                  FieldSpec('turnRole', 'str'),
                  FieldSpec('emits', 'str'),
                  FieldSpec('vuMsgId', 'str'),
                  FieldSpec('autopilotRunId', 'str'),
              )),
    EventSpec(EventType.FLOW_PLANNER_DONE, _C.FLOW,
              'Planner produced a plan.',
              fields={'content': 'plan content',
                      'thinking': 'planner reasoning text'},
              schema=(
                  FieldSpec('content', 'str', required=True),
                  FieldSpec('thinking', 'str', required=True),
              )),
    EventSpec(EventType.FLOW_CRITIC_MSG, _C.FLOW,
              'Critic verdict + feedback.',
              fields={'iteration': 'flow iteration index',
                      'content': 'critic verdict text',
                      'thinking': '(optional) critic reasoning (absent on the '
                                  'synthetic guard frame)',
                      'next_phase': 'planner|worker|stop',
                      'discard': '(optional) true when the VU discarded this '
                                 'candidate',
                      'synthetic': '(optional) true on the backend guard frame',
                      'flowProjection': '(optional) orchestration projection '
                                        'label',
                      'turnRole': '(optional) turn role',
                      'emits': '(optional) declared emit surface of the role',
                      'vuMsgId': '(optional, virtual_user role) VU bubble id',
                      'autopilotRunId': '(optional) owning autopilot run id'},
              schema=(
                  FieldSpec('iteration', 'int', required=True),
                  FieldSpec('content', 'str', required=True),
                  FieldSpec('thinking', 'str'),
                  FieldSpec('next_phase', 'str', required=True),
                  FieldSpec('discard', 'bool'),
                  FieldSpec('synthetic', 'bool'),
                  FieldSpec('flowProjection', 'str'),
                  FieldSpec('turnRole', 'str'),
                  FieldSpec('emits', 'str'),
                  FieldSpec('vuMsgId', 'str'),
                  FieldSpec('autopilotRunId', 'str'),
              )),
    EventSpec(EventType.FLOW_NEW_TURN, _C.FLOW,
              'A fresh Worker/Planner turn began (new assistant bubble).',
              fields={'phase': 'planner|worker'},
              # Registry-only (no in-process emitter) — all optional.
              schema=(
                  FieldSpec('phase', 'str'),
              )),
    EventSpec(EventType.FLOW_COMPLETE, _C.FLOW,
              'Flow loop terminated (approved or replan-capped). Sole '
              'emitter: OrchestrationChatCompletion.finish.',
              fields={'totalIterations': 'flow iteration count',
                      'reason': 'loop stop reason',
                      'replanCount': 'replans consumed (always 0 today)',
                      'flowProjection': 'orchestration projection label',
                      'orchestrationOutcome': 'TerminalOutcome dict'},
              schema=(
                  FieldSpec('totalIterations', 'int'),
                  FieldSpec('reason', 'str'),
                  FieldSpec('replanCount', 'int'),
                  FieldSpec('flowProjection', 'str'),
                  FieldSpec('orchestrationOutcome', 'dict'),
              )),
    # ───────────────────────── swarm ─────────────────────────
    EventSpec(EventType.SWARM_PHASE, _C.SWARM,
              'Top-level swarm orchestration phase.',
              fields={'phase': 'spawning|error|complete|spawn_more',
                      'content': 'human-readable phase detail',
                      'swarmKey': '(optional) swarm grouping key',
                      'agents': '(optional) spawned agent descriptors '
                                '{agentId, role, objective, model, depends_on}',
                      'error': '(optional) driver error text',
                      'agentCount': '(complete) total agents',
                      'failedCount': '(complete) failed agents',
                      'totalTokens': '(complete) aggregate token usage'},
              schema=(
                  FieldSpec('phase', 'str', required=True),
                  FieldSpec('content', 'str', required=True),
                  FieldSpec('swarmKey', 'str'),
                  FieldSpec('agents', 'list'),
                  FieldSpec('error', 'str'),
                  FieldSpec('agentCount', 'int'),
                  FieldSpec('failedCount', 'int'),
                  FieldSpec('totalTokens', 'int'),
              )),
    EventSpec(EventType.SWARM_INBOX_INJECT, _C.SWARM,
              'A completed sub-agent result was injected into the main thread.',
              fields={'roundNum': 'round index the results were injected '
                                  'before',
                      'count': 'number of sub-agent results injected',
                      'agentIds': 'delivering sub-agent ids',
                      'previews': 'list of {agentId, text} result previews'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('count', 'int', required=True),
                  FieldSpec('agentIds', 'list', required=True),
                  FieldSpec('previews', 'list', required=True),
              )),
    EventSpec(EventType.SWARM_AGENT_PHASE, _C.SWARM,
              'A sub-agent changed phase (e.g. running).',
              fields={'agentId': 'sub-agent id', 'phase': 'phase',
                      'content': 'human-readable phase detail',
                      'role': '(optional) sub-agent role',
                      'objective': '(optional) sub-agent objective',
                      'model': '(optional) sub-agent model id',
                      'status': '(optional) retrying marker on agent_retry',
                      'duration_s': '(optional) elapsed seconds',
                      'tokens': '(optional) aggregate tokens',
                      'roundNum': '(optional) sub-agent round index',
                      'total_agents': '(optional) swarm size',
                      'completed_agents': '(optional) settled count'},
              # Constructed build-then-mutate in SwarmEvent.to_legacy
              # (build_event(TYPE, content=…) then camelCase keys land by
              # mutation) — only `content` is a construction-time guarantee;
              # the rest is enforced at the append_event delivery seam.
              schema=(
                  FieldSpec('agentId', 'str'),
                  FieldSpec('phase', 'str'),
                  FieldSpec('content', 'str', required=True),
                  FieldSpec('role', 'str'),
                  FieldSpec('objective', 'str'),
                  FieldSpec('model', 'str'),
                  FieldSpec('status', 'str'),
                  FieldSpec('duration_s', 'number'),
                  FieldSpec('tokens', 'int'),
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('total_agents', 'int'),
                  FieldSpec('completed_agents', 'int'),
              )),
    EventSpec(EventType.SWARM_AGENT_PROGRESS, _C.SWARM,
              'Sub-agent progress update.',
              fields={'agentId': 'sub-agent id',
                      'content': 'progress text',
                      'status': 'running',
                      'phase': 'done|tool_use|stalled|no_progress|'
                               'tool_authority',
                      'roundNum': 'sub-agent round index',
                      'role': 'sub-agent role',
                      'preview': '(optional, done) full final answer',
                      'toolNames': '(optional, tool_use) tools in flight'},
              # build-then-mutate via SwarmEvent.to_legacy (see
              # swarm_agent_phase) — `content` only at construction.
              schema=(
                  FieldSpec('agentId', 'str'),
                  FieldSpec('content', 'str', required=True),
                  FieldSpec('status', 'str'),
                  FieldSpec('phase', 'str'),
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('role', 'str'),
                  FieldSpec('preview', 'str'),
                  FieldSpec('toolNames', 'list'),
              )),
    EventSpec(EventType.SWARM_AGENT_COMPLETE, _C.SWARM,
              'Sub-agent finished (status may be error).',
              fields={'agentId': 'sub-agent id', 'status': 'ok|error',
                      'content': 'result/preview',
                      'role': '(optional) sub-agent role',
                      'objective': '(optional) sub-agent objective',
                      'model': '(optional) sub-agent model id',
                      'elapsed': '(optional) wall-clock seconds',
                      'tokens': '(optional) aggregate tokens',
                      'summary': '(optional) bounded result summary',
                      'error': '(optional) error text on failure',
                      'modifiedFiles': '(optional) changed-path count'},
              # build-then-mutate via SwarmEvent.to_legacy (see
              # swarm_agent_phase) — `content` only at construction.
              schema=(
                  FieldSpec('agentId', 'str'),
                  FieldSpec('status', 'str'),
                  FieldSpec('content', 'str', required=True),
                  FieldSpec('role', 'str'),
                  FieldSpec('objective', 'str'),
                  FieldSpec('model', 'str'),
                  FieldSpec('elapsed', 'number'),
                  FieldSpec('tokens', 'int'),
                  FieldSpec('summary', 'str'),
                  FieldSpec('error', 'str'),
                  FieldSpec('modifiedFiles', 'int'),
              )),
    EventSpec(EventType.SWARM_AGENT_ERROR, _C.SWARM,
              'Sub-agent errored.',
              fields={'agentId': 'sub-agent id', 'error': 'error text'},
              # Registry-only (failures fold into swarm_agent_complete) — all
              # optional.
              schema=(
                  FieldSpec('agentId', 'str'),
                  FieldSpec('error', 'str'),
              )),
    EventSpec(EventType.SWARM_AGENT_TOOL_CALL, _C.SWARM,
              'A sub-agent invoked a tool (for live trace UI).',
              fields={'agentId': 'sub-agent id', 'toolName': 'tool',
                      'content': 'display text',
                      'status': 'running',
                      'phase': 'tool_use',
                      'roundNum': 'sub-agent round index',
                      'role': 'sub-agent role',
                      'callId': 'tool-call correlation id',
                      'argsBrief': 'bounded args summary',
                      'callStatus': 'running|done|error status',
                      'callElapsed': '(finish) call wall-clock seconds',
                      'preview': '(finish) bounded result preview',
                      'previewTruncated': '(finish) preview was truncated',
                      'previewFullChars': '(finish) untruncated preview chars',
                      'error': '(finish, failure) bounded error text',
                      'errorTruncated': '(finish) error was truncated',
                      'errorFullChars': '(finish) untruncated error chars'},
              # build-then-mutate via SwarmEvent.to_legacy (see
              # swarm_agent_phase) — `content` only at construction; the
              # call lifecycle keys arrive by metadata mutation.
              schema=(
                  FieldSpec('agentId', 'str'),
                  FieldSpec('toolName', 'str'),
                  FieldSpec('content', 'str', required=True),
                  FieldSpec('status', 'str'),
                  FieldSpec('phase', 'str'),
                  FieldSpec('roundNum', 'int'),
                  FieldSpec('role', 'str'),
                  FieldSpec('callId', 'str'),
                  FieldSpec('argsBrief', 'str'),
                  FieldSpec('callStatus', 'str'),
                  FieldSpec('callElapsed', 'number'),
                  FieldSpec('preview', 'str'),
                  FieldSpec('previewTruncated', 'bool'),
                  FieldSpec('previewFullChars', 'int'),
                  FieldSpec('error', 'str'),
                  FieldSpec('errorTruncated', 'bool'),
                  FieldSpec('errorFullChars', 'int'),
              )),
    # ───────────────────────── autopilot ─────────────────────────
    EventSpec(EventType.AUTOPILOT_VU_START, _C.AUTOPILOT,
              'Autopilot kicked in — create the simulated-user bubble eagerly '
              '(in-memory only; not persisted until autopilot_vu_done).',
              fields={'vuMsgId': 'stable id for the VU message bubble'},
              schema=(
                  FieldSpec('vuMsgId', 'str', required=True),
              )),
    EventSpec(EventType.AUTOPILOT_VU_EVENT, _C.AUTOPILOT,
              'Autopilot value-unit progress event.',
              fields={'vuMsgId': 'owning VU bubble id',
                      'inner': 'the wrapped forward event dict '
                               '(delta/phase/tool_* /interaction)'},
              schema=(
                  FieldSpec('vuMsgId', 'str', required=True),
                  FieldSpec('inner', 'dict', required=True),
              )),
    EventSpec(EventType.AUTOPILOT_VU_DONE, _C.AUTOPILOT,
              'Autopilot value-unit completed.',
              fields={'vuMsgId': 'owning VU bubble id',
                      'vuMessage': 'persisted VU message row'},
              schema=(
                  FieldSpec('vuMsgId', 'str', required=True),
                  FieldSpec('vuMessage', 'dict', required=True),
              )),
    EventSpec(EventType.AUTOPILOT_VU_CANCEL, _C.AUTOPILOT,
              'Autopilot value-unit cancelled.',
              fields={'vuMsgId': 'owning VU bubble id'},
              schema=(
                  FieldSpec('vuMsgId', 'str', required=True),
              )),
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
                                'is absent on a manual stop'},
              schema=(
                  FieldSpec('runId', 'str', required=True),
                  FieldSpec('record', 'dict', required=True),
              )),
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
                                  'conversation one'},
              schema=(
                  FieldSpec('kind', 'str', required=True),
                  FieldSpec('root', 'str', required=True),
                  FieldSpec('peer', 'dict'),
                  FieldSpec('peers', 'list'),
                  FieldSpec('conflict', 'dict'),
              )),
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
                                  'the original (unframed) message text'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('count', 'int', required=True),
                  FieldSpec('previews', 'list', required=True),
              )),
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
                      'previews': 'list of {text} — the steer message text'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('count', 'int', required=True),
                  FieldSpec('previews', 'list', required=True),
              )),
    # ───────────────── artifact / scheduler / transport ─────────────────
    EventSpec(EventType.BACKGROUND_COMMAND_INJECT, _C.LIFECYCLE,
              'A run_command call that was handed off to a daemon worker for '
              'an operator steer has finished, and its authoritative result '
              'reached the model MID-TURN: the fast-path inbox twin was '
              'drained at a round boundary, injected as a user-role message '
              '(coalesced with any swarm/peer/steer items), and the LLM call '
              'confirmed consumption (deferred-confirm — this chip fires only '
              'after that confirmation). The durable message_queue row (the '
              'delivery authority) is deleted in the same step (de-dup by '
              'queueId); an abort before it re-dispatches the row as a fresh '
              'turn instead, so the result is delivered exactly once. '
              'Mirrors peer_inbox_inject; the settled-turn queue-lane case '
              'renders as a normal queued-turn user message instead.',
              fields={'roundNum': 'round number the completion was injected before',
                      'count': 'number of background-command completions injected this round',
                      'previews': 'list of {commandId, text} — the detached '
                                  'command id + its result payload'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('count', 'int', required=True),
                  FieldSpec('previews', 'list', required=True),
              )),
    # ───────────────── artifact / scheduler / transport ─────────────────
    EventSpec(EventType.ARTIFACT, _C.ARTIFACT,
              'An artifact (document/canvas) was created or updated.',
              fields={'id': 'artifact id',
                      'conv_id': 'owning conversation id',
                      'task_id': 'owning task id',
                      'msg_id': 'owning message id',
                      'source': 'write_file|inline_fence|inline_doc',
                      'source_ref': 'producer-specific source reference',
                      'format': 'markdown|html|svg',
                      'title': 'artifact title',
                      'size_bytes': 'payload size in bytes',
                      'version': 'artifact version counter',
                      'parent_id': 'parent artifact id (empty when root)',
                      'pinned': 'true when pinned',
                      'created_at': 'epoch ms creation stamp',
                      'url': '/api/artifacts/<id>/raw fetch URL'},
              schema=(
                  FieldSpec('id', 'str', required=True),
                  FieldSpec('conv_id', 'str', required=True),
                  FieldSpec('task_id', 'str', required=True),
                  FieldSpec('msg_id', 'str', required=True),
                  FieldSpec('source', 'str', required=True),
                  FieldSpec('source_ref', 'dict', required=True),
                  FieldSpec('format', 'str', required=True),
                  FieldSpec('title', 'str', required=True),
                  FieldSpec('size_bytes', 'int', required=True),
                  FieldSpec('version', 'int', required=True),
                  FieldSpec('parent_id', 'str', required=True),
                  FieldSpec('pinned', 'bool', required=True),
                  FieldSpec('created_at', 'int', required=True),
                  FieldSpec('url', 'str', required=True),
              )),
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
                      'conditionCommand': '(started) command gating code-tier '
                                          'decisions',
                      'background': '(started) true — polls run in background',
                      'nextPollTs': 'epoch-ms of the next scheduled poll'},
              schema=(
                  FieldSpec('roundNum', 'int', required=True),
                  FieldSpec('toolCallId', 'str', required=True),
                  FieldSpec('timerId', 'str', required=True),
                  FieldSpec('pollNum', 'int', required=True),
                  FieldSpec('pollId', 'str'),
                  FieldSpec('decision', 'str', required=True),
                  FieldSpec('reason', 'str', required=True),
                  FieldSpec('conditionKind', 'str', required=True),
                  FieldSpec('rawContent', 'str'),
                  FieldSpec('tokensUsed', 'int'),
                  FieldSpec('checkInstruction', 'str', required=True),
                  FieldSpec('checkCommand', 'str', required=True),
                  FieldSpec('cmdOutput', 'str'),
                  FieldSpec('parseError', 'bool'),
                  FieldSpec('model', 'str'),
                  FieldSpec('toolTrace', 'list'),
                  FieldSpec('pollInterval', 'int', required=True),
                  FieldSpec('maxPolls', 'int', required=True),
                  FieldSpec('conditionCommand', 'str', required=True),
                  FieldSpec('background', 'bool', required=True),
                  FieldSpec('nextPollTs', 'int', required=True),
              )),
    EventSpec(EventType.SSE_TIMEOUT, _C.TRANSPORT,
              'Server signalled the stream idle-timed-out; client may reconnect.',
              fields={},
              # Registry-only (client-side construct; no backend emitter).
              schema=()),
    EventSpec(EventType.PING, _C.TRANSPORT,
              'Keepalive frame on the push WebSocket (ignore).',
              fields={'channel': 'push channel (system)'},
              schema=(
                  FieldSpec('channel', 'str'),
              )),
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
                      'model': '(optional) physical attempt model id',
                      'providerId': '(optional) physical attempt provider id',
                      'dispatchMode': '(optional) strict_model|pool_rescue'}),
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
    PhaseSpec(Phase.EXECUTOR_QUEUED, (_CHAT,),
              'The accepted task is waiting in the bounded, host-local FIFO '
              'for a physical root-agent worker. This is server scheduling, '
              'not provider or API quota.',
              fields={'detail': 'English fallback with current queue evidence',
                      'detailKey': 'i18n key',
                      'detailArgs': 'position, queued, active, capacity and '
                                    'waitSeconds',
                      'queuePosition': 'one-based position in the FIFO',
                      'queued': 'total queued root tasks',
                      'active': 'root tasks holding physical worker slots',
                      'capacity': 'configured physical root-worker slots',
                      'waitSeconds': 'elapsed residence in this FIFO'}),
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
                      'topic': '(longform) the report topic',
                      'action': '(research action runtime) the workspace '
                                'action being executed'}),
    PhaseSpec(Phase.AGENT, ('research',),
              'The research action runtime bound its tool epoch and the '
              'agent loop is executing against the workspace.',
              fields={'action': 'the workspace action being executed',
                      'toolCount': 'executable tool schemas in the bound '
                                   'epoch'}),
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


# ── Field-level wire contract enforcement ─────────────────────────
# The registry used to document payload fields as PROSE (EventSpec.fields),
# which let a field ride the wire undeclared (the rawToolTokens incident:
# emitted, consumed by the frontend badge, absent from the contract —
# discovered by a user, not by a gate). EventSpec.schema closes that gap for
# migrated events: build_event validates EVERY construction, and delivery
# seams call check_event() on the final (post-mutation) frame.
#
# Enforcement modes (TOFU_EVENT_SCHEMA):
#   strict — raise EventContractError (default under pytest: drift fails CI
#            at the emitting line, not at a confused frontend)
#   warn   — log once per distinct violation signature (production default:
#            a contract nit must never fail a user turn)
#   off    — no validation (emergency escape hatch)


class EventContractError(ValueError):
    """A wire event violated its declared field-level contract."""


_FIELD_KIND_SCALARS: dict[str, type] = {
    'str': str,
    'bool': bool,
    'dict': dict,
    'list': list,
}

#: The closed kind vocabulary (``None`` plus the entries above plus the
#: numeric kinds). Exported for the conformance test / generators.
FIELD_KINDS: frozenset[str] = frozenset(
    tuple(_FIELD_KIND_SCALARS) + ('int', 'number', 'None'))


def _field_value_matches(value: Any, kind: str) -> bool:
    """True if *value* satisfies one ``|``-separated alternative of *kind*.

    ``bool`` is deliberately NOT an ``int``/``number`` (Python's isinstance
    disagrees with the wire's JSON semantics), and unknown alternatives match
    NOTHING — a typo'd kind fails closed, and the conformance test rejects it
    at registry load.
    """
    for alternative in kind.split('|'):
        alternative = alternative.strip()
        if alternative == 'None':
            if value is None:
                return True
        elif alternative == 'int':
            if isinstance(value, int) and not isinstance(value, bool):
                return True
        elif alternative == 'number':
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
        elif alternative in _FIELD_KIND_SCALARS:
            if isinstance(value, _FIELD_KIND_SCALARS[alternative]):
                return True
    return False


def _schema_violations(
        event: dict, schema: tuple[FieldSpec, ...], *,
        undeclared_hint: str) -> tuple[str, ...]:
    """Field-level check shared by stream events and push frames."""
    declared = {f.name: f for f in schema}
    violations: list[str] = []
    for name, value in event.items():
        if name == 'type':
            continue
        field_spec = declared.get(name)
        if field_spec is None:
            violations.append(
                f'undeclared field {name!r} ({undeclared_hint})')
        elif not _field_value_matches(value, field_spec.kind):
            violations.append(
                f'field {name!r} expects {field_spec.kind}, got '
                f'{type(value).__name__}')
    for field_spec in schema:
        if field_spec.required and field_spec.name not in event:
            violations.append(f'missing required field {field_spec.name!r}')
    return tuple(violations)


def validate_event(event: Any) -> tuple[str, ...]:
    """Field-level contract check; return the violations (empty = conforming).

    Pure and side-effect free — callers decide enforcement.  Events whose
    type is unregistered or has no schema return ``()`` (the wire stays
    forward-compatible; registration is enforced by the drift test, field
    contracts migrate event-by-event).
    """
    if not isinstance(event, dict):
        return (f'event is {type(event).__name__}, not a dict',)
    type_ = event.get('type')
    spec = _BY_TYPE.get(type_) if isinstance(type_, str) else None
    if spec is None or spec.schema is None:
        return ()
    return _schema_violations(
        event, spec.schema,
        undeclared_hint='add it to the EventSpec schema + fields prose in '
                        'lib/agent_core/events.py')


_violation_lock = threading.Lock()
_violation_warned: set[tuple[str, tuple[str, ...]]] = set()
# Test/conformance hook: listeners observe EVERY violation (any mode) without
# changing enforcement. Install with add_event_violation_listener.
_violation_listeners: list[Callable[[dict, tuple[str, ...]], None]] = []


def add_event_violation_listener(
        listener: Callable[[dict, tuple[str, ...]], None]) -> None:
    """Observe every contract violation (conformance harnesses)."""
    with _violation_lock:
        _violation_listeners.append(listener)


def remove_event_violation_listener(
        listener: Callable[[dict, tuple[str, ...]], None]) -> None:
    with _violation_lock:
        try:
            _violation_listeners.remove(listener)
        except ValueError:
            pass


def _schema_enforcement() -> str:
    raw = (os.environ.get('TOFU_EVENT_SCHEMA') or '').strip().lower()
    if raw in ('strict', 'warn', 'off'):
        return raw
    # Under pytest the default is strict: contract drift must fail the run at
    # the emitting line. In production it is warn: a schema nit must never
    # fail a user turn.
    return 'strict' if (os.environ.get('PYTEST_CURRENT_TEST')
                        or 'pytest' in sys.modules) else 'warn'


def _handle_violations(event: dict, violations: tuple[str, ...]) -> None:
    with _violation_lock:
        listeners = tuple(_violation_listeners)
    for listener in listeners:
        try:
            listener(event, violations)
        except Exception:  # a broken observer must never mask the violation
            logger.debug('[events] violation listener failed', exc_info=True)
    mode = _schema_enforcement()
    if mode == 'off':
        return
    detail = '; '.join(violations)
    if mode == 'strict':
        raise EventContractError(
            f"event {event.get('type')!r} violates its wire contract: "
            f'{detail}')
    signature = (str(event.get('type')), violations)
    with _violation_lock:
        if signature in _violation_warned:
            return
        _violation_warned.add(signature)
    logger.warning('[events] wire contract violation on %r: %s',
                   event.get('type'), detail)


def check_event(event: Any) -> None:
    """Validate a FINAL, fully-stamped frame at a delivery seam.

    Construction-time validation in :func:`build_event` sees only kwargs;
    emitters legitimately stamp conditional fields by mutation afterwards
    (the pipeline adds ``status`` / ``rejection`` / compaction fields to
    ``tool_complete`` after building it). Delivery seams — the manager's
    ``append_event`` and any private push fan-out — call this so the shape
    that actually reaches the wire is what gets checked. Never raises in
    production (warn mode); under pytest the strict default applies.
    """
    violations = validate_event(event)
    if violations and isinstance(event, dict):
        _handle_violations(event, violations)


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

    Events with a field-level :class:`FieldSpec` schema (see
    :func:`validate_event`) are validated on EVERY construction: an
    undeclared field, a missing required field, or a type mismatch raises
    :class:`EventContractError` under pytest (``TOFU_EVENT_SCHEMA=strict``)
    and logs a warning in production. Delivery seams re-check the final
    post-mutation frame via :func:`check_event`.
    """
    if type_ not in _BY_TYPE:
        logger.debug('[events] build_event for unregistered type=%r '
                     '(add an EventSpec to lib/agent_core/events.py)', type_)
    if type_ in _CLOCK_STAMPED_TYPES and 'emittedAt' not in fields:
        fields['emittedAt'] = now_ms()
    event = {'type': type_, **fields}
    spec = _BY_TYPE.get(type_)
    if spec is not None and spec.schema is not None:
        violations = validate_event(event)
        if violations:
            _handle_violations(event, violations)
    return event


# ── Push-channel frames — owner-scoped wake hints outside the task stream ──
#
# Push frames ride the WebSocket push hub (``lib/agent_core/push.py``), NOT
# the chat task stream: they are best-effort, owner-scoped wake hints and
# receipts — never durable authority, and every consumer must keep a bounded
# reconciliation path for a lost frame.  ``PushFrameSpec.schema`` +
# :func:`build_push_frame` give them exactly the field-level contract
# discipline the stream has: one machine-readable authority, one construction
# gate, one generated TypeScript mirror.

#: Bump only on a breaking change to an EXISTING push frame's shape.
PUSH_FRAME_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class PushFrameSpec:
    """Describes one push-channel frame ``type``.

    Parameters
    ----------
    type:
        The wire ``type`` string.
    channel:
        The push-hub channel the frame is published on.
    task:
        Task-id routing semantics — a literal (``'codex-reset'``), a
        sentinel (``'__folders__'``), or a description of the dynamic key
        (``'conversation-id'``).
    purpose:
        One-line human description.
    fields:
        Prose map of payload field name → short description, kept in exact
        key sync with ``schema`` by the conformance suite.
    schema:
        Machine-readable :class:`FieldSpec` tuple — the contract
        :func:`build_push_frame` validates every construction against.
    since:
        Contract version in which the frame was introduced.
    """

    type: str
    channel: str
    task: str
    purpose: str
    fields: dict[str, str] = field(default_factory=dict)
    schema: tuple[FieldSpec, ...] | None = None
    since: int = 1


PUSH_FRAME_SPECS: tuple[PushFrameSpec, ...] = (
    PushFrameSpec(
        type='conv_changed',
        channel='notify',
        task='conversation-id',
        purpose='Wake hint: a conversation catalog entry or transcript '
                'changed; clients reconcile exclusively through Conversation '
                'Sync v3, so a lost/duplicated hint never loses data.',
        fields={
            'convId': 'Changed conversation id.',
            'userId': 'Owner user id; the browser frame-ownership check is '
                      'mandatory.',
            'rev': 'Positive transcript-revision hint; lets the browser '
                   'suppress a catalog read it has already reached.',
        },
        schema=(
            FieldSpec('convId', 'str', required=True),
            FieldSpec('userId', 'int', required=True),
            FieldSpec('rev', 'int'),
        ),
    ),
    PushFrameSpec(
        type='conv_deleted',
        channel='notify',
        task='conversation-id',
        purpose='Wake hint: a conversation was deleted; clients dispose '
                'local state without a fetch.',
        fields={
            'convId': 'Deleted conversation id.',
            'userId': 'Owner user id; the browser frame-ownership check is '
                      'mandatory.',
        },
        schema=(
            FieldSpec('convId', 'str', required=True),
            FieldSpec('userId', 'int', required=True),
        ),
    ),
    PushFrameSpec(
        type='folders_changed',
        channel='notify',
        task='__folders__',
        purpose='Folder tree changed (create/rename/delete); clients '
                'refresh the tree in place.  With deletedFolderId every '
                'device unassigns local conversations off the removed '
                'folder, not just the one that clicked delete.',
        fields={
            'userId': 'Owner user id; the browser frame-ownership check is '
                      'mandatory.',
            'deletedFolderId': 'Present only when a folder was deleted.',
        },
        schema=(
            FieldSpec('userId', 'int', required=True),
            FieldSpec('deletedFolderId', 'str'),
        ),
    ),
    PushFrameSpec(
        type='codex.reset_offer.updated',
        channel='oauth',
        task='codex-reset',
        purpose='Passive completion receipt: the bounded earned-reset '
                'daemon settled.  reset_offer is byte-compatible with the '
                'projection on GET /api/v1/oauth/status (length-bounded, no '
                'token or raw account id).  Never durable authority: '
                'consumers keep a bounded HTTP reconciliation path and must '
                'never redeem a credit from the frame.',
        fields={
            'provider': "Always 'codex'.",
            'reset_offer': 'Account-scoped reset-offer projection mirroring '
                           'the OAuth status endpoint.',
        },
        schema=(
            FieldSpec('provider', 'str', required=True),
            FieldSpec('reset_offer', 'dict', required=True),
        ),
    ),
)

_PUSH_BY_TYPE: dict[str, PushFrameSpec] = {
    spec.type: spec for spec in PUSH_FRAME_SPECS}


def all_push_frame_specs() -> tuple[PushFrameSpec, ...]:
    """Every declared push-channel frame (the generated-mirror authority)."""
    return PUSH_FRAME_SPECS


def get_push_frame_spec(type_: str) -> PushFrameSpec | None:
    """The :class:`PushFrameSpec` for *type_*, or None when undeclared."""
    return _PUSH_BY_TYPE.get(type_)


def validate_push_frame(frame: Any) -> tuple[str, ...]:
    """Field-level contract check for a push frame (empty = conforming).

    Undeclared types return ``()`` — the hub also carries self-contained
    producer↔consumer pairs outside this shared vocabulary (cookie-capture,
    project hints, timer ticks); they migrate frame-by-frame like the
    stream did.
    """
    if not isinstance(frame, dict):
        return (f'frame is {type(frame).__name__}, not a dict',)
    type_ = frame.get('type')
    spec = _PUSH_BY_TYPE.get(type_) if isinstance(type_, str) else None
    if spec is None or spec.schema is None:
        return ()
    return _schema_violations(
        frame, spec.schema,
        undeclared_hint='add it to the PushFrameSpec schema + fields prose '
                        'in lib/agent_core/events.py')


def build_push_frame(type_: str, **fields: Any) -> dict[str, Any]:
    """Construct a push-channel frame ``{'type': type_, **fields}``.

    The push-side twin of :func:`build_event`: same byte-identity guarantee
    (keyword order is preserved), same contract gate — an undeclared field,
    a missing required field, or a type mismatch raises
    :class:`EventContractError` under pytest (``TOFU_EVENT_SCHEMA=strict``)
    and logs a warning in production.  No ``emittedAt`` stamp: these frames
    are wake hints, not transport-duration samples.
    """
    if type_ not in _PUSH_BY_TYPE:
        logger.debug('[events] build_push_frame for undeclared type=%r '
                     '(add a PushFrameSpec to lib/agent_core/events.py)',
                     type_)
    frame = {'type': type_, **fields}
    spec = _PUSH_BY_TYPE.get(type_)
    if spec is not None and spec.schema is not None:
        violations = validate_push_frame(frame)
        if violations:
            _handle_violations(frame, violations)
    return frame


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
            'schema': ([{'name': f.name, 'kind': f.kind,
                         'required': f.required} for f in s.schema]
                       if s.schema is not None else None),
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
    'FIELD_KINDS',
    'EventCategory',
    'EventContractError',
    'EventSpec',
    'EventType',
    'FieldSpec',
    'Phase',
    'PhaseSpec',
    'PushFrameSpec',
    'PUSH_FRAME_CONTRACT_VERSION',
    'PUSH_FRAME_SPECS',
    'TRANSPORT_TYPES',
    'add_event_violation_listener',
    'build_event',
    'build_phase',
    'build_push_frame',
    'check_event',
    'emit',
    'emit_phase',
    'remove_event_violation_listener',
    'validate_event',
    'validate_push_frame',
    'all_event_specs',
    'all_phase_specs',
    'all_push_frame_specs',
    'event_types',
    'phase_values',
    'get_event_spec',
    'get_phase_spec',
    'get_push_frame_spec',
    'is_registered',
    'is_registered_phase',
    'terminal_types',
    'interaction_types',
    'to_capabilities_dict',
]
