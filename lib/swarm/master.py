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

import os
import threading
import time
from collections.abc import Callable

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
from lib.swarm.scheduler import StreamingScheduler

logger = get_logger(__name__)

#: Tool names that mutate files on disk. Used to flag sub-agents that
#: modified the workspace so the UI can mark them for closer review.
#: ``run_command`` is intentionally excluded — it CAN write but is also
#: used for read-only commands (git status, tests), so counting it would
#: over-flag. We count only the unambiguous write tools.
_FILE_WRITE_TOOLS = frozenset({
    'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})


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
                     output_file: str = '') -> SubAgent:
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
                 parent_config: dict | None = None):
        self.task_id = task_id
        self.conv_id = conv_id
        self.specs = list(specs)
        self.project_path = project_path
        self.model = model
        self.thinking_enabled = thinking_enabled
        self.search_mode = search_mode
        self.on_progress = on_progress
        self.abort_check = abort_check or (lambda: False)
        self.all_tools = all_tools or []
        self.max_parallel = max_parallel
        self.max_retries = max_retries

        # Per-agent streaming output directory. Each sub-agent will stream
        # its raw tokens to ``<output_dir>/<agent_id>.log`` so the main
        # agent can fetch progress with a regular file read if it explicitly
        # wants to.
        self.output_dir = output_dir
        if self.output_dir:
            try:
                os.makedirs(self.output_dir, exist_ok=True)
            except OSError as e:
                logger.warning('[Master:%s] Could not create output dir %r: %s',
                               task_id, self.output_dir, e)

        logger.info('[Master:%s] Init — %d specs model=%s parallel=%d retries=%d output_dir=%s',
                    task_id, len(specs), model or '(default)',
                    max_parallel, max_retries, self.output_dir or '(none)')
        for i, s in enumerate(specs):
            logger.debug('[Master:%s]   Spec[%d] id=%s role=%s deps=%s obj=%.120s',
                         task_id, i, s.id, s.role, list(s.depends_on or []),
                         s.objective)

        # Shared state
        self.artifact_store = ArtifactStore()
        self.rate_limiter = RateLimiter(max_concurrent=max_parallel)

        self._agents: dict[str, SubAgent] = {}
        self._results: list[tuple[SubTaskSpec, SubAgentResult]] = []
        self._results_by_id: dict[str, tuple[SubTaskSpec, SubAgentResult]] = {}
        self._lock = threading.Lock()
        self._aborted = False

        # Listeners woken when ANY agent completes (used by await_agents).
        self._completion_event = threading.Event()

        # Parent task proxy used by SubAgent for tool dispatch. Most fields
        # are placeholders, but ``config`` MUST carry through fields that
        # affect tool routing (notably ``browserClientId`` for per-client
        # playwright pool selection).  Without it, sub-agents that use
        # browser tools would silently fall back to the default pool and
        # ignore the parent's per-client routing.
        self._parent_task_proxy = {
            'id':           task_id,
            'convId':       conv_id,
            'events_lock':  threading.Lock(),
            'events':       [],
            'toolRounds':   [],
            'phase':        'tool',
            'config':       dict(parent_config or {}),
        }

        self._scheduler: StreamingScheduler | None = None
        self._driver_thread: threading.Thread | None = None
        self._terminated = False  # set when driver thread exits

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
        return resolve_model_for_tier(tier, self.model)

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
            abort_check=lambda: self._aborted or self.abort_check(),
            project_path=self.project_path,
            artifact_store=self.artifact_store,
            output_file=out_path,
        )
        with self._lock:
            self._agents[spec.id] = agent
        return agent

    # ── Scheduler callbacks ────────────────────────────

    def _on_agent_start_callback(self, spec: SubTaskSpec) -> None:
        # Echo to app.log using the same `agent-{role}-{id}` token the
        # SubAgent itself logs with — lets a user copy the ID chip from
        # the UI swarm panel and grep every transition in one shot.
        logger.info('[Master:%s] AGENT_START agent-%s-%s role=%s objective=%.120s',
                    self.task_id, spec.role, spec.id, spec.role, spec.objective)
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
        # ``summary`` is the per-card preview line, capped at 200 chars
        # because that's what fits visually before the user clicks open.
        modified_files = _count_file_writes(result.tool_log)
        if self.on_progress:
            self.on_progress({
                'type':      'swarm_agent_complete',
                'agentId':   spec.id,
                'role':      spec.role,
                'objective': spec.objective,
                'model':     (self._agents.get(spec.id).model
                              if self._agents.get(spec.id) else
                              self._resolve_spec_model(spec)),
                'status':    result.status,
                'elapsed':   round(result.elapsed_seconds, 1),
                'tokens':    result.total_tokens,
                'summary':   (result.final_answer or '')[:600],
                # ★ Number of file-mutating tool calls this agent made.
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
            running = self._scheduler.running_count if self._scheduler else 0
            pending = self._scheduler.pending_count if self._scheduler else 0
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
            inbox_enqueue(self.task_id, payload,
                          priority='later',
                          mode='swarm-update',
                          agent_id=spec.id)
        except Exception as e:
            logger.error('[Master:%s] Failed to enqueue swarm-update for agent=%s: %s',
                         self.task_id, spec.id, e, exc_info=True)

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
            abort_check=lambda: self._aborted or self.abort_check(),
            default_retries=self.max_retries,
            on_agent_start=self._on_agent_start_callback,
            on_agent_complete=self._on_agent_complete_callback,
            on_retry=self._on_retry_callback,
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
                'agents': [
                    {'agentId': s.id, 'role': s.role,
                     'objective': s.objective,
                     'model': self._resolve_spec_model(s),
                     'depends_on': list(s.depends_on or [])}
                    for s in self.specs
                ],
            })

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
                logger.error('%s Driver loop crashed: %s', log_prefix, e, exc_info=True)
            finally:
                try:
                    self._scheduler.shutdown()
                except Exception as e:
                    logger.debug('%s scheduler shutdown error: %s', log_prefix, e)
                self._terminated = True
                self._completion_event.set()

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
                    self.on_progress({
                        'type': 'swarm_phase', 'phase': 'complete',
                        'content': (f'✅ Swarm complete — {n} agents '
                                     f'({failed} failed), {total_tokens:,} tokens'),
                        'agentCount':  n,
                        'failedCount': failed,
                        'totalTokens': total_tokens,
                    })

        self._driver_thread = threading.Thread(
            target=_driver, name=f'swarm-driver-{self.task_id}', daemon=True,
        )
        self._driver_thread.start()

    # ── await_agents (blocking) ──────────────────────

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
        ``unknown`` so the model knows it asked for something invalid.
        """
        if mode not in ('any', 'all'):
            mode = 'any'

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        timed_out = False

        # Snapshot scheduler state ONCE under one lock acquisition.
        # already_done = ids the caller asked about that ARE already finished.
        # to_wait      = ids the caller asked about that are NOT yet finished.
        # unknown      = caller-supplied ids unknown to this swarm session.
        with self._lock:
            done_ids = set(self._results_by_id.keys())
            running_ids = (set(self._scheduler._running.keys())
                           if self._scheduler else set())
            pending_ids = ({s.id for s in self._scheduler._pending}
                           if self._scheduler else set())

            if ids:
                requested = set(str(x) for x in ids)
                already_done = requested & done_ids
                to_wait = requested & (running_ids | pending_ids)
                unknown = requested - done_ids - running_ids - pending_ids
            else:
                requested = running_ids | pending_ids
                already_done = set()  # only return fresh completions
                to_wait = set(requested)
                unknown = set()

        # ── Special case: nothing to wait for ──────────────────────────
        # Either the caller asked for ids that are all already done, or
        # asked for "all in flight" but none are running. Return now with
        # a clear note so the LLM doesn't think the call was a no-op.
        if not to_wait:
            note = ''
            if already_done:
                note = (f'{len(already_done)} agent(s) already completed; '
                        f'returning their results immediately.')
            elif unknown:
                note = (f'No matching agents — id(s) {sorted(unknown)} '
                        f'unknown to this swarm session.')
            else:
                note = ('No agents currently running or pending — the swarm '
                        'has finished. Call spawn_agents to launch a new wave.')
            return self._build_await_response(
                completed_ids=sorted(already_done),
                still_running=[],
                unknown=sorted(unknown),
                mode=mode,
                timed_out=False,
                note=note,
            )

        # ── Wait loop ──────────────────────────────────────────────────
        while True:
            with self._lock:
                done_now = set(self._results_by_id.keys())
                if mode == 'any' and (to_wait & done_now):
                    break
                if mode == 'all' and to_wait.issubset(done_now):
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
        completed_set = (already_done | (to_wait & done_now))
        still_running = sorted(to_wait - done_now)
        return self._build_await_response(
            completed_ids=sorted(completed_set),
            still_running=still_running,
            unknown=sorted(unknown),
            mode=mode,
            timed_out=timed_out,
            note='',
        )

    def _build_await_response(self, *, completed_ids: list[str],
                               still_running: list[str],
                               unknown: list[str],
                               mode: str, timed_out: bool, note: str) -> dict:
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
        if unknown:
            out['unknown'] = unknown
        if note:
            out['note'] = note

        # ── De-dup channel: the agents we just returned synchronously must
        #    NOT also be injected as <swarm-update> user messages on the next
        #    round. Drop their (now-redundant) inbox items. The model still
        #    sees every completion exactly once — here, in the tool return.
        if completed_payloads:
            try:
                inbox_consume(self.task_id,
                              [p['agent_id'] for p in completed_payloads])
            except Exception as e:
                logger.warning('[Master:%s] inbox consume after await failed: %s',
                               self.task_id, e)
        return out

    # ── get_agent_result ──────────────────────────────

    def get_agent_result(self, agent_id: str) -> dict:
        """Return the full result for *agent_id*, or a status notice."""
        with self._lock:
            if agent_id in self._results_by_id:
                spec, result = self._results_by_id[agent_id]
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
                    inbox_consume(self.task_id, [agent_id])
                except Exception as e:
                    logger.warning('[Master:%s] inbox consume after get_agent_result '
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
                out[spec.id] = {
                    'role':       spec.role,
                    'objective':  spec.objective[:120],
                    'status':     status,
                    'round':      rounds,
                    'max_rounds': spec.max_rounds,
                }
            return out

    def get_artifacts(self) -> dict:
        return self.artifact_store.get_all()

    def abort(self) -> None:
        """Signal abort. Best-effort — already-running agents complete on their own thread."""
        self._aborted = True
        self._completion_event.set()

    # ── Diagnostics ───────────────────────────────────

    @property
    def is_terminated(self) -> bool:
        return self._terminated

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
