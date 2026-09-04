"""lib/swarm/master.py — Async master orchestrator (no review, no synthesis).

Lifecycle:

  1. ``MasterOrchestrator(...)`` — created by ``integration._handle_spawn_agents``.
  2. ``run_in_background()``     — kicks the StreamingScheduler off in a daemon
     thread; returns immediately. Sub-agent completions:
       a. fire ``on_progress`` UI events as before, AND
       b. enqueue a ``<swarm-update>`` payload into ``lib.agent_inbox``
          for the main agent's next round.
  3. ``await_agents(...)``       — block until ≥1 / all listed agents
     complete, or timeout. Returns the same payloads it would have
     pushed to the inbox, plus a still-running list.
  4. ``get_agent_result(id)``    — fetch one agent's full final answer.
  5. ``abort()``                  — best-effort cancel.

There is NO master-review LLM call, NO synthesis LLM call, NO
``run_reactive``. The main agent IS the master.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable

from lib.agent_core.task_runtime import _epoch_ms
from lib.agent_inbox import consume as inbox_consume
from lib.agent_inbox import enqueue as inbox_enqueue
from lib.agent_inbox import format_swarm_update
from lib.log import get_logger
from lib.swarm.agent import SubAgent
from lib.swarm.registry import get_role_model_hint, resolve_model_for_tier
from lib.swarm.protocol import (
    ArtifactStore,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
)
from lib.swarm.rate_limiter import RateLimiter
from lib.swarm.resource_policy import (
    swarm_max_agents_per_session,
    swarm_max_parallel,
    swarm_max_retries,
)
from lib.swarm.liveness import ProgressBeacon
from lib.swarm.presentation_budget import (
    SWARM_TOOL_ARGS_BRIEF_CHARS,
    SWARM_TOOL_NAME_CHARS,
    SWARM_TOOL_TIMELINE_DETAIL_CHARS,
    SWARM_TOOL_TIMELINE_JSON_BYTES,
    SWARM_TOOL_TIMELINE_ROW_LIMIT,
)
from lib.swarm.scheduler import StreamingScheduler

logger = get_logger(__name__)


#: Minimum seconds between DEDICATED full-blob snapshot CAS writes (the
#: write-amplification guard for FUSE-mounted DBs — a 10-agent swarm settling
#: in a ~1s burst would otherwise do ~10 full read-modify-writes of the whole
#: conversations.messages blob, ~10ms each on beegfs, contending with the
#: frontend's own sync). Incremental per-agent writes are coalesced to this
#: cadence; the final SETTLE write is always forced (carries final truth).
#: Env-overridable. The cheap in-memory live-task stamp is NEVER throttled.
try:
    _SNAPSHOT_CAS_MIN_INTERVAL_S = float(
        os.environ.get('TOFU_SWARM_SNAPSHOT_MIN_INTERVAL', '8'))
except (TypeError, ValueError) as e:
    logger.debug('[Swarm] TOFU_SWARM_SNAPSHOT_MIN_INTERVAL parse failed, using default: %s', e)
    _SNAPSHOT_CAS_MIN_INTERVAL_S = 8.0

#: Tool names that mutate files on disk. Used to flag sub-agents that
#: modified the workspace so the UI can mark them for closer review.
#: ``run_command`` is intentionally excluded — it CAN write but is also
#: used for read-only commands (git status, tests), so counting it would
#: over-flag. We count only the unambiguous write tools.
_FILE_WRITE_TOOLS = frozenset({
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})


def _last_assistant_text(messages: list) -> str:
    """Return the last substantive assistant text in a message list.

    Fallback for a rehydrated completed agent whose stored result dict was
    empty (older checkpoint) — mirrors SubAgent._extract_partial_answer.
    """
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get('role') == 'assistant':
            content = msg.get('content')
            if isinstance(content, str) and len(content.strip()) > 20:
                return content.strip()
    return ''


def _count_file_writes(tool_log: list) -> int:
    """Count file-mutating tool calls in a sub-agent's ``tool_log``.

    Each tool_log entry is ``{round, tool, args_brief, timestamp}``; we
    tally entries whose ``tool`` is one of ``_FILE_WRITE_TOOLS``. A batch
    tool (apply_diffs / insert_contents) counts as one call here — the
    point is to flag "this agent touched files", not exact edit count.
    """
    if not tool_log:
        return 0
    return sum(1 for e in tool_log
               if isinstance(e, dict) and e.get('tool') in _FILE_WRITE_TOOLS)


#: Compatibility aliases retained for focused contract tests and diagnostics.
_SNAPSHOT_TOOLCALLS_CAP = SWARM_TOOL_TIMELINE_ROW_LIMIT
_SNAPSHOT_TOOL_TIMELINE_BYTES = SWARM_TOOL_TIMELINE_JSON_BYTES


def _timeline_json_bytes(tool_calls: list[dict]) -> int:
    """Return a conservative serialized size for a durable tool timeline.

    ``ensure_ascii=True`` upper-bounds ordinary compact UTF-8 JSON for CJK and
    astral characters as well as ASCII.  This makes the ceiling independent of
    which JSON adapter eventually writes the projection.
    """
    return len(json.dumps(
        tool_calls, ensure_ascii=True, separators=(',', ':')))


def _timeline_text(value) -> str:
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)


def _declared_full_chars(entry: dict, field: str, text: str) -> int:
    """Preserve producer truncation evidence while tolerating legacy rows."""
    try:
        declared = int(entry.get(f'{field}_full_chars') or 0)
    except (TypeError, ValueError):
        declared = 0
    return max(len(text), declared)


def _bounded_timeline_call(entry: dict, name: str) -> dict:
    """Project one checkpoint row into a bounded, truthfully marked UI row."""
    raw_args = _timeline_text(entry.get('args_brief'))
    args_brief = raw_args[:SWARM_TOOL_ARGS_BRIEF_CHARS]
    call = {
        'toolName':  name[:SWARM_TOOL_NAME_CHARS],
        'argsBrief': args_brief,
        'status':    'done',
        'preview':   '',
        'error':     '',
    }
    if len(args_brief) < len(raw_args):
        call['argsBriefTruncated'] = True
        call['argsBriefFullChars'] = len(raw_args)

    for field in ('preview', 'error'):
        raw_text = _timeline_text(entry.get(field))
        full_chars = _declared_full_chars(entry, field, raw_text)
        visible = raw_text[:SWARM_TOOL_TIMELINE_DETAIL_CHARS]
        call[field] = visible
        if full_chars:
            call[f'{field}FullChars'] = full_chars
            call[f'{field}Truncated'] = bool(
                entry.get(f'{field}_truncated')
                or len(visible) < full_chars)
    return call


def _fit_timeline_byte_budget(tool_calls: list[dict], omitted: int):
    """Prefer recent detail, then recent rows, under the hard JSON ceiling."""
    calls = list(tool_calls)
    while calls and _timeline_json_bytes(calls) > SWARM_TOOL_TIMELINE_JSON_BYTES:
        stripped = False
        # Oldest auxiliary detail is least valuable.  Keep the row identity so
        # the user still sees that the tool ran, and mark every elision.
        for call in calls:
            for field in ('preview', 'error', 'argsBrief'):
                text = call.get(field)
                if not text:
                    continue
                full_key = f'{field}FullChars'
                truncated_key = f'{field}Truncated'
                call[full_key] = max(int(call.get(full_key) or 0), len(text))
                call[truncated_key] = True
                call[field] = ''
                stripped = True
                break
            if stripped:
                break
        if not stripped:
            calls.pop(0)
            omitted += 1
    return calls, omitted


def _snapshot_tool_timeline(tool_log: list):
    """Derive the panel's tool timeline from a sub-agent's ``tool_log``.

    The live ``_swarmAgents`` array carries both an aggregate ``tools`` name
    list and a per-call ``_toolCalls`` timeline (synthesized frontend-side from
    ``swarm_agent_tool_call`` SSE events). Neither survives reload, so the
    recovered panel showed no tools at all. Rebuild both from the persisted
    ``tool_log`` (``[{round, tool, args_brief, timestamp}, ...]``).

    Returns ``(tools, tool_calls, omitted)`` where ``tools`` is the unique tool-name
    list (order preserved) and ``tool_calls`` matches the frontend row shape
    ``{toolName, argsBrief, status}``. ``omitted`` counts older rows excluded
    by the row/byte budgets. ``tool_log`` records only calls that completed, so
    every row is ``status='done'``.
    """
    if not tool_log:
        return [], [], 0
    tools: list = []
    tool_calls: list = []
    for e in tool_log:
        if not isinstance(e, dict):
            continue
        name = _timeline_text(e.get('tool'))
        if not name:
            continue
        name = name[:SWARM_TOOL_NAME_CHARS]
        if name not in tools:
            tools.append(name)
        tool_calls.append(_bounded_timeline_call(e, name))
    omitted = max(0, len(tool_calls) - _SNAPSHOT_TOOLCALLS_CAP)
    if len(tool_calls) > _SNAPSHOT_TOOLCALLS_CAP:
        tool_calls = tool_calls[-_SNAPSHOT_TOOLCALLS_CAP:]
    tool_calls, omitted = _fit_timeline_byte_budget(tool_calls, omitted)
    return tools, tool_calls, omitted


# ═══════════════════════════════════════════════════════════
#  Sub-agent factory (replaces lib/swarm/compat.py:spawn_sub_agent)
# ═══════════════════════════════════════════════════════════

def _build_sub_agent(spec: SubTaskSpec, *,
                     parent_task: dict,
                     all_tools: list,
                     model: str,
                     thinking_enabled: bool,
                     on_event: Callable | None,
                     abort_check: Callable | None,
                     project_path: str,
                     artifact_store: ArtifactStore | None,
                     output_file: str = '',
                     swarm_key: str = '') -> SubAgent:
    """Construct a SubAgent. Does not start execution."""
    logger.info('[Master:Spawn] id=%s role=%s model=%s objective=%.80s deps=%s',
                spec.id, spec.role, model or '(default)',
                spec.objective, spec.depends_on or [])
    agent = SubAgent(
        spec,
        parent_task=parent_task,
        all_tools=all_tools,
        model=model,
        thinking_enabled=thinking_enabled,
        on_event=on_event,
        abort_check=abort_check,
        project_path=project_path,
        artifact_store=artifact_store,
    )
    if output_file:
        # SubAgent itself writes streaming output to ``output_file`` when
        # the attribute is present. See lib/swarm/agent.py for the hook.
        agent.output_file = output_file
    if swarm_key:
        # Enables the SubAgent's round-boundary DB checkpoint (durable resume).
        agent.swarm_key = swarm_key
    return agent


# ═══════════════════════════════════════════════════════════
#  MasterOrchestrator
# ═══════════════════════════════════════════════════════════

class MasterOrchestrator:
    """Owns one swarm session for one main task.

    Lifetime is bounded by the task's lifetime; ``integration.py`` registers
    the instance in a per-task session table and removes it when the task
    ends.
    """

    def __init__(self, task_id: str, conv_id: str, specs: list[SubTaskSpec], *,
                 user_id,
                 project_path: str = '',
                 model: str = '',
                 thinking_enabled: bool = True,
                 search_mode: str = 'multi',
                 on_progress: Callable | None = None,
                 abort_check: Callable | None = None,
                 all_tools: list | None = None,
                 max_parallel: int = 8,
                 max_retries: int = 1,
                 output_dir: str = '',
                 parent_config: dict | None = None,
                 inbox_key: str = '',
                 on_settled: Callable | None = None):
        self.task_id = task_id
        self.conv_id = conv_id
        self.user_id = user_id
        # Fired ONCE from the driver thread when the swarm terminates (all
        # agents done / aborted). ``integration`` uses it to auto-continue the
        # main agent when the spawning turn already ended but pending
        # <swarm-update>s would otherwise sit unread in the inbox. Must never
        # raise into the driver loop.
        self.on_settled = on_settled
        # The model-facing inbox is keyed by the STABLE swarm key (conv id
        # when available) so <swarm-update>s enqueued by background agents
        # survive into later turns of the same conversation. Defaults to
        # conv_id, then task_id. ``integration._handle_spawn_agents`` passes
        # this explicitly via ``swarm_key_for(task)``.
        self.inbox_key = inbox_key or conv_id or task_id
        self.specs = list(specs)
        self.project_path = project_path
        self.model = model
        self.thinking_enabled = thinking_enabled
        self.search_mode = search_mode
        self.on_progress = on_progress
        self.abort_check = abort_check or (lambda: False)
        self.all_tools = all_tools or []
        parallel_ceiling = swarm_max_parallel()
        retry_ceiling = swarm_max_retries()
        try:
            self.max_parallel = max(
                1, min(int(max_parallel), parallel_ceiling))
        except (TypeError, ValueError, OverflowError):
            self.max_parallel = parallel_ceiling
        try:
            self.max_retries = max(
                0, min(int(max_retries), retry_ceiling))
        except (TypeError, ValueError, OverflowError):
            self.max_retries = retry_ceiling
        self.max_total_agents = swarm_max_agents_per_session()

        # Per-agent streaming output directory. Each sub-agent will stream
        # its raw tokens to ``<output_dir>/<agent_id>.log`` so the main
        # agent can fetch progress with a regular file read if it explicitly
        # wants to. The directory is created lazily by SubAgent on its first
        # real streamed chunk; no-output sessions leave no empty inode behind.
        self.output_dir = output_dir

        logger.info('[Master:%s] Init — %d specs model=%s parallel=%d retries=%d output_dir=%s',
                    task_id, len(specs), model or '(default)',
                    self.max_parallel, self.max_retries,
                    self.output_dir or '(none)')
        for i, s in enumerate(specs):
            logger.debug('[Master:%s]   Spec[%d] id=%s role=%s deps=%s obj=%.120s',
                         task_id, i, s.id, s.role, list(s.depends_on or []),
                         s.objective)

        # Shared state
        self.artifact_store = ArtifactStore()
        self.rate_limiter = RateLimiter(
            max_concurrent=self.max_parallel,
            owner_key=user_id,
        )

        self._agents: dict[str, SubAgent] = {}
        self._results: list[tuple[SubTaskSpec, SubAgentResult]] = []
        self._results_by_id: dict[str, tuple[SubTaskSpec, SubAgentResult]] = {}
        self._lock = threading.Lock()
        self._aborted = False
        # Internal cancellation used when the shared liveness beacon has
        # positively classified the swarm as stalled. Keep it separate from
        # _aborted so durable/UI verdicts remain "stalled", not the lie that a
        # user clicked Stop. SubAgent tool proxies observe both signals.
        self._stop_stalled = False

        # Throttle for the DEDICATED full-blob snapshot CAS write (write-amp
        # guard on FUSE-mounted DBs). The cheap in-memory live-task stamp runs
        # on EVERY agent-complete and rides the regular ~10s checkpoint loop;
        # the expensive read-modify-write of the whole conversations.messages
        # blob is coalesced to at most once per interval — EXCEPT the settle
        # write, which is always forced (it carries final truth, so any
        # throttled-out incremental is covered by it). See _persist_agent_snapshot.
        # -inf, not 0.0: time.monotonic() counts from BOOT, so 0.0 means
        # "written at boot" and a swarm started within the first interval
        # after a machine boot (CI runners start jobs at uptime <1000s —
        # 70479ac unit leg) would wrongly have its FIRST write throttled out.
        self._last_snapshot_cas = float('-inf')
        self._snapshot_cas_lock = threading.Lock()

        # Listeners woken when ANY agent completes (used by await_agents).
        self._completion_event = threading.Event()
        # One no-progress timeout per logical wait-set is enough. Models often
        # retry an identical await immediately; without this receipt the second
        # call burns the entire hard cap despite learning no new fact. Values
        # are the completed-id snapshot observed at the last timeout. The map is
        # session-local and explicitly bounded.
        self._await_timeout_progress: dict[
            tuple[str, tuple[str, ...]], frozenset[str]
        ] = {}
        self._await_timeout_progress_capacity = 64
        # Results already shown to the parent model, either synchronously by
        # await/get_result or asynchronously through an inbox injection. This
        # set is bounded by the finite session spec list. Explicit-id reads may
        # still replay a result; no-id awaits consume only completion deltas.
        self._delivered_result_ids: set[str] = set()

        # Parent task proxy used by SubAgent for tool dispatch. Most fields
        # are placeholders, but ``config`` MUST carry through fields that
        # affect tool routing (notably ``browserClientId`` for per-client
        # playwright pool selection).  Without it, sub-agents that use
        # browser tools would silently fall back to the default pool and
        # ignore the parent's per-client routing.
        self._parent_task_proxy = {
            'id':           task_id,
            'convId':       conv_id,
            '_userId':      user_id,
            '_tenant_id':   (parent_config or {}).get('_tenant_id'),
            'events_lock':  threading.Lock(),
            'events':       [],
            'toolRounds':   [],
            'phase':        'tool',
            'config':       dict(parent_config or {}),
        }

        self._scheduler: StreamingScheduler | None = None
        self._driver_thread: threading.Thread | None = None
        self._terminated = False  # set when driver thread exits
        #: Set when the driver loop crashed (``_driver`` except branch). The
        #: terminal ``swarm_phase`` emission carries it so the panel renders
        #: Failed-with-reason instead of a green Complete over crashed work.
        self._driver_error: str | None = None

        #: THE shared liveness fact for this swarm (lib/swarm/liveness.py).
        #: Handed BY REFERENCE to the scheduler and to every SubAgent, and read
        #: by the session-TTL sweep via the ``progress_beacon`` property below,
        #: so all four components answer "is this still working?" from ONE
        #: record. Three private clocks that never compared notes is exactly
        #: what abandoned a running agent at 600s while it went on to finish.
        self._beacon = ProgressBeacon()

        #: spec_id → {'silent_seconds', 'note'} for agents the beacon JUDGED
        #: stalled when the driver exited. Harvested once in the driver's
        #: finally, consumed by ``_build_agent_snapshot`` so the panel can say
        #: "已停滞 · 静默 Ns" instead of coercing a judged agent into the
        #: 'unknown' → 无结果 bucket (which is now reserved for "never
        #: started / never produced"). Entries are dropped on completion —
        #: a stalled agent that comes back self-heals to done (measured in
        #: production: the smoke agent returned at 1010s and completed).
        self._stalled_agents: dict[str, dict] = {}

        # Rehydration: agent_id → persisted ``messages`` array. When set
        # (by ``rehydrate_in_background``), ``_make_agent`` seeds the new
        # SubAgent's conversation from this checkpoint instead of building a
        # fresh initial-message list — that's what makes a resumed agent
        # continue mid-conversation rather than restart from scratch.
        self._resume_messages: dict[str, list] = {}
        # Agent id -> number of rounds completed by its durable checkpoint.
        # ``SubAgent`` runs on a generic chassis whose local counter always
        # starts at zero, so resume must restore this independently of the
        # message payload.
        self._resume_rounds: dict[str, int] = {}

    # ── Model resolution (mirrors SubAgent._resolve_model) ──

    def _resolve_spec_model(self, spec: SubTaskSpec) -> str:
        """Resolve the concrete model a spec will run on, for UI display.

        Mirrors ``SubAgent._resolve_model`` (spec override → role tier
        hint → parent default) so the swarm panel can show the actual
        model BEFORE the SubAgent is constructed (the start event fires
        ahead of the agent factory).
        """
        if spec.model_override:
            return spec.model_override
        tier = get_role_model_hint(spec.role)
        provider_id = self._parent_task_proxy['config'].get(
            '_pinned_provider_id', '')
        return resolve_model_for_tier(
            tier,
            self.model,
            role=spec.role,
            provider_id=provider_id,
            owner_user_id=self.user_id,
            tenant_id=self._parent_task_proxy.get('_tenant_id'),
        )

    # ── Agent factory used by StreamingScheduler ──────

    def _make_agent(self, spec: SubTaskSpec) -> SubAgent:
        out_path = ''
        if self.output_dir:
            out_path = os.path.join(self.output_dir, f'{spec.id}.log')
        agent = _build_sub_agent(
            spec,
            parent_task=self._parent_task_proxy,
            all_tools=self.all_tools,
            model=self.model,
            thinking_enabled=self.thinking_enabled,
            on_event=self.on_progress,
            abort_check=lambda: (
                self._aborted or self._stop_stalled or self.abort_check()),
            project_path=self.project_path,
            artifact_store=self.artifact_store,
            output_file=out_path,
            swarm_key=self.inbox_key,
        )
        # ── Durable resume: seed the conversation from the checkpoint so the
        #    agent continues from its last completed round instead of starting
        #    over. The interrupted (uncheckpointed) round is naturally re-run.
        resume = self._resume_messages.get(spec.id)
        if resume:
            resumed = [dict(m) if isinstance(m, dict) else m for m in resume]
            initial = getattr(agent, 'messages', None) or []
            fresh_system = (dict(initial[0])
                            if initial and isinstance(initial[0], dict)
                            and initial[0].get('role') == 'system' else None)
            # Checkpoints preserve dialogue, not stale authority. Tool schemas,
            # role policy, and security wording can change between processes;
            # retaining the old system prompt let a resumed browser worker
            # keep following a withdrawn browser_execute_js promise and route
            # around it with run_command. Refresh only the system carrier and
            # leave every user/assistant/tool turn byte-for-byte intact.
            if fresh_system:
                if (resumed and isinstance(resumed[0], dict)
                        and resumed[0].get('role') == 'system'):
                    resumed[0] = fresh_system
                else:
                    resumed.insert(0, fresh_system)
            agent.messages = resumed
        resume_rounds = max(0, int(self._resume_rounds.get(spec.id, 0) or 0))
        if resume or resume_rounds:
            agent._round_offset = resume_rounds
            # Honest state if an abort/stall lands before the first resumed
            # dispatch. A real dispatch advances this to offset + local round.
            if getattr(agent, 'result', None) is not None:
                agent.result.rounds_used = resume_rounds
            logger.info('[Master:%s] Rehydrated agent %s from checkpoint '
                        '(%d msgs, %d completed rounds)',
                        self.task_id, spec.id, len(resume or []), resume_rounds)
        # Share THE liveness record with this agent: every token / tool call it
        # makes must land on the same beacon the driver and the TTL sweep read,
        # otherwise we would just have created a fourth private clock.
        agent.progress_beacon = self._beacon
        with self._lock:
            self._agents[spec.id] = agent
        return agent

    # ── Durable agent snapshot (reload-faithful swarm panel) ──────────

    def _build_agent_snapshot(self) -> dict:
        """Build the durable per-agent snapshot for the swarm panel.

        Mirrors the per-agent fields the live ``_swarmAgents`` array carries
        (synthesized frontend-side from ``swarm_*`` SSE events) so the reload
        path can render an identical, fully-expandable panel WITHOUT the live
        array or any ``await_agents`` sibling round.

        Sourced from the authoritative ``_results_by_id`` (completed/failed
        agents) and the scheduler's running/pending sets — so a fire-and-forget
        swarm that was never awaited still gets real per-agent status, not the
        ``unknown`` stubs the handle-only recovery produced.
        """

        with self._lock:
            results = dict(self._results_by_id)
            running_ids = (set(self._scheduler._running.keys())
                           if self._scheduler else set())
            pending_ids = ({s.id for s in self._scheduler._pending}
                           if self._scheduler else set())
            # Wall-clock launch instants for the agents still in flight. This
            # is the ONLY source for a running agent's start: its result (and
            # thus `elapsed`) does not exist yet.
            started_at = (self._scheduler.started_at_map()
                          if self._scheduler else {})
            specs = list(self.specs)
            terminated = self._terminated

        agents: list[dict] = []
        total_tokens = 0
        for spec in specs:
            pair = results.get(spec.id)
            if pair is not None:
                _, result = pair
                # Normalise the SubAgentStatus value ('completed'/'failed') to
                # the frontend vocabulary ('done'/'failed').
                status = ('done'
                          if result.status == SubAgentStatus.COMPLETED.value
                          else 'failed' if result.status == SubAgentStatus.FAILED.value
                          else result.status)
                total_tokens += result.total_tokens or 0
                _tools, _tool_calls, _tool_calls_omitted = (
                    _snapshot_tool_timeline(result.tool_log))
                agents.append({
                    'id':            spec.id,
                    'role':          spec.role,
                    'model':         (getattr(self._agents.get(spec.id), 'model', '')
                                      or self._resolve_spec_model(spec)),
                    'objective':     spec.objective,
                    'status':        status,
                    'elapsed':       round(result.elapsed_seconds, 1),
                    'tokens':        result.total_tokens,
                    # FULL final answer — the durable snapshot is the
                    # authoritative terminal record a reloaded panel renders
                    # from, so slicing here would permanently destroy the tail
                    # of every sub-agent result. The panel shows it complete.
                    'preview':       (result.final_answer or ''),
                    'modifiedFiles': _count_file_writes(result.tool_log),
                    'tools':         _tools,
                    'toolCalls':     _tool_calls,
                    'toolCallsOmitted': _tool_calls_omitted,
                    'error':         (result.error_message or '')
                                     if result.status != SubAgentStatus.COMPLETED.value
                                     else '',
                })
            else:
                # No result yet. If the swarm has TERMINATED this agent is
                # stranded — it never produced a result, so it must NOT be
                # frozen as 'running'/'pending' under settled:true (a stopped
                # swarm would otherwise persist agents "Running" forever).
                # Coerce to 'aborted' when the swarm was explicitly aborted,
                # else 'unknown'. Only a still-live swarm reflects running/pending.
                if terminated:
                    # Precedence: an explicit ABORT outranks a stall verdict
                    # (the user killed it — that is the honest label), and a
                    # stall verdict outranks 'unknown' (the beacon JUDGED it;
                    # 'unknown' → 无结果 is now reserved for "never started /
                    # never produced", so the panel can always say WHY).
                    if self._aborted:
                        live = 'aborted'
                    elif spec.id in self._stalled_agents:
                        live = 'stalled'
                    else:
                        live = 'unknown'
                elif spec.id in running_ids:
                    live = 'running'
                elif spec.id in pending_ids:
                    live = 'pending'
                else:
                    live = 'pending'
                agents.append({
                    'id':            spec.id,
                    'role':          spec.role,
                    'model':         self._resolve_spec_model(spec),
                    'objective':     spec.objective,
                    'status':        live,
                    'elapsed':       '',
                    'tokens':        0,
                    'preview':       '',
                    'modifiedFiles': 0,
                    'error':         '',
                    # The live stopwatch's anchor. The frontend renders a
                    #   per-agent timer only while `status` is running AND it
                    #   has a start; minting that start client-side meant a
                    #   reload DROPPED THE TIMER NODE ENTIRELY for an agent
                    #   that was still working (`elapsed` only exists once the
                    #   agent finishes, so the fallback could not cover it).
                    #   Epoch MILLISECONDS via the shared _epoch_ms seam — the
                    #   same wire unit as every other clock in this codebase;
                    #   seconds here would silently render a ~50-year elapsed.
                    #   None for pending/unknown agents: not started yet, so
                    #   there is no honest instant to report.
                    'startedAt':     (_epoch_ms(started_at.get(spec.id))
                                      if live == 'running' else None),
                    # Stall evidence (only meaningful when live == 'stalled'):
                    # the panel renders 「已停滞 · 静默 Ns」 from these.
                    'stallSilentSeconds': (
                        self._stalled_agents[spec.id]['silent_seconds']
                        if live == 'stalled' else None),
                    'stallNote': (
                        self._stalled_agents[spec.id].get('note') or ''
                        if live == 'stalled' else ''),
                })

        # ── Monotonic version key (#2) ──
        # A later snapshot must never be clobbered by an earlier one that
        # loses a CAS race and retries late. Order by (settled, terminal-agent
        # count): settled always outranks unsettled, and among unsettled the
        # one that has resolved MORE agents is newer. persist_snapshot_to_conversation
        # refuses to overwrite a higher version with a lower one.
        done_count = sum(1 for a in agents
                         if a['status'] in ('done', 'failed', 'aborted'))
        version = (1 if terminated else 0) * 100000 + done_count
        return {
            'agents':       agents,
            'settled':      terminated,
            'totalTokens':  total_tokens,
            'agentCount':   len(agents),
            'doneCount':    done_count,
            'version':      version,
        }

    def _persist_agent_snapshot(self, *, force: bool = False,
                                cas: bool = True) -> None:
        """Write the current agent snapshot durably onto the spawn round.

        Two coordinated projections keep the panel current:
          1. Stamp the live task's spawn round while its attempt owns the turn.
             The normal turn-event bridge folds that state into the live
             projection.
          2. CAS-patch settled Sidecar turn projections for detached
             fire-and-forget completions. These updates are throttled to one
             per ``_SNAPSHOT_CAS_MIN_INTERVAL_S``; ``force=True`` always
             writes the final snapshot.

        ``cas=False`` only stamps the attempt-owned live projection. It is
        used at agent start, before the spawn round can exist in a settled turn.

        Best-effort; never raises into the driver / scheduler thread.
        """
        try:
            snapshot = self._build_agent_snapshot()
            agent_ids = [a['id'] for a in snapshot.get('agents') or []]
            if not agent_ids:
                return
            from lib.swarm import snapshot as _snap

            agent_id_set = set(agent_ids)

            # (1) Stamp the live task's spawn round(s), if reachable. The stamp
            #     happens inside TaskRuntime's transaction boundary so two swarm driver threads can't
            #     mutate the same round concurrently. The cross-thread race
            #     with executor checkpoints is closed on the other side:
            #     _merge_tool_rounds returns independent round dictionaries.
            #
            #     #4: a follow-up wave merges both waves' specs into ONE
            #     snapshot, so we stamp EACH matching spawn round with only the
            #     agents ITS handle launched (filter_snapshot) — never the
            #     combined set onto one panel.
            try:
                from lib.tasks_pkg.manager.runtime import chat_task_runtime

                def _stamp_task(task):
                    for round_record in task.get('toolRounds') or []:
                        handle_ids = (
                            _snap._round_handle_ids(round_record) & agent_id_set
                        )
                        if handle_ids:
                            _snap.stamp_round(
                                round_record,
                                _snap.filter_snapshot(snapshot, handle_ids),
                            )

                chat_task_runtime.update_matching(
                    predicate=lambda task: (
                        task.get('convId') == self.conv_id
                        and int(task.get('_userId') or 0) == int(self.user_id)
                    ),
                    updater=_stamp_task,
                )
            except Exception as e:
                logger.debug('[Master:%s] live-task snapshot stamp skipped: %s',
                             self.task_id, e)

            # (2) Durable CAS write into settled turn projections (covers the
            #     detached fire-and-forget case and multi-wave scoping).
            #     THROTTLED unless
            #     forced: a per-agent burst coalesces to one write per interval;
            #     the monotonic version guard (snapshot.stamp_round) makes a
            #     skipped incremental harmless — the next write (or the forced
            #     settle) carries the strictly-newer state.
            if not cas:
                return
            now = time.monotonic()
            with self._snapshot_cas_lock:
                due = force or (now - self._last_snapshot_cas
                                >= _SNAPSHOT_CAS_MIN_INTERVAL_S)
                if due:
                    self._last_snapshot_cas = now
            if not due:
                logger.debug('[Master:%s] snapshot CAS throttled (%.1fs since '
                             'last, interval=%.0fs) — live stamp kept, projection '
                             'deferred', self.task_id,
                             now - self._last_snapshot_cas,
                             _SNAPSHOT_CAS_MIN_INTERVAL_S)
                return
            _snap.persist_snapshot_to_conversation(
                self.conv_id, agent_ids, snapshot, user_id=self.user_id)
        except Exception as e:
            logger.warning('[Master:%s] agent snapshot persist failed: %s',
                           self.task_id, e, exc_info=True)

    # ── Scheduler callbacks ────────────────────────────

    def _on_agent_start_callback(self, spec: SubTaskSpec) -> None:
        # Echo to app.log using the same `agent-{role}-{id}` token the
        # SubAgent itself logs with — lets a user copy the ID chip from
        # the UI swarm panel and grep every transition in one shot.
        logger.info('[Master:%s] AGENT_START agent-%s-%s role=%s objective=%.120s',
                    self.task_id, spec.role, spec.id, spec.role, spec.objective)
        # Roster snapshot at agent START (live-task stamp only): the first
        # completion-time snapshot can be minutes away, and a reload in that
        # window recovered handle-only stubs (every agent 'unknown'). The
        # stamp rides the turn's next regular checkpoint to disk, so a
        # mid-run reload rebuilds real pending/running statuses instead.
        self._persist_agent_snapshot(cas=False)
        if self.on_progress:
            # NOTE: ``objective`` is sent to the UI agent card and is
            # rendered with CSS wrapping, so do NOT truncate here — the
            # user wants to see the full text. ``content`` is the
            # status-line preview (≤60 chars is appropriate there).
            self.on_progress({
                'type': 'swarm_agent_phase', 'phase': 'running',
                'content': f'▶️ Starting [{spec.role}]: {spec.objective[:60]}',
                'agentId': spec.id, 'role': spec.role,
                'objective': spec.objective,
                'model': self._resolve_spec_model(spec),
            })

    def _on_agent_complete_callback(self, spec: SubTaskSpec,
                                     result: SubAgentResult) -> None:
        logger.info('[Master:%s] AGENT_COMPLETE agent-%s-%s status=%s elapsed=%.1fs tokens=%d rounds=%d',
                    self.task_id, spec.role, spec.id,
                    result.status, result.elapsed_seconds,
                    result.total_tokens, result.rounds_used)
        # 1) UI event (kept identical to legacy schema for the swarm panel)
        # ``objective`` is the agent-card body — full text, no truncation.
        # ``summary`` is the agent card's RESULT body. It carries the FULL
        # final answer, for the same reason ``_build_agent_snapshot`` does:
        # the panel is a debugging surface and CSS owns the visual bounding
        # (``.sw-a-preview`` scrolls), not a backend slice. A cap here was the
        # live/reload divergence users hit — the durable snapshot showed the
        # whole answer after F5 while the LIVE panel stopped mid-sentence,
        # which reads as "the sub-agent produced a truncated result".
        # Wire economy does not argue for a cap: this fires ONCE per agent
        # (unlike the per-tool-call frame, which stays bounded by
        # ``agent._SSE_TOOL_PREVIEW_CHARS`` because it fires per call).
        modified_files = _count_file_writes(result.tool_log)
        # Error transparency: a failed agent's reason MUST reach the panel —
        # previously only the model-facing inbox payload carried it, so the UI
        # card showed a bare ❌ with an empty body and no way to say WHY.
        _agent_error = ((result.error_message or '')
                        if result.status != SubAgentStatus.COMPLETED.value else '')
        if self.on_progress:
            self.on_progress({
                'type':      'swarm_agent_complete',
                'agentId':   spec.id,
                'role':      spec.role,
                'objective': spec.objective,
                'model':     (getattr(self._agents.get(spec.id), 'model', '')
                              or self._resolve_spec_model(spec)),
                'status':    result.status,
                'elapsed':   round(result.elapsed_seconds, 1),
                'tokens':    result.total_tokens,
                'summary':   (result.final_answer or ''),
                'error':     _agent_error,
                # Number of file-mutating tool calls this agent made.
                #   Surfaced in the UI so agents that edited the workspace
                #   are flagged for closer review.
                'modifiedFiles': modified_files,
                'content': (
                    f'{"✅" if result.status == SubAgentStatus.COMPLETED.value else "❌"} '
                    f'[{spec.role}] Done in {result.elapsed_seconds:.1f}s'
                ),
            })

        # 2) Record + signal waiters
        with self._lock:
            self._results.append((spec, result))
            self._results_by_id[spec.id] = (spec, result)
            # Scheduler publishes this callback before removing the current
            # id from ``_running`` so its driver cannot outrun master result
            # publication. Exclude the current completion from the remaining
            # count surfaced to the inbox/UI.
            running = (max(0, self._scheduler.running_count - 1)
                       if self._scheduler else 0)
            pending = self._scheduler.pending_count if self._scheduler else 0
            # A result EXISTS for this agent now — it was never stuck, just
            # slow. Drop the stall verdict so the next snapshot self-heals to
            # done (production smoke: judged stalled at 900s, completed at
            # 1010s, card became a real completion).
            if self._stalled_agents.pop(spec.id, None) is not None:
                logger.info('[Master:%s] agent %s recovered after a stall '
                            'verdict — snapshot self-heals to done',
                            self.task_id, spec.id)
        self._completion_event.set()

        # 3) Push <swarm-update> to the model inbox so the main agent sees it
        #    on its next round. Errors here must NEVER kill the master thread.
        try:
            output_file = ''
            if self.output_dir:
                output_file = os.path.join(self.output_dir, f'{spec.id}.log')
            payload = format_swarm_update(
                agent_id=spec.id,
                role=spec.role,
                status=result.status,
                elapsed_seconds=result.elapsed_seconds,
                tokens=result.total_tokens,
                preview=result.final_answer or '',
                output_file=output_file,
                remaining_running=running,
                remaining_pending=pending,
                error=(result.error_message or '') if result.status != SubAgentStatus.COMPLETED.value else '',
            )
            inbox_enqueue(self.inbox_key, payload,
                          priority='later',
                          mode='swarm-update',
                          agent_id=spec.id)
        except Exception as e:
            logger.error('[Master:%s] Failed to enqueue swarm-update for agent=%s: %s',
                         self.task_id, spec.id, e, exc_info=True)

        # 4) Durably snapshot the swarm onto the spawn round so a reload (even
        #    one with no live _swarmAgents array and no await_agents sibling)
        #    rebuilds a faithful, fully-expandable panel. Incremental: each
        #    completion advances the persisted per-agent state.
        self._persist_agent_snapshot()

    def _on_retry_callback(self, spec: SubTaskSpec, attempt: int, err: str) -> None:
        logger.warning('[Master:%s] AGENT_RETRY agent-%s-%s attempt=%d err=%.200s',
                       self.task_id, spec.role, spec.id, attempt, err)
        if self.on_progress:
            self.on_progress({
                'type': 'swarm_agent_phase', 'phase': 'retrying',
                'agentId': spec.id,
                'content': f'🔄 Retrying [{spec.role}] (attempt {attempt}): {err[:100]}',
            })

    def _build_scheduler(self) -> StreamingScheduler:
        return StreamingScheduler(
            agent_factory=self._make_agent,
            rate_limiter=self.rate_limiter,
            max_parallel=self.max_parallel,
            abort_check=lambda: (
                self._aborted or self._stop_stalled or self.abort_check()),
            default_retries=self.max_retries,
            on_agent_start=self._on_agent_start_callback,
            on_agent_complete=self._on_agent_complete_callback,
            on_retry=self._on_retry_callback,
            progress_beacon=self._beacon,
            max_total_agents=self.max_total_agents,
        )

    # ── Non-blocking entry point ─────────────────────

    def run_in_background(self) -> None:
        """Kick off the swarm asynchronously; returns immediately.

        The caller (``_handle_spawn_agents``) is responsible for keeping a
        reference to this orchestrator (in ``_active_sessions``) so the
        ``await_agents`` / ``get_agent_result`` tools can reach it.
        """
        if self._scheduler is not None:
            logger.warning('[Master:%s] run_in_background called twice — ignoring',
                           self.task_id)
            return

        self._scheduler = self._build_scheduler()

        # Add specs synchronously: ``add_specs`` validates the DAG and
        # raises ``ValueError`` on a cycle, which we want to surface to
        # the LLM as an error before returning the handle.  The returned
        # list is the post-dedup spec set; on first call it should equal
        # ``self.specs`` (no prior session state to dedupe against).
        accepted = self._scheduler.add_specs(self.specs)
        if len(accepted) != len(self.specs):
            logger.warning(
                '[Master:%s] add_specs deduplicated %d → %d on first add — '
                'this is unexpected for a fresh session',
                self.task_id, len(self.specs), len(accepted))

        if self.on_progress:
            # ``objective`` populates the agent cards in the swarm panel
            # — full text, no truncation. CSS handles wrapping.
            self.on_progress({
                'type': 'swarm_phase', 'phase': 'spawning',
                'content': f'🚀 Spawning {len(self.specs)} agent(s) (async)…',
                # Frontend stamps this onto the panel; the reconciler probes
                # the status route with it (the key that always resolves).
                'swarmKey': self.inbox_key,
                'agents': [
                    {'agentId': s.id, 'role': s.role,
                     'objective': s.objective,
                     'model': self._resolve_spec_model(s),
                     'depends_on': list(s.depends_on or [])}
                    for s in self.specs
                ],
            })

        self._start_driver()

    def _start_driver(self) -> None:
        """Spawn the daemon driver thread that drains the scheduler.

        Shared by ``run_in_background`` (fresh swarm) and
        ``rehydrate_in_background`` (resumed swarm). Assumes
        ``self._scheduler`` is already built and seeded with specs.
        """
        def _driver():
            log_prefix = f'[Master:{self.task_id}]'
            t0 = time.time()
            try:
                # Drain the scheduler's completion stream. We don't actually
                # need the values here — ``_on_agent_complete_callback``
                # already records and pushes inbox notifications. Iterating
                # is what keeps the scheduler advancing.
                for _spec, _result in self._scheduler.iter_completions():
                    if self._aborted or self.abort_check():
                        break
            except Exception as e:
                self._driver_error = f'{type(e).__name__}: {e}'
                logger.error('%s Driver loop crashed: %s', log_prefix, e, exc_info=True)
            finally:
                # iter_completions returns on a liveness verdict, but returning
                # alone does not stop a worker already blocked inside a tool.
                # Publish the distinct internal stop BEFORE shutdown waits;
                # SubAgent mirrors it into run_command's cooperative aborted
                # field, which kills the whole subprocess tree. Previously the
                # pool merely waited 30s and abandoned the still-live thread.
                try:
                    if (self._scheduler.running_count > 0
                            and not self._beacon.is_making_progress()):
                        self._stop_stalled = True
                        logger.warning(
                            '%s propagating stall verdict to %d running '
                            'agent(s) before scheduler shutdown',
                            log_prefix, self._scheduler.running_count)
                except Exception as e:
                    logger.warning('%s stall cancellation probe failed: %s',
                                   log_prefix, e)

                # ORDER MATTERS. This block used to shut the pool down and set
                # ``_terminated`` while agents were STILL EXECUTING, because
                # the loop above could exit on a fixed 600s budget. Two lies
                # followed: ``await_agents`` told the model those ids "will
                # NEVER complete" (one delivered 21 minutes later), and
                # ``_build_agent_snapshot`` coerced them to ``unknown`` under
                # ``settled:true`` — the "no result" cards next to a green
                # "Complete" pill.
                #
                # So drain first: ``shutdown()`` now WAITS for in-flight
                # agents, and only once nothing is running may we claim the
                # swarm has terminated.
                try:
                    self._scheduler.shutdown()
                except Exception as e:
                    logger.debug('%s scheduler shutdown error: %s', log_prefix, e)

                _still = 0
                try:
                    _still = self._scheduler.running_count
                except Exception as e:
                    logger.debug('%s running_count probe failed: %s', log_prefix, e)
                if _still:
                    # Only reachable when shutdown's bounded wait expired on a
                    # wedged tool thread. Say so loudly rather than silently
                    # publishing "settled" over live work.
                    logger.warning(
                        '%s marking terminated with %d agent(s) still in '
                        'flight (wedged past the shutdown wait) — beacon: %s',
                        log_prefix, _still, self._beacon.describe())

                # ── Verdict propagation (measured smoke test, swarm 797036b8):
                # the stall verdict used to live ONLY in the log line above,
                # while ``_build_agent_snapshot`` coerced the same agents to
                # 'unknown' → the panel's 无结果 bucket — so for an agent that
                # genuinely never came back the panel still could not answer
                # "why no result?". Record the judgement HERE (before
                # _terminated flips the snapshot's live/terminated branch) so
                # the final snapshot says 'stalled' WITH its evidence.
                try:
                    for _aid, _silent, _note in self._beacon.stalled_agents():
                        self._stalled_agents[_aid] = {
                            'silent_seconds': round(_silent),
                            'note': _note,
                        }
                    if self._stalled_agents:
                        logger.info(
                            '%s stall verdict recorded for %d agent(s): %s',
                            log_prefix, len(self._stalled_agents),
                            {k: v['silent_seconds']
                             for k, v in self._stalled_agents.items()})
                except Exception as e:
                    logger.warning('%s stalled-agents harvest failed: %s',
                                   log_prefix, e)

                with self._lock:
                    n = len(self._results)
                    failed = sum(1 for _, r in self._results
                                 if r.status == SubAgentStatus.FAILED.value)
                    total_tokens = sum(r.total_tokens for _, r in self._results)
                elapsed = time.time() - t0
                logger.info(
                    '%s Driver done — agents=%d failed=%d tokens=%d elapsed=%.1fs',
                    log_prefix, n, failed, total_tokens, elapsed)

                if self.on_progress:
                    # Error transparency: a crashed driver used to surface ONLY
                    # in app.log while the panel fell through to Unconfirmed /
                    # a false green Complete. Say it on the wire, BEFORE the
                    # complete frame, so the UI settles as Failed-with-reason.
                    try:
                        if self._driver_error:
                            self.on_progress({
                                'type': 'swarm_phase', 'phase': 'error',
                                'error': self._driver_error,
                                'content': (
                                    '❌ Swarm driver error: '
                                    f'{self._driver_error}'),
                            })
                        self.on_progress({
                            'type': 'swarm_phase', 'phase': 'complete',
                            'content': (f'✅ Swarm complete — {n} agents '
                                        f'({failed} failed), '
                                        f'{total_tokens:,} tokens'),
                            'agentCount': n,
                            'failedCount': failed,
                            'totalTokens': total_tokens,
                            **({'error': self._driver_error}
                               if self._driver_error else {}),
                        })
                    except Exception as _progress_error:
                        logger.warning(
                            '%s terminal progress callback failed: %s',
                            log_prefix, _progress_error, exc_info=True,
                        )

                # Observable termination is published only after the terminal
                # wire frames. Callers that see ``is_terminated`` can now rely
                # on error/complete delivery having been attempted already.
                self._terminated = True
                self._completion_event.set()

                # Mark the durable session row terminated so startup
                # rehydration won't try to resume a swarm that has finished.
                # Undelivered results remain eligible for inbox recovery.
                try:
                    from lib.swarm import persistence
                    persistence.mark_session_terminated(self.inbox_key)
                except Exception as _pe:
                    logger.debug('%s mark_session_terminated failed: %s',
                                 log_prefix, _pe)

                # ── Final durable snapshot (settled=True) ──
                # Mirrors the terminal swarm_phase:complete UI event into the
                # persisted store so the reload panel reads 'Complete' with
                # every agent's real outcome, independent of the SSE stream.
                try:
                    self._persist_agent_snapshot(force=True)
                except Exception as _se:
                    logger.debug('%s final snapshot persist failed: %s',
                                 log_prefix, _se)

                # ── Settle hook: may auto-continue the main agent ──
                # Fires AFTER the terminal UI event so the panel reads as
                # complete before a continuation turn (if any) starts. The
                # callback decides whether to wake the main agent — it must
                # not raise into the driver thread.
                if self.on_settled:
                    try:
                        self.on_settled()
                    except Exception as e:
                        logger.error('%s on_settled hook failed: %s',
                                     log_prefix, e, exc_info=True)

        self._driver_thread = threading.Thread(
            target=_driver, name=f'swarm-driver-{self.task_id}', daemon=True,
        )
        self._driver_thread.start()

    # ── Rehydration entry point (startup resume) ─────

    def rehydrate_in_background(self, persisted_agents: list[dict]) -> None:
        """Resume a swarm from persisted per-agent checkpoints after a restart.

        ``persisted_agents`` is the ``agents`` list from
        ``persistence.load_resumable_sessions`` — each dict has agent_id,
        status, messages, result, delivered. Behaviour by agent status:

          * completed / failed / cancelled → preloaded into the results map
            so ``await_agents`` / ``get_agent_result`` return them straight
            away. A completed-but-undelivered one is re-enqueued as a
            ``<swarm-update>`` so the main agent still sees it.
          * pending / running / retrying → re-spawned. If a checkpoint has
            messages, the new SubAgent's conversation is seeded from it
            (round-level resume); otherwise it starts fresh from its spec.

        Only the non-terminal specs are handed to the scheduler, so finished
        agents are NOT re-executed.
        """
        if self._scheduler is not None:
            logger.warning('[Master:%s] rehydrate called after start — ignoring',
                           self.task_id)
            return

        by_id = {a['agent_id']: a for a in (persisted_agents or [])}

        resume_specs: list[SubTaskSpec] = []
        preloaded = 0
        re_enqueued = 0

        for spec in self.specs:
            a = by_id.get(spec.id)
            if a is None:
                # No checkpoint at all → never started; run fresh.
                resume_specs.append(spec)
                continue
            status = a.get('status', 'pending')
            if status in ('completed', 'failed', 'cancelled'):
                result = self._result_from_dict(a.get('result') or {})
                # A completed agent with no stored result dict (older row) —
                # fall back to its last message text as the final answer.
                if not result.final_answer and a.get('messages'):
                    result.final_answer = _last_assistant_text(a['messages'])
                with self._lock:
                    self._results.append((spec, result))
                    self._results_by_id[spec.id] = (spec, result)
                    if a.get('delivered'):
                        self._delivered_result_ids.add(spec.id)
                preloaded += 1
                if status == 'completed' and not a.get('delivered'):
                    self._reenqueue_swarm_update(spec, result)
                    re_enqueued += 1
            else:
                # Non-terminal → resume. Seed messages if we have a checkpoint.
                msgs = a.get('messages') or []
                if msgs:
                    self._resume_messages[spec.id] = msgs
                try:
                    self._resume_rounds[spec.id] = max(
                        0, int(a.get('rounds_used') or 0))
                except (TypeError, ValueError):
                    logger.warning('[Master:%s] invalid persisted rounds_used '
                                   'for agent %s: %r; resuming from round 0',
                                   self.task_id, spec.id,
                                   a.get('rounds_used'))
                    self._resume_rounds[spec.id] = 0
                resume_specs.append(spec)

        logger.info('[Master:%s] Rehydrate — preloaded=%d (re-enqueued=%d) resume=%d',
                    self.task_id, preloaded, re_enqueued, len(resume_specs))

        self._scheduler = self._build_scheduler()
        if resume_specs:
            try:
                self._scheduler.add_specs(resume_specs)
            except ValueError as e:
                logger.warning('[Master:%s] rehydrate add_specs cycle: %s',
                               self.task_id, e)

        if self.on_progress:
            self.on_progress({
                'type': 'swarm_phase', 'phase': 'spawning',
                'content': (f'♻️ Resumed swarm after restart — '
                            f'{len(resume_specs)} agent(s) continuing, '
                            f'{preloaded} already done'),
                'agents': [
                    {'agentId': s.id, 'role': s.role, 'objective': s.objective,
                     'model': self._resolve_spec_model(s),
                     'depends_on': list(s.depends_on or [])}
                    for s in resume_specs
                ],
            })

        # Even with zero resume_specs we still start the driver: it fires the
        # terminal swarm_phase:complete + on_settled (auto-continue) so the
        # re-enqueued <swarm-update>s get delivered.
        self._start_driver()

    def _result_from_dict(self, d: dict) -> SubAgentResult:
        """Reconstruct a SubAgentResult from a persisted to_dict() payload."""
        r = SubAgentResult()
        for k in r.__dataclass_fields__:
            if k in d:
                try:
                    setattr(r, k, d[k])
                except Exception as e:
                    logger.debug('[Master:%s] result field %s restore failed: %s',
                                 self.task_id, k, e)
        if not r.status:
            r.status = SubAgentStatus.COMPLETED.value
        return r

    def _reenqueue_swarm_update(self, spec: SubTaskSpec,
                                result: SubAgentResult) -> None:
        """Re-push an undelivered completed result to the model inbox."""
        try:
            output_file = (os.path.join(self.output_dir, f'{spec.id}.log')
                           if self.output_dir else '')
            payload = format_swarm_update(
                agent_id=spec.id, role=spec.role, status=result.status,
                elapsed_seconds=result.elapsed_seconds, tokens=result.total_tokens,
                preview=result.final_answer or '', output_file=output_file,
                error=result.error_message if result.status != SubAgentStatus.COMPLETED.value else '',
            )
            inbox_enqueue(self.inbox_key, payload, priority='later',
                          mode='swarm-update', agent_id=spec.id)
        except Exception as e:
            logger.warning('[Master:%s] re-enqueue swarm-update for %s failed: %s',
                           self.task_id, spec.id, e)

    # ── await_agents (blocking) ──────────────────────

    def mark_results_delivered(
        self,
        agent_ids: Iterable[str] | str | None,
    ) -> None:
        """Record results already projected to the parent model.

        Unknown ids are ignored so malformed inbox metadata cannot grow the
        session-local set beyond its authoritative completed-result map.
        """
        values = (agent_ids,) if isinstance(agent_ids, str) else (agent_ids or ())
        wanted = {str(agent_id) for agent_id in values if agent_id}
        if not wanted:
            return
        with self._lock:
            self._delivered_result_ids.update(
                wanted.intersection(self._results_by_id))

    def await_agents(self, *,
                     ids: list[str] | None = None,
                     mode: str = 'any',
                     timeout_seconds: float = 60.0) -> dict:
        """Block until matching agents complete, or until *timeout_seconds*.

        Returns a dict::

            {
              'completed':      list[dict] — payload-shaped summaries
              'still_running':  list[str]  — agent ids still running at timeout
              'mode':           str        — echoed
              'timed_out':      bool
              'note':           str        — human-readable explanation when
                                              there's nothing to wait for
            }

        Each ``completed`` entry has the same shape as ``<swarm-update>`` would
        carry, plus a ``preview`` field with a 200-char excerpt.

        Caller-asked ids that have already completed (from previous rounds)
        are returned immediately as ``completed`` — the model gets the data
        even if it forgot it had been notified.  Unknown ids appear in
        ``unknown`` so the model knows it asked for something invalid. A no-id
        await is delta-oriented: results already delivered to the model count
        as satisfied but are not returned again.
        """
        if mode not in ('any', 'all'):
            mode = 'any'

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        timed_out = False

        # Snapshot the master's durable session membership under its lock.
        # Do NOT derive no-id membership from the scheduler's transient
        # running/pending maps: a scheduler worker records completion and pops
        # ``_running`` immediately before its completion callback publishes the
        # result into ``_results_by_id``.  In that small but real window an
        # agent exists in neither map, so ``await_agents(ids=ALL)`` used to
        # forget it and return k/N results.
        # already_done = ids the caller asked about that ARE already finished.
        # to_wait      = ids the caller asked about that are NOT yet finished.
        # unknown      = caller-supplied ids unknown to this swarm session.
        with self._lock:
            done_ids = set(self._results_by_id.keys())
            delivered_ids = set(self._delivered_result_ids)
            known_ids = {str(s.id) for s in self.specs}

            if ids:
                requested = set(str(x) for x in ids)
            else:
                # No ids → "every agent this session knows about". MUST
                # include already-finished agents: otherwise an agent that
                # completed BEFORE this await call is in neither already_done
                # nor to_wait, so it can never be reported and mode='all'
                # silently evaluates "all" over only the still-in-flight
                # subset — yielding k/N < total while the panel shows N/N.
                requested = known_ids | done_ids
            already_done = requested & done_ids
            if not ids:
                already_done -= delivered_ids
            to_wait = (requested & known_ids) - done_ids
            unknown = requested - known_ids - done_ids

            await_signature = (mode, tuple(sorted(requested)))
            prior_timeout_progress = self._await_timeout_progress.get(
                await_signature)
            current_progress = frozenset(requested & done_ids)

        # Critical trace: WHAT this await is blocking on, plus a snapshot of
        # the swarm state. When an await later reports timed_out, this ENTER
        # line is the anchor that explains why (e.g. mode=all but agents are
        # still running and their runtime exceeds the requested window).
        logger.info(
            '[Master:%s] await_agents ENTER mode=%s timeout=%.0fs ids=%s — '
            'to_wait=%s already_done=%s unknown=%s '
            '(snapshot: done=%d delivered=%d known=%d, hard_cap=%ss)',
            self.task_id, mode, timeout_seconds,
            (sorted(str(x) for x in ids) if ids else 'ALL'),
            sorted(to_wait), sorted(already_done), sorted(unknown),
            len(done_ids), len(delivered_ids), len(known_ids),
            int(timeout_seconds))

        # ── Special case: nothing to wait for ──────────────────────────
        # Either the caller asked for ids that are all already done, or
        # asked for "all in flight" but none are running. Return now with
        # a clear note so the LLM doesn't think the call was a no-op.
        if not to_wait:
            with self._lock:
                self._await_timeout_progress.pop(await_signature, None)
            note = ''
            # When caller asks for ALL (no ids) and no agents are running,
            # return EVERY completed agent (not just already_done snapshot)
            if not ids:
                with self._lock:
                    all_completed = (
                        set(self._results_by_id)
                        - self._delivered_result_ids
                    )
                completed = all_completed
            else:
                completed = already_done

            if completed:
                note = (f'{len(completed)} agent(s) already completed; '
                        f'returning their results immediately.')
            elif not ids and done_ids:
                note = (
                    'All completed agent results were already delivered. '
                    'Use get_agent_result with explicit ids only when you '
                    'need to reread a full result.')
            elif unknown:
                note = (f'No matching agents — id(s) {sorted(unknown)} '
                        f'unknown to this swarm session.')
            else:
                note = ('No agents currently running or pending — the swarm '
                        'has finished. Call spawn_agents to launch a new wave.')
            return self._build_await_response(
                completed_ids=sorted(completed),
                still_running=[],
                unknown=sorted(unknown),
                mode=mode,
                timed_out=False,
                note=note,
            )

        # Do not spend another full wait window until the underlying result set
        # changes. Completion callbacks remain active and will deliver an inbox
        # update; this only suppresses a redundant synchronous block.
        if (
            prior_timeout_progress is not None
            and current_progress == prior_timeout_progress
        ):
            note = (
                'An identical await already timed out and no requested agent '
                'has completed since then. The redundant wait was skipped; '
                'agents are still running in the background and their '
                '<swarm-update> will arrive when progress occurs. Continue '
                'useful local work instead of repeating this await unchanged.'
            )
            return self._build_await_response(
                completed_ids=sorted(already_done),
                still_running=sorted(to_wait),
                unknown=sorted(unknown),
                mode=mode,
                timed_out=True,
                note=note,
                wait_suppressed=True,
            )

        # ── Wait loop ──────────────────────────────────────────────────
        # ``swarm_terminated`` distinguishes the two ways the loop can exit
        # without the mode condition being met: a genuine wall-clock timeout
        # (agents still in flight) vs. the swarm driver having already
        # finished. The second case is the "panel shows done but await keeps
        # spinning to the hard cap" desync: once the driver thread exits
        # (``self._terminated``) NO agent can ever newly land in
        # ``_results_by_id`` (e.g. specs that were cancel_pending'd on abort,
        # or otherwise dropped by the scheduler), so blocking is pointless —
        # those ids are stranded, not in-flight. Break immediately and report
        # them as still_running with a clear, non-timeout note so the tool
        # row stops spinning the instant the swarm is done, matching the
        # panel's swarm_phase:complete sweep.
        swarm_terminated = False
        while True:
            with self._lock:
                done_now = set(self._results_by_id.keys())
                if mode == 'any' and (already_done or (to_wait & done_now)):
                    break
                if mode == 'all' and to_wait.issubset(done_now):
                    break
                if self._terminated:
                    swarm_terminated = True
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            self._completion_event.clear()
            self._completion_event.wait(timeout=min(remaining, 2.0))

        # Build response — include both already_done (from before the wait)
        # and any of to_wait that finished during the wait.
        with self._lock:
            done_now = set(self._results_by_id.keys())
            delivered_now = set(self._delivered_result_ids)
        completed_set = (already_done | (to_wait & done_now))
        if not ids:
            completed_set -= delivered_now
        still_running = sorted(to_wait - done_now)
        terminated_note = ''
        if swarm_terminated and still_running:
            logger.info(
                '[Master:%s] await_agents BREAK-ON-TERMINATED mode=%s — swarm '
                'driver exited; %d stranded id(s) will never complete: %s',
                self.task_id, mode, len(still_running), still_running)
            terminated_note = (
                f'The swarm has finished — {len(completed_set)} agent(s) '
                f'completed, but {len(still_running)} requested agent(s) '
                f'({", ".join(still_running)}) never produced a result '
                f'(cancelled/aborted or dropped before running) and will NOT '
                f'complete. Not waiting further. Re-spawn them with '
                f'spawn_agents if you still need that work.')
        with self._lock:
            if timed_out:
                if (
                    await_signature not in self._await_timeout_progress
                    and len(self._await_timeout_progress)
                    >= self._await_timeout_progress_capacity
                ):
                    oldest = next(iter(self._await_timeout_progress))
                    self._await_timeout_progress.pop(oldest, None)
                self._await_timeout_progress[await_signature] = frozenset(
                    requested & done_now
                )
            else:
                self._await_timeout_progress.pop(await_signature, None)
        return self._build_await_response(
            completed_ids=sorted(completed_set),
            still_running=still_running,
            unknown=sorted(unknown),
            mode=mode,
            timed_out=timed_out,
            note=terminated_note,
        )

    def _build_await_response(self, *, completed_ids: list[str],
                               still_running: list[str],
                               unknown: list[str],
                               mode: str, timed_out: bool, note: str,
                               wait_suppressed: bool = False) -> dict:
        """Materialize completed payloads from ``_results_by_id``."""
        completed_payloads: list[dict] = []
        with self._lock:
            for sid in completed_ids:
                pair = self._results_by_id.get(sid)
                if not pair:
                    continue
                spec, result = pair
                completed_payloads.append({
                    'agent_id':     sid,
                    'role':         spec.role,
                    'objective':    spec.objective[:200],
                    'status':       result.status,
                    'elapsed':      round(result.elapsed_seconds, 1),
                    'tokens':       result.total_tokens,
                    'preview':      (result.final_answer or '')[:200],
                    'output_file':  os.path.join(self.output_dir, f'{sid}.log')
                                    if self.output_dir else '',
                    'error':        result.error_message
                                    if result.status != SubAgentStatus.COMPLETED.value else '',
                })
        # When the wait hit the hard cap before the mode condition was met,
        # synthesise an explicit, actionable note (the caller passes note=''
        # on the timeout path). Without this the model just sees
        # ``timed_out:true`` with no guidance on what to do next.
        if timed_out and not note:
            n_done = len(completed_payloads)
            n_left = len(still_running)
            note = (
                f'Timed out waiting for the await window to satisfy mode={mode!r}: '
                f'{n_done} agent(s) done, {n_left} still running '
                f'({", ".join(still_running) or "none"}). '
                f'The running agents are NOT cancelled — they keep going in the '
                f'background and you will receive their <swarm-update> on a later '
                f'round. Continue with other work, or call await_agents again to '
                f'keep waiting.'
            )

        out = {
            'completed':     completed_payloads,
            'still_running': still_running,
            'mode':          mode,
            'timed_out':     timed_out,
        }
        if wait_suppressed:
            out['wait_suppressed'] = True
        if unknown:
            out['unknown'] = unknown
        if note:
            out['note'] = note

        # Critical trace: every await resolution leaves a line. A timeout is
        # logged at WARNING (it's the symptom the user reports) and names the
        # agents that out-ran the window so the cause is grep-able without a
        # debugger; a clean satisfy is INFO.
        if wait_suppressed:
            logger.info(
                '[Master:%s] await_agents SKIPPED redundant no-progress wait '
                'mode=%s still_running=%s',
                self.task_id, mode, still_running or 'none')
        elif timed_out:
            # Per-stuck-agent live state so the timeout line is SELF-CONTAINED:
            # an operator can see WHAT each still-running agent is doing (its
            # round count + model) without cross-referencing the LLM stream
            # log. A zero-progress agent (rounds=0 after the full cap) is the
            # tell-tale of an upstream stall (e.g. gateway HTTP 500 pool
            # timeout) — it never produced a single round.
            stuck_detail = []
            with self._lock:
                for sid in still_running:
                    ag = self._agents.get(sid)
                    rounds = getattr(getattr(ag, 'result', None), 'rounds_used', 0) if ag else 0
                    model = getattr(ag, 'model', '') if ag else ''
                    stuck_detail.append(
                        f'{sid}(rounds={rounds}'
                        + (f',model={model}' if model else '') + ')')
            logger.warning(
                '[Master:%s] await_agents TIMEOUT mode=%s — satisfied=%d/%d, '
                'still_running=%s. Agents are NOT cancelled; they keep running '
                'and will emit <swarm-update> later. If their runtime exceeds '
                'the per-call cap, mode=all REPEATEDLY times out — prefer '
                "mode='any' or await specific ids.",
                self.task_id, mode, len(completed_payloads),
                len(completed_payloads) + len(still_running),
                still_running or 'none')
            if stuck_detail:
                zero_round_ids = []
                progressed_ids = []
                with self._lock:
                    for sid in still_running:
                        ag = self._agents.get(sid)
                        rounds = getattr(
                            getattr(ag, 'result', None), 'rounds_used', 0) if ag else 0
                        (zero_round_ids if rounds == 0 else progressed_ids).append(sid)
                diagnosis = []
                if zero_round_ids:
                    diagnosis.append(
                        'rounds=0 agents never completed an LLM round and may '
                        'be stalled upstream: ' + ','.join(zero_round_ids))
                if progressed_ids:
                    diagnosis.append(
                        'non-zero-round agents already made LLM progress; '
                        'inspect their latest tool/dispatch instead of '
                        'classifying them as pre-round stalls: '
                        + ','.join(progressed_ids))
                logger.warning(
                    '[Master:%s] await_agents TIMEOUT stuck-agent state: %s '
                    '(%s)', self.task_id, '; '.join(stuck_detail),
                    '; '.join(diagnosis))
        else:
            logger.info(
                '[Master:%s] await_agents EXIT mode=%s satisfied — completed=%s '
                'still_running=%s',
                self.task_id, mode,
                [p['agent_id'] for p in completed_payloads] or 'none',
                still_running or 'none')

        # ── De-dup channel: the agents we just returned synchronously must
        #    NOT also be injected as <swarm-update> user messages on the next
        #    round. Drop their (now-redundant) inbox items. The model still
        #    sees every completion exactly once — here, in the tool return.
        if completed_payloads:
            _delivered_ids = [p['agent_id'] for p in completed_payloads]
            self.mark_results_delivered(_delivered_ids)
            try:
                inbox_consume(self.inbox_key, _delivered_ids)
            except Exception as e:
                logger.warning('[Master:%s] inbox consume after await failed: %s',
                               self.task_id, e)
            # Persist the delivered flag so a restart doesn't re-notify these.
            try:
                from lib.swarm import persistence
                persistence.mark_delivered(self.inbox_key, _delivered_ids)
            except Exception as e:
                logger.debug('[Master:%s] mark_delivered after await failed: %s',
                             self.task_id, e)
        return out

    # ── get_agent_result ──────────────────────────────

    def get_agent_result(self, agent_id: str) -> dict:
        """Return the full result for *agent_id*, or a status notice."""
        with self._lock:
            if agent_id in self._results_by_id:
                spec, result = self._results_by_id[agent_id]
                self._delivered_result_ids.add(agent_id)
                payload = {
                    'found':         True,
                    'agent_id':      agent_id,
                    'role':          spec.role,
                    'objective':     spec.objective,
                    'status':        result.status,
                    'final_answer':  result.final_answer,
                    'error':         result.error_message,
                    'elapsed':       round(result.elapsed_seconds, 1),
                    'tokens':        result.total_tokens,
                    'tool_calls':    result.tool_calls_made,
                    'rounds':        result.rounds_used,
                    'output_file':   os.path.join(self.output_dir, f'{agent_id}.log')
                                     if self.output_dir else '',
                }
                # De-dup: the full answer is now in the tool return — drop the
                # pending <swarm-update> for this agent so it isn't injected again.
                try:
                    inbox_consume(self.inbox_key, [agent_id])
                except Exception as e:
                    logger.warning('[Master:%s] inbox consume after get_agent_result '
                                   'failed: %s', self.task_id, e)
                try:
                    from lib.swarm import persistence
                    persistence.mark_delivered(self.inbox_key, [agent_id])
                except Exception as e:
                    logger.debug('[Master:%s] mark_delivered after get_agent_result '
                                 'failed: %s', self.task_id, e)
                return payload
            running_ids = (set(self._scheduler._running.keys())
                           if self._scheduler else set())
            pending_ids = ({s.id for s in self._scheduler._pending}
                           if self._scheduler else set())

        if agent_id in running_ids:
            return {
                'found':  True,
                'agent_id': agent_id,
                'status': 'running',
                'message': (
                    f'Agent {agent_id} is still running — '
                    f'wait for the next <swarm-update> or call await_agents.'),
            }
        if agent_id in pending_ids:
            return {
                'found':  True,
                'agent_id': agent_id,
                'status': 'pending',
                'message': f'Agent {agent_id} is queued waiting on dependencies.',
            }
        return {
            'found':   False,
            'agent_id': agent_id,
            'message': f'No agent with id={agent_id!r} in this swarm session.',
        }

    # ── Spec tracking for followup spawns ──────────────

    def register_followup_specs(self, specs: list[SubTaskSpec]) -> None:
        """Add specs accepted by a followup ``spawn_agents`` call.

        Without this, ``get_status()`` (used by /api/v1/swarm/status) would
        only see the first wave's agents, and the swarm panel rendered
        from server-side status would miss any agent added later.

        ``integration._handle_spawn_agents`` calls this AFTER
        ``add_specs`` returns the post-dedup accepted list.
        """
        if not specs:
            return
        with self._lock:
            existing_ids = {s.id for s in self.specs}
            for s in specs:
                if s.id not in existing_ids:
                    self.specs.append(s)

    # ── Status / artifacts / abort (kept for /api/v1/swarm/status route) ──

    def get_status(self) -> dict:
        """Per-agent status snapshot — used by routes/api_v1/swarm status endpoints."""
        with self._lock:
            out = {}
            for spec in self.specs:
                agent = self._agents.get(spec.id)
                pair = self._results_by_id.get(spec.id)
                if pair:
                    _, result = pair
                    status = result.status
                    rounds = result.rounds_used
                elif agent and agent.result:
                    status = agent.result.status
                    rounds = agent.result.rounds_used
                else:
                    if self._scheduler and spec.id in self._scheduler._running:
                        status = 'running'
                    else:
                        status = 'pending'
                    rounds = 0
                _err = ''
                if pair:
                    _err = pair[1].error_message or ''
                elif agent and agent.result:
                    _err = agent.result.error_message or ''
                out[spec.id] = {
                    'role':       spec.role,
                    'objective':  spec.objective[:120],
                    'status':     status,
                    'round':      rounds,
                    # Surfaced by /api/v1/swarm/status so a reconciling panel
                    # can show WHY an agent failed, not just THAT it failed.
                    'error':      _err,
                }
            return out

    def abort(self) -> None:
        """Signal abort. Best-effort — already-running agents complete on their own thread."""
        self._aborted = True
        self._completion_event.set()

    # ── Diagnostics ───────────────────────────────────

    @property
    def is_terminated(self) -> bool:
        return self._terminated

    @property
    def progress_beacon(self) -> ProgressBeacon:
        """The shared liveness record — read by ``_session_is_producing``.

        Exposed as a property so the session-TTL sweep asks THIS swarm whether
        its agents are still producing, instead of inferring death from the
        session's age (which is what aborted 105 sessions).
        """
        return self._beacon

    @property
    def pending_count(self) -> int:
        return self._scheduler.pending_count if self._scheduler else 0

    @property
    def running_count(self) -> int:
        return self._scheduler.running_count if self._scheduler else 0

    @property
    def completed_count(self) -> int:
        with self._lock:
            return len(self._results)
