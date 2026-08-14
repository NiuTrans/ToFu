"""lib/swarm/agent.py — SubAgent: isolated execution environment for a single worker.

Each SubAgent is a self-contained unit that:
  1. Gets a SubTaskSpec (objective, context, role, tools)
  2. Runs an LLM loop with tool access
  3. Returns a SubAgentResult

Key design principles:
  • Isolation: each agent has its own message history
  • Unbounded by default: runs until task completion (timeout/abort as safety nets)
  • Observable: structured SwarmEvent events emitted for UI progress tracking
  • Artifact-aware: can store/read shared artifacts via local handling
  • Early-stop: detects final answers and stops when done
  • Graceful degradation: timeout/abort returns partial results, not empty
"""

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.agent_core.events import Phase
from lib.agent_core.store import get_conversation_store
from lib.llm import build_body as _default_build_body
from lib.llm_dispatch import dispatch_stream as _default_dispatch_stream
from lib.llm_dispatch.retry_i18n import retry_phase_fields
from lib.log import audit_log, get_logger
from lib.project_mod import format_tool_args_brief
from lib.protocols import BodyBuilder
from lib.swarm.liveness import ProgressBeacon, thread_progress_sink
from lib.swarm.protocol import (
    ArtifactStore,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
    SwarmEvent,
    SwarmEventType,
)
from lib.swarm.registry import (
    get_role_model_hint,
    get_role_system_suffix,
    resolve_model_for_tier,
    scope_tools_for_role,
)
from lib.swarm.tools import ARTIFACT_TOOLS
from lib.tool_input_repair import ingest_tool_call

logger = get_logger(__name__)

# Request-inspector rows bypass TaskRuntime (intentionally: they belong to a
# sub-agent child stream, not the parent SSE stream), so they do not inherit
# TaskRuntime's monotonic sequence allocator.  Serialise the small
# read-tail+append critical section in-process; the durable tail plus the event
# writer's pending shadow make the sequence survive both server restarts and
# write-behind lag.
_REQUEST_SNAPSHOT_SEQ_LOCK = threading.Lock()


def _next_request_snapshot_event_id(inspector_id: str,
                                    round_num: int) -> int:
    """Return an unused inspector event id at/after this logical round.

    ``round_num - 1`` preserves the historical 0-based id for a brand-new
    agent.  Existing durable/pending rows win on resume, so a checkpoint whose
    old code reset its round counter cannot collide and silently lose the new
    snapshot.

    The caller must hold ``_REQUEST_SNAPSHOT_SEQ_LOCK`` through the matching
    ``append_persistent_event`` call; otherwise two freshly constructed agent
    objects could both observe the same tail.
    """
    return get_conversation_store().next_task_event_id(
        inspector_id, floor=max(0, int(round_num) - 1))


def _emit_request_snapshot(agent, round_num: int) -> str:
    """Persist a Request Inspector snapshot for ONE sub-agent LLM round.

    Epic pt_e3dc7198e7e34bb1 (P4): sub-agent LLM calls bypass ``run_task``
    (no ``messages_snapshot``) and their proxy tasks set ``_suppressEvents``
    — so the parent stream must stay clean. We persist DIRECTLY to the
    durable ``task_events`` log under the agent's own inspector id
    ``{parent_task_id}#agent:{agent_id}``: no SSE fan-out, no parent-round
    pollution, the suppression contract untouched. The Request Inspector
    folds ``task_events`` server-side, so the row is retrievable per task
    id without any live emission.

    ``kind='request'`` + the frozen params schema
    (docs/DEBUG_PANEL_REDESIGN.md §3.3) + ``turn='swarm-agent'``.
    Returns the inspector id ('' on failure — NEVER raises:
    observability must not break the agent loop).
    """
    parent_id = (agent.parent_task or {}).get('id', '') or ''
    if not parent_id:
        return ''
    inspector_id = f'{parent_id}#agent:{agent.agent_id}'
    try:
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.event_log import append_persistent_event
        from lib.tasks_pkg.manager import _strip_base64_for_snapshot
        from lib.tasks_pkg.wire_messages import apply_wire_sanitize
        _wire = apply_wire_sanitize(
            [dict(m) for m in agent.messages],
            conv_id=(agent.parent_task or {}).get('convId', '') or '',
            provider_id=(agent.parent_task or {}).get('provider_id') or '')
        snap = _strip_base64_for_snapshot(_wire)
        role = getattr(agent.spec, 'role', '') or ''
        event = build_event(
                EventType.MESSAGES_SNAPSHOT,
                kind='request',
                model=agent.model,
                turn='swarm-agent',
                agentId=agent.agent_id,
                agentRole=role,
                params={
                    'maxTokens': 64000,
                    'temperature': 1.0,
                    'thinkingEnabled': agent.thinking_enabled,
                    'thinkingDepth': None,
                    'preset': '',
                    'responseFormat': None,
                    'stream': True,
                },
                roundNum=round_num,
                label=f'[{role}] Round {round_num} 请求前 · {len(snap)}条',
                messages=snap,
            )
        # This child stream has no TaskRuntime sequence owner.  Keep the tail
        # lookup and enqueue together so a resumed/duplicated in-process
        # worker cannot mint the same id.  The database tail makes this survive
        # process restart; pending_event_rows covers the asynchronous writer.
        with _REQUEST_SNAPSHOT_SEQ_LOCK:
            event_id = _next_request_snapshot_event_id(
                inspector_id, round_num)
            append_persistent_event(inspector_id, event_id, event)
        return inspector_id
    except Exception as e:
        logger.debug('[Agent:%s] request-inspector snapshot failed '
                     '(non-fatal): %s', agent.agent_id, e)
        return ''


def _build_dispatch_retry_phase(attempt: int, reason: str,
                                status_code: int, model: str):
    """Compute (legacy_detail, meta) for a dispatch-retry 'retrying' phase.

    Module-level seam (unit-testable) — the closure ``_on_dispatch_retry``
    below is a thin adapter over this. The legacy English ``detail`` string
    is computed EXACTLY as before (byte-parity for headless clients); the
    structured ``detailKey``/``detailArgs`` (+ typed ``reasonKey`` for known
    dispatcher reason tokens) come from the SHARED helper
    (lib/llm_dispatch/retry_i18n.retry_phase_fields) so this emitter can
    never drift from the main chat bubble's mapping
    (pt_18ebee9c9ea64cf3 — the "Retrying… Endpoint unreachable" raw-token
    leak family).
    """
    _r = reason or 'Retrying'
    if status_code == 429 and 'rate' not in _r.lower():
        _r = f'{_r} (rate-limited)'
    legacy = f'{_r}… (attempt {attempt})' if attempt else f'{_r}…'
    fields = retry_phase_fields(model=model, attempt=attempt, reason=reason,
                                status_code=status_code,
                                legacy_detail=legacy)
    meta = {'attempt': attempt, 'status_code': status_code,
            'detailKey': fields['detailKey'],
            'detailArgs': fields['detailArgs']}
    return fields['detail'], meta

# ─────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────

# Default truncation limit for tool results (chars)
DEFAULT_TOOL_RESULT_MAX_CHARS = 30_000

#: Chars of a tool result carried on the LIVE ``agent_tool_call`` SSE frame.
#: The swarm panel is a DEBUGGING surface, so this must be wide enough to read
#: a real tool return (a fetch_url staging note, a grep block) end to end — the
#: previous 300 cut mid-path through ``/mnt/your-fs`` and made the panel
#: misreport what the sub-agent actually saw. The full text is ALSO persisted
#: onto ``tool_log`` (see ``_execute_one_tool_call``) so a reloaded panel keeps
#: it; this bound only governs the live wire frame.
_SSE_TOOL_PREVIEW_CHARS = 4000

# Max parallel tool calls per round
MAX_PARALLEL_TOOLS = int(os.environ.get('TOOL_MAX_PARALLEL_WORKERS', '16'))

# File-edit tools whose touched path is recorded on this sub-agent's presence
# peer (→ cross-peer overlap detection). Maps tool name → the action label
# shown in the presence strip. Read-only tools / run_command are excluded
# (run_command's edits aren't reliably attributable from args alone).
_PRESENCE_EDIT_ACTIONS = {
    'write_file': 'written',
    'edit_file': 'edited',
    'apply_diff': 'patched',
    'apply_diffs': 'patched',
    'insert_content': 'inserted',
    'insert_contents': 'inserted',
}

# Patterns that suggest the agent has reached a final answer
# (used for early-stop detection on content without tool calls)
_DONE_PATTERNS = re.compile(
    r'(?:^|\n)\s*(?:'
    r'(?:in\s+(?:summary|conclusion))'
    r'|(?:final\s+answer)'
    r'|(?:to\s+summarize)'
    r'|(?:here\s+(?:is|are)\s+(?:the|my)\s+(?:final|complete))'
    r'|(?:task\s+(?:complete|done|finished))'
    r')',
    re.IGNORECASE,
)


class SubAgent:
    """An isolated worker agent that executes a single subtask.

    Usage:
        spec = SubTaskSpec(role='researcher', objective='Find ...')
        agent = SubAgent(spec, parent_task=task, all_tools=tool_list)
        result = agent.run()  # blocking
    """

    def __init__(self, spec: SubTaskSpec, *,
                 parent_task: dict,
                 all_tools: list,
                 system_prompt_base: str = '',
                 model: str = '',
                 thinking_enabled: bool = True,
                 on_event: Callable | None = None,
                 abort_check: Callable | None = None,
                 project_path: str = '',
                 artifact_store: ArtifactStore | None = None,
                 tool_result_max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS,
                 build_body_fn: BodyBuilder | None = None,
                 dispatch_stream_fn: Callable | None = None,
                 stream_sink: Callable | None = None):
        self.spec = spec
        self.parent_task = parent_task
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self.model = self._resolve_model(spec, model)
        self.thinking_enabled = thinking_enabled
        self.on_event = on_event
        # Optional per-token sink: ``stream_sink(kind, chunk, **meta)`` where
        # kind is 'content' | 'thinking' | 'phase'. 'content'/'thinking' carry
        # an output chunk; 'phase' carries a transient status detail (+ a
        # ``phase=`` kwarg) so the engine can surface "waiting for model…" /
        # "retrying…" on the live bubble while dispatch is in flight. Lets a
        # caller (the orchestration engine) stream this sub-agent's output live
        # into a chat bubble, identical to a first-class agent turn. No-op when
        # unset (tests / swarm use).
        self.stream_sink = stream_sink
        self.abort_check = abort_check or (lambda: False)
        self.project_path = project_path
        self.artifact_store = artifact_store  # shared across agents
        self.tool_result_max_chars = tool_result_max_chars
        # DI: allow injecting custom build_body / dispatch_stream (e.g. mocks in tests)
        self._build_body = build_body_fn or _default_build_body
        self._dispatch_stream = dispatch_stream_fn or _default_dispatch_stream

        # Build scoped tool list
        self.tools = scope_tools_for_role(spec.role, all_tools)
        # Inject artifact tools if an artifact store is available
        if self.artifact_store is not None:
            self._inject_artifact_tools()

        # Result tracking
        self.result = SubAgentResult()

        # Internal message history
        self.messages = self._build_initial_messages(system_prompt_base)

        # Cleanup state
        self._started = False
        self._cleaned_up = False
        # Presence: throttle anchor for the streaming heartbeat (one bump per
        # ~5s of token flow). 0.0 → the first chunk beats immediately.
        self._last_presence_beat = 0.0
        # Set by ``_run_loop`` when the caller deliberately supplied an agent
        # wall-clock ceiling.  Tool dispatch reads the same deadline so a
        # process blocked inside run_command can observe the ceiling instead
        # of waiting until the next (unreachable) before_round hook.
        self._run_deadline_monotonic: float | None = None

        # Number of rounds already completed by a durable checkpoint.  The
        # loop chassis deliberately numbers each invocation from zero, so the
        # master restores this offset when rehydrating an interrupted agent.
        # Without it, progress, inspector snapshots, and the next checkpoint
        # all time-travelled back to Round 1 after every server restart.
        self._round_offset = 0
        # Independent semantic breaker for capability-boundary violations.
        # Alternating one legal call with one hallucinated privileged call is
        # not caught by the identical-tool fingerprint, yet it is still a
        # non-recovering loop. Two explicit rejections are enough evidence to
        # stop and write an honest capability-limited result.
        self._tool_authority_rejections = 0
        self._tool_authority_lock = threading.Lock()

        # Durable-resume key. Set by the master orchestrator's _make_agent
        # factory (like ``output_file``). When present, the agent checkpoints
        # its full ``messages`` array to the DB at each round boundary so a
        # server restart can rehydrate and resume it mid-conversation. Empty
        # in unit tests / standalone use → all checkpoint writes are no-ops.
        self.swarm_key = ''

        #: Shared liveness record (lib/swarm/liveness.py), injected by the
        #: master's ``_make_agent``. Every progress point below touches it, so
        #: "is this agent still working?" is answered by OUTPUT rather than by
        #: elapsed time. A private fallback keeps standalone/unit use working.
        self.progress_beacon = ProgressBeacon()

        # ── Debug: log agent initialization details ──
        tool_names = []
        for t in self.tools:
            if isinstance(t, dict):
                tool_names.append(t.get('function', {}).get('name', '?'))
        logger.debug(
            '[%s] Initialized: model=%s, thinking=%s, '
            'tools=[%s%s] (%d total), depends_on=%s, objective="%s"',
            self.agent_id, self.model, self.thinking_enabled,
            ', '.join(tool_names[:10]), '...' if len(tool_names) > 10 else '',
            len(tool_names), list(spec.depends_on or []), spec.objective[:100]
        )

    # ─────────────────────────────────────────────────
    #  Model resolution
    # ─────────────────────────────────────────────────

    def _resolve_model(self, spec: SubTaskSpec, default_model: str) -> str:
        """Resolve override → evidence-backed role tier → parent model."""
        if spec.model_override:
            return spec.model_override
        tier = get_role_model_hint(spec.role)
        provider_id = ((self.parent_task or {}).get('config') or {}).get(
            '_pinned_provider_id', '')
        return resolve_model_for_tier(
            tier, default_model, role=spec.role, provider_id=provider_id)

    # ─────────────────────────────────────────────────
    #  Artifact tool injection
    # ─────────────────────────────────────────────────

    def _inject_artifact_tools(self):
        """Add artifact tools to the agent's tool list if not already present."""
        existing_names = set()
        for t in self.tools:
            if isinstance(t, dict):
                name = t.get('function', {}).get('name', '')
                existing_names.add(name)
        for at in ARTIFACT_TOOLS:
            name = at.get('function', {}).get('name', '')
            if name not in existing_names:
                self.tools.append(at)

    # ─────────────────────────────────────────────────
    #  Message construction
    # ─────────────────────────────────────────────────

    def _build_initial_messages(self, system_prompt_base: str) -> list:
        """Construct the initial messages for this agent."""
        role_suffix = get_role_system_suffix(self.spec.role)

        system_content = self._strip_parent_prompt(system_prompt_base)
        if role_suffix:
            system_content += f'\n\n{role_suffix}'

        # Agent identity and constraints
        system_content += (
            f'\n\nYou are sub-agent [{self.agent_id}] working on a specific subtask. '
            f'Focus exclusively on your objective. Do NOT attempt tasks outside your scope. '
            f'When your task is complete, provide a clear final answer. '
            f'Do NOT call tools after you have gathered sufficient information — '
            f'write your final answer directly.'
        )

        # The role prose is static, while security/capability gates can remove
        # tools from one concrete request.  State the effective inventory in
        # the agent's own prompt so a role description can never be mistaken
        # for an authorization grant (the browser role previously promised
        # browser_execute_js even when that schema was absent, after which the
        # model tried to emulate it with an unbounded run_command).
        available_tools = sorted(self._known_tool_names())
        system_content += (
            '\n\n## Tool authority for this run\n'
            'You may call ONLY tools present in the tool schemas supplied to '
            'this run. Never emulate a missing browser or privileged tool via '
            'run_command. If a required tool is absent, report that limitation '
            'plainly in your final answer.\n'
            f'Available tools: {", ".join(available_tools) or "(none)"}.'
        )

        # Artifact store instructions
        if self.artifact_store is not None:
            existing_artifacts = self.artifact_store.list_keys()
            system_content += (
                '\n\n## Shared Artifact Store\n'
                'You have access to a shared artifact store for inter-agent data sharing:\n'
                '  • store_artifact(key, content) — save data for other agents\n'
                '  • read_artifact(key) — read data saved by previous agents\n'
                '  • list_artifacts() — see all available artifacts\n'
                'Use this to share important findings or data that other agents might need.'
            )
            if existing_artifacts:
                system_content += (
                    f'\n\nAvailable artifacts from previous agents: {", ".join(existing_artifacts)}'
                )

        messages = [{'role': 'system', 'content': system_content}]

        # User message with objective + context
        user_parts = [f'## Your Task\n{self.spec.objective}']
        if self.spec.context:
            user_parts.append(f'\n## Context\n{self.spec.context}')
        messages.append({'role': 'user', 'content': '\n'.join(user_parts)})

        return messages

    def _strip_parent_prompt(self, prompt: str) -> str:
        """Remove parent-specific instructions that don't apply to sub-agents.

        The ``<parallel_execution>`` block teaches the master how to use
        spawn / await / get_agent_result and how to interpret
        ``<swarm-update>``.  Sub-agents have none of those tools (see
        ``SUB_AGENT_DENYLIST``) and never receive ``<swarm-update>``
        notifications, so leaking the master's prompt would just confuse
        them with rules they can't follow.
        """
        if not prompt:
            return ''
        kept = []
        skip_section = False
        for line in prompt.split('\n'):
            lower = line.lower().strip()
            if any(kw in lower for kw in [
                'spawn_agents', 'await_agents', 'get_agent_result',
                'parallel_execution', 'swarm-update', 'swarm mode',
            ]):
                skip_section = True
                continue
            if skip_section and line.strip() == '':
                skip_section = False
                continue
            if not skip_section:
                kept.append(line)
            if len('\n'.join(kept)) > 4000:
                break
        return '\n'.join(kept)

    # ─────────────────────────────────────────────────
    #  Event emission (structured SwarmEvent)
    # ─────────────────────────────────────────────────

    def _emit_event(self, event_type: str, text: str, **extra):
        """Emit a structured SwarmEvent to the parent.

        Creates a proper SwarmEvent and calls on_event with both the
        structured object (via to_dict()) and backward-compatible format
        (via to_legacy()).

        Args:
            event_type: One of SwarmEventType values or custom string.
            text: Human-readable description.
            **extra: Additional fields for SwarmEvent.metadata.
        """
        if not self.on_event:
            return

        try:
            evt = SwarmEvent(
                type=event_type,
                text=text,
                agent_id=self.spec.id,       # Use spec.id — consistent with scheduler callbacks
                role=self.spec.role,
                phase=extra.pop('phase', ''),
                status=extra.pop('status', ''),
                duration_s=extra.pop('duration_s', 0.0),
                tokens=extra.pop('tokens', 0),
                round_num=extra.pop('round_num', 0),
                metadata=extra,  # remaining kwargs go into metadata
            )
            # Emit legacy format for backward compatibility
            self.on_event(evt.to_legacy())
        except Exception as e:
            logger.warning('[SubAgent] Event emission error (non-fatal): %s', e, exc_info=True)

    # ─────────────────────────────────────────────────
    #  Execution
    def _emit_stream_phase(self, phase: str, detail: str, **meta):
        """Push a transient 'phase' status through the stream sink.

        Used to surface "waiting for the model…" / "retrying…" on the live
        chat bubble while this agent's dispatch is in flight (e.g. blocked on
        a rate-limited strict_model). The engine's stream_sink maps it to a
        ``step_phase`` event → the EndpointEventAdapter emits a wire ``phase``
        event (transient UI, per the retry-notification-phase-not-delta
        convention — NEVER a delta, so it can't pollute the assistant content).
        No-op when no stream_sink is wired (swarm / unit tests). Never raises.
        """
        if not self.stream_sink:
            return
        try:
            self.stream_sink('phase', detail or '', phase=phase, **meta)
        except TypeError:
            # An older 2-arg stream_sink (kind, chunk) — degrade gracefully:
            # the phase is best-effort UX, so just drop it rather than crash.
            logger.debug('[Agent:%s] stream_sink lacks phase support — '
                         'dropping phase=%s', self.agent_id, phase)
        except Exception as _se:
            logger.debug('[Agent:%s] stream_sink phase error (non-fatal): %s',
                         self.agent_id, _se)

    # ─────────────────────────────────────────────────
    #  Execution
    # ─────────────────────────────────────────────────

    def _presence_conv_id(self) -> str:
        return (self.parent_task or {}).get('convId') or ''

    def _presence_announce(self, phase: str = 'working') -> None:
        """Register this sub-agent as a live presence peer of the project root.

        Keyed by ``(parent convId, agent_id)`` so N concurrent sub-agents of
        ONE conversation are N distinct peers that group under the parent
        conversation (the composite key in lib/presence/registry.py). No-op
        when there's no project root or convId. Best-effort.
        """
        conv_id = self._presence_conv_id()
        if not (self.project_path and conv_id):
            return
        try:
            from lib.presence import announce as _announce
            _announce(
                self.project_path, conv_id,
                agent_id=self.agent_id,
                task_id=self.parent_task.get('id', ''),
                title=self.spec.role,
                objective=self.spec.objective or '',
                parent_title=(self.parent_task.get('config') or {}).get('convTitle') or '',
                phase=phase,
            )
        except Exception as _pe:
            logger.debug('[%s] presence announce failed: %s', self.agent_id, _pe)

    def _presence_heartbeat(self, phase: str = 'generating') -> None:
        conv_id = self._presence_conv_id()
        if not (self.project_path and conv_id):
            return
        try:
            from lib.presence import heartbeat as _heartbeat
            _heartbeat(self.project_path, conv_id, agent_id=self.agent_id, phase=phase)
        except Exception as _pe:
            logger.debug('[%s] presence heartbeat failed: %s', self.agent_id, _pe)

    def _presence_record_file(self, rel_path: str, action: str = 'edited') -> None:
        conv_id = self._presence_conv_id()
        if not (self.project_path and conv_id and rel_path):
            return
        try:
            from lib.presence import record_files as _record_files
            _record_files(self.project_path, conv_id,
                          [{'path': rel_path, 'action': action}],
                          agent_id=self.agent_id)
        except Exception as _pe:
            logger.debug('[%s] presence record_file failed: %s', self.agent_id, _pe)

    def _presence_idle(self) -> None:
        conv_id = self._presence_conv_id()
        if not (self.project_path and conv_id):
            return
        try:
            from lib.presence import mark_idle as _mark_idle
            _mark_idle(self.project_path, conv_id, agent_id=self.agent_id)
        except Exception as _pe:
            logger.debug('[%s] presence idle failed: %s', self.agent_id, _pe)

    @staticmethod
    def _presence_edited_path(fn_name: str, fn_args: dict) -> str:
        """Extract the project-relative path a file-edit tool just wrote.

        Returns '' for non-edit tools. Handles the single-path tools
        (write_file / apply_diff / insert_content — ``path`` arg) and the batch
        tools (apply_diffs / insert_contents — first entry of the ``edits``
        array). The swarm path reads from the tool ARGS rather than
        commit_round's journal, which attributes by the shared parent taskId
        and so can't separate sibling sub-agents.
        """
        if fn_name not in _PRESENCE_EDIT_ACTIONS:
            return ''
        p = fn_args.get('path')
        if isinstance(p, str) and p.strip():
            return p.strip()
        edits = fn_args.get('edits')
        if isinstance(edits, list):
            for e in edits:
                if isinstance(e, dict) and isinstance(e.get('path'), str) and e['path'].strip():
                    return e['path'].strip()
        return ''

    def _touch_progress(self, note: str = '') -> None:
        """Record a sign of life on the shared beacon (never raises).

        Called from every point that proves forward motion: round start, token
        deltas, tool dispatch, tool return. This is what makes an agent blocked
        inside a long-but-live tool distinguishable from a wedged one — the
        distinction the round-top wall clock structurally could not make,
        because an agent stuck in a tool never reaches the top of a round.
        """
        try:
            self.progress_beacon.touch(self.spec.id, note)
        except Exception as e:
            logger.debug('[Agent:%s] beacon touch failed (non-fatal): %s',
                         self.agent_id, e)

    def run(self) -> SubAgentResult:
        """Execute the sub-agent synchronously. Returns SubAgentResult."""
        start_time = time.time()
        self._started = True
        self.result.status = SubAgentStatus.RUNNING.value

        # ★ Per-client browser routing: set thread-local for this sub-agent thread
        _browser_cid = self.parent_task.get('config', {}).get('browserClientId')
        if _browser_cid:
            from lib.browser import _set_active_client
            _set_active_client(_browser_cid)

        # ★ Presence: register this sub-agent as a live peer (groups under the
        #   parent conversation). Mirror of the orchestrator's announce@start.
        self._presence_announce(phase='working')

        logger.info('[Agent:%s] ========== RUN START ==========', self.agent_id)
        logger.info('[Agent:%s] role=%s model=%s tools=%s',
                     self.agent_id, self.spec.role, self.model,
                     [t.get('function', {}).get('name', '?') for t in (self.tools or [])])
        logger.info('[Agent:%s] objective: %s', self.agent_id, self.spec.objective[:200])
        logger.debug('[Agent:%s] context: %s', self.agent_id, (self.spec.context or '')[:300])

        # NOTE: Do NOT emit AGENT_START here — the scheduler's
        # _on_agent_start_callback already fires a 'running' phase event.
        # Emitting again here would regress the phase from 'running' → 'starting'
        # and (if IDs ever mismatch) create duplicate frontend cards.

        # Hard provider isolation: a swarm sub-agent runs on its OWN thread,
        # so the parent's thread-local provider pin does not propagate
        # automatically. Re-apply it here (forwarded via parent_task config)
        # so every dispatch this sub-agent makes stays bound to the same BYO
        # endpoint. No-op when unset. See lib/llm_dispatch/provider_pin.py.
        _pin = ''
        try:
            _pin = ((self.parent_task or {}).get('config') or {}).get(
                '_pinned_provider_id') or ''
        except Exception as _pe:
            logger.debug('[%s] provider-pin lookup failed: %s', self.agent_id, _pe)

        try:
            from lib.llm_dispatch.provider_pin import provider_pin
            with provider_pin(_pin):
                self._run_loop(start_time)
        except Exception as e:
            self.result.status = SubAgentStatus.FAILED.value
            self.result.error_message = f'{type(e).__name__}: {e}'
            logger.error('[%s] Failed: %s', self.agent_id, e, exc_info=True)
            self._emit_event(
                SwarmEventType.AGENT_FAILED.value,
                f'❌ [{self.spec.role}] Failed: {str(e)[:200]}',
                status='failed', error=str(e)[:200],
            )

        self.result.elapsed_seconds = time.time() - start_time

        logger.info('[Agent:%s] ========== RUN END ==========', self.agent_id)
        logger.info('[Agent:%s] status=%s elapsed=%.1fs rounds=%d tokens=%d answer_len=%d',
                     self.agent_id, self.result.status,
                     self.result.elapsed_seconds, self.result.rounds_used,
                     self.result.total_tokens,
                     len(self.result.final_answer or ''))

        # Finalize status
        if self.result.status == SubAgentStatus.RUNNING.value:
            # Still "running" means we fell through without explicit completion
            if self.result.final_answer:
                self.result.status = SubAgentStatus.COMPLETED.value
            else:
                self.result.status = SubAgentStatus.FAILED.value
                self.result.error_message = self.result.error_message or 'No final answer produced'

        # Final checkpoint with the terminal status + result, BEFORE _cleanup
        # truncates self.messages. The scheduler/master then marks the session
        # row terminated once every agent is done.
        self._checkpoint(final=True)

        # Cleanup
        self._cleanup()

        # ★ Presence: this sub-agent finished — transition its peer to IDLE
        #   (kept, then faded by the sweep). Mirror of the orchestrator's
        #   mark_idle@done in run_task's finally.
        self._presence_idle()

        # NOTE: Do NOT emit AGENT_COMPLETE here — the MasterOrchestrator's
        # on_agent_complete callback already emits swarm_agent_complete with
        # richer data (elapsed, tokens, preview).  Emitting here too would
        # cause the frontend to process TWO completion events per agent.

        return self.result

    def _checkpoint(self, *, final: bool = False):
        """Persist this agent's resumable state to the DB (best-effort).

        Called at every round boundary (after the assistant message +
        tool results for the round are in ``self.messages``) and once more
        at run end. No-op unless ``self.swarm_key`` was set by the master.
        Never raises — persistence is a safety net, not a critical path.
        """
        if not self.swarm_key:
            return
        try:
            from lib.swarm import persistence
            status = self.result.status or SubAgentStatus.RUNNING.value
            persistence.save_agent(
                self.swarm_key, self.spec.id,
                role=self.spec.role,
                objective=self.spec.objective,
                status=status,
                messages=self.messages,
                result=self.result.to_dict() if final else None,
                rounds_used=self.result.rounds_used,
            )
        except Exception as e:
            logger.debug('[Agent:%s] checkpoint failed (non-fatal): %s',
                         self.agent_id, e)

    # Consecutive rounds re-issuing an IDENTICAL tool call before the
    # chassis declares the loop wedged. 10 leaves ample room for a genuine
    # retry (a flaky fetch re-tried a few times) while bounding the runaway
    # shape at a cost of ~10 rounds instead of 26.7 million.
    _MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS = 10

    def _round_safety_label(self) -> str:
        """Render semantic safety breakers for the per-round log line."""
        bounds = [f'np={self._MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS}']
        timeout_seconds = getattr(self.spec, 'timeout_seconds', 0) or 0
        if timeout_seconds > 0:
            bounds.append(f't={timeout_seconds}s')
        return '\u221e(' + ','.join(bounds) + ')'

    def _run_loop(self, start_time: float):
        """Core agent loop: LLM call → tool execution → repeat.

        Rides the shared ``run_agent_loop`` chassis (charter 2026-07-27 iron
        rule): the chassis owns the round loop, the abort checks and the
        ``before_round`` halt seam (the timeout guard lives there).
        Everything swarm-specific — the streaming log, presence heartbeats,
        stream phases, request snapshots, the parallel tool pool, round
        checkpoints, partial-answer extraction — stays in the hooks below.
        """
        from lib.agent_loop import (
            AbortSignal,
            run_agent_loop,
            unparseable_tool_calls,
        )

        timeout_seconds = getattr(self.spec, 'timeout_seconds', None)
        self._run_deadline_monotonic = (
            time.monotonic() + float(timeout_seconds)
            if timeout_seconds and float(timeout_seconds) > 0 else None)
        abort = AbortSignal.from_callback(self.abort_check)
        class _LlmFailed(Exception):
            """Sentinel: the dispatch hook already ran the LLM-error path."""

        def _before_round(rnd):
            # Liveness guard (replaces the wall-clock budget that used to sit
            # here). The old check was ``elapsed > timeout_seconds``, which had
            # two defects: it punished agents for taking long even while they
            # were producing, and — because ``before_round`` only runs at the
            # TOP of a round — it was structurally UNREACHABLE for an agent
            # blocked inside a tool. That is the shape that actually hangs:
            # one agent sat in a ``pytest`` child for over an hour and never
            # tripped the 1800s guard, because it never started another round.
            #
            # Now: a round may always begin while the agent is producing; it is
            # halted only after genuine silence. Tool-level stalls are caught by
            # the beacon (tools heartbeat on subprocess output), not here.
            with self._tool_authority_lock:
                authority_rejections = self._tool_authority_rejections
            if authority_rejections >= 2:
                return 'tool_authority'
            self._touch_progress('round_start')
            if not self.progress_beacon.is_making_progress(self.spec.id):
                return 'stalled'
            # An explicit wall-clock ceiling remains available independently
            # of tool progress.
            if timeout_seconds and (time.time() - start_time) > timeout_seconds:
                return 'timeout'
            return None

        def _dispatch(rnd, _tools):
            """Round hook: the whole per-round LLM machinery (body build,
            request snapshot, streaming log, heartbeats, stream phases,
            dispatch, usage/trace bookkeeping, assistant-message append,
            final-answer branch). ``_tools`` is unused — the body reads
            ``self.tools`` directly on every round."""
            round_num = self._round_offset + rnd + 1
            self.result.rounds_used = round_num
            round_start = time.time()

            # INFO (not debug): this is the per-round heartbeat that reaches
            # app.log. Without it a long-running flow/swarm worker that is
            # merely SLOW (e.g. blocked on a rate-limited strict_model dispatch)
            # is indistinguishable from a wedged one — the round START with no
            # matching "LLM done" line below is precisely what tells an operator
            # "it began round N and is still waiting on the model".
            logger.info('[Agent:%s] \u2500\u2500 Round %d/%s START \u2500\u2500 messages=%d',
                        self.agent_id, round_num,
                        self._round_safety_label(), len(self.messages))

            # ── LLM call (uses DI-injected or default build_body / dispatch_stream) ──
            body = self._build_body(
                model=self.model,
                messages=self.messages,
                tools=self.tools if self.tools else None,
                max_tokens=64000,
                thinking_enabled=self.thinking_enabled,
                temperature=1.0,
            )
            # ★ Attach a session-stable id so add_cache_breakpoints latches the
            #   extended-TTL (1h) decision for this agent's whole multi-round
            #   loop — matches the main orchestrator (orchestrator.py:_task_id).
            #   agent_id is constant across rounds, so the prefix cache key
            #   never shifts mid-session. Released in _cleanup().
            body['_task_id'] = self.agent_id
            # ★ Request Inspector (P4): persist THIS round's LLM request
            #   under the agent's own inspector id — makes sub-agent calls
            #   visible without touching the parent stream (see helper).
            _emit_request_snapshot(self, round_num)

            content_parts = []
            thinking_parts = []

            # Optional per-agent streaming log file. ``output_file`` is
            # set by the master orchestrator's _make_agent factory; if
            # absent (e.g. unit tests), all writes are no-ops.
            #
            # Performance: streaming chunks can arrive ~50–500 times per
            # second.  Opening the file per chunk is wasteful (a syscall
            # per token-ish), so we buffer to memory and flush once per
            # round at well-defined boundaries.
            _output_path = getattr(self, 'output_file', '') or ''
            _log_buffer: list[str] = []

            def _flush_log():
                if not _output_path or not _log_buffer:
                    return
                try:
                    with open(_output_path, 'a', encoding='utf-8') as fp:
                        fp.write(''.join(_log_buffer))
                except OSError as _e:
                    logger.debug('[Agent:%s] output_file write failed (non-fatal): %s',
                                 self.agent_id, _e)
                _log_buffer.clear()

            def _beat_on_stream():
                # ★ Liveness: EVERY token proves the agent is working, so the
                #   beacon is touched unthrottled (a dict write under a short
                #   lock). The presence heartbeat below stays throttled at ~5s
                #   because it does real work; conflating the two would let a
                #   fast-token agent read as silent for up to 5s at a time.
                self._touch_progress('token')
                # ★ Presence heartbeat (throttled, ~5s — mirrors the
                #   conversation path's checkpoint-throttle). Token flow IS
                #   work, so a long single-LLM sub-agent generation with no
                #   tool rounds keeps its peer ACTIVE instead of flapping to
                #   idle. One bump per interval, never per token.
                _now = time.time()
                if _now - self._last_presence_beat >= 5.0:
                    self._last_presence_beat = _now
                    self._presence_heartbeat(phase='generating')

            def on_content(chunk):
                content_parts.append(chunk)
                if _output_path and chunk:
                    _log_buffer.append(chunk)
                if chunk:
                    _beat_on_stream()
                if self.stream_sink and chunk:
                    try:
                        self.stream_sink('content', chunk)
                    except Exception as _se:
                        logger.debug('[Agent:%s] stream_sink content error '
                                     '(non-fatal): %s', self.agent_id, _se)

            def on_thinking(chunk):
                thinking_parts.append(chunk)
                if _output_path and chunk:
                    _log_buffer.append(chunk)
                if chunk:
                    _beat_on_stream()
                if self.stream_sink and chunk:
                    try:
                        self.stream_sink('thinking', chunk)
                    except Exception as _se:
                        logger.debug('[Agent:%s] stream_sink thinking error '
                                     '(non-fatal): %s', self.agent_id, _se)

            # ── Live "waiting for the model" signal ──
            # Before the (potentially minutes-long, rate-limited) dispatch, push
            # a transient 'phase' through the stream sink so a flow/swarm worker
            # bubble shows "waiting for model…" instead of a bare static pulse.
            # The first content/thinking delta clears it on the frontend. The
            # on_retry hook below refreshes it while the dispatcher cycles on
            # cooldown (rate-limited strict_model) — the exact 5-minute
            # first-token stall the user saw as a "hang". Phase, not delta:
            # transient UI, never pollutes the assistant content.
            self._emit_stream_phase(Phase.WAITING_MODEL,
                                    'Sent to the model, waiting for it to '
                                    'start replying…')

            def _on_dispatch_retry(attempt=0, reason='', status_code=0):
                # Surface dispatch retries / cooldown waits as a transient
                # 'retrying' phase on the worker bubble. Bounded by the
                # dispatcher itself (fires on the 1st cooldown cycle, then
                # every ~20 cycles ≈ 6s), so no per-cycle spam here.
                # Structured detailKey/detailArgs ride the **meta passthrough
                # (engine _stream_sink → step_phase → EndpointEventAdapter)
                # so the frontend HUD localizes the cause.
                _d, _meta = _build_dispatch_retry_phase(
                    attempt, reason, status_code, self.model)
                self._emit_stream_phase(Phase.RETRYING, _d, **_meta)

            try:
                msg, stop_reason, usage = self._dispatch_stream(
                    body,
                    on_content=on_content,
                    on_thinking=on_thinking,
                    abort_check=self.abort_check,
                    prefer_model=body.get('model', ''),
                    log_prefix=f'[{self.agent_id}]',
                    on_retry=_on_dispatch_retry,
                )
            except Exception as e:
                logger.error('[%s] LLM call failed round %d: %s', self.agent_id, round_num, e, exc_info=True)
                # Flush whatever we managed to stream before the error so
                # the on-disk log isn't truncated.
                _flush_log()
                # On LLM error, try to extract partial answer from previous rounds
                self.result.error_message = f'LLM call failed at round {round_num}: {e}'
                self._extract_partial_answer(f'LLM error at round {round_num}')
                if self.result.final_answer and round_num > 1:
                    # We have partial results — mark completed with caveat
                    self.result.status = SubAgentStatus.COMPLETED.value
                else:
                    self.result.status = SubAgentStatus.FAILED.value
                raise _LlmFailed

            # Round complete — flush the streaming log file in one shot.
            _flush_log()

            # Track token usage
            round_elapsed = time.time() - round_start
            if usage:
                self.result.prompt_tokens += usage.get('prompt_tokens', 0)
                self.result.completion_tokens += usage.get('completion_tokens', 0)
                self.result.total_tokens += usage.get('total_tokens', 0)
            logger.info('[Agent:%s] Round %d LLM done in %.1fs \u2014 stop=%s '
                        'content_len=%d thinking_len=%d total_tokens=%d',
                        self.agent_id, round_num, round_elapsed, stop_reason,
                        len(''.join(content_parts)), len(''.join(thinking_parts)),
                        self.result.total_tokens)

            # Save thinking for trace
            if thinking_parts:
                self.result.reasoning_trace += (
                    f'\n--- Round {round_num} ---\n' +
                    ''.join(thinking_parts)[:2000]
                )

            # ── Premature-close poison guard ──
            # The gateway can sever the SSE stream without its terminal
            # frames (``usage['_missing_done']``). Two poison shapes must NOT
            # flow on:
            #   (a) tool calls whose arguments JSON was cut mid-stream —
            #       executing them would run tools on corrupt arguments (or
            #       the sanitizer's '{}' substitution);
            #   (b) an empty, tool-less "answer" — accepting it would
            #       silently finalize the sub-agent with nothing.
            # Both are retried through the chassis' retry_bonus (bounded by
            # max_retry_bonus below; ``self._poison_strikes`` mirrors that
            # budget so this hook knows whether the chassis will grant the
            # retry). On exhaustion the corrupt calls are stripped and the
            # round degrades to whatever valid calls / content survive —
            # never executed raw.
            if isinstance(usage, dict) and usage.get('_missing_done'):
                _bad_tcs = unparseable_tool_calls(msg)
                _empty_close = (not msg.get('tool_calls')
                                and not (msg.get('content') or '').strip())
                if _bad_tcs or _empty_close:
                    _strikes = getattr(self, '_poison_strikes', 0)
                    if _strikes < 2:  # == max_retry_bonus on the call below
                        self._poison_strikes = _strikes + 1
                        _why = ('truncated tool args: ' + ', '.join(
                            (tc.get('function') or {}).get('name', '?')
                            for tc in _bad_tcs)) if _bad_tcs else 'empty stream'
                        logger.warning(
                            '[Agent:%s] Round %d: premature close (%s) — '
                            'transparent retry %d/2 (round discarded before '
                            'history append)', self.agent_id, round_num,
                            _why, self._poison_strikes)
                        self._emit_stream_phase(
                            Phase.RETRYING,
                            f'⚠️ 上游断流（{_why}），正在自动重试 '
                            f'({self._poison_strikes}/2)…')
                        _flush_log()
                        return msg, stop_reason, usage  # unappended → retry
                    if _bad_tcs:
                        msg['tool_calls'] = [
                            tc for tc in (msg.get('tool_calls') or [])
                            if tc not in _bad_tcs]
                        logger.warning(
                            '[Agent:%s] Round %d: premature-close retry budget '
                            'exhausted — stripped %d corrupt tool call(s), '
                            'proceeding with %d valid one(s)',
                            self.agent_id, round_num, len(_bad_tcs),
                            len(msg['tool_calls']))

            # Append assistant message
            self.messages.append(msg)

            # ── Check for tool calls ──
            tool_calls = msg.get('tool_calls', [])

            if not tool_calls:
                # No tool calls → agent has produced a text response
                content = msg.get('content', ''.join(content_parts))
                self.result.final_answer = content
                self.result.status = SubAgentStatus.COMPLETED.value
                logger.debug('[Agent:%s] Round %d: FINAL ANSWER produced (len=%d)',
                             self.agent_id, round_num, len(content or ''))

                self._emit_event(
                    'progress',
                    f'📝 [{self.spec.role}] Round {round_num}: produced final answer',
                    status='running', phase='done',
                    round_num=round_num,
                    # FULL answer — same contract as the terminal
                    # ``swarm_agent_complete`` frame and the durable snapshot.
                    # This is the in-flight paint of the SAME text, so a cap
                    # here made the panel show a truncated answer for the whole
                    # window between "agent finished thinking" and "master
                    # emitted complete". Fires once per agent, not per token.
                    preview=(content or ''),
                )

            return msg, stop_reason, usage

        def _execute_round_tools(rnd, tool_calls):
            """Batch hook (chassis ``execute_tools``): progress event →
            parallel tool pool → round-boundary checkpoint. Called once per
            tool round; the old post-tools abort check is covered by the
            chassis' before-round check of the NEXT round (outcome.aborted →
            CANCELLED below).
            """
            round_num = self._round_offset + rnd + 1
            # ── Execute tool calls ──
            tool_names = []
            for tc in tool_calls:
                fn = tc.get('function', {})
                tool_names.append(fn.get('name', '?'))

            logger.debug('[Agent:%s] Round %d: %d tool call(s) → %s',
                         self.agent_id, round_num, len(tool_calls), tool_names)

            self._emit_event(
                'progress',
                f'🔧 [{self.spec.role}] Round {round_num}: '
                f'{len(tool_calls)} tool call(s): '
                f'{", ".join(tool_names[:5])}'
                f'{"..." if len(tool_names) > 5 else ""}',
                status='running', phase='tool_use',
                round_num=round_num,
                toolNames=tool_names,
            )

            self._execute_tool_calls(tool_calls, round_num)

            # ── Round-boundary checkpoint ──
            # Persist the full message history (assistant turn + tool results)
            # so a restart can rehydrate and resume from exactly here. This is
            # the last completed round; an interrupted next round is re-run on
            # resume (side-effecting tools may therefore re-execute — accepted
            # by design, see lib/swarm/persistence.py).
            # (The checkpoint itself now lives on the chassis' on_round_end
            #  seam — see the run_agent_loop call below.)

        try:
            outcome = run_agent_loop(
                abort=abort,
                round_tools=None,  # the dispatch hook reads self.tools itself
                dispatch=_dispatch,
                execute_tools=_execute_round_tools,
                before_round=_before_round,
                # Wedged-loop breaker. On 2026-07-27 one sub-agent re-issued
                # an identical tool call for 26.7M rounds (3.5h, 9.1 GB of
                # app.log). The chassis halts on N consecutive rounds whose
                # tool-call fingerprint is unchanged; empty CONTENT is NOT
                # the criterion (measured: 50.3% of legitimate rounds have
                # content_len==0 — that is just a pure tool-calling turn).
                max_consecutive_no_progress_rounds=(
                    self._MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS),
                # Premature-close retry: a severed stream (missing [DONE])
                # with truncated tool args / no usable output earns ONE bonus
                # round per poison, capped — the dispatch hook above discards
                # the poisoned round from history, so the retry is a clean
                # re-issue of the same context.
                retry_bonus=lambda rnd, msg, finish, usage: (
                    isinstance(usage, dict) and bool(usage.get('_missing_done'))
                    and (bool(unparseable_tool_calls(msg))
                         or (isinstance(msg, dict)
                             and not msg.get('tool_calls')
                             and not (msg.get('content') or '').strip()))
                ),
                max_retry_bonus=2,
                # Round-boundary checkpoint lives on the chassis'
                # on_round_end seam (NOT inside the batch hook) so the
                # orchestrator's throttled checkpoint converges on the
                # same placement — one shape, not two.
                on_round_end=lambda rnd: self._checkpoint(),
            )
        except _LlmFailed as _e:
            logger.debug('run loop: _LlmFailed (%s)', _e)
            return  # the dispatch hook already ran the LLM-error path

        completed_rounds = self._round_offset + outcome.rounds
        next_round = completed_rounds + 1

        if outcome.completed:
            return  # the final-answer branch in _dispatch set everything

        if outcome.aborted:
            logger.info('[%s] Aborted (%s) at round %d',
                        self.agent_id, outcome.exit_reason, completed_rounds)
            self.result.status = SubAgentStatus.CANCELLED.value
            self._extract_partial_answer(
                f'Agent cancelled at round {completed_rounds}')
            return

        if outcome.halted and outcome.exit_reason == 'stalled':
            _silent = self.progress_beacon.seconds_since_activity(self.spec.id)
            logger.warning(
                '[%s] Stalled: no progress signal for %.0fs (stall_at=%.0fs) '
                'at round %d — halting a silent agent',
                self.agent_id, _silent,
                self.progress_beacon.stall_timeout, next_round)
            audit_log('agent_stalled',
                      agent_id=self.agent_id, role=self.spec.role,
                      model=self.model, rounds=completed_rounds,
                      silent_seconds=round(_silent),
                      objective=(self.spec.objective or '')[:200])
            self.result.status = SubAgentStatus.COMPLETED.value
            self._finalize_with_wrapup(
                f'no activity for {_silent:.0f}s (stopped at round '
                f'{completed_rounds})')
            self._emit_event(
                'progress',
                f'⏸️ [{self.spec.role}] Stopped: silent for {_silent:.0f}s',
                status='running', phase='stalled',
                round_num=next_round,
            )
            return

        if outcome.halted and outcome.exit_reason == 'timeout':
            logger.warning(
                '[%s] Timeout after %ss at round %d',
                self.agent_id, timeout_seconds, next_round,
            )
            self.result.status = SubAgentStatus.COMPLETED.value
            self._finalize_with_wrapup(
                f'timed out after the explicit {timeout_seconds}s ceiling '
                f'(completed {completed_rounds} rounds)')
            self._emit_event(
                'timeout',
                f'⏰ [{self.spec.role}] Timed out after {timeout_seconds}s',
                status='timeout', phase='timeout',
                round_num=next_round,
            )
            return

        if outcome.halted and outcome.exit_reason == 'no_progress':
            # Wedged loop: the model re-issued an identical tool call for
            # _MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS rounds. Without its own
            # branch it would be mislabelled, hiding the very defect it detects.
            logger.warning(
                '[%s] No-progress breaker: %d consecutive identical '
                'tool-call rounds at round %d — halting a wedged loop',
                self.agent_id, outcome.consecutive_no_progress_rounds,
                completed_rounds)
            # L3 feedback: a structured, greppable record so a wedged agent
            # is a first-class signal rather than something an operator has
            # to infer from log VOLUME after the fact.
            audit_log('agent_loop_no_progress',
                      agent_id=self.agent_id,
                      role=self.spec.role,
                      model=self.model,
                      rounds=completed_rounds,
                      consecutive_identical=outcome.consecutive_no_progress_rounds,
                      objective=(self.spec.objective or '')[:200])
            self.result.status = SubAgentStatus.COMPLETED.value
            self._finalize_with_wrapup(
                f'no progress for '
                f'{outcome.consecutive_no_progress_rounds} consecutive rounds '
                f'(stopped at round {completed_rounds})',
                guidance=('You were repeating the same tool call. Do not '
                          'retry it; report what you already learned.'))
            self._emit_event(
                'progress',
                f'🛑 [{self.spec.role}] Stopped: no progress for '
                f'{outcome.consecutive_no_progress_rounds} rounds',
                status='running', phase='no_progress',
                round_num=completed_rounds,
            )
            return

        if outcome.halted and outcome.exit_reason == 'tool_authority':
            with self._tool_authority_lock:
                rejected = self._tool_authority_rejections
            logger.warning(
                '[%s] Tool-authority breaker: %d unavailable tool calls '
                'rejected by round %d — forcing a tool-less wrap-up',
                self.agent_id, rejected, completed_rounds)
            audit_log('agent_tool_authority_breaker',
                      agent_id=self.agent_id,
                      role=self.spec.role,
                      model=self.model,
                      rounds=completed_rounds,
                      rejected_calls=rejected,
                      objective=(self.spec.objective or '')[:200])
            self.result.status = SubAgentStatus.COMPLETED.value
            self._finalize_with_wrapup(
                f'{rejected} unavailable tool calls were rejected '
                f'(stopped at round {completed_rounds})',
                guidance=(
                    'The required capability is not authorized in this run. '
                    'Do not suggest or emulate a shell workaround; report the '
                    'capability limitation plainly.'))
            self._emit_event(
                'progress',
                f'🛑 [{self.spec.role}] Stopped: unavailable tool repeatedly requested',
                status='running', phase=Phase.TOOL_AUTHORITY,
                round_num=completed_rounds,
            )
            return

        logger.warning('[%s] Agent loop halted unexpectedly: %s',
                       self.agent_id, outcome.exit_reason)
        self._finalize_with_wrapup(
            f'agent loop halted: {outcome.exit_reason}')
        self.result.status = SubAgentStatus.COMPLETED.value

    # ─────────────────────────────────────────────────
    #  Answer extraction helpers
    # ─────────────────────────────────────────────────

    def _finalize_with_wrapup(self, reason: str, *, guidance: str = '') -> None:
        """Ask the agent to WRITE UP its work, instead of scraping history.

        ``_extract_partial_answer`` walks backwards for the last assistant text
        and staples ``[Partial — …]`` on the front. For a halted agent that is
        a bad trade: the measured case had 16 rounds and 1,091,830 tokens of
        real investigation reduced to whatever intermediate sentence happened
        to be last — the findings were paid for and then discarded.

        So give it one final, TOOL-LESS turn to summarize what it already
        knows. The work is already bought; a single short completion is a
        rounding error against it, and turns a truncated fragment into a
        usable report.

        Falls back to ``_extract_partial_answer`` when the wrap-up cannot run
        (no history, dispatch failure) — degradation must never be worse than
        the behaviour it replaces.
        """
        if self.result.final_answer:
            return

        has_history = any(m.get('role') == 'assistant' for m in self.messages)
        if not has_history:
            self._extract_partial_answer(reason)
            return

        self._emit_stream_phase(
            'writing', 'Stopping — writing up findings so far…')
        try:
            wrap_msgs = list(self.messages) + [{
                'role': 'user',
                'content': (
                    f'STOP: {reason}. Do not call any more tools.\n\n'
                    'Write your FINAL ANSWER now from what you have already '
                    'established. Report the findings you actually confirmed, '
                    'and state plainly which parts of your objective you did '
                    'not get to — an honest partial report is useful, an '
                    'invented complete one is not.'
                    + (f'\n\n{guidance}' if guidance else '')
                ),
            }]
            body = self._build_body(
                model=self.model,
                messages=wrap_msgs,
                tools=None,          # tool-less: this turn must produce prose
                max_tokens=8000,
                thinking_enabled=False,
                temperature=1.0,
            )
            body['_task_id'] = self.agent_id
            parts: list[str] = []
            msg, _stop, usage = self._dispatch_stream(
                body,
                on_content=parts.append,
                on_thinking=lambda _c: None,
                abort_check=self.abort_check,
                prefer_model=body.get('model', ''),
                log_prefix=f'[{self.agent_id}:wrapup]',
            )
            if usage:
                self.result.prompt_tokens += usage.get('prompt_tokens', 0)
                self.result.completion_tokens += usage.get('completion_tokens', 0)
                self.result.total_tokens += usage.get('total_tokens', 0)
            content = (msg.get('content') if isinstance(msg, dict) else '') \
                or ''.join(parts)
            content = (content or '').strip()
            if len(content) > 20:
                self.result.final_answer = f'[Stopped — {reason}]\n\n{content}'
                logger.info('[Agent:%s] Wrap-up produced %d chars after: %s',
                            self.agent_id, len(content), reason)
                return
            logger.warning('[Agent:%s] Wrap-up returned too little (%d chars) '
                           '— falling back to history extraction',
                           self.agent_id, len(content))
        except Exception as e:
            logger.warning('[Agent:%s] Wrap-up turn failed (%s) — falling back '
                           'to history extraction', self.agent_id, e)

        self._extract_partial_answer(reason)

    def _extract_partial_answer(self, reason: str = ''):
        """Extract the best available answer from message history.

        Scans backwards through messages looking for the last substantive
        assistant content. Sets result.final_answer if found.
        """
        if self.result.final_answer:
            return  # Already have an answer

        # Walk backwards through messages for assistant content
        for msg in reversed(self.messages):
            if msg.get('role') == 'assistant' and msg.get('content'):
                content = msg['content'].strip()
                if len(content) > 20:  # Skip trivial responses
                    prefix = f'[Partial — {reason}]\n\n' if reason else ''
                    self.result.final_answer = prefix + content
                    return

        # If nothing found, note the reason
        if reason:
            self.result.final_answer = f'[{reason}] No substantive answer was produced.'

    # ─────────────────────────────────────────────────
    #  Tool execution
    # ─────────────────────────────────────────────────

    def _execute_tool_calls(self, tool_calls: list, round_num: int):
        """Execute one or more tool calls, potentially in parallel."""

        if len(tool_calls) == 1:
            # Single tool call — run directly (no thread overhead)
            tc = tool_calls[0]
            result = self._execute_single_tool(tc, round_num)
            self.messages.append({
                'role': 'tool',
                # ``or``, NOT ``get('id', default)``: the SSE accumulator always
                # SETS the key (to ''), so a default-arg fallback is
                # unreachable and a blank id would reach the wire verbatim — a
                # malformed turn the gateway cannot match to its tool_call.
                'tool_call_id': tc.get('id') or str(uuid.uuid4())[:8],
                'content': result,
            })
        else:
            # Multiple tool calls — run in parallel.
            #
            # Results are keyed by the tool call's POSITION, never by its id.
            # Keying by ``tc.get('id', uuid4())`` was silently lossy, because
            # the SSE accumulator creates every tool-call slot with
            # ``'id': ''`` (lib/llm/_sse_core.py:854/922) — the KEY always
            # exists, so the uuid fallback never fires and cannot de-collide
            # anything:
            #   * two parallel calls that both kept ``id=''`` collapse onto ONE
            #     dict key, so the first tool is fed the SECOND tool's output —
            #     silently, with no error anywhere (verified);
            #   * had the fallback ever fired it would mint a DIFFERENT uuid at
            #     execution and at lookup, yielding the literal string
            #     ``(no result)`` as that tool's content.
            # Position is intrinsic to the batch and cannot collide, so the
            # mapping is total by construction.
            results: dict[int, str] = {}
            max_workers = min(len(tool_calls), MAX_PARALLEL_TOOLS)
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f'{self.agent_id}-tools',
            ) as pool:
                futures = {}
                for idx, tc in enumerate(tool_calls):
                    future = pool.submit(self._execute_single_tool, tc, round_num)
                    futures[future] = idx

                for future in as_completed(futures):
                    idx = futures[future]
                    tc = tool_calls[idx]
                    try:
                        # NO timeout. ``as_completed`` only ever yields futures
                        # that are ALREADY done (measured: done=True at every
                        # yield, including a 2.0s task read with timeout=0.01),
                        # so the former ``result(timeout=300)`` could not fire —
                        # it was unreachable, not a live 300s cap. Keeping a
                        # number there implied a bound that did not exist; a
                        # genuinely slow tool is bounded by the liveness beacon,
                        # which asks whether output is still arriving rather
                        # than how long the work has taken.
                        result = future.result()
                    except Exception as e:
                        fn_name = tc.get('function', {}).get('name', '?')
                        logger.warning(
                            '[%s] Tool %s raised in thread: %s',
                            self.agent_id, fn_name, e,
                            exc_info=True)

                        result = f'Tool execution error ({fn_name}): {type(e).__name__}: {e}'
                    results[idx] = result

            # Append results in original order (important for reproducibility).
            # ``tool_call_id`` still carries the wire id the model sent (that is
            # what the gateway matches on) — only the internal RESULT lookup is
            # position-keyed. A blank id is repaired here too: two tool results
            # sharing '' is a malformed turn on the wire. A missing position
            # would be a real internal bug, so it is logged loudly instead of
            # quietly degrading to '(no result)'.
            for idx, tc in enumerate(tool_calls):
                if idx not in results:
                    logger.error(
                        '[%s] Round %d: no result recorded for tool %s at '
                        'position %d — internal bookkeeping bug, not a tool '
                        'failure', self.agent_id, round_num,
                        tc.get('function', {}).get('name', '?'), idx)
                self.messages.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id') or str(uuid.uuid4())[:8],
                    'content': results.get(idx, '(no result)'),
                })

    def _known_tool_names(self) -> set[str]:
        """Live set of REAL tool names available to THIS sub-agent this turn.

        Derived from the role-scoped ``self.tools`` schema list (built-ins +
        MCP + swarm/artifact tools injected for this agent). Used as the
        membership oracle for :func:`resolve_tool_name` so an alias never maps
        onto a tool the agent doesn't actually have, and a legitimate
        dynamically-injected tool is never mis-aliased. The three
        locally-handled artifact tools are included only when this agent has
        the backing store and schemas for them.
        """
        names: set[str] = set()
        if self.artifact_store is not None:
            names.update({'store_artifact', 'read_artifact', 'list_artifacts'})
        for t in (self.tools or []):
            if isinstance(t, dict):
                n = (t.get('function') or {}).get('name')
                if n:
                    names.add(n)
        return names

    def _execute_single_tool(self, tool_call: dict, round_num: int) -> str:
        """Execute a single tool call and return the result string."""
        fn_info = tool_call.get('function', {})
        _raw_name = fn_info.get('name', '?')
        tc_id = tool_call.get('id') or str(uuid.uuid4())[:8]
        tool_start = time.time()

        # ── Unified tool-call ingestion ──
        # The sub-agent path dispatches to the executor DIRECTLY, bypassing the
        # main chat dispatcher's parse_tool_calls — so it must funnel each call
        # through the SAME ingestion seam so name-alias (WebFetch→fetch_url …),
        # JSON decode+repair, AND schema/param repair (previously missing here)
        # all apply identically. Hallucination rejection is enabled: an invented
        # name is returned to the sub-agent as an actionable error instead of
        # the executor's raw "unknown tool" wall. The membership oracle is THIS
        # agent's role-scoped tool set.
        _ingested = ingest_tool_call(
            tool_call,
            known_tools=self._known_tool_names(),
            model=self.model or '',
            conv_id=self._presence_conv_id(),
        )
        if _ingested.dropped:
            logger.warning('[Agent:%s] Dropping tool call %r (%s)',
                           self.agent_id, _raw_name, _ingested.drop_reason)
            return f'Error: ignored malformed tool name {_raw_name!r} ({_ingested.drop_reason}).'
        if _ingested.alias_kind:
            logger.info('[Agent:%s] Aliased tool name %r → %r (%s)',
                        self.agent_id, _raw_name, _ingested.fn_name, _ingested.alias_kind)
        fn_name = _ingested.fn_name
        fn_info['name'] = fn_name  # persist canonical name onto the tool_call
        if _ingested.rejected:
            with self._tool_authority_lock:
                self._tool_authority_rejections += 1
                rejection_count = self._tool_authority_rejections
            logger.warning('[Agent:%s] Rejected hallucinated tool %r (suggestions=%s)',
                           self.agent_id, _raw_name, _ingested.rejection.get('suggestions'))
            suffix = (f' This is unavailable-tool rejection '
                      f'{rejection_count}/2; after the second rejection this '
                      f'agent will stop and report the capability limitation.')
            return (_ingested.parse_error or '') + suffix
        if _ingested.parse_error:
            logger.warning('[Agent:%s] Unparseable args for %s: %s',
                           self.agent_id, fn_name, _ingested.parse_error)
            return _ingested.parse_error
        fn_args = _ingested.fn_args

        logger.debug('[Agent:%s] Round %d → TOOL_CALL %s args=%s',
                     self.agent_id, round_num, fn_name, str(fn_args)[:300])

        # One name-keyed brief drives BOTH the persisted tool_log below and
        # the live SSE events — reload recovery replays tool_log verbatim
        # (master._snapshot_tool_timeline), so a single formatting call keeps
        # the live and recovered panels identical.
        args_brief = format_tool_args_brief(fn_name, fn_args)

        # Dispatching a tool is forward motion, and so is its return. Touching
        # BOTH ends means a long tool that produces no intermediate output
        # still bookends its own execution window.
        self._touch_progress(f'tool_start:{fn_name}')

        # Log the tool call
        self.result.tool_log.append({
            'round': round_num,
            'tool': fn_name,
            'args_brief': args_brief,
            'timestamp': time.time(),
            # Filled in by _emit_finish once the call returns. Persisted so the
            # durable snapshot can rebuild the panel's timeline WITH the result
            # text, not just the tool name.
            'preview': '',
        })

        # ── Per-tool-call SSE event: started ──
        # Surfaces the agent's execution timeline in the swarm panel —
        # the user sees each sub-agent tool call live, not just an
        # aggregate `Using X, Y, Z` summary.
        self._emit_event(
            'agent_tool_call',
            f'🔧 [{self.spec.role}] {fn_name}',
            status='running', phase='tool_use',
            round_num=round_num,
            callId=tc_id, toolName=fn_name,
            argsBrief=args_brief, callStatus='running',
        )

        def _emit_finish(status: str, *, preview: str = '', error: str = ''):
            # The other end of the tool window — a returning tool is proof of
            # forward motion even when it emitted nothing while it ran.
            self._touch_progress(f'tool_done:{fn_name}')
            _full_len = len(preview or '')
            _sent = (preview or '')[:_SSE_TOOL_PREVIEW_CHARS]
            # Persist the preview onto the tool_log row this call already
            # appended, so the DURABLE snapshot (and therefore a reloaded
            # panel) carries what the live frame showed. Without this the
            # text exists only on a transient SSE frame.
            if self.result.tool_log:
                _row = self.result.tool_log[-1]
                if isinstance(_row, dict) and _row.get('tool') == fn_name:
                    _row['preview'] = preview or ''
                    if error:
                        _row['error'] = error
            self._emit_event(
                'agent_tool_call',
                f'{"✅" if status == "done" else "❌"} [{self.spec.role}] {fn_name}',
                status='running', phase='tool_use',
                round_num=round_num,
                callId=tc_id, toolName=fn_name,
                argsBrief=args_brief, callStatus=status,
                callElapsed=round(time.time() - tool_start, 2),
                preview=_sent,
                previewTruncated=(_full_len > len(_sent)),
                previewFullChars=_full_len,
                error=error or '',
            )

        # ── Handle artifact tools locally ──
        if fn_name in ('store_artifact', 'read_artifact', 'list_artifacts'):
            try:
                if fn_name == 'store_artifact':
                    result = self._handle_store_artifact(fn_args)
                elif fn_name == 'read_artifact':
                    result = self._handle_read_artifact(fn_args)
                else:
                    result = self._handle_list_artifacts(fn_args)
                _emit_finish('done', preview=result or '')
                return result
            except Exception as e:
                _emit_finish('failed', error=f'{type(e).__name__}: {e}')
                raise

        # ── Dispatch to real tools via executor ──
        # Bind the thread-scoped progress sink for the WHOLE dispatch: a long
        # tool (``run_command`` on a build/test run) heartbeats from inside,
        # so the stall check can tell "subprocess is printing" from "wedged".
        # Without this the beacon goes quiet the instant a tool is entered —
        # exactly the >1h pytest hang that no guard could see.
        try:
            logger.debug('[Agent:%s] Dispatching tool %s args=%s',
                         self.agent_id, fn_name, str(fn_args)[:200])
            with thread_progress_sink(self.progress_beacon, self.spec.id):
                result = self._dispatch_tool(tool_call, fn_name, fn_args, round_num)
            truncated = self._truncate_tool_result(result)
            tool_elapsed = time.time() - tool_start
            logger.debug('[Agent:%s] Tool %s completed in %.2fs result_len=%d',
                         self.agent_id, fn_name, tool_elapsed, len(truncated))
            logger.debug('[Agent:%s] Tool %s result preview: %s',
                         self.agent_id, fn_name, truncated[:300])
            # ★ Presence: if this was a file-edit tool, record the touched path
            #   on this sub-agent's peer so cross-peer overlap detection can flag
            #   two sub-agents (or a sub-agent + a sibling conversation)
            #   clobbering the same file. We read the path straight from the
            #   tool args (the swarm path can't use commit_round's journal,
            #   which attributes by the shared parent taskId). Only on success
            #   (an error already returned above via the except).
            _edited = self._presence_edited_path(fn_name, fn_args)
            if _edited:
                self._presence_record_file(_edited, action=_PRESENCE_EDIT_ACTIONS.get(fn_name, 'edited'))
            _emit_finish('done', preview=truncated)
            return truncated
        except Exception as e:
            tool_elapsed = time.time() - tool_start
            logger.warning('[Agent:%s] Tool %s FAILED in %.2fs: %s',
                           self.agent_id, fn_name, tool_elapsed, e, exc_info=True)
            _emit_finish('failed', error=f'{type(e).__name__}: {e}')
            return f'Tool error ({fn_name}): {type(e).__name__}: {e}'

    # ─────────────────────────────────────────────────
    #  Artifact handling
    # ─────────────────────────────────────────────────

    def _handle_store_artifact(self, args: dict) -> str:
        """Handle the store_artifact tool call."""
        if self.artifact_store is None:
            logger.warning('[Agent:%s] store_artifact called but no artifact_store', self.agent_id)
            return 'Error: artifact store not available'
        key = args.get('key', '')
        content = args.get('content', '')
        if not key:
            return 'Error: "key" is required'
        if not content:
            return 'Error: "content" is required (cannot store empty artifact)'
        tags = args.get('tags', [])
        logger.debug('[Agent:%s] STORE_ARTIFACT key=%s len=%d tags=%s',
                      self.agent_id, key, len(content), tags)
        try:
            self.artifact_store.put(key, content, writer_id=self.agent_id, tags=tags)
        except Exception as e:
            logger.warning('[%s] artifact store put failed: %s', self.agent_id, e, exc_info=True)
            return f'Error storing artifact "{key}": {e}'
        self.result.artifacts_written.append(key)
        return f'Stored artifact "{key}" ({len(content):,} chars)'

    def _handle_read_artifact(self, args: dict) -> str:
        """Handle the read_artifact tool call."""
        if self.artifact_store is None:
            logger.warning('[Agent:%s] read_artifact called but no artifact_store', self.agent_id)
            return 'Error: artifact store not available'
        key = args.get('key', '')
        if not key:
            return 'Error: "key" is required'
        logger.debug('[Agent:%s] READ_ARTIFACT key=%s', self.agent_id, key)
        try:
            content = self.artifact_store.get(key)
        except Exception as e:
            logger.warning('[%s] artifact store get failed: %s', self.agent_id, e, exc_info=True)
            return f'Error reading artifact "{key}": {e}'
        if not content:
            available = self.artifact_store.list_keys()
            logger.debug('[Agent:%s] READ_ARTIFACT key=%s NOT FOUND, available=%s',
                          self.agent_id, key, available)
            return f'Artifact "{key}" not found. Available: {", ".join(available) or "(none)"}'
        logger.debug('[Agent:%s] READ_ARTIFACT key=%s → OK len=%d',
                      self.agent_id, key, len(content))
        self.result.artifacts_read.append(key)
        return content

    def _handle_list_artifacts(self, args: dict = None) -> str:
        """Handle the list_artifacts tool call."""
        if self.artifact_store is None:
            return 'Error: artifact store not available'
        tag = (args or {}).get('tag', '')
        try:
            if tag:
                keys = self.artifact_store.list_keys(tag=tag)
                return f'Artifacts with tag "{tag}": {", ".join(keys) or "(none)"}'
            return self.artifact_store.summary()
        except Exception as e:
            logger.warning('[%s] artifact store list failed: %s', self.agent_id, e, exc_info=True)
            return f'Error listing artifacts: {e}'

    # ─────────────────────────────────────────────────
    #  Tool dispatch
    # ─────────────────────────────────────────────────

    def _dispatch_tool(self, tool_call: dict, fn_name: str, fn_args: dict,
                       round_num: int) -> str:
        """Execute a tool by name using the project tools executor.

        Delegates to ``_execute_tool_one`` from the executor module, which
        handles web_search, fetch_url, project tools, browser tools, etc.
        """
        from lib.tasks_pkg.executor import _execute_tool_one

        # Work on a private args copy. The ingested args are persisted in the
        # assistant message and tool log; adding a swarm-only safety timeout
        # must not rewrite what the model actually sent.
        fn_args = dict(fn_args or {})

        # An explicit whole-agent deadline must also reach a process blocked
        # inside run_command. Do not invent a default command timeout here:
        # the swarm liveness contract deliberately permits productive, long
        # commands to run while they emit progress. A genuinely stalled swarm
        # is cancelled by the master's distinct internal stop signal, mirrored
        # into task_proxy['aborted'] below.
        if fn_name == 'run_command':
            try:
                requested_timeout = float(fn_args.get('timeout') or 0)
            except (TypeError, ValueError) as exc:
                logger.debug(
                    '[Agent:%s] invalid run_command timeout %r: %s',
                    self.agent_id, fn_args.get('timeout'), exc)
                requested_timeout = 0.0
            remaining = None
            if self._run_deadline_monotonic is not None:
                remaining = max(0.1, self._run_deadline_monotonic - time.monotonic())
            if remaining is not None and (
                    requested_timeout <= 0 or requested_timeout > remaining):
                fn_args['timeout'] = remaining

        def _tool_abort_requested() -> bool:
            try:
                if self.abort_check():
                    return True
            except Exception as e:
                logger.debug('[Agent:%s] tool abort probe failed: %s',
                             self.agent_id, e)
            return (self._run_deadline_monotonic is not None
                    and time.monotonic() >= self._run_deadline_monotonic)

        # Build an ISOLATED minimal task dict for tool execution. Sharing the
        # parent's events/toolRounds let sub-agent checkpoint callbacks write a
        # half-built inner round onto the parent conversation. The sub-agent's
        # durable transcript and UI timeline are owned by its own checkpoint /
        # on_event paths, so the executor proxy must stay local.
        # ★ _suppressEvents: tool handlers call _finalize_tool_round →
        #   append_event, which would otherwise push tool_start/tool_result
        #   SSE events onto the PARENT's stream (task id is shared). Those
        #   events carry this sub-agent's own small roundNum and an empty
        #   toolCallId, so the frontend's roundNum fallback grafts them onto a
        #   same-numbered parent round (e.g. a run_command rendered as an
        #   apply_diffs batch block). The sub-agent's tool activity is already
        #   surfaced via the master orchestrator's on_event (swarm_agent_*)
        #   callbacks, so suppressing the leak here loses nothing.
        task_proxy = {
            'id': self.parent_task.get('id', 'unknown'),
            'convId': self.parent_task.get('convId', 'unknown'),
            'status': 'running',
            'aborted': _tool_abort_requested(),
            'content': '',
            'thinking': '',
            'events': [],
            'events_lock': threading.Lock(),
            'content_lock': threading.RLock(),
            'toolRounds': [],
            'phase': self.parent_task.get('phase'),
            '_suppressEvents': True,
            '_suppressCheckpoint': True,
            # There is no human input channel attached to a background swarm
            # worker. The code-exec handler uses this to select its truly
            # non-interactive subprocess path instead of waiting forever on a
            # stdin request that no UI can answer.
            '_unattended': True,
            # ★ Inherit per-request custom tools (handlers resolve task-locally
            #   in _execute_tool_one before the global registry). The schema
            #   side is already role-scoped via scope_tools_for_role.
            '_tool_env': self.parent_task.get('_tool_env'),
        }

        tc_id = tool_call.get('id') or str(uuid.uuid4())[:8]

        # Build a round_entry stub (executor expects this for side-effects)
        round_entry = {
            'roundNum': round_num,
            'query': f'{fn_name}({str(fn_args)[:60]})',
            'results': None,
            'status': 'searching',
            'toolName': fn_name,
        }

        # Config for the executor — include browserClientId for per-device routing
        cfg = {
            'model': self.model,
            'thinking_enabled': self.thinking_enabled,
            'search_mode': 'multi',
            'browserClientId': self.parent_task.get('config', {}).get('browserClientId'),
        }

        # run_command polls task['aborted'] every ~200 ms. Mirror the parent's
        # live abort callback (and an explicit agent deadline) into the proxy
        # while the executor is blocked so Stop reaches the subprocess tree.
        monitor_stop = threading.Event()

        def _mirror_abort():
            while not monitor_stop.wait(0.1):
                if _tool_abort_requested():
                    task_proxy['aborted'] = True
                    return

        monitor = None
        if fn_name == 'run_command':
            monitor = threading.Thread(
                target=_mirror_abort,
                name=f'swarm-abort-{self.spec.id}', daemon=True)
            monitor.start()

        try:
            _, tool_content, _ = _execute_tool_one(
                task_proxy, tool_call, fn_name, tc_id, fn_args,
                round_num, round_entry, cfg,
                self.project_path, bool(self.project_path),
            )
            if isinstance(tool_content, dict):
                # Some tools (e.g. browser_screenshot) return dicts
                return json.dumps(tool_content, ensure_ascii=False)
            return str(tool_content) if tool_content is not None else ''
        except Exception as e:
            logger.error('[%s] Tool dispatch error for %s: %s', self.agent_id, fn_name, e, exc_info=True)
            return f'Error executing {fn_name}: {type(e).__name__}: {e}'
        finally:
            monitor_stop.set()
            if monitor is not None:
                monitor.join(timeout=0.3)

    # ─────────────────────────────────────────────────
    #  Result truncation
    # ─────────────────────────────────────────────────

    def _truncate_tool_result(self, result: str, max_chars: int = None) -> str:
        """Truncate tool results to avoid blowing up the sub-agent's context.

        Strategy:
          • If result fits within limit, return as-is.
          • Otherwise, keep the first ~70% and last ~15% of the limit,
            with a clear truncation marker in between.
          • The truncation marker tells the model the full size so it can
            decide whether to make a more specific query.

        Args:
            result: Raw tool output string.
            max_chars: Override for self.tool_result_max_chars.
        """
        if not result:
            return result or ''

        limit = max_chars if max_chars is not None else self.tool_result_max_chars
        if len(result) <= limit:
            return result

        # Preserve structure: head (70%) + truncation notice + tail (15%)
        head_size = int(limit * 0.70)
        tail_size = int(limit * 0.15)
        # Ensure we don't overshoot
        marker = (
            f'\n\n... [TRUNCATED: showing {head_size:,} + {tail_size:,} of '
            f'{len(result):,} total chars. Use more specific queries to narrow results.]\n\n'
        )
        available = limit - len(marker)
        if available < 100:
            # Very small limit — just hard truncate
            return result[:limit]

        head_size = int(available * 0.82)
        tail_size = available - head_size

        return result[:head_size] + marker + result[-tail_size:]

    # ─────────────────────────────────────────────────
    #  Cleanup
    # ─────────────────────────────────────────────────

    def _cleanup(self):
        """Release resources after agent execution.

        Called automatically at the end of run(). Safe to call multiple times.
        Truncates the message history to free memory while keeping the
        result intact for the caller.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True

        # Release the per-task extended-TTL latch keyed on agent_id (set in
        # run()), mirroring orchestrator._finalize_and_emit_done — otherwise
        # _ttl_latch leaks one entry per sub-agent for the process lifetime.
        try:
            from lib.tasks_pkg.cache_tracking import release_ttl_latch
            release_ttl_latch(self.agent_id)
        except Exception as _e:
            logger.debug('[Agent:%s] TTL latch release skipped: %s',
                         self.agent_id, _e)

        # Compact message history — keep only system, first user, and last 2 messages
        # This frees memory from potentially large tool results
        if len(self.messages) > 6:
            # Keep: system prompt + first user msg + last 2 messages
            self.messages = self.messages[:2] + self.messages[-2:]

        logger.info(
            '[Agent:%s] Cleanup complete — status=%s answer_len=%d tokens=%d rounds=%d artifacts_w=%s artifacts_r=%s',
            self.agent_id, self.result.status,
            len(self.result.final_answer or ''),
            self.result.total_tokens,
            self.result.rounds_used,
            self.result.artifacts_written,
            self.result.artifacts_read,
        )
